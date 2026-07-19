#!/usr/bin/env node
/**
 * wrapper-nvidia v4.5.0 — Node.js NVIDIA NIM API proxy
 * Ported from Python main.py (FastAPI) — functionally identical.
 */

const path = require('path');
const { join, dirname } = path;

// Load env from .env file FIRST (portable)
try {
  const dotenv = require('dotenv');
  dotenv.config({ path: join(__dirname, '..', '.env') });
} catch {}

const http = require('http');
const { URL } = require('url');
const { fetch: undiciFetch, Agent } = require('undici');

// Combine multiple AbortSignals into one (fires if ANY signal aborts)
// Canonical wrapper dir
const WRAPPER_DIR = path.resolve(__dirname, '..');

const { KeyPool, NVIDIA_BASE_URL, NVIDIA_GENAI_URL, NVIDIA_NVCF_URL } = require('../key_pool');
const { anthropicToOpenai, openaiToAnthropic, streamOpenaiToAnthropic, estimateInputTokens, anthropicError } = require('./anthropic_compat');
const { classify, describe, buildCatalog, summarize, CAPABILITY_PARAMS, CURATED_GENAI, getCapabilityParams, MODEL_CONTEXT_WINDOWS, DEFAULT_CONTEXT_WINDOW, getContextWindow } = require('./capabilities');
const createResponsesHandler = require('./responses_compat');
const { Metrics } = require('./metrics');
const { Registry } = require('./registry');

// Bug R2: structural request fields that must NEVER be stripped, even if an
// upstream 400 error message mentions them (e.g. "extra fields not permitted:
// messages"). Stripping these mutilates / empties the conversation on retry.
// Structural fields that must NEVER be stripped even if an upstream 422 lists
// them (stripping them would break the request contract). Sampling params
// (temperature/top_p/top_k) are intentionally NOT protected: some NIM models
// reject them (e.g. nvidia/gliner-pii → 422 "Unknown parameter 'top_p'"), and
// the param-strip retry path must be free to drop them and re-send.
const PROTECTED_PARAMS = new Set([
  'messages', 'model', 'stream', 'tools', 'tool_choice', 'system',
]);

// Dynamic, NGC-synced authoritative context registry (no more silent guesses).
const registry = new Registry();

// ── Config ──────────────────────────────────────────────────────────────
const LISTEN_PORT = parseInt(process.env.LISTEN_PORT || '9100', 10);
const BIND_HOST   = process.env.LISTEN_HOST || '0.0.0.0';
const BASE_LLM    = (process.env.NVIDIA_BASE_URL || NVIDIA_BASE_URL).replace(/\/+$/, '');
const BASE_GENAI  = (process.env.NVIDIA_GENAI_URL || NVIDIA_GENAI_URL).replace(/\/+$/, '');
const BASE_NVCF   = (process.env.NVIDIA_NVCF_URL || NVIDIA_NVCF_URL).replace(/\/+$/, '');
const DB_PATH     = process.env.METRICS_DB || path.join(WRAPPER_DIR, 'metrics.db');
const QUIET_RETRIED_429 = parseInt(process.env.QUIET_RETRIED_429 || '3', 10);
const MAX_RETRIES = QUIET_RETRIED_429;
// Read version from package.json — single source of truth (no more hardcoded
// duplicates in index.js, key_pool.js healthJson, etc.).
let VERSION = '8.6.0-node';
try {
  const pkg = require(path.join(WRAPPER_DIR, 'package.json'));
  if (pkg && pkg.version) VERSION = `${pkg.version}-node`;
} catch { /* keep default */ }
// MODEL_CONTEXT_WINDOWS, DEFAULT_CONTEXT_WINDOW, and getContextWindow() are now
// imported from capabilities.js — single source of truth shared with anthropic_compat.js.

// ── Centralized Reasoning Config ──────────────────────────────────────────
// Ordered by specificity (most specific first).
// Each entry maps a model-name pattern to its required NIM reasoning mechanism.
//   requires_reasoning: true  → model HANGS without the toggle (auto-inject)
//   requires_reasoning: false → model optionally supports thinking (inject only
//                               when client explicitly asks for it)
// IMPORTANT: No catch-all fallback. Injecting chat_template_kwargs into a model
// that doesn't support it can cause the upstream to HANG indefinitely (observed
// with llama-3.2 and similar non-reasoning models). New reasoning model
// families MUST be added here explicitly. The warning log messages will tell
// you when an unknown model is hit with a thinking request.
const REASONING_CONFIGS = [
  // REQUIRES reasoning toggle: model HANGS with no response unless thinking is on.
  { patterns: ['deepseek-v4', 'deepseek-r1', 'deepseek-reasoner'], mechanism: 'chat_template_kwargs', params: { enable_thinking: true, thinking: true }, requires_reasoning: true },
  { patterns: ['deepseek-coder'], mechanism: 'chat_template_kwargs', params: { enable_thinking: true }, requires_reasoning: false },
  // NVIDIA NIM reasoning variants (suffix `-reasoning`) use chat-template thinking.
  { patterns: ['-reasoning', 'reason'], mechanism: 'chat_template_kwargs', params: { enable_thinking: true, thinking: true }, requires_reasoning: true },
  // Thinking Machines / "inkling" — reasoning model family.
  { patterns: ['thinkingmachines', 'inkling'], mechanism: 'chat_template_kwargs', params: { enable_thinking: true }, requires_reasoning: false },
  // Qwen3 / Qwen3.5 (incl. qwen3.5-*, qwen3-next) all support thinking.
  { patterns: ['qwen'], mechanism: 'chat_template_kwargs', params: { enable_thinking: true }, requires_reasoning: false },
  // GLM (z-ai/glm-*) — thinking toggle.
  { patterns: ['glm'], mechanism: 'chat_template_kwargs', params: { thinking: true }, requires_reasoning: false },
  // Phi-4 reasoning variants.
  { patterns: ['phi-4'], mechanism: 'chat_template_kwargs', params: { enable_thinking: true }, requires_reasoning: false },
  { patterns: ['yi-'], mechanism: 'chat_template_kwargs', params: { enable_thinking: true }, requires_reasoning: false },
  { patterns: ['llama-3.3', 'llama-3.2', 'llama-4'], mechanism: 'chat_template_kwargs', params: { enable_thinking: true }, requires_reasoning: false },
  { patterns: ['gemma-3'], mechanism: 'chat_template_kwargs', params: { enable_thinking: true }, requires_reasoning: false },
  // reasoning_effort families (NIM accepts `reasoning_effort` for these).
  { patterns: ['nemotron', 'gpt-oss', 'kimi', 'mistral-'], mechanism: 'reasoning_effort', params: { effort: 'high' }, requires_reasoning: false },
  // FIX B-1: NVIDIA Nemotron reasoning family (ultra/super/nemotron-3) uses its
  // OWN chat_template_kwargs schema: { enable_thinking, force_nonempty_content }.
  // `force_nonempty_content` is REQUIRED — Nemotron reasoning can return an
  // EMPTY content body (reasoning only) that breaks clients unless the wrapper
  // guarantees a non-empty content before forwarding (see verifyNonemptyContent
  // in proxyOpenai). reasoning_budget is read from extra_body.reasoning_budget
  // and passed through verbatim. We keep BOTH this entry AND the generic
  // `nemotron` reasoning_effort entry above? No — this more-specific entry must
  // win, so the generic one is removed and Nemotron is handled solely here.
  { patterns: ['nemotron'], mechanism: 'nemotron_chat_template', params: { enable_thinking: true, force_nonempty_content: true }, requires_reasoning: false },
];

function findReasoningConfig(modelId) {
  const m = (modelId || '').toLowerCase();
  for (const cfg of REASONING_CONFIGS) {
    if (cfg.patterns.some(p => m.includes(p))) return cfg;
  }
  return null;
}

// ══ Hot-reloadable runtime config ═══════════════════════════════════
// Every change to .env is picked up live — no restart needed.
// DEFAULT_<param> fills gap if client doesn't send the param.
// DROP_PARAMS lists known-incompatible params to strip before upstream.
let DEFAULT_PARAMS = {};
let PROACTIVE_DROP = new Set();

function buildRuntimeConfig() {
  const WRAPPER_PARAMS = (process.env.WRAPPER_PARAMS || 'temperature,top_p').split(',').map(s => s.trim()).filter(Boolean);
  const dp = {};
  for (const p of WRAPPER_PARAMS) {
    const dv = process.env['DEFAULT_' + p.toUpperCase()];
    if (dv) dp[p] = dv;
  }
  DEFAULT_PARAMS = dp;
  // NVIDIA NIM forbids context_length/context_window (and variants) in the
  // request payload. Strip them proactively even if a client/agent sends them.
  // `think` retained for backward compatibility with the prior default.
  const FORBIDDEN_CONTEXT_PARAMS = [
    'context_length', 'context_window', 'context_len',
    'max_position_embeddings', 'max_context_length',
    'max_input_tokens', 'max_output_tokens', 'token_limit',
  ];
  PROACTIVE_DROP = new Set([
    ...((process.env.DROP_PARAMS || 'think').split(',').map(s => s.trim()).filter(Boolean)),
    ...FORBIDDEN_CONTEXT_PARAMS,
  ]);
}
buildRuntimeConfig();

// ── .env file watcher (hot-reload without restart) ─────────────────
const fs = require('fs');
const DOTENV_PATH = path.resolve(__dirname, '..', '.env');
function reloadDotenv() {
  try {
    require('dotenv').config({ path: DOTENV_PATH, override: true });
    buildRuntimeConfig();
    console.log('[reload] .env applied at', new Date().toISOString());
  } catch (e) {
    console.error('[reload] Failed:', e.message);
  }
}
let reloadTimer;
try {
  fs.watch(DOTENV_PATH, () => {
    clearTimeout(reloadTimer);
    reloadTimer = setTimeout(reloadDotenv, 500);
  });
  console.log('[reload] Watching .env for changes');
} catch (e) {
  console.warn('[reload] Cannot watch:', e.message);
}

// Concurrent verification config
const VERIFY_CONCURRENCY = parseInt(process.env.VERIFY_CONCURRENCY || '8', 10);
const VERIFY_INTERVAL = parseInt(process.env.VERIFY_INTERVAL || '600', 10) * 1000;
const VERIFY_ON_BOOT = process.env.VERIFY_ON_BOOT !== 'false';

// Upstream routing config
let UPSTREAM_ROUTES_SORTED = [];

// ── Globals ────────────────────────────────────────────────────────────
const pool    = new KeyPool();   // keys loaded in main() after dotenv

// OpenAI Responses API handler (codex >=0.144 requires wire_api="responses").
// NVIDIA-native models are translated to chat/completions; non-NVIDIA models
// are rejected (wrapper-nvidia is NVIDIA-NIM-only; no third-party OpenRouter
// routing -- see responses_compat.js isNvidiaModel()).
const responsesHandler = createResponsesHandler({
  pool, resolveTargetModel, proxyOpenai, forwardHeaders,
  incInFlight, decInFlight, BASE_LLM, BASE_GENAI, describe, CURATED_GENAI,
  translateThinkingToNim,
});
let metrics;                      // initialized in main() after dotenv sets METRICS_DB
const MAX_CONNECTIONS = parseInt(process.env.MAX_CONNECTIONS || '200', 10);
// Fix dead-upstream hang (REVISI audit): undici's bodyTimeout monitors the GAP
// between consecutive body chunks, but headersTimeout monitors the gap BEFORE
// the FIRST response byte (status line + headers). A reasoning model that thinks
// silently for >300s with no body chunks must NOT trip bodyTimeout — so bodyTimeout
// stays 0. headersTimeout guards a stalled upstream that accepts TLS but never sends
// HTTP headers back (blackhole).
//
// CAVEAT (2026 fix): heavy reasoning models (e.g. z-ai/glm-5.2, deepseek reasoning,
// nemotron ultra) hold the HTTP response headers until they START emitting the
// first token — which can be 35-150s+ of silent "thinking". undici cannot tell
// this apart from a blackhole, so a short headersTimeout (the old 15-30s) aborted
// these models with "fetch failed" across ALL keys → the model looked dead even
// though it was merely slow. We therefore set headersTimeout generously (120s) so
// slow-TTFT reasoning models can respond. The client-facing PRE_RESPONSE_TIMEOUT_MS
// watchdog still caps the total wait for a genuine blackhole, and the overall
// streaming time budget is owned by AbortSignal.timeout (STREAM_REQUEST_TIMEOUT_SEC).
const HEADERS_TIMEOUT_MS = parseInt(process.env.HEADERS_TIMEOUT_MS || '120000', 10);
const agent   = new Agent({ connections: MAX_CONNECTIONS, pipelining: 10, bodyTimeout: 0, headersTimeout: HEADERS_TIMEOUT_MS, connectTimeout: 15000 });
pool.setExternalAgent(agent);
let inFlight  = 0;

// ── SSE Real-time Push ────────────────────────────────
const sseClients = new Set();

function broadcastSSE(event, data) {
  const msg = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  for (const client of sseClients) {
    try {
      if (client.destroyed || client.errored || !client.writable) {
        sseClients.delete(client);
        continue;
      }
      // Respect backpressure: a slow/stuck dashboard client would otherwise
      // make Node buffer every broadcast in memory indefinitely → OOM under
      // load. Drop clients whose write buffer grows past 1MB instead.
      if (client.writableLength > 1024 * 1024) {
        // B9 FIX: log warning so operators can debug dashboard client drops.
        console.warn(`[SSE] Dropping slow dashboard client (buffer ${Math.round(client.writableLength/1024)}KB > 1MB)`);
        sseClients.delete(client);
        try { client.destroy(); } catch {}
        continue;
      }
      client.write(msg);
    } catch {
      sseClients.delete(client);
    }
  }
}

// Periodic sweep of stale SSE clients (zombie connections)
setInterval(() => {
  for (const client of sseClients) {
    try {
      if (client.destroyed || client.errored || !client.writable) {
        sseClients.delete(client);
      }
    } catch {
      sseClients.delete(client);
    }
  }
}, 30000).unref();

function incInFlight() {
  inFlight++;
}

function decInFlight() {
  if (inFlight > 0) inFlight--;
}
const unavailableModels = new Set();
// Subset of unavailableModels that are DEFINITIVELY dead (upstream 404/410,
// end-of-life, or DEGRADED). Only THESE are hidden from /v1/models discovery.
// Slow models that merely time out on the probe (status 0) stay in
// `unavailableModels` for telemetry but remain visible/callable, because a
// probe timeout means "slow", not "gone" — hiding them made heavy reasoning
// models (e.g. z-ai/glm-5.2, ~35-90s TTFT) silently disappear from the catalog.
const retiredModels = new Set();

// Classify a verify/live failure as "definitively dead" (→ hide from discovery)
// vs merely slow/transient. 404/410 and DEGRADED/end-of-life are definitive.
function isDefinitiveDeadStatus(status, reason) {
  if (status === 404 || status === 410) return true;
  const r = String(reason || '').toLowerCase();
  return r.includes('degraded') || r.includes('end of life') ||
         r.includes('gone') || r.includes('retired') || r.includes('not found');
}

// Generate unique request ID for tracing
function generateRequestId() {
  return `req_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
}

// ── Claude Code / Anthropic alias mapping ────────────────────────────────
// Claude Code sends built-in family aliases (haiku/sonnet/opus) for background
// tasks and subagents even when a custom model is pinned. NIM 404s those
// aliases → Claude Code errors / all traffic silently falls back. We map them
// to real NIM models (configurable via env). The discovery surface also aliases
// every NIM id to a claude-* id so the Claude Code gateway model picker shows
// the wrapper (Claude Code only lists ids that begin with "claude"/"anthropic").
let ALIAS_TO_NIM = {};
let DISCOVERY_TO_NIM = {};                 // "claude-<slug>" -> real NIM id
const DISCOVERY_PREFIX = 'claude-';

function _normAliasKey(s) { return (s || '').toLowerCase().trim(); }

// Alias targets must be real NIM ids (owner/model). Client-side Claude Code
// env vars (ANTHROPIC_DEFAULT_* / ANTHROPIC_MODEL) often carry OpenRouter-style
// ids like "tencent/hy3:free" when the wrapper is launched from an agent shell.
// Those are NOT valid NIM routes and would 404 every haiku/sonnet/opus request.
// Accept only owner/model shapes without OpenRouter ":tag" suffixes.
function _isValidNimAliasTarget(id) {
  if (!id || typeof id !== 'string') return false;
  const s = id.trim();
  if (!s || s.includes(':') || s.includes(' ')) return false;
  // Prefer owner/model; also allow bare curated ids without slash only if they
  // look like dotted org-less names used by a few NIM models (rare). Require
  // at least one alphanumeric segment.
  return /^[A-Za-z0-9._-]+(?:\/[A-Za-z0-9._-]+)+$/.test(s);
}

function _pickAliasTarget(envKeys, fallback, family) {
  for (const k of envKeys) {
    const v = process.env[k];
    if (!v) continue;
    if (_isValidNimAliasTarget(v)) return v.trim();
    console.warn(`[alias] Ignoring invalid ${family} alias from ${k}="${v}" (not a NIM owner/model id). Using default ${fallback}`);
  }
  return fallback;
}

function loadAliasConfig() {
  // Wrapper-side vars (CLAUDE_CODE_DEFAULT_*) are preferred. ANTHROPIC_DEFAULT_*
  // is accepted only when it looks like a NIM id — otherwise it is treated as
  // client-shell pollution (Claude Code / OpenRouter meta session) and ignored.
  const haiku = _pickAliasTarget(
    ['CLAUDE_CODE_DEFAULT_HAIKU_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL'],
    'meta/llama-3.1-8b-instruct', 'haiku');
  const sonnet = _pickAliasTarget(
    ['CLAUDE_CODE_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL'],
    'deepseek-ai/deepseek-v4-pro', 'sonnet');
  const opus = _pickAliasTarget(
    ['CLAUDE_CODE_DEFAULT_OPUS_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL'],
    'nvidia/nemotron-3-ultra-550b-a55b', 'opus');

  const map = {
    haiku, sonnet, opus,
    'claude-haiku': haiku, 'claude-sonnet': sonnet, 'claude-opus': opus,
    'claude-3-5-haiku': haiku, 'claude-3-5-sonnet': sonnet, 'claude-3-opus': opus,
    'claude-3-haiku': haiku, 'claude-3-sonnet': sonnet,
    'claude-3-5-haiku-latest': haiku, 'claude-3-5-sonnet-latest': sonnet,
    'claude-3-5-haiku-20241022': haiku, 'claude-3-5-sonnet-20241022': sonnet,
    'claude-haiku-4-5': haiku, 'claude-sonnet-4-5': sonnet, 'claude-opus-4-5': opus,
    'claude-haiku-4-5-latest': haiku, 'claude-sonnet-4-5-latest': sonnet, 'claude-opus-4-5-latest': opus,
    'claude-sonnet-4': sonnet, 'claude-opus-4': opus, 'claude-haiku-4': haiku,
  };

  // User-defined arbitrary alias overrides (JSON: {"my-alias":"owner/model"})
  const extra = process.env.ANTHROPIC_ALIAS_MAP;
  if (extra) {
    try {
      const parsed = JSON.parse(extra);
      for (const [k, v] of Object.entries(parsed)) {
        if (k && v) map[_normAliasKey(k)] = v;
      }
    } catch (e) {
      console.warn(`[alias] Failed to parse ANTHROPIC_ALIAS_MAP: ${e.message}`);
    }
  }
  ALIAS_TO_NIM = map;
  console.log(`[alias] haiku=${haiku} sonnet=${sonnet} opus=${opus}`);
}

// Strip client-side context-window suffixes (e.g. "[1m]") that Claude Code
// appends to model ids. These are not part of the upstream model name.
function _stripContextSuffix(modelId) {
  if (!modelId) return modelId;
  return modelId.replace(/\[[0-9]+[mk]?\]$/i, '').trim();
}

// Map external model request names (Claude aliases, gateway discovery ids, or
// raw NIM ids) to the real NIM model id. Raw NIM ids pass through unchanged
// (transparent proxy for OpenAI-compatible clients). Claude Code family aliases
// (haiku/sonnet/opus + their claude-* variants) resolve to configured NIM models.
function resolveTargetModel(requestedModel) {
  let m = _stripContextSuffix(requestedModel);
  if (!m) return requestedModel;
  const lower = m.toLowerCase();
  // 1) gateway discovery alias (claude-<slug>)
  if (m.startsWith(DISCOVERY_PREFIX) && DISCOVERY_TO_NIM[m]) {
    return DISCOVERY_TO_NIM[m];
  }
  // 2) explicit alias map (by normalized key)
  if (ALIAS_TO_NIM[lower]) return ALIAS_TO_NIM[lower];
  // 3) family match (contains haiku/sonnet/opus) so any claude-* variant maps.
  // Bug R3: restrict to claude-*-prefixed ids so a raw NIM id that merely
  // contains "opus"/"sonnet"/"haiku" is NOT silently remapped to the alias
  // target (transparent passthrough must be preserved for non-alias ids).
  for (const fam of ['opus', 'sonnet', 'haiku']) {
    if (m.startsWith('claude-') && lower.includes(fam) && ALIAS_TO_NIM[fam]) return ALIAS_TO_NIM[fam];
  }
  // 4) transparent passthrough (raw NIM id for OpenAI-compatible clients)
  return m;
}

function discoveryAlias(nimId) {
  return DISCOVERY_PREFIX + nimId.replace(/\//g, '-');
}

// Rebuild the reverse map used by resolveTargetModel for gateway discovery ids.
function refreshDiscoveryMap(ids) {
  const map = {};
  const all = new Set(ids || []);
  for (const c of CURATED_GENAI) all.add(c);
  for (const id of all) {
    if (!id) continue;
    map[discoveryAlias(id)] = id;
  }
  DISCOVERY_TO_NIM = map;
}

// ── Helpers ─────────────────────────────────────────────────────────────
function clientIp(req) {
  return req.headers['x-forwarded-for']?.split(',')[0]?.trim()
    || req.headers['x-real-ip']
    || req.socket?.remoteAddress
    || 'unknown';
}

function jsonResp(res, code, obj, keyLabel, extraHeaders) {
  // Defensive: never attempt to write a response twice (e.g. a streaming path
  // that falls through, or a race with res.on('close')). A second writeHead on
  // an already-finished response throws and crashes the request handler.
  if (res.headersSent || res.writableEnded) return;
  const body = JSON.stringify(obj);
  const respHeaders = {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(body),
  };
  if (code < 400 && keyLabel) {
    addRateLimitHeaders(respHeaders, keyLabel, pool);
  }
  // Forward any caller-supplied headers (e.g. Retry-After on 429 so OpenAI/
  // Anthropic clients honor the upstream backoff window).
  if (extraHeaders && typeof extraHeaders === 'object') {
    for (const [k, v] of Object.entries(extraHeaders)) {
      if (v !== undefined && v !== null && v !== '') respHeaders[k] = v;
    }
  }
  res.writeHead(code, respHeaders);
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    let settled = false;
    // REVISI audit: a stalled/aborted client that opens a request but never
    // finishes sending the body would leave readBody() pending forever, which
    // in turn leaves the whole request handler (and its close-abort watchdog)
    // stuck. Bound the body read by a wall-clock deadline and bail out the
    // instant the underlying socket is already gone.
    const READ_BODY_TIMEOUT_MS = parseInt(process.env.READ_BODY_TIMEOUT_MS || '30000', 10);
    const readTimer = setTimeout(() => {
      if (!settled) {
        settled = true;
        reject(new Error('Request body read timed out'));
      }
    }, READ_BODY_TIMEOUT_MS);
    // Claude Code sessions can grow very large (long conversation history, many
    // tool_result blocks, pasted files, base64 images). The previous 25MB cap
    // rejected legitimate long sessions with a 413-style error before the
    // request ever reached upstream. 100MB is configurable via MAX_BODY_MB.
    const limit = parseInt(process.env.MAX_BODY_MB || '100', 10) * 1024 * 1024;
    const onData = (c) => {
      size += c.length;
      if (size > limit) {
        req.off('data', onData);
        if (!settled) {
          settled = true;
          clearTimeout(readTimer);
          reject(new Error('Request entity too large'));
        }
        req.destroy(new Error('Request entity too large'));
      } else {
        chunks.push(c);
      }
    };
    req.on('data', onData);
    req.on('end', () => {
      if (!settled) {
        settled = true;
        clearTimeout(readTimer);
        resolve(Buffer.concat(chunks).toString('utf8'));
      }
    });
    req.on('error', (err) => {
      if (!settled) {
        settled = true;
        clearTimeout(readTimer);
        reject(err);
      }
    });
  });
}

// Single source of truth for "is this a vision-capable model?". Uses the
// capabilities classifier (which knows vila/neva/llava/paligemma/kosmos/
// florence/phi-3-vision/nvclip/parse/…) instead of a duplicated, drift-prone
// substring list. The previous convertVisionImages + sanitizeNvidiaPayload
// each kept their OWN hardcoded list that missed models the classifier knows
// (kosmos, florence, phi-3-vision, nvclip, …), so image blocks were silently
// stripped for those vision models.
function isVisionModel(modelId) {
  if (!modelId) return false;
  const t = classify(modelId).type;
  return t === 'vision_chat' || t === 'parse';
}

async function convertVisionImages(body) {
  if (!body || !Array.isArray(body.messages)) return;
  const isVision = isVisionModel(body.model);
  if (!isVision) return;

  // Configurable limits
  const MAX_IMAGE_SIZE = parseInt(process.env.MAX_IMAGE_SIZE_MB || '10', 10) * 1024 * 1024; // default 10MB
  const ALLOWED_IMAGE_TYPES = (process.env.ALLOWED_IMAGE_TYPES || 'image/jpeg,image/png,image/webp,image/gif').split(',');

  // Bug R1: SSRF Protection - block internal/private IP ranges. The previous
  // check was a string-prefix test on url.hostname only, which is bypassable
  // via DNS rebinding, IPv6 literals ([::1]), encoded IPv4 (2130706433 /
  // 0x7f000001 / 127.1), and case-variant schemes. Now we resolve DNS and
  // reject any resolved IP in a private/link-local/loopback range (fail-closed).
  const dns = require('dns').promises;
  const BLOCKED_IP_PREFIXES = ['127.', '10.', '172.16.', '172.17.', '172.18.', '172.19.', '172.20.', '172.21.', '172.22.', '172.23.', '172.24.', '172.25.', '172.26.', '172.27.', '172.28.', '172.29.', '172.30.', '172.31.', '192.168.', '169.254.', '100.64.', '0.0.0.0'];
  const _hostBlocked = async (host) => {
    if (BLOCKED_IP_PREFIXES.some(p => host === p || host.startsWith(p))) return true; // literal / range
    let addrs;
    try { addrs = await dns.lookup(host, { all: true }); } catch { return true; } // fail closed
    for (const { address, family } of addrs) {
      if (family === 6 && (address === '::1' || address.startsWith('fe80:') || address.startsWith('fc') || address.startsWith('fd'))) return true;
      if (BLOCKED_IP_PREFIXES.some(p => address === p || address.startsWith(p))) return true;
    }
    return false;
  };

  for (const msg of body.messages) {
    if (!msg || !msg.content) continue;
    if (Array.isArray(msg.content)) {
      for (const item of msg.content) {
        if (item && item.type === 'image_url' && item.image_url && typeof item.image_url.url === 'string') {
          const imgUrl = item.image_url.url;
          if (/^https?:\/\//i.test(imgUrl)) {
            // SSRF Protection: resolve host and reject internal IPs (DNS rebind safe)
            try {
              const url = new URL(imgUrl);
              if (await _hostBlocked(url.hostname)) {
                console.warn(`[SSRF] Blocked internal/private host access attempt: ${url.hostname}`);
                continue;
              }
            } catch (e) {
              console.warn(`[SSRF] Invalid URL format: ${imgUrl}`);
              continue;
            }
            try {
              console.log(`[wrapper-nvidia] Downloading image from URL for vision model: ${imgUrl}`);
              const res = await undiciFetch(imgUrl, { dispatcher: agent, signal: AbortSignal.timeout(15000) });
              if (res.ok) {
                const contentType = res.headers.get('content-type') || '';
                // Validate content type
                if (!ALLOWED_IMAGE_TYPES.some(t => contentType.startsWith(t.trim()))) {
                  console.warn(`[wrapper-nvidia] Rejected image with invalid content-type: ${contentType}`);
                  continue;
                }
                const contentLength = parseInt(res.headers.get('content-length') || '0', 10);
                if (contentLength > MAX_IMAGE_SIZE) {
                  console.warn(`[wrapper-nvidia] Rejected image too large: ${contentLength} bytes > ${MAX_IMAGE_SIZE}`);
                  continue;
                }
                const buffer = Buffer.from(await res.arrayBuffer());
                // Double-check actual size
                if (buffer.length > MAX_IMAGE_SIZE) {
                  console.warn(`[wrapper-nvidia] Rejected image too large after download: ${buffer.length} bytes`);
                  continue;
                }
                const base64Data = buffer.toString('base64');
                item.image_url.url = `data:${contentType};base64,${base64Data}`;
                console.log(`[wrapper-nvidia] Successfully converted vision image URL to base64 data URL (${buffer.length} bytes)`);
              }
            } catch (e) {
              console.error(`[wrapper-nvidia] Failed to download/convert image URL ${imgUrl}:`, e.message);
            }
          }
        }
      }
    }
  }
}
// Add standard rate limit headers to response
function addRateLimitHeaders(headers, keyLabel, pool) {
  const key = pool.keys.find(k => k.label === keyLabel);
  if (key) {
    const hardLimit = key.effectiveHardLimit(pool.hardLimit);
    const remaining = Math.max(0, hardLimit - key.currentRpm());
    headers['X-RateLimit-Limit'] = hardLimit;
    headers['X-RateLimit-Remaining'] = remaining;
    headers['X-RateLimit-Reset'] = Math.ceil(Date.now() / 1000) + 60;
  }
}

// Reshape an arbitrary upstream error body into the canonical
// OpenAI/Anthropic error envelope { error: { message, type } } while keeping
// the REAL upstream HTTP status and wording. Returns { body, changed } where
// `body === input` when the input is already well-formed so callers can keep
// verbatim bodies. FastAPI errors arrive as {detail:"..."} or {detail:[...]}
// (array of {loc,msg,type}) — neither is OpenAI-shaped; an OpenAI SDK
// (e.g. Hermes) reads resp.error.message and throws on a raw `detail` object.
function normalizeErrorEnvelope(input, status, modelId) {
  if (input && input.error && typeof input.error.message === 'string') {
    // Already a valid envelope — leave verbatim (preserves upstream error.type
    // that clients such as Claude Code match for retry/recovery).
    return { body: input, changed: false };
  }
  let message = '';
  if (input && typeof input === 'object') {
    const detail = input.detail;
    if (typeof detail === 'string') {
      message = detail;
    } else if (Array.isArray(detail)) {
      message = detail.map(x => {
        if (typeof x === 'string') return x;
        if (x && typeof x === 'object') {
          const loc = Array.isArray(x.loc) ? x.loc.join('.') : (x.loc || '');
          return [x.msg, x.message, loc].filter(Boolean).join(' ');
        }
        return String(x);
      }).join(' ') || 'Validation error';
    } else if (typeof input.message === 'string') {
      message = input.message;
    } else {
      message = JSON.stringify(input).slice(0, 500);
    }
  } else if (typeof input === 'string') {
    message = input;
  } else {
    message = `Upstream error ${status}`;
  }
  const type = status === 429 ? 'rate_limit_error'
    : status === 401 ? 'authentication_error'
    : status === 403 ? 'permission_error'
    : status === 404 ? 'not_found_error'
    : status === 413 ? 'request_too_large'
    : status >= 400 && status < 500 ? 'invalid_request_error'
    : 'api_error';
  if (!message) message = `Upstream error ${status}`;
  return { body: { error: { message, type } }, changed: true };
}

function extractUsageFields(usage) {
  const u = usage || {};
  // Support both OpenAI format (prompt_tokens/completion_tokens) and
  // Anthropic format (input_tokens/output_tokens)
  const pt = u.prompt_tokens || u.input_tokens || 0;
  const ct = u.completion_tokens || u.output_tokens || 0;
  const tt = u.total_tokens || (pt + ct) || 0;

  // For OpenAI/NVIDIA format: cached tokens may be in prompt_tokens_details.cached_tokens
  // For models that don't support caching (e.g., GLM-5.2), prompt_tokens_details may be missing entirely.
  // Fall back to 0 when caching is not supported.
  let cacht = 0;
  if (u.prompt_tokens_details) {
    cacht = u.prompt_tokens_details.cached_tokens || 0;
  } else if ('cache_read_input_tokens' in u) {
    cacht = u.cache_read_input_tokens || 0;
  }
  return { pt, ct, tt, cacht };
}

function resolveBase(modelId) {
  const desc = describe(modelId, BASE_LLM, BASE_GENAI);
  const ep = (desc.endpoints || [])[0];
  return ep?.base_url || BASE_LLM;
}

// FIX B-3: Verify a chat-completion response carries a non-empty assistant
// content before forwarding it to the client. NVIDIA Nemotron reasoning can
// return an EMPTY content body (reasoning-only) which breaks OpenAI/Anthropic
// clients that require output text or tool calls. When content is empty we
// synthesize a minimal placeholder so the contract is satisfied instead of
// forwarding a hollow message. Only applied to chat-family models.
function messageHasContent(data) {
  const msg = data && data.choices && data.choices[0] && data.choices[0].message;
  if (!msg) return false;
  if (typeof msg.content === 'string') return msg.content.trim().length > 0;
  if (Array.isArray(msg.content)) return msg.content.some(c => (c && c.type === 'text' && (c.text || '').trim()) || (c && c.type === 'tool_use'));
  if (Array.isArray(msg.tool_calls) && msg.tool_calls.length > 0) return true;
  return false;
}

function ensureNonemptyContent(data) {
  if (!messageHasContent(data)) {
    const msg = data && data.choices && data.choices[0] && data.choices[0].message;
    if (msg && typeof msg.content === 'string') {
      msg.content = '[No text response; the model returned reasoning only.]';
    } else if (msg) {
      msg.content = msg.content || '[No text response; the model returned reasoning only.]';
    }
  }
  return data;
}

// FIX B9: Translate the Anthropic `thinking` param into the NIM model-specific
// reasoning toggle. CRITICAL for deepseek-v4-pro / deepseek-r1: per
// MASTER_PROMPT Lampiran A, those models HANG with no response unless
// `chat_template_kwargs: {enable_thinking:true, thinking:true}` is sent. The
// Anthropic `thinking` block is otherwise silently dropped, so Claude Code's
// extended-thinking requests to those models never return → stream cut by
// timeout → "Claude Code stops mid-process".
// Client-provided reasoning controls (chat_template_kwargs / reasoning_effort
// via extra_body) always win (override principle).
const _unknownReasoningLogged = new Set();

function translateThinkingToNim(oaiBody, nimModel, anthropicThinking) {
  if (anthropicThinking === undefined || anthropicThinking === null) return;
  const enabled = anthropicThinking === true ||
    (typeof anthropicThinking === 'object' && anthropicThinking.type !== 'disabled');

  const cfg = findReasoningConfig(nimModel);
  if (!cfg) {
    if (!_unknownReasoningLogged.has(nimModel)) {
      _unknownReasoningLogged.add(nimModel);
      console.warn(`[REASONING] Model "${nimModel}" is NOT in REASONING_CONFIGS and client requested thinking. Add an entry for this model family if it supports reasoning — otherwise, the model responds without thinking (synthetic thinking block is emitted).`);
    }
    return;
  }

  if (cfg.mechanism === 'chat_template_kwargs') {
    const obj = {};
    for (const [k, v] of Object.entries(cfg.params)) {
      obj[k] = enabled ? v : false;
    }
    oaiBody.chat_template_kwargs = { ...(oaiBody.chat_template_kwargs || {}), ...obj };
  } else if (cfg.mechanism === 'reasoning_effort') {
    oaiBody.reasoning_effort = enabled ? (cfg.params.effort || 'high') : 'low';
  } else if (cfg.mechanism === 'nemotron_chat_template') {
    // FIX B-2: NVIDIA Nemotron family reasoning schema. Inject
    // { enable_thinking, force_nonempty_content } into chat_template_kwargs and
    // pass reasoning_budget through from extra_body verbatim (client wins).
    const obj = {};
    for (const [k, v] of Object.entries(cfg.params)) {
      obj[k] = enabled ? v : false;
    }
    oaiBody.chat_template_kwargs = { ...(oaiBody.chat_template_kwargs || {}), ...obj };
    // FIX B-2: pass reasoning_budget through verbatim. The gateway brief
    // specifies extra_body.reasoning_budget; NVIDIA docs also accept
    // chat_template_kwargs.reasoning_budget. Honor whichever the client sent.
    const rb = (oaiBody.extra_body && oaiBody.extra_body.reasoning_budget) ??
      (oaiBody.chat_template_kwargs && oaiBody.chat_template_kwargs.reasoning_budget);
    if (rb !== undefined && rb !== null) {
      oaiBody.extra_body = { ...oaiBody.extra_body, reasoning_budget: rb };
    }
  }
}

// FIX B9: Auto-inject the required reasoning toggle for model families that
// HANG without it (deepseek-v4 / deepseek-r1). Only when the client did not
// already send an explicit reasoning control. This makes plain requests
// (e.g. a trivial "PONG") to those models actually return instead of hanging.
function applyDefaultReasoning(body, modelId) {
  const hasExplicit = !!(body.chat_template_kwargs || body.reasoning_effort ||
    (body.extra_body && (body.extra_body.chat_template_kwargs || body.extra_body.reasoning_effort || body.extra_body.reasoning_budget)));
  if (hasExplicit) return;

  const cfg = findReasoningConfig(modelId);
  if (!cfg) return;
  // Only auto-inject for model families KNOWN to hang without the toggle
  // (e.g. deepseek). For unknown models and models that optionally support
  // thinking, do NOT inject — they work fine without it and injecting could
  // cause 400 errors for non-reasoning models.
  if (!cfg.requires_reasoning) return;

  if (cfg.mechanism === 'chat_template_kwargs') {
    const obj = {};
    for (const [k, v] of Object.entries(cfg.params)) {
      obj[k] = v;
    }
    body.chat_template_kwargs = { ...(body.chat_template_kwargs || {}), ...obj };
  } else if (cfg.mechanism === 'reasoning_effort') {
    body.reasoning_effort = cfg.params.effort || 'high';
  } else if (cfg.mechanism === 'nemotron_chat_template') {
    // FIX B-2 (cont.): auto-inject Nemotron reasoning toggle when client did
    // not send one, and forward reasoning_budget if present.
    const obj = {};
    for (const [k, v] of Object.entries(cfg.params)) {
      obj[k] = v;
    }
    body.chat_template_kwargs = { ...(body.chat_template_kwargs || {}), ...obj };
    const rb = (body.extra_body && body.extra_body.reasoning_budget) ??
      (body.chat_template_kwargs && body.chat_template_kwargs.reasoning_budget);
    if (rb !== undefined && rb !== null) {
      body.extra_body = { ...body.extra_body, reasoning_budget: rb };
    }
  }
}

const SKIP_HEADERS = new Set([
  'host','connection','content-length','transfer-encoding',
  'accept-encoding','x-forwarded-for','x-real-ip',
  'authorization','x-api-key','api-key'
]);

function forwardHeaders(req) {
  const h = {};
  h['Accept'] = 'application/json, text/event-stream';
  if (req.headers['content-type']) h['Content-Type'] = req.headers['content-type'];
  // Forward EVERY non-sensitive header as an OPEN LIST — including anthropic-*
  // (anthropic-version, anthropic-beta, …) and x-hermes-*. Capability headers
  // must reach upstream verbatim: cutting them silently disables extended
  // context / interleaved thinking / tool schema features or even triggers a
  // 400. The previous allowlist behaviour dropped them. We only skip the
  // hop-level / auth headers in SKIP_HEADERS plus content-type/accept (set
  // explicitly below).
  for (const [k, v] of Object.entries(req.headers)) {
    const lk = k.toLowerCase();
    if (SKIP_HEADERS.has(lk)) continue;
    if (lk === 'content-type' || lk === 'accept') continue;
    h[k] = v;
  }
  return h;
}

function parseUnsupportedParams(bodyText) {
  let msg = '';
  try {
    const d = JSON.parse(bodyText);
    // FastAPI validation errors return `detail` as an ARRAY of
    // {loc,msg,type} objects (e.g. NIM 422 "extra fields not permitted"), and
    // some upstreams return a STRING. Coerce both to a string so the rest of
    // this function (which calls msg.toLowerCase()) never throws. Previously an
    // array `detail` threw `msg.toLowerCase is not a function`, which was
    // swallowed by the param-strip try/catch and silently turned a clean 422
    // into up to 4 masked NETWORK-ERROR retries + a raw, non-OpenAI-shaped body
    // passthrough that broke OpenAI clients (Hermes).
    const detail = d.detail;
    if (typeof detail === 'string') {
      msg = d.message || detail || (d.error && d.error.message) || '';
    } else if (Array.isArray(detail)) {
      msg = detail.map(x => {
        if (typeof x === 'string') return x;
        if (x && typeof x === 'object') {
          const loc = Array.isArray(x.loc) ? x.loc.join('.') : (x.loc || '');
          return [x.msg, x.message, loc].filter(Boolean).join(' ');
        }
        return String(x);
      }).join(' ');
      msg = d.message || msg || (d.error && d.error.message) || '';
    } else {
      msg = d.message || (d.error && d.error.message) || '';
    }
  } catch {
    msg = bodyText || '';
  }
  const lower = msg.toLowerCase();
  if (!lower.includes('unsupported parameter') && !lower.includes('extra fields') && !lower.includes('unexpected') && !lower.includes('unknown parameter') && !lower.includes('not allowed') && !lower.includes('not permitted')) {
    return [];
  }
  const matches = new Set();
  // NVIDIA NIM 422 envelopes often carry a structured `details`/`errors` array
  // with an explicit `field` (e.g. gliner-pii: "Unknown parameter 'top_p' is not
  // allowed" + details:[{field:"top_p",...}]). Pull those out directly so we
  // strip exactly the offending param without regex guessing.
  try {
    const d = JSON.parse(bodyText);
    for (const key of ['details', 'errors']) {
      if (Array.isArray(d[key])) {
        for (const item of d[key]) {
          const f = item && (item.field || item.loc);
          if (typeof f === 'string') {
            // loc may be a JSON pointer like ["body","top_p"] or "body.top_p"
            const cleaned = String(f).replace(/^body[.\/]?/, '').replace(/^["\[]?body["\]]\.?\/?/, '');
            matches.add(cleaned.replace(/^["']|["']$/g, ''));
          }
        }
      }
    }
  } catch {}
  // Main regex: match backtick, single-quote, double-quote, and JSON pointer
  // formats. Also support nested paths with dots.
  const regex = /`([^`]+)`|'([^']+)'|"([^"]+)"|\/([a-z_][a-z0-9_/]*)/gm;
  let m;
  while ((m = regex.exec(msg)) !== null) {
    const p = m[1] || m[2] || m[3] || m[4];
    if (p && p.trim()) {
      let param = p.trim();
      // Convert JSON pointer (/body/chat_template_kwargs/thinking) to dot notation.
      // Group 4 (m[4]) captures the path WITHOUT the leading slash (e.g., "body/chat_template_kwargs/thinking").
      if (m[4] !== undefined) {
        const parts = param.split('/');
        if (parts.length >= 2) {
          param = parts.slice(1).join('.');
        }
      }
      matches.add(param);
    }
  }
  // Fallback: scan for bare nested parameter paths after keywords like
  // "unsupported parameter", "extra fields", "unexpected field", etc.
  const keywordRegex = /(?:unsupported parameters?|extra fields?|unexpected fields?)(?: not permitted)?:?\s*([a-z_][a-z0-9_.]*(?:,\s*[a-z_][a-z0-9_.]*)*)/gi;
  let bm;
  while ((bm = keywordRegex.exec(msg)) !== null) {
    if (bm[1] && bm[1].trim()) {
      const parts = bm[1].split(',').map(s => s.trim());
      for (const part of parts) matches.add(part);
    }
  }
  return Array.from(matches);
}

// ── Upstream Routing Table ─────────────────────────────────────────────
function initUpstreamRoutes() {
  const defaultRoutes = {
    '/v2/nvcf': BASE_NVCF,
    '/v1/status': BASE_NVCF,
    '/v1/images': BASE_GENAI,
    '/v1/genai': BASE_GENAI,
    '/v1/infer': BASE_GENAI,
    '/v1/audio': BASE_GENAI,
    '/v1/video': BASE_GENAI,
    '/v1/retrieval': BASE_GENAI,
    // Rerank models live on BASE_LLM (integrate.api.nvidia.com/v1/ranking),
    // NOT BASE_GENAI — see the dedicated /v1/ranking handler and
    // capabilities.js rerank endpoint def. Keep this in sync so catch-all
    // requests to /v1/ranking route to the correct host.
    '/v1/ranking': BASE_LLM,
    '/v1/chat': BASE_LLM,
    '/v1/completions': BASE_LLM,
    '/v1/embeddings': BASE_LLM,
    '/v1/models': BASE_LLM
  };

  let custom = {};
  try {
    custom = JSON.parse(process.env.UPSTREAM_ROUTES || '{}');
  } catch (e) {
    console.error('[CONFIG ERROR] Failed to parse UPSTREAM_ROUTES env:', e.message);
  }

  const merged = { ...defaultRoutes, ...custom };
  UPSTREAM_ROUTES_SORTED = Object.entries(merged)
    .map(([k, v]) => [k.replace(/\/+$/, ''), v.replace(/\/+$/, '')])
    .sort((a, b) => b[0].length - a[0].length);
}

function routeUpstream(path) {
  const p = '/' + (path.startsWith('/') ? path.slice(1).toLowerCase() : path.toLowerCase());
  for (const [prefix, host] of UPSTREAM_ROUTES_SORTED) {
    if (p === prefix || p.startsWith(prefix + '/')) {
      return host;
    }
  }
  return BASE_LLM;
}

function modelFromPath(path) {
  const KNOWN_MODEL_HINTS = [
    'llama', 'mixtral', 'mistral', 'phi', 'gemma', 'nv-embed', 'glm',
    'qwen', 'deepseek', 'yi-', 'nemotron', 'minitron', 'llava', 'vila',
    'neva', 'paligemma', 'falcon', 'dbrx', 'command-r', 'cohere',
    'sdxl', 'sd3', 'flux', 'cascade', 'edgen', 'kosmos', 'eagle2',
    'seamless', 'canary', 'parakeet', 'titan', 'claude', 'gpt-',
    'o1-', 'o3-'
  ];
  const parts = path.split('/');
  for (const part of parts) {
    const lower = part.toLowerCase();
    for (const hint of KNOWN_MODEL_HINTS) {
      if (lower.includes(hint)) return part;
    }
  }
  return null;
}

// ── Model Verification Sweep ───────────────────────────────────────────
// P0-5 FIX: Increased grace period from 2 to 5 to prevent model flapping
const MODEL_GRACE_FAILS = 5;
const modelFailCount = {};

// Verify-sweep probe timeout. Heavy reasoning models (e.g. z-ai/glm-5.2,
// deepseek reasoning, nemotron ultra) can take 60-120s TTFT even for a 1-token
// "ping". A short probe timeout (the old hardcoded 15s) times these out and the
// grace mechanism then marks them "unavailable" → they vanish from /v1/models,
// and because they're hidden no live call ever recovers them (circular). Align
// the probe timeout with the real request TTFT budget so slow-but-alive models
// still verify. Configurable via PROBE_TIMEOUT_MS (default = TTFT_TIMEOUT_MS or 120s).
const PROBE_TIMEOUT_MS = parseInt(
  process.env.PROBE_TIMEOUT_MS || process.env.TTFT_TIMEOUT_MS || '120000', 10
);

function isModelUnavailable(modelId) {
  // BLOCK_UNAVAILABLE_MODELS: set to 'true' to enable proactive blocking of
  // models that the verification sweep has marked unavailable (404, DEGRADED,
  // or consecutive timeouts). Default 'false' = transparent proxy mode: all
  // models pass through, upstream returns the real error. The verification
  // infrastructure (probeModel, markModel, verifyModels, verifyLoop) still
  // runs and populates unavailableModels + the metrics DB regardless of this
  // toggle — it always informs the dashboard and /metrics/model-status.
  // Enable this when you want the wrapper to short-circuit known-dead models
  // with an immediate 404 instead of wasting a key slot and upstream timeout.
  if (process.env.BLOCK_UNAVAILABLE_MODELS === 'true') {
    return unavailableModels.has(modelId);
  }
  return false;
}

function markModel(modelId, ok, status, path, reason) {
  if (ok) {
    if (unavailableModels.has(modelId)) {
      console.log(`[verify] Model recovered: ${modelId} (${reason})`);
      unavailableModels.delete(modelId);
    }
    retiredModels.delete(modelId);
  } else {
    if (!unavailableModels.has(modelId)) {
      console.warn(`[verify] Model marked unavailable: ${modelId} (${reason})`);
      unavailableModels.add(modelId);
    }
    // Only DEFINITIVELY-dead failures hide the model from /v1/models discovery.
    // Timeouts (status 0) keep the model visible/callable — they are slow, not gone.
    // PATCH-A: Only hide from discovery when DISCOVERY_HIDE_PROBE_FAILED=true
    if (process.env.DISCOVERY_HIDE_PROBE_FAILED === 'true' && isDefinitiveDeadStatus(status, reason)) {
      retiredModels.add(modelId);
    } else {
      retiredModels.delete(modelId);
    }
  }
  metrics.setModelStatus(modelId, ok, status, reason, path);
}

function isDegradedResponse(respText) {
  if (!respText) return false;
  const lower = respText.toLowerCase();
  return lower.includes('degraded') && lower.includes('cannot be invoked');
}

function isContextLengthError(status, respText) {
  if (status !== 400 && status !== 413) return false;
  if (!respText) return false;
  const lower = respText.toLowerCase();
  return lower.includes('context length') ||
         lower.includes('maximum context') ||
         lower.includes('too many tokens') ||
         lower.includes('max_tokens') ||
         lower.includes('context window') ||
         lower.includes('prompt length') ||
         lower.includes('exceeds the limit') ||
         lower.includes('exceeds context') ||
         lower.includes('token limit exceeded') ||
         lower.includes('length of the messages');
}

// REMOVED: getFriendlyContextLimitError() — transparent proxy must pass upstream
// errors verbatim (no custom envelope). Context-length errors are now returned
// exactly as the upstream sent them so clients can match the wording for
// retry/recovery logic.

function noteLiveResult(path, model, status, respText) {
  const p = path.toLowerCase().replace(/\/+$/, '');
  if (!p.endsWith('chat/completions') && !p.endsWith('embeddings')) {
    return;
  }
  if (status === 200 && unavailableModels.has(model)) {
    markModel(model, true, 200, path, 'recovered via live traffic');
  } else if (status === 404) {
    markModel(model, false, 404, path, '404 on live traffic');
  }
  // Don't mark DEGRADED from live traffic - it's per-key, not global
}

async function probeModel(modelId) {
  const d = classify(modelId);
  const t = d.type;
  let path = '';
  let body = {};
  
  if (t === 'chat' || t === 'vision_chat') {
    path = 'v1/chat/completions';
    body = {
      model: modelId,
      messages: [{ role: 'user', content: 'ping' }],
      max_tokens: 1
    };
  } else if (t === 'embedding') {
    path = 'v1/embeddings';
    body = {
      model: modelId,
      input: ['ping'],
      input_type: 'query'
    };
  } else {
    return;
  }

  const key = pool.peekKey();
  if (!key) return;

  const baseUrl = resolveBase(modelId);
  const url = `${baseUrl}/${path}`;

  try {
    const resp = await undiciFetch(url, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${key.apiKey}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify(body),
      dispatcher: agent,
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS)
    });

    if (resp.status === 200 || resp.status === 422 || resp.status === 400) {
      delete modelFailCount[modelId];
    }
    if (resp.status === 404) {
      markModel(modelId, false, 404, path, '404 on verification probe');
    } else if (resp.status === 400) {
      let probeText = '';
      try { probeText = await resp.text(); } catch {}
      if (isDegradedResponse(probeText || '')) {
        markModel(modelId, false, 400, path, 'DEGRADED on verification probe');
      } else {
        markModel(modelId, true, 400, path, 'verified');
      }
    } else if (resp.status === 200 || resp.status === 422) {
      markModel(modelId, true, resp.status, path, 'verified');
    }
  } catch (e) {
    // Timeout / network failure → require consecutive failures before marking unavailable
    if (e.name === 'AbortError' || /timeout|abort|TEA/i.test(e.message)) {
      const fails = (modelFailCount[modelId] || 0) + 1;
      modelFailCount[modelId] = fails;
      if (fails >= MODEL_GRACE_FAILS) {
        markModel(modelId, false, 0, path, `TIMEOUT ${fails}x on verification probe (upstream silent)`);
      }
    }
    // Other transient errors (DNS, reset) → leave status unchanged
  }
}

async function verifyModels() {
  const ids = pool.modelsCached;
  if (!ids || ids.length === 0) return;
  
  console.log(`[verify] Model verification sweep: probing ${ids.length} models with concurrency ${VERIFY_CONCURRENCY}...`);
  
  let index = 0;
  const workers = Array.from({ length: VERIFY_CONCURRENCY }, async () => {
    while (index < ids.length) {
      const modelId = ids[index++];
      if (!modelId) break;
      try {
        await probeModel(modelId);
      } catch (e) {
        // ignore
      }
    }
  });

  await Promise.all(workers);
  console.log(`[verify] Model verification done: ${unavailableModels.size} unavailable (${retiredModels.size} hidden from discovery; the rest are slow-but-callable).`);
}

let serverInstance = null;

async function verifyLoop() {
  await new Promise(resolve => setTimeout(resolve, 30000));
  while (serverInstance && serverInstance.listening) {
    try {
      await verifyModels();
    } catch (e) {
      console.error('[verify] Verify sweep error:', e.message);
    }
    await new Promise(resolve => setTimeout(resolve, VERIFY_INTERVAL));
  }
}

async function loadUnavailableModelsFromDb() {
  try {
    const detailed = metrics.getUnavailableModelsDetailed();
    for (const row of detailed) {
      unavailableModels.add(row.model);
      // Persisted "definitively dead" models stay hidden from discovery across
      // restarts; persisted slow/timeout models are re-shown (the next verify
      // sweep, with the aligned PROBE_TIMEOUT_MS, will confirm them).
      if (isDefinitiveDeadStatus(row.last_status, row.reason)) {
        retiredModels.add(row.model);
      }
    }
    console.log(`[verify] Loaded ${unavailableModels.size} unavailable models from database (${retiredModels.size} definitively retired/hidden).`);
  } catch (e) {
    console.error('[verify] Failed to load unavailable models:', e.message);
  }
}

// ── Upstream Proxy (OpenAI format) ─────────────────────────────────────
function sanitizeNvidiaPayload(body) {
  if (!body || !Array.isArray(body.messages)) return;
  const isVision = isVisionModel(body.model);

  const newMessages = [];
  
  for (let i = 0; i < body.messages.length; i++) {
    let msg = body.messages[i];
    
    if (!isVision && msg.content && Array.isArray(msg.content)) {
      msg.content = msg.content.map(item => {
        if (item && item.type === 'image_url') {
          return { type: 'text', text: '[Image removed: multimodal processing not enabled for this model]' };
        }
        return item;
      });
    }

    if (msg.role === 'assistant' && Array.isArray(msg.tool_calls) && msg.tool_calls.length > 1) {
      const originalToolCalls = [...msg.tool_calls];
      const toolResults = [];
      let j = i + 1;
      while (j < body.messages.length && body.messages[j].role === 'tool') {
        toolResults.push(body.messages[j]);
        j++;
      }
      i = j - 1;
      // B3 FIX: msg.content can be a string, null, or an array (multimodal).
      // Only the first split message gets the text content; the rest get "".
      // For array content, extract only text blocks — image_url blocks are
      // stripped earlier for non-vision models, but vision models could have
      // them. NIM does not expect array content on assistant tool_call messages.
      let firstContent = "";
      if (typeof msg.content === 'string') {
        firstContent = msg.content;
      } else if (Array.isArray(msg.content)) {
        firstContent = msg.content
          .filter(c => c && c.type === 'text')
          .map(c => c.text || '')
          .join('');
      } else if (msg.content === null || msg.content === undefined) {
        firstContent = null;
      }
      for (let k = 0; k < originalToolCalls.length; k++) {
        const tc = originalToolCalls[k];
        const matchingResult = toolResults.find(r => r.tool_call_id === tc.id);
        newMessages.push({
          role: 'assistant',
          content: k === 0 ? firstContent : "",
          tool_calls: [tc]
        });
        if (matchingResult) {
          newMessages.push(matchingResult);
        }
      }
    } else {
      newMessages.push(msg);
    }
  }
  body.messages = newMessages;
}

async function proxyOpenai(body, reqHeaders, model, req = null, metricPath = '/v1/chat/completions') {
  sanitizeNvidiaPayload(body);
  let modelId = body.model || model || '';
  if (isModelUnavailable(modelId)) {
    return { status: 404, data: { error: { message: `Model ${modelId} is retired or unavailable`, type: 'invalid_request_error' } } };
  }

  await convertVisionImages(body);

  // Map max_completion_tokens → max_tokens for OpenAI-compatible clients
  if (body.max_completion_tokens !== undefined && body.max_tokens === undefined) {
    body.max_tokens = body.max_completion_tokens;
    delete body.max_completion_tokens;
  }

  // FIX B1b: Always inject stream_options.include_usage for streaming requests
  // so NVIDIA NIM includes usage in the final streaming chunk. This ensures
  // the dashboard Activity tab always shows accurate prompt/completion tokens.
  // FORCE include_usage regardless of what the client sent: some clients
  // (Hermes, OpenAI SDK, Claude Code) send stream_options:{} or
  // stream_options:{include_usage:false}, in which case NIM omits the final
  // usage chunk and usage is silently dropped/estimated. We always request it
  // and let the downstream translation layer honor the client's reporting
  // preference separately.
  if (body.stream) {
    body.stream_options = Object.assign({}, body.stream_options, { include_usage: true });
  }

  const headers = { ...reqHeaders };

  // Inject DEFAULT_ params from .env — only fills gap if client didn't send
  for (const [p, v] of Object.entries(DEFAULT_PARAMS)) {
    if (body[p] === undefined) {
      const num = Number(v);
      body[p] = Number.isFinite(num) ? num : v;
    }
  }


// Preserve chat_template_kwargs, reasoning_effort, nvext, and extra_body before proactive drop
const preservedParams = {};
["chat_template_kwargs", "reasoning_effort", "nvext"].forEach(p => {
  if (body[p] !== undefined) {
    preservedParams[p] = body[p];
    console.log(`[proxyOpenai] Preserving ${p}:`, JSON.stringify(body[p]));
  }
});

// Handle extra_body.nvext (merge into body.nvext if present)
if (body.extra_body && body.extra_body.nvext) {
  preservedParams.nvext = { ...preservedParams.nvext, ...body.extra_body.nvext };
  console.log('[proxyOpenai] Merged extra_body.nvext into nvext:', JSON.stringify(body.extra_body.nvext));
}

// Proactive drop: silently remove known-incompatible params (after defaults so the drop always wins)
for (const p of PROACTIVE_DROP) {
  if (["chat_template_kwargs", "reasoning_effort", "nvext"].includes(p)) continue;
  delete body[p];
}

// Restore preserved params
Object.assign(body, preservedParams);

  // FIX B9: Auto-inject required reasoning toggle for families that hang without
  // it (deepseek-v4 / deepseek-r1). Client-provided reasoning controls win.
  applyDefaultReasoning(body, modelId);

  const strippedParams = new Set();
  let attempt = 0;
  const maxAttempts = Math.max(MAX_RETRIES + 1, pool.totalKeys);
  // Tracks the LAST real upstream error so that, when every key/model fails,
  // we pass the original upstream status + body through verbatim instead of
  // wrapping it in a synthetic envelope. Clients (e.g. Claude Code) match the
  // upstream wording for retry/recovery, so a custom message breaks them.
  let lastUpstream = null;
  let key = null;
  let keyReleased = false;
  // FIX: Track consecutive network/blackhole errors per attempt. If all keys
  // produce the same blackhole timeout, fail fast with 503 instead of waiting
  // for the global pre-response watchdog to fire and return a confusing 504.
  let consecutiveNetworkErrors = 0;
  while (attempt < maxAttempts) {
    let keyResult = null;
    key = null;
    keyReleased = false;
    let pacingMs = 0;
    let cycles = 0;
    while (cycles < 3) {
      keyResult = await pool.acquire(modelId, req?.clientAbortSignal);
      key = keyResult ? keyResult.key : null;
      pacingMs = keyResult ? keyResult.waitedMs : 0;
      if (key) break;

      // Client disconnected — no point retrying
      if (req?.clientAbortSignal?.aborted) {
        if (cycles === 0) {
          return { status: 0 };
        }
        break;
      }

      cycles++;
      if (cycles >= 3) break;
      console.warn(`[RETRY-CYCLE] All keys exhausted for model: ${modelId}. Cycle ${cycles}/3: Waiting for adaptive revalidation...`);
      await new Promise(resolve => setTimeout(resolve, cycles * 1500));
      await pool.healInFlight();
      
      // Revalidate: unblock keys/models that are close to unblocking early to retry
      // Bug K2: only unblock keys/models that are within a small grace of true
      // expiry. KEY_BLOCK_CAP=30 and MODEL_BLOCK_CAP=10, so the old thresholds
      // (45 / 30) cleared EVERY block on the very next retry cycle (~1.5–3s
      // later), defeating the cooldown → immediate re-429 → premature 503.
      const GRACE = 3;
      for (const s of pool.keys) {
        if (s.isHardBlocked() && s.hardBlockedUntil - (Date.now() / 1000) < GRACE) {
          s.hardBlockedUntil = 0;
        }
        if (modelId && s.modelBlocks[modelId]) {
          const rem = s.modelBlocks[modelId] - (Date.now() / 1000);
          if (rem < GRACE) {
            delete s.modelBlocks[modelId];
          }
        }
      }
    }

    if (!key) {
      return { status: 503, data: { error: { message: `All API keys exhausted — no capacity available after revalidation cycles${modelId ? ` for model ${modelId} (${pool.availableForModel(modelId)} key(s) available, ${pool.availableKeys} total)` : ''}`, type: 'server_error' } } };
    }

    const startMs = Date.now();
    let ttftTimer = null;
    keyReleased = false;
    try {
      incInFlight();
      const baseUrl = resolveBase(modelId);
      const url = `${baseUrl}/v1/chat/completions`;
      // Forward all non-sensitive client headers (anthropic-*, x-hermes-*,
      // nv-*, x-nv-*, …) as an open list so capability headers reach NIM intact.
      const h = {
        ...forwardHeaders(req),
        'Authorization': `Bearer ${key.apiKey}`,
        'Content-Type': 'application/json',
        'Accept': body.stream ? 'text/event-stream' : 'application/json',
      };
const timeoutSec = parseInt(process.env.REQUEST_TIMEOUT || process.env.REQUEST_TIMEOUT_SEC || '120', 10);
// FIX B8: Streaming requests (esp. reasoning models that think for
// minutes before/while generating) need a MUCH larger timeout than
// non-streaming. The old Math.max(timeoutSec,120)=120s cap aborted
// deepseek-v4-pro etc. mid-stream (~119.7s), which truncates the SSE
// stream and makes Claude Code stop mid-process. Use a dedicated,
// generous streaming timeout decoupled from REQUEST_TIMEOUT_SEC.
// Reasoning models (deepseek-v4-pro, Qwen3-thinking) can think for
// 5-15 minutes before first token. 900s = 15 min gives enough headroom.
const streamTimeoutSec = parseInt(process.env.STREAM_REQUEST_TIMEOUT_SEC || '900', 10);
      const timeoutMs = (body.stream ? streamTimeoutSec : timeoutSec) * 1000;

      const ttftMs = parseInt(process.env.TTFT_TIMEOUT_MS || '110000', 10);
      ttftTimer = setTimeout(() => {
        console.warn(`[TTFT] Upstream model=${modelId} slow (>${ttftMs}ms), still waiting for ${body.stream ? 'STREAM_REQUEST_TIMEOUT_SEC=' + streamTimeoutSec + 's' : 'REQUEST_TIMEOUT=' + timeoutSec + 's'}`);
      }, ttftMs);
      
      const clientSignal = req?.clientAbortSignal;
      const fetchSignal = clientSignal
        ? AbortSignal.any([clientSignal, AbortSignal.timeout(timeoutMs)])
        : AbortSignal.timeout(timeoutMs);
      const resp = await undiciFetch(url, {
        method: 'POST',
        headers: h,
        body: JSON.stringify(body),
        dispatcher: agent,
        signal: fetchSignal,
      });
      clearTimeout(ttftTimer);

      let _400respText = '';
      if (resp.status === 400) {
        try { _400respText = await resp.clone().text(); } catch {}
      }
      noteLiveResult('v1/chat/completions', modelId, resp.status, _400respText);

      if (resp.status === 429) {
        const ra = parseInt(resp.headers.get('retry-after') || '0', 10) || 65;
        if (!keyReleased) { decInFlight(); key.decrementInFlight(); keyReleased = true; }
        let bodyText = '';
        try { bodyText = await resp.text(); } catch {}
        const [scope, reason] = await pool.registerRateLimit(key, modelId, ra, null, bodyText);
        metrics.recordRateLimitEvent({ keyLabel: key.label, model: modelId, retryAfterS: ra });
        if (attempt < maxAttempts - 1) {
          attempt++;
          await new Promise(resolve => setTimeout(resolve, 50));
          continue;
        }
        return { status: 429, retryAfter: String(ra), data: { error: { message: `Rate limited (retry-after ${ra}s). Scope: ${scope}, Reason: ${reason}`, type: 'rate_limit_error' } } };
      }

      if (resp.status === 400 || resp.status === 413 || resp.status === 422) {
        let respText = '';
        try { respText = await resp.text(); } catch {}
        if (isDegradedResponse(respText || '')) {
          console.warn(`[DEGRADED] Model ${modelId} is DEGRADED upstream on key ${key.label} — trying next key`);
          metrics.recordRequest({
            method: 'POST', path: metricPath,
            model: modelId, keyLabel: key.label,
            streaming: !!body.stream, statusCode: resp.status, latencyMs: Date.now() - startMs,
            wasRateLimited: false, pacingMs
          });
          if (!keyReleased) { pool.releaseSuccess(key); decInFlight(); keyReleased = true; }
          attempt++;
          await new Promise(resolve => setTimeout(resolve, 50));
          continue;
        }
        if (isContextLengthError(resp.status, respText)) {
          // Transparent proxy: pass upstream error verbatim (no custom envelope).
          // Clients match upstream wording for retry/recovery; rewriting breaks that.
          let errBody = null;
          try { errBody = JSON.parse(respText); } catch {}
          if (!errBody) errBody = { error: { message: respText || 'Context length exceeded', type: 'invalid_request_error' } };
          metrics.recordRequest({
            method: 'POST', path: metricPath,
            model: modelId, keyLabel: key.label,
            streaming: !!body.stream, statusCode: resp.status, latencyMs: Date.now() - startMs,
            wasRateLimited: false, pacingMs
          });
          if (!keyReleased) { pool.releaseSuccess(key); decInFlight(); keyReleased = true; }
          return { status: resp.status, data: errBody };
        }
        if ((resp.status === 400 || resp.status === 422) && attempt < maxAttempts - 1) {
          const badParams = parseUnsupportedParams(respText);
          const toStrip = badParams.filter(p => {
            if (strippedParams.has(p)) return false;
            if (PROTECTED_PARAMS.has(p)) return false; // Bug R2: never strip structural fields
            if (p.includes('.')) {
              const parts = p.split('.');
              let obj = body;
              for (let i = 0; i < parts.length - 1; i++) {
                if (obj && typeof obj === 'object' && parts[i] in obj) {
                  obj = obj[parts[i]];
                } else {
                  return false;
                }
              }
              return obj && typeof obj === 'object' && parts[parts.length - 1] in obj;
            }
            return body[p] !== undefined;
          });
          if (toStrip.length > 0) {
            for (const p of toStrip) {
              if (p.includes('.')) {
                const parts = p.split('.');
                let obj = body;
                for (let i = 0; i < parts.length - 1; i++) {
                  obj = obj[parts[i]];
                }
                delete obj[parts[parts.length - 1]];
              } else {
                delete body[p];
              }
              strippedParams.add(p);
            }
            console.warn(`[PARAM STRIP] Stripping unsupported params ${JSON.stringify(toStrip)} and retrying`);
            if (!keyReleased) { pool.releaseSuccess(key); decInFlight(); keyReleased = true; }
            attempt++;
            continue;
          }
        }
        let errBody = null;
        try { errBody = JSON.parse(respText); } catch {}
        console.warn(`[UPSTREAM ERROR] status: ${resp.status} for model: ${modelId} | error: ${JSON.stringify(errBody).slice(0, 500)}`);
        metrics.recordRequest({
          method: 'POST', path: metricPath,
          model: modelId, keyLabel: key.label,
          streaming: !!body.stream, statusCode: resp.status, latencyMs: Date.now() - startMs,
          wasRateLimited: false, pacingMs
        });
        if (!keyReleased) { pool.releaseSuccess(key); decInFlight(); keyReleased = true; }
        // Keep the real upstream HTTP status + wording, but always reshape the
        // body into the canonical {error:{message,type}} envelope so OpenAI
        // clients (Hermes) can parse resp.error.message even when upstream sent
        // a FastAPI {detail} body.
        const normalized = normalizeErrorEnvelope(errBody || { _raw: respText }, resp.status, modelId);
        lastUpstream = { status: resp.status, data: normalized.changed ? normalized.body : (errBody || { error: { message: respText || 'Bad Request', type: 'invalid_request_error' } }) };
        return lastUpstream;
      }

      if (resp.status >= 500 && attempt < maxAttempts - 1) {
        let respText = '';
        try { respText = await resp.clone().text(); } catch {}
        
        // Intercept NVIDIA 500 validation errors masquerading as server errors
        if (respText.includes('only supports single tool-calls at once') || 
            respText.includes('single tool-calls') ||
            respText.includes('multimodal processing is not enabled')) {
          const errBody = { 
            error: { 
              message: "NVIDIA NIM Validation Error: " + (respText.length < 200 ? respText : "Invalid Request (e.g. parallel tools or images not supported)"),
              type: 'invalid_request_error' 
            } 
          };
          console.warn(`[UPSTREAM 500 INTERCEPT] Converted to 400: ${respText}`);
          metrics.recordRequest({
            method: 'POST', path: metricPath,
            model: modelId, keyLabel: key.label,
            streaming: !!body.stream, statusCode: 400, latencyMs: Date.now() - startMs,
            wasRateLimited: false, pacingMs
          });
          if (!keyReleased) { pool.releaseSuccess(key); decInFlight(); keyReleased = true; }
          return { status: 400, data: errBody };
        }

        console.warn(`[UPSTREAM ERROR] status: ${resp.status} for model: ${modelId} — retrying next key`);
        if (!keyReleased) { pool.releaseSuccess(key); decInFlight(); keyReleased = true; }
        attempt++;
        await new Promise(resolve => setTimeout(resolve, Math.min(200 * attempt, 2000)));
        continue;
      }

      if (!resp.ok) {
        let errBody = null;
        try { errBody = await resp.json(); } catch {}
        console.warn(`[UPSTREAM ERROR] status: ${resp.status} for model: ${modelId} | error: ${JSON.stringify(errBody).slice(0, 500)}`);
        const latencyMs = Date.now() - startMs;
        // TRANSPARENT passthrough of the upstream HTTP STATUS + wording, but we
        // ALWAYS reshape the body into the canonical OpenAI/Anthropic error
        // envelope {error:{message,type}}. Many NVIDIA/NIM FastAPI errors come
        // back as {detail:"..."} or {detail:[...]} (not OpenAI-shaped), and an
        // OpenAI client (e.g. Hermes) reads resp.error.message — a raw `detail`
        // object makes it throw. We preserve the real status + wording while
        // giving every client a parseable envelope. Already-well-formed bodies
        // (including upstream OpenAI-shaped {error:{...}}) pass through verbatim
        // so Claude Code's upstream-type matching still works.
        const normalized = normalizeErrorEnvelope(errBody, resp.status, modelId);
        if (normalized.changed) errBody = normalized.body;
        metrics.recordRequest({
          method: 'POST', path: metricPath,
          model: modelId, keyLabel: key.label,
          streaming: !!body.stream, statusCode: resp.status, latencyMs,
          wasRateLimited: false, pacingMs
        });
        if (!keyReleased) { pool.releaseSuccess(key); decInFlight(); keyReleased = true; }
        lastUpstream = { status: resp.status, data: errBody };
        return lastUpstream;
      }

      if (body.stream) {
        // For streaming, we'll decInFlight in handleChatCompletions after streaming completes
        keyReleased = true;
        return { status: 200, stream: resp.body, key, model: modelId, startMs, pacingMs };
      }

      const data = await resp.json();
      // Normalize model name in response — NVIDIA sometimes prefixes with stg/
      if (data.model && modelId && data.model !== modelId) {
        data.model = modelId;
      }
      // FIX B-3: guarantee a non-empty assistant content for chat models so
      // downstream clients (Claude Code / OpenAI SDK) never receive a hollow
      // reasoning-only message from Nemotron family reasoning models.
      if (classify(modelId).type === 'chat' || classify(modelId).type === 'vision_chat' || classify(modelId).type === 'parse') {
        ensureNonemptyContent(data);
      }
      const { pt, ct, tt, cacht } = extractUsageFields(data.usage);
      metrics.recordRequest({
        method: 'POST', path: metricPath,
        model: modelId, keyLabel: key.label,
        streaming: false, statusCode: 200, latencyMs: Date.now() - startMs,
        promptTokens: pt, completionTokens: ct, cachedTokens: cacht, totalTokens: tt,
        wasRateLimited: false, pacingMs
      });
      if (!keyReleased) { pool.releaseSuccess(key); decInFlight(); keyReleased = true; }
      return { status: 200, data, key };
    } catch (e) {
      if (typeof ttftTimer !== 'undefined') clearTimeout(ttftTimer);
      // Client disconnected — abort upstream immediately, free the key
      if (req?.clientAbortSignal?.aborted) {
        const latencyMs = Date.now() - startMs;
        // Distinguish a genuine client disconnect from our own pre-response
        // watchdog abort: only the latter is a server-side upstream timeout.
        const serverAborted = !!req._preRespTimedOut;
        metrics.recordRequest({
          method: 'POST', path: metricPath,
          model: modelId, keyLabel: key.label,
          streaming: !!body.stream, statusCode: serverAborted ? 504 : 499, latencyMs,
          wasRateLimited: false, pacingMs
        });
        if (!keyReleased) { pool.releaseSuccess(key); decInFlight(); keyReleased = true; }
        return { status: serverAborted ? 504 : 499, data: { error: { message: serverAborted ? 'Upstream did not respond within the pre-response timeout' : 'Client disconnected', type: serverAborted ? 'upstream_error' : 'client_error' } } };
      }

      // Count consecutive blackhole/network errors across key attempts
      consecutiveNetworkErrors++;

      if (attempt < maxAttempts - 1) {
        console.warn(`[NETWORK ERROR] ${e.message} — retrying (attempt ${attempt + 1}/${maxAttempts}, consecutive_net_errors=${consecutiveNetworkErrors})`);
        if (!keyReleased) { pool.releaseSuccess(key); decInFlight(); keyReleased = true; }
        attempt++;
        // Short pause between retries so we don't immediately slam the next key
        await new Promise(resolve => setTimeout(resolve, Math.min(200 * attempt, 1000)));
        continue;
      }
      // All key attempts exhausted with network/timeout errors — upstream is blackholing
      // Return 503 (service unavailable) to clearly indicate the model/upstream is down
      const latencyMs = Date.now() - startMs;
      console.warn(`[BLACKHOLE] All ${maxAttempts} key attempts failed for model=${modelId} with network errors — upstream appears down`);
      metrics.recordRequest({
        method: 'POST', path: metricPath,
        model: modelId, keyLabel: key.label,
        streaming: !!body.stream, statusCode: 503, latencyMs,
        wasRateLimited: false, pacingMs
      });
      if (!keyReleased) { pool.releaseSuccess(key); decInFlight(); keyReleased = true; }
      return { status: 503, data: { error: { message: `Upstream model ${modelId} is not responding (all ${maxAttempts} API keys timed out). The model may be temporarily unavailable — please try again in a moment or switch to an available model.`, type: 'upstream_error' } } };
    } finally {
      if (!keyReleased && key) {
        pool.releaseSuccess(key);
        decInFlight();
      }
    }
  }
  // All attempts exhausted - ensure counter is decremented
  if (key && !keyReleased) {
    pool.releaseSuccess(key);
    decInFlight();
  }
  // Pass through the LAST real upstream error verbatim instead of a synthetic
  // envelope. If we somehow have nothing (e.g. pure network failure with no
  // upstream body on any key), surface a 502 with the actual failure note.
  if (lastUpstream) return lastUpstream;
  return { status: 502, data: { error: { message: 'All API keys failed to reach upstream NVIDIA NIM', type: 'api_error' } } };
}

// ── Generic POST Helper (embeddings, images, ranking) ─────────────────
async function proxyPost({ req, res, body, rawBody, modelId, path, getTargetUrl }) {
  // Validate modelId
  if (!modelId || typeof modelId !== 'string' || modelId.trim() === '') {
    console.warn(`[proxyPost] Empty modelId for path ${path}, rejecting request`);
    return jsonResp(res, 400, { error: { message: 'Model is required', type: 'invalid_request_error' } });
  }

  const strippedParams = new Set();
  let attempt = 0;
  const maxAttempts = Math.max(MAX_RETRIES + 1, pool.totalKeys);
  let lastUpstream = null;
  while (attempt < maxAttempts) {
    let keyResult = null;
    let key = null;
    let pacingMs = 0;
    let cycles = 0;
    while (cycles < 3) {
      keyResult = await pool.acquire(modelId, req?.clientAbortSignal);
      key = keyResult ? keyResult.key : null;
      pacingMs = keyResult ? keyResult.waitedMs : 0;
      if (key) break;

      if (req?.clientAbortSignal?.aborted) {
        if (cycles === 0) {
          return;
        }
        break;
      }

      cycles++;
      if (cycles >= 3) break;
      console.warn(`[RETRY-CYCLE] All keys exhausted for model: ${modelId} in proxyPost. Cycle ${cycles}/3: Waiting for adaptive revalidation...`);
      await new Promise(resolve => setTimeout(resolve, cycles * 1500));
      await pool.healInFlight();
      
      // Revalidate: unblock keys/models that are close to unblocking early to retry
      // Bug K2: only unblock keys/models that are within a small grace of true
      // expiry. KEY_BLOCK_CAP=30 and MODEL_BLOCK_CAP=10, so the old thresholds
      // (45 / 30) cleared EVERY block on the very next retry cycle (~1.5–3s
      // later), defeating the cooldown → immediate re-429 → premature 503.
      const GRACE = 3;
      for (const s of pool.keys) {
        if (s.isHardBlocked() && s.hardBlockedUntil - (Date.now() / 1000) < GRACE) {
          s.hardBlockedUntil = 0;
        }
        if (modelId && s.modelBlocks[modelId]) {
          const rem = s.modelBlocks[modelId] - (Date.now() / 1000);
          if (rem < GRACE) {
            delete s.modelBlocks[modelId];
          }
        }
      }
    }

    if (!key) {
      return jsonResp(res, 503, { error: { message: `All API keys exhausted — no capacity available after revalidation cycles${modelId ? ` for model ${modelId} (${pool.availableForModel(modelId)} key(s) available, ${pool.availableKeys} total)` : ''}`, type: 'server_error' } });
    }

    const startMs = Date.now();
    let ttftFired = false;
    const ttftMs = parseInt(process.env.TTFT_TIMEOUT_MS || '110000', 10);
    let ttftTimer = null;
    try {
      incInFlight();
      const targetUrl = getTargetUrl ? getTargetUrl(key) : `${resolveBase(modelId)}${path || '/v1/chat/completions'}`;
      const ppTimeoutSec = parseInt(process.env.REQUEST_TIMEOUT || process.env.REQUEST_TIMEOUT_SEC || '120', 10);
      const ppStreamTimeoutSec = parseInt(process.env.STREAM_REQUEST_TIMEOUT_SEC || '600', 10);
      // Image/audio/video/ranking generation can legitimately take minutes
      // (flux steps, video diffusion, …). The default 120s request timeout
      // would abort them mid-generation with a client-disconnect. Use a much
      // longer, dedicated timeout for those paths.
      const genTimeoutSec = parseInt(process.env.GEN_TIMEOUT_SEC || '900', 10);
      const isGenerationPath = /images|genai|infer|audio|video|ranking/i.test(path || '');
      const ppTimeoutMs = (body.stream ? ppStreamTimeoutSec : (isGenerationPath ? genTimeoutSec : ppTimeoutSec)) * 1000;
      
      ttftTimer = setTimeout(() => {
        ttftFired = true;
        console.warn(`[TTFT] Upstream model=${modelId} slow (>${ttftMs}ms), still waiting for REQUEST_TIMEOUT=${ppTimeoutSec}s`);
      }, ttftMs);
      
      const ppClientSignal = req?.clientAbortSignal;
      const ppFetchSignal = ppClientSignal
        ? AbortSignal.any([ppClientSignal, AbortSignal.timeout(ppTimeoutMs)])
        : AbortSignal.timeout(ppTimeoutMs);
      const resp = await undiciFetch(targetUrl, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${key.apiKey}`,
          'Content-Type': 'application/json',
          ...forwardHeaders(req)
        },
        body: JSON.stringify(body),
        dispatcher: agent,
        signal: ppFetchSignal,
      });
      clearTimeout(ttftTimer);

      let _pp400text = '';
      if (resp.status === 400) {
        try { _pp400text = await resp.clone().text(); } catch {}
      }
      noteLiveResult(path, modelId, resp.status, _pp400text);

      if (resp.status === 429) {
        const ra = parseInt(resp.headers.get('retry-after') || '0', 10) || 65;
        decInFlight();
        key.decrementInFlight();
        let bodyText = '';
        try { bodyText = await resp.text(); } catch {}
        await pool.registerRateLimit(key, modelId, ra, null, bodyText);
        metrics.recordRateLimitEvent({ keyLabel: key.label, model: modelId, retryAfterS: ra });
        if (attempt < maxAttempts - 1) {
          attempt++;
          await new Promise(resolve => setTimeout(resolve, 50));
          continue;
        }
        return jsonResp(res, 429, { error: { message: `Rate limited (retry-after ${ra}s)`, type: 'rate_limit_error' } });
      }

      if (resp.status === 400 || resp.status === 413 || resp.status === 422) {
        let respText = '';
        try { respText = await resp.text(); } catch {}
        if (isDegradedResponse(respText || '')) {
          console.warn(`[DEGRADED] Model ${modelId} is DEGRADED upstream on key ${key.label} (${path}) — trying next key`);
          let errBody = null;
          try { errBody = JSON.parse(respText); } catch {}
          metrics.recordRequest({
            method: 'POST', path, model: modelId, keyLabel: key.label,
            streaming: false, statusCode: resp.status, latencyMs: Date.now() - startMs,
            wasRateLimited: false, requestBytes: rawBody.length, pacingMs
          });
          pool.releaseSuccess(key);
          decInFlight();
          attempt++;
          await new Promise(resolve => setTimeout(resolve, 50));
          continue;
        }
        if (isContextLengthError(resp.status, respText)) {
          // Transparent proxy: pass upstream error verbatim (no custom envelope).
          let errBody = null;
          try { errBody = JSON.parse(respText); } catch {}
          if (!errBody) errBody = { error: { message: respText || 'Context length exceeded', type: 'invalid_request_error' } };
          metrics.recordRequest({
            method: 'POST', path, model: modelId, keyLabel: key.label,
            streaming: false, statusCode: resp.status, latencyMs: Date.now() - startMs,
            wasRateLimited: false, requestBytes: rawBody.length, pacingMs
          });
          pool.releaseSuccess(key);
          decInFlight();
          return jsonResp(res, resp.status, errBody);
        }
        if ((resp.status === 400 || resp.status === 422) && attempt < maxAttempts - 1) {
          const badParams = parseUnsupportedParams(respText);
          const toStrip = badParams.filter(p => body[p] !== undefined && !strippedParams.has(p) && !PROTECTED_PARAMS.has(p));
          if (toStrip.length > 0) {
            for (const p of toStrip) {
              delete body[p];
              strippedParams.add(p);
            }
            console.warn(`[PARAM STRIP] Stripping unsupported params ${JSON.stringify(toStrip)} and retrying`);
            pool.releaseSuccess(key);
            decInFlight();
            attempt++;
            continue;
          }
        }
        // No strippable params or retries exhausted — return 400 with captured body
        let errBody = null;
        try { errBody = JSON.parse(respText); } catch {}
        metrics.recordRequest({
          method: 'POST', path, model: modelId, keyLabel: key.label,
          streaming: false, statusCode: resp.status, latencyMs: Date.now() - startMs,
          wasRateLimited: false, requestBytes: rawBody.length, pacingMs
        });
        pool.releaseSuccess(key);
        decInFlight();
        return jsonResp(res, resp.status, errBody || { error: { message: respText || 'Bad Request', type: 'invalid_request_error' } });
      }

      if (resp.status >= 500 && attempt < maxAttempts - 1) {
        console.warn(`[UPSTREAM ERROR] status: ${resp.status} for model: ${modelId} — retrying next key`);
        pool.releaseSuccess(key);
        decInFlight();
        attempt++;
        await new Promise(resolve => setTimeout(resolve, Math.min(200 * attempt, 2000)));
        continue;
      }

      if (resp.status >= 500) {
        let respText = '';
        try { respText = await resp.text(); } catch {}
        let errBody = null;
        try { errBody = JSON.parse(respText); } catch {}
        if (!errBody) {
          errBody = normalizeErrorEnvelope(null, resp.status, modelId).body;
        } else {
          const normalized = normalizeErrorEnvelope(errBody, resp.status, modelId);
          if (normalized.changed) errBody = normalized.body;
        }
        console.warn(`[UPSTREAM ERROR] status: ${resp.status} for model: ${modelId} — all retries exhausted`);
        metrics.recordRequest({
          method: 'POST', path, model: modelId, keyLabel: key.label,
          streaming: false, statusCode: resp.status, latencyMs: Date.now() - startMs,
          wasRateLimited: false, requestBytes: rawBody.length, pacingMs
        });
        pool.releaseSuccess(key);
        decInFlight();
        lastUpstream = { status: resp.status, data: errBody };
        return jsonResp(res, resp.status, errBody);
      }

      const contentType = resp.headers.get('content-type') || '';
      let responseData;
      if (contentType.includes('json')) {
        responseData = await resp.json();
        // Normalize non-standard response from Flux/SD/Qwen models (artifacts -> data).
        // Trigger for any image-gen path: /v1/images/generations, /v1/images/edits,
        // and /v1/infer (which shares the image-gen handler). Native NIM genai
        // returns {artifacts:[{base64}]}; OpenAI clients expect {data:[{b64_json}]}.
        if ((path.includes('images') || path.includes('infer')) && responseData && Array.isArray(responseData.artifacts)) {
          responseData = {
            created: Math.floor(Date.now() / 1000),
            data: responseData.artifacts.map(art => ({
              b64_json: art.base64 || art.b64_json || '',
              revised_prompt: body.prompt || (body.text_prompts && body.text_prompts[0] && body.text_prompts[0].text) || ''
            }))
          };
        }
      } else {
        responseData = { status: resp.status, content_type: contentType, note: 'Binary response' };
      }

      const usage = responseData.usage;
      const { pt, ct, tt, cacht } = extractUsageFields(usage);
      metrics.recordRequest({
        method: 'POST', path, model: modelId, keyLabel: key.label,
        streaming: false, statusCode: resp.status, latencyMs: Date.now() - startMs,
        promptTokens: pt, completionTokens: ct, cachedTokens: cacht, totalTokens: tt,
        wasRateLimited: false, requestBytes: rawBody.length, pacingMs
      });
      pool.releaseSuccess(key);
      decInFlight();
      return jsonResp(res, resp.status, responseData);
    } catch (e) {
      if (typeof ttftTimer !== 'undefined') clearTimeout(ttftTimer);
      if (req?.clientAbortSignal?.aborted) {
        metrics.recordRequest({
          method: 'POST', path, model: modelId, keyLabel: key.label,
          streaming: false, statusCode: 499, latencyMs: Date.now() - startMs,
          wasRateLimited: false, requestBytes: rawBody.length, pacingMs
        });
        pool.releaseSuccess(key);
        decInFlight();
        return jsonResp(res, 499, { error: { message: 'Client disconnected', type: 'client_error' } });
      }
      if (attempt < maxAttempts - 1) {
        console.warn(`[NETWORK ERROR] ${e.message} — retrying (attempt ${attempt + 1}/${maxAttempts})`);
        pool.releaseSuccess(key);
        decInFlight();
        attempt++;
        await new Promise(resolve => setTimeout(resolve, Math.min(200 * attempt, 1000)));
        continue;
      }
      // All key attempts exhausted with network/timeout errors — upstream is blackholing
      console.warn(`[BLACKHOLE] All ${maxAttempts} key attempts failed for model=${modelId} (proxyPost) — upstream appears down`);
      metrics.recordRequest({
        method: 'POST', path, model: modelId, keyLabel: key.label,
        streaming: false, statusCode: 503, latencyMs: Date.now() - startMs,
        wasRateLimited: false, requestBytes: rawBody.length, pacingMs
      });
      pool.releaseSuccess(key);
      decInFlight();
      return jsonResp(res, 503, { error: { message: `Upstream model ${modelId} is not responding (all ${maxAttempts} API keys timed out). The model may be temporarily unavailable.`, type: 'upstream_error' } });
    }
  }
  if (lastUpstream) return jsonResp(res, lastUpstream.status, lastUpstream.data, undefined, lastUpstream.retryAfter ? { "Retry-After": lastUpstream.retryAfter } : undefined);
  return jsonResp(res, 502, { error: { message: 'All attempts failed to reach upstream NVIDIA NIM', type: 'api_error' } });
}

// ── Route Handlers ──────────────────────────────────────────────────────

/** POST /v1/chat/completions */
async function handleChatCompletions(body, req, res) {
  body.model = resolveTargetModel(body.model);
  const result = await proxyOpenai(body, forwardHeaders(req), body.model, req);

  if (result.stream) {
    const HEARTBEAT_MS = parseInt(process.env.HEARTBEAT_INTERVAL_MS || '5000', 10);
    let hbTimer = null;
    const hbTick = () => { try { if (!res.writableEnded && !res.destroyed) res.write(': keepalive\n\n'); } catch {} };
    try {
      const respHeaders = {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no',
      };
      addRateLimitHeaders(respHeaders, result.key.label, pool);
      res.writeHead(200, respHeaders);
      // FIX A-2: swallow async EPIPE/CONNRESET on client disconnect so a
      // dead socket never becomes an uncaught exception (Bug A-2).
      res.on('error', () => {});
      const reader = result.stream.getReader();
      const decoder = new TextDecoder();
      hbTimer = setInterval(hbTick, HEARTBEAT_MS);
      let lastUsage = null;
      let ttftMs = 0;
      // P1-3 FIX: Streaming buffer configurable via MAX_STREAM_BUFFER_KB env
      const MAX_STREAM_BUFFER = (parseInt(process.env.MAX_STREAM_BUFFER_KB || '512', 10)) * 1024;
      let streamBuffer = '';
      let isFirstChunk = true;
      let hasContent = false;
      let generatedChars = 0;
      let lastUsageSnippet = '';
      try {
        while (true) {
          if (res.writableEnded || res.destroyed) break;
          const { done, value } = await reader.read();
          if (done) {
            // FIX A-4: flush any trailing bytes still buffered in the
            // TextDecoder (multi-byte sequences split across a chunk
            // boundary) so a final partial UTF-8 char is NOT silently dropped.
            try { const tail = decoder.decode(); if (tail) res.write(tail); } catch {}
            break;
          }
          if (isFirstChunk) {
            ttftMs = Date.now() - result.startMs;
            isFirstChunk = false;
          }
          const chunkStr = decoder.decode(value, { stream: true });
          try { res.write(chunkStr); } catch { break; }
          if (chunkStr.includes('choices') || chunkStr.includes('content') || chunkStr.includes('text')) {
            hasContent = true;
          }
          // Accumulate emitted text length so we can estimate output tokens
          // accurately when NIM omits a usage chunk (see metrics below).
          for (const line of chunkStr.split('\n')) {
            const t = line.trim();
            if (t.startsWith('data:') && t !== 'data:[DONE]' && t !== 'data: [DONE]') {
              try {
                const c = JSON.parse(t.slice(5).trim());
                const d = c.choices?.[0]?.delta?.content;
                if (typeof d === 'string') generatedChars += d.length;
              } catch {}
            }
          }
          // Preserve usage chunks unconditionally by keeping the last 64KB
          if (chunkStr.includes('"usage"') || chunkStr.includes('"usage')) {
            lastUsageSnippet = chunkStr;
          }
          streamBuffer += chunkStr;
          if (streamBuffer.length > MAX_STREAM_BUFFER) {
            streamBuffer = streamBuffer.slice(-MAX_STREAM_BUFFER);
          }
        }
      } catch (e) { console.error('[stream error] handleChatCompletions:', e.message); }
      // Bug M-B: on client disconnect mid-stream, release the key + clear the
      // heartbeat timer. The early return previously skipped the cleanup
      // `finally` (which only wraps the metrics block below), leaking
      // key.inFlight and leaving an orphaned setInterval writing to a dead
      // socket → inflated effectiveLoad → premature 503s under agent load.
      if (res.writableEnded || res.destroyed) {
        if (hbTimer) { clearInterval(hbTimer); hbTimer = null; }
        // FIX: reader.cancel() returns a Promise that REJECTS with AbortError
        // when the upstream stream was aborted (client disconnect / timeout).
        // A bare `try { reader.cancel(); } catch {}` only swallows a
        // SYNCHRONOUS throw — the async rejection was leaked as an
        // `unhandledRejection` ([FATAL] in logs; potential instability
        // under agent load where streams are aborted constantly). Resolve
        // and swallow the rejection explicitly instead.
        try { Promise.resolve(reader.cancel()).catch(() => {}); } catch {}
        pool.releaseSuccess(result.key);
        decInFlight();
        return;
      }
      try {
        // FIX A-3/A-5: only treat the stream as "empty" when the
        // upstream never emitted a terminal [DONE] (any SSE-legal spacing
        // variant). A valid completion that ends with ONLY a usage chunk
        // (no content delta) must NOT be misclassified as context-too-large.
        const upDone = /data:\s*\[DONE\]/.test(streamBuffer);
        if (!hasContent && !upDone) {
          const friendlyMsg = `The context/history for model "${body.model}" is too large and exceeds the model's limit (or the upstream connection closed immediately). Please exit the current session and start a clean one.`;
          const errChunk = `data: ${JSON.stringify({ error: { message: friendlyMsg, type: 'invalid_request_error' } })}\n\n`;
          try { if (!res.destroyed) res.write(errChunk); } catch {}
        }
        // Emit exactly ONE canonical data: [DONE] — never a second one
        // even if upstream used a non-canonical spacing variant.
        if (!upDone) {
          try { if (!res.destroyed) res.write('data: [DONE]\n\n'); } catch {}
        }
        if (!res.destroyed) { try { res.end(); } catch {} }
      } catch {}

      try {
        // First try the preserved usage snippet
        if (lastUsageSnippet) {
          const lines2 = lastUsageSnippet.split('\n');
          for (const line of lines2) {
            const t = line.trim();
            if (t.startsWith('data:') && t.includes('"usage"')) {
              const parsed = JSON.parse(t.slice(5).trim());
              if (parsed && parsed.usage) { lastUsage = parsed.usage; }
            }
          }
        }
        if (!lastUsage) {
          const lines = streamBuffer.split('\n');
          for (let i = lines.length - 1; i >= 0; i--) {
            const line = lines[i].trim();
            if (line.startsWith('data:') && line !== 'data:[DONE]' && line !== 'data: [DONE]' && line.includes('"usage"')) {
              const parsed = JSON.parse(line.slice(5).trim());
              if (parsed && parsed.usage) {
                lastUsage = parsed.usage;
                break;
              }
            }
          }
        }
      } catch (usageErr) { console.warn(`[USAGE] Parse failed for ${body.model}: ${usageErr.message}`); }

      let { pt, ct, tt, cacht } = extractUsageFields(lastUsage);
      if (!pt) {
        const estimatedInput = estimateInputTokens(body);
        if (estimatedInput > 0) pt = estimatedInput;
      }
      // FIX B6: Estimate output tokens when upstream omits completion_tokens
      // in streaming usage (common for reasoning-only responses). Use the real
      // emitted text length, not a flat 1, so the dashboard matches actual work.
      if (!ct && (hasContent || generatedChars > 0)) {
        ct = Math.max(1, Math.ceil(generatedChars / 4));
      }
      tt = pt + ct;
      metrics.recordRequest({
        method: 'POST',
        path: '/v1/chat/completions',
        model: result.model,
        keyLabel: result.key?.label || 'unknown',
        streaming: true,
        statusCode: 200,
        latencyMs: Date.now() - result.startMs,
        ttftMs,
        promptTokens: pt,
        completionTokens: ct,
        cachedTokens: cacht,
        totalTokens: tt,
        wasRateLimited: false,
        retries: 0,
        pacingMs: result.pacingMs || 0
      });
      } finally {
        if (hbTimer) { clearInterval(hbTimer); hbTimer = null; }
        // FIX: reader.cancel() returns a Promise that REJECTS with AbortError
        // when the upstream stream was aborted (client disconnect / timeout).
        // A bare `try { reader.cancel(); } catch {}` only swallows a
        // SYNCHRONOUS throw — the async rejection was leaked as an
        // `unhandledRejection` ([FATAL] in logs; potential instability
        // under agent load where streams are aborted constantly). Resolve
        // and swallow the rejection explicitly instead.
        try { Promise.resolve(reader.cancel()).catch(() => {}); } catch {}
        pool.releaseSuccess(result.key);
        decInFlight();
      }
    return;
  }

  try { jsonResp(res, result.status, result.data, result.key?.label, result.retryAfter ? { 'Retry-After': result.retryAfter } : undefined); } catch {}
}

/** POST /v1/messages — Anthropic-compatible endpoint */
async function handleAnthropicMessages(rawBody, req, res) {
  if (process.env.WRAPPER_DEBUG) console.log('[handleAnthropicMessages] Raw request:', rawBody.slice(0, 500));
  let aBody;
  try { aBody = JSON.parse(rawBody); } catch (e) {
    console.error('[JSON PARSE ERROR] messages raw:', JSON.stringify(rawBody).slice(0, 1000), 'err:', e.message);
    return jsonResp(res, 400, anthropicError('invalid_request_error', 'Invalid JSON body: ' + e.message));
  }
  if (process.env.WRAPPER_DEBUG) console.log('[handleAnthropicMessages] Parsed body:', JSON.stringify(aBody).slice(0, 500));

  if (!aBody.model) {
    return jsonResp(res, 400, anthropicError('invalid_request_error', 'model is required'));
  }

  if (process.env.WRAPPER_DEBUG) console.log('[handleAnthropicMessages] Calling anthropicToOpenai...');
  // Estimate input tokens BEFORE translation — anthropicToOpenai() performs
  // context-window pruning that mutates aBody.messages (shifts off old turns),
  // so computing it afterwards would under-report usage to Claude Code.
  const inputTokens = estimateInputTokens(aBody);
  // Force Anthropic → OpenAI translation for ALL /v1/messages requests.
  // anthropicToOpenai returns either a valid OpenAI body or {error:{type,message}}.
  // Pass the authoritative NGC registry context so pruning uses the real context
  // window (e.g. deepseek-v4-pro=262144) instead of the stale hardcoded heuristic.
  // Bug R-M-A: resolve the alias to the real NIM id BEFORE looking up the
  // authoritative context window. registry.getOfficialContext does not know
  // Claude Code aliases (sonnet/opus/claude-*), so using aBody.model caused
  // getContextWindow() to fall back to a far-too-small heuristic window and
  // over-prune long conversations (e.g. opus → 131072 instead of 1,048,576).
  const officialContext = registry.getOfficialContext(resolveTargetModel(aBody.model));
  const translated = anthropicToOpenai(aBody, officialContext);
  if (process.env.WRAPPER_DEBUG) console.log('[handleAnthropicMessages] anthropicToOpenai result:', JSON.stringify(translated).slice(0, 500));
  if (translated.error) {
    return jsonResp(res, 400, anthropicError(translated.error.type || 'invalid_request_error', translated.error.message));
  }
  // From here on, `translated` is a valid OpenAI-format body (error already handled above).

  const requestedModel = aBody.model;
  const oaiBody = translated;
  oaiBody.model = resolveTargetModel(oaiBody.model);

  if (isModelUnavailable(oaiBody.model)) {
    return jsonResp(res, 404, anthropicError('not_found_error', `Model ${oaiBody.model} is retired or unavailable`));
  }

  // Proactive drop for Anthropic path too
  for (const p of PROACTIVE_DROP) { delete oaiBody[p]; }

  // FIX B9: Translate Anthropic `thinking` → NIM model-specific reasoning
  // toggle. Without this, deepseek-v4-pro / deepseek-r1 HANG with no response
  // (MASTER_PROMPT Lampiran A) and the stream is later cut by the timeout,
  // making Claude Code stop mid-process. translateThinkingToNim only sets the
  // toggle when the client sends `thinking`; applyDefaultReasoning (in
  // proxyOpenai) covers the no-`thinking` case for these families.
  if (aBody.thinking !== undefined) {
    translateThinkingToNim(oaiBody, oaiBody.model, aBody.thinking);
  }

  // FIX B1: Inject stream_options.include_usage for streaming requests so
  // NVIDIA NIM includes usage in the final streaming chunk. Without this,
  // capture.usage in streamOpenaiToAnthropic frequently misses prompt_tokens
  // resulting in zero-token records in the dashboard Activity tab.
  // FORCE include_usage (same rationale as B1b) so a client that sends
  // stream_options:{} or {include_usage:false} does not lose the usage chunk.
  if (oaiBody.stream) {
    oaiBody.stream_options = Object.assign({}, oaiBody.stream_options, { include_usage: true });
  }

  const result = await proxyOpenai(oaiBody, forwardHeaders(req), oaiBody.model, req, '/v1/messages');

  if (result.stream) {
    // Write SSE headers ONCE (before any retry loop)
    const respHeaders = {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      'Connection': 'keep-alive',
      'X-Accel-Buffering': 'no',
    };
    addRateLimitHeaders(respHeaders, result.key.label, pool);
    if (res.destroyed) {
      // Client disconnected before we could write headers. The key was already
      // acquired by proxyOpenai — release it so it isn't leaked.
      pool.releaseSuccess(result.key);
      decInFlight();
      return;
    }
    res.writeHead(200, respHeaders);
    // FIX A-2: swallow async EPIPE/CONNRESET on client disconnect
    // for the Anthropic streaming path too (same rationale as the
    // OpenAI path — a dead socket must not become an uncaught exception).
    res.on('error', () => {});

    const MAX_STREAM_RETRIES = 2;
    let streamRetries = 0;
    let retryResult = result;
    let shouldRetry = false;
    let finalStreamStatus = 200;
    let finalCapture = null;

    do {
      shouldRetry = false;
      const capture = { _startMs: retryResult.startMs };
      let hasContent = false;
      let streamError = null;
      let attemptStatus = 200;

      try {
        try {
          // Bug S1: buffer upstream SSE chunks until the FIRST content delta
          // (text/tool) arrives, then flush. If the upstream errors before any
          // content delta, the buffered message_start/ping is discarded and a
          // retry emits a FRESH, single message_start — instead of a duplicate
          // message_start that breaks the Anthropic SDK contract.
          const streamBuf = [];
          let flushed = false;
          const flushStreamBuf = () => {
            if (!flushed) {
              for (const c of streamBuf) {
                if (res.writableEnded || res.destroyed) break;
                try { res.write(c); } catch { break; }
              }
              streamBuf.length = 0;
              flushed = true;
            }
          };
          for await (const chunk of streamOpenaiToAnthropic(retryResult.stream, aBody.model, capture, inputTokens, req.requestId, !!aBody.thinking)) {
            if (res.writableEnded || res.destroyed) break;
            const isContent = chunk.includes('content_block_delta') || chunk.includes('text_delta') || chunk.includes('input_json_delta');
            if (!flushed) {
              streamBuf.push(chunk);
              if (isContent) flushStreamBuf();
            } else {
              try {
                res.write(chunk);
              } catch (writeErr) {
                console.error('[stream write error] handleAnthropicMessages:', writeErr.message);
                break;
              }
            }
            if (isContent) hasContent = true;
          }
          if (!flushed) streamBuf.length = 0; // discard partial stream on error-before-content
        } catch (e) {
          streamError = e;
          attemptStatus = 502;
          console.error('[stream error] handleAnthropicMessages:', e.message);
        }
        const connDead = res.writableEnded || res.destroyed;
        if (connDead) {
          attemptStatus = 499;
          // B1 FIX: use break instead of return so the finally block releases
          // the key AND the post-loop metrics block (lines 1900-1958) still
          // records the disconnect event. Previously `return` skipped metrics.
          finalStreamStatus = 499;
          finalCapture = null;
          break;
        }
        if (!connDead) {
          const streamEmittedStop = capture.stop !== undefined;
          const errored = streamError || capture.errored;
          const errDetail = capture.errorMessage || (streamError ? streamError.message : '');

          // RETRY: transient upstream error before any content was sent
          if (errored && !hasContent && streamRetries < MAX_STREAM_RETRIES) {
            const isTransient = !errDetail || ['terminated', 'socket hang up', 'ECONNRESET'].some(s => errDetail.includes(s));
            if (isTransient) {
              streamRetries++;
              shouldRetry = true;
              console.warn(`[stream retry] ${req.requestId} stream err="${errDetail}" retry=${streamRetries}/${MAX_STREAM_RETRIES}`);
            }
          }

          if (!shouldRetry) {
            if (errored) {
              attemptStatus = 502;
              const friendlyMsg = `The stream was interrupted due to an upstream network or model error: ${errDetail}.`;
              console.error(`[stream error] Emitting error event: model=${aBody.model} rid=${req.requestId} hasContent=${hasContent} error="${errDetail}"`);
              const errEvent = `event: error\ndata: ${JSON.stringify({ type: 'error', error: { type: 'api_error', message: friendlyMsg } })}\n\n`;
              try { if (!res.destroyed) res.write(errEvent); } catch {}
            } else {
              const onlyFriendlyErr = !hasContent && !streamEmittedStop;
              if (onlyFriendlyErr) {
                attemptStatus = 400;
                const friendlyMsg = `The Claude Code session history is too large and exceeds the model's context limit (or the upstream connection was closed immediately). Please exit the current Claude session (type /exit or Ctrl+D) and run 'claude' again to start a clean session.`;
                const errEvent = `event: error\ndata: ${JSON.stringify({ type: 'error', error: { type: 'api_error', message: friendlyMsg } })}\n\n`;
                try { if (!res.destroyed) res.write(errEvent); } catch {}
              }
              if (!streamEmittedStop && !onlyFriendlyErr) {
                if (!res.writableEnded && !res.destroyed) {
                  try { res.write(`event: message_stop\ndata: {"type":"message_stop"}\n\n`); } catch {}
                }
              }
            }
            if (!res.writableEnded && !res.destroyed) {
              try { res.end(); } catch {}
            }
          }
        }
        finalStreamStatus = attemptStatus;
        finalCapture = shouldRetry ? null : capture;
      } catch (e) {
        if (!res.destroyed && !shouldRetry) {
          console.error('[stream outer error] handleAnthropicMessages:', e.message);
        }
      } finally {
        pool.releaseSuccess(retryResult.key);
        decInFlight();
      }

      if (shouldRetry && streamRetries <= MAX_STREAM_RETRIES) {
        const backoffMs = 1000 * streamRetries;
        console.warn(`[stream retry] ${req.requestId} re-acquiring key and retrying...`);
        await new Promise(r => setTimeout(r, backoffMs));
        retryResult = await proxyOpenai(oaiBody, forwardHeaders(req), oaiBody.model, req, '/v1/messages');
        if (!retryResult.stream) {
          // FIX: a non-stream retry result is an OpenAI-shaped error
          // {error:{message,type}}. This is an ANTHROPIC SSE stream, so
          // we must emit a proper Anthropic `event: error` frame — NOT a
          // raw OpenAI `data: {...}` chunk, which is invalid for the
          // Anthropic Messages SSE contract and confuses the SDK
          // (Claude Code treats a bare `data:` as a message_delta and
          // throws on the malformed JSON / missing `type`).
          const oe = retryResult.data?.error || {};
          const mappedType =
            oe.type === 'rate_limit_error' ? 'rate_limit_error' :
            oe.type === 'invalid_request_error' ? 'invalid_request_error' :
            oe.type === 'authentication_error' ? 'authentication_error' :
            oe.type === 'permission_error' ? 'permission_error' :
            oe.type === 'not_found_error' ? 'not_found_error' :
            'api_error';
          const errEvent = `event: error\ndata: ${JSON.stringify({ type: 'error', error: { type: mappedType, message: oe.message || `Upstream error ${retryResult.status}` } })}\n\n`;
          try { if (!res.destroyed) res.write(errEvent); } catch {}
          break;
        }
      }
    } while (shouldRetry && streamRetries <= MAX_STREAM_RETRIES);

    // After retries exhausted: emit error from last failed attempt
    if (!finalCapture) {
      finalStreamStatus = 502;
      if (!res.destroyed && !res.writableEnded) {
        const fallbackMsg = 'The upstream service is temporarily unavailable. Please try again.';
        const errEvent = `event: error\ndata: ${JSON.stringify({ type: 'error', error: { type: 'api_error', message: fallbackMsg } })}\n\n`;
        try { res.write(errEvent); } catch {}
        try { res.end(); } catch {}
      }
      // Record metrics even for exhausted retries so the dashboard reflects the failure.
      // Bug: must log the RESOLVED NIM model id (oaiBody.model), not the raw client
      // alias (aBody.model e.g. 'sonnet'/'haiku'/'opus'). The dashboard Activity tab
      // is a per-model view of NVIDIA NIM usage; logging bare Claude aliases mixes
      // virtual labels into the real catalog and breaks per-model filtering.
      metrics.recordRequest({
        method: 'POST',
        path: '/v1/messages',
        model: oaiBody.model,
        keyLabel: retryResult.key?.label || 'unknown',
        streaming: true,
        statusCode: finalStreamStatus,
        latencyMs: Date.now() - (retryResult.startMs || 0),
        ttftMs: 0,
        promptTokens: inputTokens,
        completionTokens: 0,
        cachedTokens: 0,
        totalTokens: inputTokens,
        wasRateLimited: false,
        retries: streamRetries,
        pacingMs: retryResult.pacingMs || 0
      });
    }

    if (finalCapture) {
      let { pt, ct, tt, cacht } = extractUsageFields(finalCapture.usage);
      // Prefer the tokens actually emitted to the client (set in
      // streamOpenaiToAnthropic) so the dashboard matches what Claude Code saw,
      // even when NIM omitted a usage chunk.
      if (finalCapture.reportedInputTokens != null) pt = finalCapture.reportedInputTokens;
      if (finalCapture.reportedOutputTokens != null) ct = finalCapture.reportedOutputTokens;
      if (!pt && inputTokens > 0) {
        pt = inputTokens;
        cacht = 0;
      }
      tt = pt + ct;
      // Bug: record the RESOLVED NIM model id (oaiBody.model), not the raw client
      // alias. Keeps the dashboard Activity tab aligned with the upstream catalog.
      metrics.recordRequest({
        method: 'POST',
        path: '/v1/messages',
        model: oaiBody.model,
        keyLabel: retryResult.key.label,
        streaming: true,
        statusCode: finalStreamStatus,
        latencyMs: Date.now() - (finalCapture._startMs || 0),
        ttftMs: finalCapture.ttftMs || 0,
        promptTokens: pt,
        completionTokens: ct,
        cachedTokens: cacht,
        totalTokens: tt || (pt + ct),
        wasRateLimited: false,
        retries: streamRetries,
        pacingMs: retryResult.pacingMs || 0
      });
    }
    return;
  }

  // Non-streaming Anthropic response — proxyOpenai already recorded under
  // /v1/chat/completions with result.data.usage.  Just transform and return.
  if (result.status === 200 && result.data) {
    const anthroResp = openaiToAnthropic(result.data, aBody.model, req.requestId, !!aBody.thinking, estimateInputTokens(aBody));
    try { jsonResp(res, 200, anthroResp, result.key?.label); } catch {}
    return;
  }

  const errData = result.data || {};
  const errMsg = errData?.error?.message || `Upstream error ${result.status}`;
  // Preserve original upstream error type; fall back to status-derived type
  const originalType = errData?.error?.type;
  // HTTP 404 must map to Anthropic not_found_error even when upstream
  // returned an OpenAI-shaped type (e.g. invalid_request_error).
  const errType = (result.status === 404)
    ? 'not_found_error'
    : originalType || (
    result.status === 429 ? 'rate_limit_error' :
    result.status === 401 ? 'authentication_error' :
    result.status === 403 ? 'permission_error' :
    result.status === 404 ? 'not_found_error' :
    result.status >= 400 && result.status < 500 ? 'invalid_request_error' :
    'api_error'
  );
  try { jsonResp(res, result.status, anthropicError(errType, errMsg)); } catch {}
}

/** GET /health */
function handleHealth(res) {
  jsonResp(res, 200, { ...pool.healthJson(), keys: pool.allStats() });
}

/** GET /stats */
function handleStats(res) {
  const summ = metrics.summary();
  jsonResp(res, 200, {
    ...pool.healthJson(),
    ...summ,
    keys: pool.keyDetails(),
    models_cached_sample: pool.modelsCached.slice(0, 20),
    catalog_summary: summarize(buildCatalog(pool.modelsCached, BASE_LLM, BASE_GENAI)),
  });
}

function enrichModelMetadata(id, desc) {
  const isChat = desc.type === 'chat' || desc.type === 'vision_chat' || desc.type === 'parse';
  const isVision = desc.type === 'vision_chat' || desc.type === 'parse';
  // context_window / max_output_tokens only make sense for text-generation
  // models (chat / vision_chat / parse). Embedding, image, rerank, asr, tts,
  // audio, video, ocr models have no LLM-style context window — advertising
  // 131072 for them (the previous unconditional default) misled clients like
  // Claude Code that read /v1/models to size requests.
  const hasContextWindow = isChat;
  // IMPORTANT: spread `...desc` BEFORE the defaulted fields. The previous
  // order (`{context_window: desc.context_window || DEFAULT, ...desc}`) let the
  // spread re-introduce `context_window: undefined` for models without a
  // heuristic override (mistral-large, yi-large, deepseek-r1, …), which
  // JSON.stringify then dropped entirely — so /v1/models silently lost the
  // field for exactly the models that needed the default most. Spreading first
  // and then applying `?? DEFAULT` guarantees the default wins when upstream
  // metadata is absent, while still letting an explicit desc value override it.
  return {
    ...desc,
    id,
    object: 'model',
    owned_by: id.split('/')[0] || 'nvidia',
    created: 0,
    // Context window / max output come from the NGC-synced authoritative
    // registry when available (verified live, never a silent guess), then fall
    // back to the curated heuristic map, then a sane default. This fixes the
    // wrong context reported for models like deepseek-v4-pro (NGC: 262144,
    // previously hardcoded to 64000).
    context_window: hasContextWindow ? (registry.getOfficialContext(id)?.context ?? desc.context_window ?? getContextWindow(id)) : undefined,
    max_output_tokens: hasContextWindow ? (registry.getOfficialContext(id)?.maxOutput ?? desc.max_output_tokens ?? 4096) : undefined,
    supports_vision: isVision,
    supports_function_calling: isChat,
    // NVIDIA NIM only supports a SINGLE tool call per turn (the upstream
    // returns HTTP 500 "only supports single tool-calls at once" for parallel
    // tool calls — see the 500-intercept in proxyOpenai). Advertising
    // supports_parallel_tool_calls=true (the previous `isChat` default) made
    // Claude Code send parallel tool calls that NIM then rejected. Report the
    // real capability so clients serialize their tool calls.
    supports_parallel_tool_calls: false,
    supports_streaming: desc.streaming !== false,
    supports_structured_output: isChat,
    supports_tool_choice: isChat,
    supports_stop_sequences: isChat,
    supports_system_prompt: isChat,
    supports_temperature: isChat,
    supports_top_p: isChat,
    supports_top_k: isChat,
    supports_seed: isChat,
    supports_logprobs: isChat && !desc.type?.includes('embedding'),
    supports_embedding: desc.type === 'embedding',
    supports_batch: false,
    // ── Correct call method (per NVIDIA NIM API reference) ──────────────
    // How to actually invoke this model: base_url + endpoint + auth + protocol.
    // Clients (Claude Code picker, Hermes, OpenAI SDK) use this as the
    // authoritative "how to call" field. Multimodal models live on BASE_GENAI;
    // text/embedding/rerank on BASE_LLM.
    call: (() => {
      const primary = (desc.endpoints && desc.endpoints[0]) || null;
      if (!primary) return null;
      const openaiKinds = new Set(['chat', 'embeddings', 'ranking', 'asr', 'tts', 'openai_image']);
      const protocol = openaiKinds.has(primary.kind) ? 'openai-compatible' : 'native-nvidia';
      return {
        protocol,
        base_url: primary.base_url,
        endpoint: primary.path,
        method: 'POST',
        auth: 'Bearer',
      };
    })(),
    // PATCH-A: Probe status annotation for client filtering
    probe_status: unavailableModels.has(id) ? (metrics.modelStatusCache?.[id]?.last_status || null) : null,
    probe_reason: unavailableModels.has(id) ? (metrics.modelStatusCache?.[id]?.reason || null) : null,
    provider: 'nvidia',
    model_family: id.includes('/') ? id.split('/')[1]?.split('-')[0] || id : id.split('-')[0] || id,
  };
}

/** GET /v1/models
 *  Returns the live upstream catalog (pool.modelsCached) PLUS the curated
 *  non-chat models (CURATED_GENAI — FLUX/SD/Qwen image-gen, etc.) so that
 *  clients which only call the standard OpenAI /v1/models discovery (Claude
 *  Code gateway, OpenCode, Hermes, OpenAI SDK, …) actually SEE image-gen and
 *  other non-chat models. Without this, those models were invisible to any
 *  client that never calls /v1/capabilities — the §8 "metadata only in
 *  /v1/capabilities" bug class. /v1/capabilities already used buildCatalog;
 *  now /v1/models does too, so the two surfaces stay consistent.
 *
 *  DEFAULT: clean list — only original NIM IDs (no claude-* duplicates).
 *    Claude Code alias mapping (claude-<slug> → real NIM id) still works
 *    behind the scenes via DISCOVERY_TO_NIM + resolveTargetModel(), so
 *    clients CAN still send claude-* model names and they resolve correctly.
 *    They just don't clutter the discovery surface.
 *
 *  Query params:
 *    ?refresh=true   Force a live re-fetch from upstream (bypasses cache).
 *    ?gateway=1      Include claude-* discovery aliases (for Claude Code
 *                     gateway model picker — Claude Code only lists ids that
 *                     start with "claude"/"anthropic").
 */
async function handleModels(res, url = null) {
  const force = url?.searchParams?.get('refresh') === 'true';
  const gateway = url?.searchParams?.get('gateway') === '1';
  const allIds = await pool.refreshModels(force);
  // Only expose models that are genuinely usable. Unavailable models (retired,
  // or marked unavailable by the boot/periodic verify-sweep, live 404s, or
  // upstream degradation) are tracked in `unavailableModels` and still 404/503
  // if requested directly — so transparent-proxy routing is unchanged. They
  // simply don't appear in discovery, so clients (Claude Code model picker,
  // OpenAI SDK, dashboards) only see models that actually work on NVIDIA NIM.
  // Only expose models that are genuinely usable. `unavailableModels` is the
  // live record maintained by the boot/periodic verify-sweep, live 404s, and
  // upstream degradation (and loaded from the metrics DB at boot). We filter
  // the DISCOVERY list directly against it — independent of the
  // BLOCK_UNAVAILABLE_MODELS toggle, which only governs request-time short-
  // circuiting. Routing stays a transparent proxy: a request to a model not in
  // this list still passes through to NVIDIA NIM and gets the real upstream
  // error. Clients (Claude Code picker, OpenAI SDK, dashboards) only see models
  // that currently work.
  // PATCH-A: Discovery transparency — show ALL upstream models by default.
  // Verify-sweep telemetry still runs and populates unavailableModels/retiredModels,
  // but these sets no longer control discovery visibility. Upstream errors are
  // returned verbatim (transparent proxy). Set DISCOVERY_HIDE_PROBE_FAILED=true
  // to restore the old behavior of hiding probe-failed models from discovery.
  const DISCOVERY_HIDE_FAILED = process.env.DISCOVERY_HIDE_PROBE_FAILED === 'true';
  const ids = DISCOVERY_HIDE_FAILED ? allIds.filter(id => !retiredModels.has(id)) : allIds;
  console.log('[discovery] ' + ids.length + '/' + allIds.length + ' models (hide_failed=' + DISCOVERY_HIDE_FAILED + ')');
  // Always rebuild the DISCOVERY_TO_NIM map so behind-the-scenes claude-*
  // alias resolution works even when aliases are not in the response.
  refreshDiscoveryMap(ids);
  const catalog = buildCatalog(ids, BASE_LLM, BASE_GENAI);
  const data = [];
  for (const d of catalog) {
    // 1) Original NIM ID (always included — the clean default view)
    const raw = enrichModelMetadata(d.id, d);
    raw.owned_by = d.id.split('/')[0] || 'nvidia';
    raw.original_id = d.id;
    raw.aliases = [d.id];
    data.push(raw);

    // 2) Gateway discovery mode. Claude Code's model picker
    //    (CLAUDE_CODE_GATEWAY_MODEL_DISCOVERY_URL) requests /v1/models?gateway=1.
    //    The picker ONLY displays entries whose id begins with "claude"/"anthropic"
    //    and sends the selected id straight back as the model. So in gateway mode we
    //    MUST emit a "claude-<slug>" routing id (the key resolveTargetModel maps back
    //    to the exact NIM id) and label it with display_name = the exact NVIDIA NIM
    //    name. The picker then shows the real upstream name (e.g. "z-ai/glm-5.2")
    //    while the selected id routes deterministically through the DISCOVERY_TO_NIM
    //    reverse map. The exact NIM id is still the FIRST entry (clean passthrough for
    //    OpenAI-compatible clients like Codex/Hermes); the alias is an ADDITIONAL
    //    entry, not a replacement -- and it never clutters the default (non-gateway)
    //    discovery surface.
    if (gateway) {
      const alias = discoveryAlias(d.id);
      if (alias !== d.id) {
        const m = enrichModelMetadata(d.id, d);
        m.id = alias;
        m.original_id = d.id;
        m.aliases = [d.id, alias];
        m.nim_id = d.id;
        m.display_name = d.id;
        m.owned_by = d.id.split("/")[0] || "nvidia";
        data.push(m);
      }
    }
  }
  jsonResp(res, 200, { object: 'list', data });
}

/** GET /v1/models/:model */
async function handleModelInfo(modelId, res) {
  const targetId = resolveTargetModel(modelId);
  const desc = describe(targetId, BASE_LLM, BASE_GENAI);
  const m = enrichModelMetadata(targetId, desc);
  if (modelId.startsWith(DISCOVERY_PREFIX)) {
    m.id = modelId;
  } else {
    m.id = targetId;
  }
  jsonResp(res, 200, m);
}

/** GET /metrics/prom */
function handlePromMetrics(res) {
  pool._inFlight = inFlight;
  pool._avgLatency24h = metrics.avgLatency24h();
  pool._exhaust24h = metrics.exhaustionCount24h();
  res.writeHead(200, { 'Content-Type': 'text/plain; version=0.0.4' });
  res.end(pool.promMetrics());
}

/** GET / — dashboard redirect */
function handleDashboard(res) {
  res.writeHead(302, { 'Location': '/dashboard.html' });
  res.end();
}

// ── Static File Server (dashboard.html only) ─────────────────────────────
const DASHBOARD_PATH = join(WRAPPER_DIR, 'dashboard.html');

function serveDashboard(res) {
  try {
    let html = fs.readFileSync(DASHBOARD_PATH, 'utf8');
    // Inject the BEARER_TOKEN into the dashboard HTML so its JavaScript can
    // authenticate API calls without the user having to manually configure it.
    // The token is embedded as a `<meta>` tag read by the dashboard's init code.
    // When BEARER_TOKEN is empty (auth disabled), the meta tag is omitted and
    // the dashboard works in no-auth mode — matching production behavior.
    const token = process.env.BEARER_TOKEN?.trim() || '';
    if (token) {
      const metaTag = `<meta name="wrapper-bearer-token" content="${token.replace(/"/g, '&quot;')}">`;
      html = html.replace('<head>', '<head>\n' + metaTag);
    }
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(html);
  } catch {
    jsonResp(res, 404, { error: 'Dashboard not found' });
  }
}

// ── Catch-all Proxy Fallback ──────────────────────────────────────────
async function handleCatchAll(req, res, path, url) {
  const method = req.method;
  const isPost = method === 'POST' || method === 'PUT' || method === 'PATCH';
  
  let rawBody = '';
  let body = {};
  if (isPost) {
    rawBody = await readBody(req);
    try { body = JSON.parse(rawBody); } catch {}
  }

  const requestedModel = body.model || modelFromPath(path) || 'unknown';
  let modelId = resolveTargetModel(requestedModel);
  if (isPost) {
    body.model = modelId;
  }
  if (isModelUnavailable(modelId) || modelId === 'unknown') {
    return jsonResp(res, 404, { error: { message: modelId === 'unknown' ? 'Unknown model — cannot route request' : `Model ${modelId} is retired or unavailable`, type: 'invalid_request_error' } });
  }
  
  const targetHost = routeUpstream(path);
  const targetUrl = targetHost + path + (url.search ? url.search : '');
  const isStreaming = !!body.stream || (req.headers['accept'] && req.headers['accept'].includes('text/event-stream'));

  let attempt = 0;
  const strippedParams = new Set();
  const maxAttempts = Math.max(MAX_RETRIES + 1, pool.totalKeys);
  let lastUpstream = null;
  while (attempt < maxAttempts) {
    let keyResult = null;
    let key = null;
    let pacingMs = 0;
    let cycles = 0;
    while (cycles < 3) {
      keyResult = await pool.acquire(modelId, req?.clientAbortSignal);
      key = keyResult ? keyResult.key : null;
      pacingMs = keyResult ? keyResult.waitedMs : 0;
      if (key) break;

      if (req?.clientAbortSignal?.aborted) {
        if (cycles === 0) {
          return;
        }
        break;
      }

      cycles++;
      if (cycles >= 3) break;
      console.warn(`[RETRY-CYCLE] All keys exhausted for model: ${modelId} in handleCatchAll. Cycle ${cycles}/3: Waiting for adaptive revalidation...`);
      await new Promise(resolve => setTimeout(resolve, cycles * 1500));
      await pool.healInFlight();
      
      // Revalidate: unblock keys/models that are close to unblocking early to retry
      // Bug K2: only unblock keys/models that are within a small grace of true
      // expiry. KEY_BLOCK_CAP=30 and MODEL_BLOCK_CAP=10, so the old thresholds
      // (45 / 30) cleared EVERY block on the very next retry cycle (~1.5–3s
      // later), defeating the cooldown → immediate re-429 → premature 503.
      const GRACE = 3;
      for (const s of pool.keys) {
        if (s.isHardBlocked() && s.hardBlockedUntil - (Date.now() / 1000) < GRACE) {
          s.hardBlockedUntil = 0;
        }
        if (modelId && s.modelBlocks[modelId]) {
          const rem = s.modelBlocks[modelId] - (Date.now() / 1000);
          if (rem < GRACE) {
            delete s.modelBlocks[modelId];
          }
        }
      }
    }

    if (!key) {
      return jsonResp(res, 503, { error: { message: `All API keys exhausted — no capacity available after revalidation cycles${modelId ? ` for model ${modelId} (${pool.availableForModel(modelId)} key(s) available, ${pool.availableKeys} total)` : ''}`, type: 'server_error' } });
    }

    const startMs = Date.now();
    try {
      incInFlight();
      const headers = {
        'Authorization': `Bearer ${key.apiKey}`,
        ...forwardHeaders(req)
      };
      if (isPost) {
        headers['Content-Type'] = 'application/json';
      }
      const caTimeoutSec = parseInt(process.env.REQUEST_TIMEOUT || process.env.REQUEST_TIMEOUT_SEC || '120', 10);
      const caStreamTimeoutSec = parseInt(process.env.STREAM_REQUEST_TIMEOUT_SEC || '600', 10);
      const caGenTimeoutSec = parseInt(process.env.GEN_TIMEOUT_SEC || '900', 10);
      const caIsGen = /images|genai|infer|audio|video|ranking/i.test(path || '');
      const caTimeoutMs = (isStreaming ? caStreamTimeoutSec : (caIsGen ? caGenTimeoutSec : caTimeoutSec)) * 1000;
      const caClientSignal = req?.clientAbortSignal;
      const caFetchSignal = caClientSignal
        ? AbortSignal.any([caClientSignal, AbortSignal.timeout(caTimeoutMs)])
        : AbortSignal.timeout(caTimeoutMs);
      const resp = await undiciFetch(targetUrl, {
        method,
        headers,
        body: isPost ? JSON.stringify(body) : undefined,
        dispatcher: agent,
        signal: caFetchSignal,
      });

      let _gen400text = '';
      if (resp.status === 400) {
        try { _gen400text = await resp.clone().text(); } catch {}
      }
      noteLiveResult(path, modelId, resp.status, _gen400text);

      if (resp.status === 429) {
        const ra = parseInt(resp.headers.get('retry-after') || '0', 10) || 65;
        decInFlight();
        key.decrementInFlight();
        let bodyText = '';
        try { bodyText = await resp.text(); } catch {}
        await pool.registerRateLimit(key, modelId, ra, null, bodyText);
        metrics.recordRateLimitEvent({ keyLabel: key.label, model: modelId, retryAfterS: ra });
        if (attempt < maxAttempts - 1) {
          attempt++;
          await new Promise(resolve => setTimeout(resolve, 50));
          continue;
        }
        return jsonResp(res, 429, { error: { message: `Rate limited (retry-after ${ra}s)`, type: 'rate_limit_error' } });
      }

      if ((resp.status === 400 || resp.status === 413) && isPost) {
        let respText = '';
        try { respText = await resp.text(); } catch {}
        if (isDegradedResponse(respText || '')) {
          console.warn(`[DEGRADED] Model ${modelId} is DEGRADED upstream on key ${key.label} (${path}) — trying next key`);
          let errBody = null;
          try { errBody = JSON.parse(respText); } catch {}
          metrics.recordRequest({
            method, path, model: modelId, keyLabel: key.label,
            streaming: false, statusCode: resp.status, latencyMs: Date.now() - startMs,
            wasRateLimited: false, requestBytes: rawBody.length, pacingMs
          });
          pool.releaseSuccess(key);
          decInFlight();
          attempt++;
          await new Promise(resolve => setTimeout(resolve, 50));
          continue;
        }
        if (isContextLengthError(resp.status, respText)) {
          // Transparent proxy: pass upstream error verbatim (no custom envelope).
          let errBody = null;
          try { errBody = JSON.parse(respText); } catch {}
          if (!errBody) errBody = { error: { message: respText || 'Context length exceeded', type: 'invalid_request_error' } };
          metrics.recordRequest({
            method, path, model: modelId, keyLabel: key.label,
            streaming: false, statusCode: resp.status, latencyMs: Date.now() - startMs,
            wasRateLimited: false, requestBytes: rawBody.length, pacingMs
          });
          pool.releaseSuccess(key);
          decInFlight();
          return jsonResp(res, resp.status, errBody);
        }
        if ((resp.status === 400 || resp.status === 422) && attempt < maxAttempts - 1) {
          const badParams = parseUnsupportedParams(respText);
          const toStrip = badParams.filter(p => body[p] !== undefined && !strippedParams.has(p) && !PROTECTED_PARAMS.has(p));
          if (toStrip.length > 0) {
            for (const p of toStrip) {
              delete body[p];
              strippedParams.add(p);
            }
            console.warn(`[PARAM STRIP] Stripping unsupported params ${JSON.stringify(toStrip)} and retrying`);
            pool.releaseSuccess(key);
            decInFlight();
            attempt++;
            continue;
          }
        }
        // No strippable params or retries exhausted — return 400 with captured body
        let errBody = null;
        try { errBody = JSON.parse(respText); } catch {}
        metrics.recordRequest({
          method, path, model: modelId, keyLabel: key.label,
          streaming: false, statusCode: resp.status, latencyMs: Date.now() - startMs,
          wasRateLimited: false, requestBytes: rawBody.length, pacingMs
        });
        pool.releaseSuccess(key);
        decInFlight();
        return jsonResp(res, resp.status, errBody || { error: { message: respText || 'Bad Request', type: 'invalid_request_error' } });
      }

      if (resp.status >= 500 && attempt < maxAttempts - 1) {
        console.warn(`[UPSTREAM ERROR] status: ${resp.status} for model: ${modelId} — retrying next key`);
        pool.releaseSuccess(key);
        decInFlight();
        attempt++;
        await new Promise(resolve => setTimeout(resolve, Math.min(200 * attempt, 2000)));
        continue;
      }

      if (resp.status >= 500) {
        let respText = '';
        try { respText = await resp.text(); } catch {}
        let errBody = null;
        try { errBody = JSON.parse(respText); } catch {}
        if (!errBody) {
          errBody = normalizeErrorEnvelope(null, resp.status, modelId).body;
        } else {
          const normalized = normalizeErrorEnvelope(errBody, resp.status, modelId);
          if (normalized.changed) errBody = normalized.body;
        }
        console.warn(`[UPSTREAM ERROR] status: ${resp.status} for model: ${modelId} — all retries exhausted`);
        metrics.recordRequest({
          method, path, model: modelId, keyLabel: key.label,
          streaming: isStreaming, statusCode: resp.status, latencyMs: Date.now() - startMs,
          wasRateLimited: false, requestBytes: rawBody.length, pacingMs
        });
        pool.releaseSuccess(key);
        decInFlight();
        lastUpstream = { status: resp.status, data: errBody };
        return jsonResp(res, resp.status, errBody);
      }

      const contentType = resp.headers.get('content-type') || '';
      if (isStreaming || contentType.includes('text/event-stream')) {
        res.writeHead(resp.status, {
          'Content-Type': contentType,
          'Cache-Control': 'no-cache',
          'Connection': 'keep-alive',
          'X-Accel-Buffering': 'no',
        });
        // FIX A-2: swallow async EPIPE/CONNRESET on client disconnect
        // for the catch-all streaming path too (same rationale as the
        // OpenAI / Anthropic paths).
        res.on('error', () => {});

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        // P1-3 FIX: Streaming buffer configurable via MAX_STREAM_BUFFER_KB env
        const MAX_STREAM_BUFFER = (parseInt(process.env.MAX_STREAM_BUFFER_KB || '512', 10)) * 1024;
        let streamBuffer = '';
        let ttftMs = 0;
        let isFirstChunk = true;
        let hasContent = false;
        let lastUsageSnippet = '';
        try {
          while (true) {
            if (res.writableEnded || res.destroyed) break;
            const { done, value } = await reader.read();
            if (done) break;
            if (isFirstChunk) {
              ttftMs = Date.now() - startMs;
              isFirstChunk = false;
            }
            const chunkStr = decoder.decode(value, { stream: true });
            try { res.write(chunkStr); } catch { break; }
            if (chunkStr.includes('choices') || chunkStr.includes('content') || chunkStr.includes('text')) {
              hasContent = true;
            }
            if (chunkStr.includes('"usage"') || chunkStr.includes('"usage')) {
              lastUsageSnippet = chunkStr;
            }
            streamBuffer += chunkStr;
            if (streamBuffer.length > MAX_STREAM_BUFFER) {
              streamBuffer = streamBuffer.slice(-MAX_STREAM_BUFFER);
            }
          }
        } catch (e) { console.error('[stream error] handleCatchAll:', e.message); }
        // FIX: reader.cancel() returns a Promise that REJECTS with AbortError
        // when the upstream stream was aborted (client disconnect / timeout).
        // A bare `try { reader.cancel(); } catch {}` only swallows a
        // SYNCHRONOUS throw — the async rejection was leaked as an
        // `unhandledRejection` ([FATAL] in logs; potential instability
        // under agent load where streams are aborted constantly). Resolve
        // and swallow the rejection explicitly instead.
        try { Promise.resolve(reader.cancel()).catch(() => {}); } catch {}
        // B5 FIX: previously `if (res.destroyed) { release; return; }` skipped
        // the usage-extraction + metrics-recording block below entirely, so
        // client disconnects during streaming left no trace in the dashboard.
        // Now we always run usage extraction + metrics, then check destroyed
        // only to decide whether to write the friendly-error chunk.
        let lastUsage = null;
        try {
          if (lastUsageSnippet) {
            const lines2 = lastUsageSnippet.split('\n');
            for (const line of lines2) {
              const t = line.trim();
              if (t.startsWith('data:') && t.includes('"usage"')) {
                try {
                  const parsed = JSON.parse(t.slice(t.indexOf(':') + 1).trim());
                  if (parsed && parsed.usage) { lastUsage = parsed.usage; }
                } catch {}
              }
            }
          }
          if (!lastUsage) {
            const lines = streamBuffer.split('\n');
            for (let i = lines.length - 1; i >= 0; i--) {
              const line = lines[i].trim();
              if (line.startsWith('data:') && line.includes('"usage"')) {
                try {
                  const parsed = JSON.parse(line.slice(line.indexOf(':') + 1).trim());
                  if (parsed && parsed.usage) {
                    lastUsage = parsed.usage;
                    break;
                  }
                } catch {}
              }
            }
          }
        } catch {}

        try {
          const { pt, ct, tt, cacht } = extractUsageFields(lastUsage);
          metrics.recordRequest({
            method, path, model: modelId, keyLabel: key.label,
            streaming: true, statusCode: res.destroyed ? 499 : resp.status, latencyMs: Date.now() - startMs,
            ttftMs,
            promptTokens: pt, completionTokens: ct, cachedTokens: cacht, totalTokens: tt,
            wasRateLimited: false, requestBytes: rawBody.length, pacingMs
          });
        } catch {} finally {
          pool.releaseSuccess(key);
          decInFlight();
        }

        if (res.destroyed) return;
        try {
          if (!hasContent) {
            const friendlyMsg = `The context/history for model "${modelId}" is too large and exceeds the model's limit (or the upstream connection closed immediately). Please exit the current session and start a clean one.`;
            const errChunk = `data: ${JSON.stringify({ error: { message: friendlyMsg, type: 'invalid_request_error' } })}\n\n`;
            try { if (!res.destroyed) res.write(errChunk); } catch {}
          }
          if (!res.destroyed) { try { res.end(); } catch {} }
        } catch {}
        return;
      }

      let responseData;
      let isJson = false;
      const respText = await resp.text();
      try {
        responseData = JSON.parse(respText);
        isJson = true;
      } catch {
        responseData = respText;
      }

      if (isJson) {
        try {
          const { pt, ct, tt, cacht } = extractUsageFields(responseData.usage);
          metrics.recordRequest({
            method, path, model: modelId, keyLabel: key.label,
            streaming: false, statusCode: resp.status, latencyMs: Date.now() - startMs,
            promptTokens: pt, completionTokens: ct, cachedTokens: cacht, totalTokens: tt,
            wasRateLimited: false, requestBytes: rawBody.length, pacingMs
          });
        } catch {} finally {
          pool.releaseSuccess(key);
          decInFlight();
        }
        return jsonResp(res, resp.status, responseData);
      } else {
        try {
          metrics.recordRequest({
            method, path, model: modelId, keyLabel: key.label,
            streaming: false, statusCode: resp.status, latencyMs: Date.now() - startMs,
            wasRateLimited: false, requestBytes: rawBody.length, pacingMs
          });
        } catch {} finally {
          pool.releaseSuccess(key);
          decInFlight();
        }
        res.writeHead(resp.status, { 'Content-Type': contentType });
        return res.end(responseData);
      }
    } catch (e) {
      if (req?.clientAbortSignal?.aborted) {
        metrics.recordRequest({
          method, path, model: modelId, keyLabel: key.label,
          streaming: isStreaming, statusCode: 499, latencyMs: Date.now() - startMs,
          wasRateLimited: false, requestBytes: rawBody.length, pacingMs
        });
        pool.releaseSuccess(key);
        decInFlight();
        return;
      }
      if (attempt < maxAttempts - 1) {
        console.warn(`[NETWORK ERROR] ${e.message} — retrying`);
        pool.releaseSuccess(key);
        decInFlight();
        attempt++;
        await new Promise(resolve => setTimeout(resolve, Math.min(200 * attempt, 2000)));
        continue;
      }
      metrics.recordRequest({
        method, path, model: modelId, keyLabel: key.label,
        streaming: isStreaming, statusCode: e.name === 'TimeoutError' ? 408 : 502, latencyMs: Date.now() - startMs,
        wasRateLimited: false, requestBytes: rawBody.length, pacingMs
      });
      pool.releaseSuccess(key);
      decInFlight();
      return jsonResp(res, e.name === 'TimeoutError' ? 408 : 502, { error: { message: e.message, type: 'upstream_error' } });
    }
  }
  if (lastUpstream) return jsonResp(res, lastUpstream.status, lastUpstream.data, undefined, lastUpstream.retryAfter ? { "Retry-After": lastUpstream.retryAfter } : undefined);
  return jsonResp(res, 502, { error: { message: 'All attempts failed to reach upstream NVIDIA NIM', type: 'api_error' } });
}

// ── Router ──────────────────────────────────────────────────────────────
async function handleRequest(req, res) {
  const requestId = generateRequestId();
  req.requestId = requestId;
  res.setHeader('X-Request-ID', requestId);

  let url, path, method;
  try {
    url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
    path = url.pathname;
    method = req.method;

    // ── OpenAI-compatible path normalization (FIX: missing /v1 prefix) ──
    // Many OpenAI-SDK based clients and agents (Hermes, LiteLLM, OpenCode,
    // Kilo Code, OpenAI SDK, curl scripts, …) send requests to
    // `/chat/completions`, `/embeddings`, `/models`, `/images/generations`,
    // `/ranking`, `/infer`, `/responses`, … WITHOUT the `/v1` prefix. Previously
    // these fell through to handleCatchAll, which forwarded the *unprefixed*
    // path to NVIDIA (e.g. https://integrate.api.nvidia.com/chat/completions)
    // and got a 404 — breaking EVERY text model for those clients. Here we
    // transparently rewrite the well-known OpenAI endpoint stems to their /v1
    // form so they hit the real handlers. We never touch /v1, /v2, /api
    // (Ollama), /metrics, /health, /dashboard, /events, /props, /version, ….
    if (!path.startsWith('/v1') && !path.startsWith('/v2') && !path.startsWith('/api')) {
      const OPENAI_NORMALIZE_STEMS = [
        '/chat/completions', '/completions', '/embeddings', '/models', '/engines',
        '/images/generations', '/images/edits', '/images/variations',
        '/audio/transcriptions', '/audio/translations', '/audio/speech',
        '/moderations', '/responses', '/files', '/fine_tuning', '/batches',
        '/ranking', '/infer',
      ];
      for (const stem of OPENAI_NORMALIZE_STEMS) {
        if (path === stem || path.startsWith(stem + '/')) {
          path = '/v1' + path;
          console.log(`[normalize] Rewrote path -> ${path}`);
          break;
        }
      }
    }

  } catch (e) {
    console.warn(`[${requestId}] Malformed request URL: ${req.url}`);
    return jsonResp(res, 400, { error: { message: 'Invalid request URL', type: 'invalid_request_error' } });
  }

  const controller = new AbortController();
  req.clientAbortSignal = controller.signal;
  res.on('close', () => {
    if (!res.writableEnded) {
      console.warn(`[close-abort] request ${requestId} path=${path} closed before writableEnded — aborting`);
      controller.abort();
    }
  });

  // REVISI audit: hard ceiling on time-to-first-byte for the WHOLE request
  // handler. When the upstream silently blackholes (TLS connects, no HTTP
  // response) the per-attempt undici headersTimeout (30s) + multi-key retry
  // loop can still keep a *patient* client (e.g. Claude Code, which does NOT
  // impose a short client-side timeout) hanging for minutes. This watchdog
  // aborts the upstream fetch if we have not written any response headers
  // within PRE_RESPONSE_TIMEOUT_MS, so the handler's normal error path returns
  // a clean 502/504 instead of stalling indefinitely. Once headers are sent
  // (streaming or any response) the handler is committed and the watchdog is
  // inert — it only checks res.headersSent before acting.
  // PRE_RESPONSE_TIMEOUT_MS: global watchdog that aborts the ENTIRE request if we have
  // not sent any response headers within this budget. Must be >= headersTimeout * maxKeyAttempts
  // so all key retries complete before the watchdog fires. With headersTimeout=15s and
  // 5 keys, worst case = 15s * 5 = 75s. Default = 180s gives generous safety margin.
  // Previously was 45s which caused it to fire mid-retry (after ~1.5 key attempts).
  const PRE_RESPONSE_TIMEOUT_MS = parseInt(process.env.PRE_RESPONSE_TIMEOUT_MS || '180000', 10);
  const preRespTimer = setTimeout(() => {
    if (!res.headersSent && !res.writableEnded && !res.destroyed) {
      req._preRespTimedOut = true;
      console.warn(`[pre-resp-timeout] request ${requestId} path=${path} exceeded ${PRE_RESPONSE_TIMEOUT_MS}ms with no response headers — aborting upstream`);
      controller.abort();
    }
  }, PRE_RESPONSE_TIMEOUT_MS);

  // Log incoming request
  const startTime = Date.now();
  console.log(`[${requestId}] ${method} ${path} from ${clientIp(req)}`);

  // ── CORS headers for all responses ──
  // Allow-Origin '*' so any browser-based client (Claude Code web, OpenRouter-
  // style gateways, custom UIs) can call the wrapper cross-origin. The
  // Allow-Headers list must include the Anthropic SDK headers
  // (anthropic-version, anthropic-beta, x-api-key) and the OpenAI headers
  // (OpenAI-Beta) so browser preflight doesn't reject them — the previous list
  // omitted them, which broke any browser client using the Anthropic SDK.
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Request-ID, anthropic-version, anthropic-beta, x-api-key, OpenAI-Beta, x-stainless-*');
  res.setHeader('Access-Control-Expose-Headers', 'X-Request-ID, X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset');
  if (method === 'OPTIONS') {
    res.writeHead(204);
    return res.end();
  }

  // ── Bearer Token Auth (if configured) ──
  const BEARER_TOKEN = process.env.BEARER_TOKEN?.trim();
  // Read-only public paths that never require auth (health, dashboard, the
  // Prometheus scrape, the live activity SSE feed). NOTE: the previous list
  // exempted the entire /metrics/* namespace, which let the destructive
  // POST /metrics/reset bypass auth even when BEARER_TOKEN was set. Only
  // GET /metrics/prom remains public; everything else under /metrics now
  // requires the bearer token when one is configured.
  // Public paths: no auth needed. /v1/models and /v1/models/:id are read-only
  // discovery endpoints needed by dashboards and client model-pickers.
  const publicPaths = ['/health', '/metrics/prom', '/', '/dashboard.html', '/dashboard', '/favicon.ico', '/events'];
  const isPublic = publicPaths.includes(path)
    // Bug R6: only the Prometheus scrape is public. The previous
    // path.startsWith('/metrics') made every GET /metrics/* (keys, rate-limits,
    // activity, model-status) readable without the bearer token. Expose only
    // /metrics/prom explicitly.
    || path === '/metrics/prom'
    || (method === 'GET' && path === '/v1/models')
    || (method === 'GET' && path.startsWith('/v1/models/'))
    || (method === 'GET' && path === '/v1/engines')
    || (method === 'GET' && path.startsWith('/v1/engines/'))
    || (method === 'GET' && (path === '/version' || path === '/api/version'))
    || (method === 'GET' && path === '/api/tags')
    || (method === 'GET' && (path === '/api/v1/models' || path === '/models'))
    || (method === 'GET' && (path === '/props' || path === '/v1/props'))
    || (method === 'GET' && path === '/v1/capabilities')
    || (method === 'GET' && path === '/v1/capabilities/params');
  if (BEARER_TOKEN && !isPublic) {
    // Accept both Authorization: Bearer <token> (OpenAI style) AND
    // x-api-key: <token> (Anthropic SDK style). Claude Code and other
    // Anthropic-compatible clients send x-api-key by default.
    const authHeader = (req.headers.authorization || '').trim();
    const apiKeyHeader = (req.headers['x-api-key'] || '').trim();
    const token = authHeader.replace(/^Bearer\s+/i, '') || apiKeyHeader;
    if (token !== BEARER_TOKEN) {
      console.warn(`[${requestId}] Auth failed for ${method} ${path}`);
      // Route-aware error envelope: the Anthropic Messages API expects
      // {"type":"error","error":{type,message}}; OpenAI expects
      // {"error":{message,type}}. Return the correct shape per route so
      // strict Anthropic clients (Claude Code) get a parseable 401.
      if (path === '/v1/messages' || path.startsWith('/v1/messages/')) {
        return jsonResp(res, 401, { type: 'error', error: { type: 'authentication_error', message: 'Unauthorized' } });
      }
      return jsonResp(res, 401, { error: { message: 'Unauthorized', type: 'authentication_error' } });
    }
  }

  try {
    // ─ Health / Meta ──
    if (path === '/health') {
      if (method === 'GET') return handleHealth(res);
      return jsonResp(res, 200, { status: 'ok' });
    }
    // ─ Root probe (HEAD /) — no upstream hit, no key acquire ──
    if (path === '/' && method === 'HEAD') { res.writeHead(200); return res.end(); }
    // ─ SSE Real-time Events ──
    if (path === '/events') {
      res.writeHead(200, {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no',
        'Access-Control-Allow-Origin': '*',
      });
      // Send initial connected event
      res.write(`event: connected\ndata: {"status":"ok"}\n\n`);
      sseClients.add(res);
      const keepalive = setInterval(() => {
        if (res.destroyed) { clearInterval(keepalive); return; }
        try { res.write(': keepalive\n\n'); } catch { clearInterval(keepalive); }
      }, 3000);
      req.on('close', () => {
        sseClients.delete(res);
        clearInterval(keepalive);
      });
      return;
    }
    if (method === 'GET' && path === '/stats')         return handleStats(res);
    if (method === 'GET' && path === '/metrics/prom')   return handlePromMetrics(res);
    if (method === 'GET' && path === '/v1/models')      return await handleModels(res, url);
    if (method === 'GET' && path.startsWith('/v1/models/')) {
      const modelId = decodeURIComponent(path.slice('/v1/models/'.length));
      return await handleModelInfo(modelId, res);
    }
    if (method === 'GET' && (path === '/' || path === '/dashboard.html' || path === '/dashboard')) {
      return serveDashboard(res);
    }

    // ─ Ollama / OpenAI Compat Probes ──
    // Legacy OpenAI Compatibility: /v1/engines -> /v1/models
    if (method === 'GET' && path === '/v1/engines') {
      return await handleModels(res, url);
    }
    // Legacy OpenAI Compatibility: /v1/engines/:model -> /v1/models/:model
    if (method === 'GET' && path.startsWith('/v1/engines/')) {
      const modelId = decodeURIComponent(path.slice('/v1/engines/'.length));
      return await handleModelInfo(modelId, res);
    }
    // Legacy OpenAI Compatibility: /v1/complete -> /v1/chat/completions
    if (method === 'POST' && path === '/v1/complete') {
      // Transform legacy completions format to chat completions
      const raw = await readBody(req);
      let body;
      try { body = JSON.parse(raw); } catch { return jsonResp(res, 400, { error: { message: 'Invalid JSON', type: 'invalid_request_error' } }); }
      if (body.prompt && !body.messages) {
        body.messages = [{ role: 'user', content: body.prompt }];
        delete body.prompt;
      }
      return await handleChatCompletions(body, req, res);
    }

    if (method === 'GET' && (path === '/version' || path === '/api/version')) {
      return jsonResp(res, 200, { version: `wrapper-nvidia-${VERSION}` });
    }
    if (method === 'GET' && path === '/api/tags') {
      // Use buildCatalog (not raw pool.modelsCached) so curated non-chat models
      // (FLUX/SD/Qwen image-gen, fugatto audio, …) appear in Ollama discovery
      // too — consistent with /v1/models. Without this, Ollama clients only saw
      // the 121 chat/embedding/vision models and never the image-gen surface.
      const catalog = buildCatalog(pool.modelsCached, BASE_LLM, BASE_GENAI);
      const models = catalog.map(d => {
        const mid = d.id;
        return {
          name: mid,
          model: mid,
          modified_at: '1970-01-01T00:00:00Z',
          size: 0,
          digest: '',
          details: {
            parent_model: '',
            format: 'gguf',
            family: mid.includes('/') ? mid.split('/')[0] : mid,
            families: [mid.includes('/') ? mid.split('/')[0] : mid],
            parameter_size: '',
            quantization_level: '',
          }
        };
      });
      return jsonResp(res, 200, { models });
    }
    if (method === 'GET' && (path === '/api/v1/models' || path === '/models')) {
      return await handleModels(res, url);
    }
    if (method === 'GET' && (path === '/props' || path === '/v1/props')) {
      return jsonResp(res, 200, { system_prompt: "", default_generation_settings: {}, total_slots: 1 });
    }
    if ((method === 'GET' || method === 'POST') && path === '/api/show') {
      return jsonResp(res, 200, { license: "", modelfile: "", parameters: "", template: "", details: {} });
    }
    if (method === 'GET' && path === '/favicon.ico') {
      res.writeHead(204);
      return res.end();
    }

    // ─ Dashboard Metrics API ──
    if (method === 'GET' && path === '/metrics') {
      const windowStr = url.searchParams.get('window') || '24h';
      const summary = metrics.summary(windowStr);
      const totals = metrics.getTotalCounts();
      return jsonResp(res, 200, { ...summary, ...totals, live_keys: pool.allStats ? pool.allStats() : [] });
    }
    if (method === 'GET' && path === '/metrics/tokens') {
      const windowStr = url.searchParams.get('window') || '24h';
      const s = metrics.summary(windowStr);
      return jsonResp(res, 200, {
        window: windowStr,
        prompt_tokens: s.prompt_tokens,
        completion_tokens: s.completion_tokens,
        cached_tokens: s.cached_tokens,
        total_tokens: s.total_tokens,
        cache_hit_pct: s.cache_hit_pct
      });
    }
    if (method === 'GET' && path === '/metrics/models') {
      const windowStr = url.searchParams.get('window') || '24h';
      return jsonResp(res, 200, {
        window: windowStr,
        models: metrics.getPerModel(windowStr),
        blocked_models: pool.blockedModels ? pool.blockedModels() : {}
      });
    }
    if (method === 'GET' && path === '/metrics/models/timeseries') {
      const model = url.searchParams.get('model') || '';
      const hours = parseInt(url.searchParams.get('hours') || '24', 10);
      return jsonResp(res, 200, {
        model,
        hours,
        data: metrics.getModelTimeseries(model, hours)
      });
    }
    if (method === 'GET' && path === '/metrics/keys') {
      const windowStr = url.searchParams.get('window') || '24h';
      const hist = metrics.getPerKey(windowStr);
      const live = {};
      const stats = pool.allStats ? pool.allStats() : [];
      for (const k of stats) {
        live[k.label] = k;
      }
      const merged = [];
      const seen = new Set();
      for (const h of hist) {
        const label = h.key_label || 'unknown';
        merged.push({ ...h, live: live[label] || {} });
        seen.add(label);
      }
      for (const [label, live_data] of Object.entries(live)) {
        if (!seen.has(label)) {
          merged.push({
            key_label: label,
            requests: 0,
            total_tokens: 0,
            avg_latency_ms: 0,
            rate_limited_count: 0,
            total_retries: 0,
            live: live_data
          });
        }
      }
      return jsonResp(res, 200, { window: windowStr, keys: merged });
    }
    if (method === 'GET' && path === '/metrics/activity') {
      const limit = parseInt(url.searchParams.get('limit') || '50', 10);
      const offset = parseInt(url.searchParams.get('offset') || '0', 10);
      const rows = metrics.recentRequests(limit, offset);
      return jsonResp(res, 200, { limit, offset, count: rows.length, rows });
    }
    if (method === 'GET' && path === '/metrics/rate-limits') {
      const limit = parseInt(url.searchParams.get('limit') || '100', 10);
      const windowStr = url.searchParams.get('window') || '24h';
      const events = metrics.rateLimitEvents(limit);
      const summary = metrics.rateLimitSummary(windowStr);
      const full = metrics.summary(windowStr);
      return jsonResp(res, 200, {
        events,
        summary,
        blocked_models: pool.blockedModels ? pool.blockedModels() : {},
        learned_model_limits: pool.summary ? (pool.summary().learned_model_limits || {}) : {},
        pacing: {
          paced_requests: full.paced_requests || 0,
          total_pacing_ms: full.total_pacing_ms || 0
        },
        live_keys: pool.allStats ? pool.allStats() : []
      });
    }
    if (method === 'POST' && path === '/metrics/reset') {
      const removed = metrics.resetAll();
      if (pool.resetCounters) pool.resetCounters();
      return jsonResp(res, 200, { status: 'ok', reset: removed });
    }
    if (method === 'GET' && path === '/v1/capabilities') {
      const modelId = url.searchParams.get('model') || '';
      if (modelId) {
        const adHoc = !pool.modelsCached.includes(modelId) && !CURATED_GENAI.includes(modelId);
        const d = describe(modelId, BASE_LLM, BASE_GENAI);
        if (adHoc) d.source = 'heuristic-adhoc';
        // Enrich with the SAME metadata /v1/models exposes (context_window,
        // supports_*, max_output_tokens, …) so /v1/capabilities and /v1/models
        // stay bit-consistent. Without this, clients querying
        // /v1/capabilities?model=X saw `max_output_tokens: null` /
        // `supports_function_calling: null` while /v1/models showed real values
        // — the §8 "metadata only in one surface" bug class.
        return jsonResp(res, 200, enrichModelMetadata(modelId, d));
      }
      const catalog = buildCatalog(pool.modelsCached, BASE_LLM, BASE_GENAI);
      const enriched = catalog.map(d => enrichModelMetadata(d.id, d));
      return jsonResp(res, 200, {
        object: 'list', models: enriched,
        summary: summarize(catalog),
        hosts: { llm: BASE_LLM, genai: BASE_GENAI, nvcf: BASE_NVCF }
      });
    }
    if (method === 'GET' && path === '/v1/capabilities/params') {
      const modelId = url.searchParams.get('model') || '';
      const capability = url.searchParams.get('capability') || '';
      if (modelId) {
        const d = classify(modelId);
        return jsonResp(res, 200, { model: modelId, type: d.type, supported_params: d.supported_params || {} });
      }
      if (capability) {
        return jsonResp(res, 200, { type: capability, supported_params: getCapabilityParams(capability) });
      }
      return jsonResp(res, 200, CAPABILITY_PARAMS);
    }
    if (method === 'GET' && path === '/metrics/model-status') {
      const status = metrics.getModelStatus ? metrics.getModelStatus() : {};
      const unavailable = Array.from(unavailableModels);
      return jsonResp(res, 200, {
        unavailable, unavailable_count: unavailable.length,
        verified_count: Object.values(status).filter(s => s.ok).length,
        checked: Object.keys(status).length,
        learned_model_limits: pool.summary ? (pool.summary().learned_model_limits || {}) : {}
      });
    }
    if (method === 'POST' && path === '/admin/heal-in-flight') {
      if (pool.healInFlight) pool.healInFlight();
      return jsonResp(res, 200, { status: 'ok', message: 'in_flight counters healed' });
    }
    if (method === 'GET' && path === '/metrics/chart/hourly') {
      const hours = parseInt(url.searchParams.get('hours') || '24', 10);
      return jsonResp(res, 200, { hours, data: metrics.getHourlyChart(hours) });
    }
    if (method === 'GET' && path === '/metrics/chart/daily') {
      const days = parseInt(url.searchParams.get('days') || '30', 10);
      return jsonResp(res, 200, { days, data: metrics.getDailyChart(days) });
    }

    // ─ Chat Completions ──
    if (method === 'POST' && path === '/v1/chat/completions') {
      const raw = await readBody(req);
      let body;
      try { body = JSON.parse(raw); } catch (e) {
        console.error('[JSON PARSE ERROR] completions raw:', JSON.stringify(raw).slice(0, 1000), 'err:', e.message);
        return jsonResp(res, 400, { error: { message: 'Invalid JSON: ' + e.message, type: 'invalid_request_error' } });
      }
      // handleChatCompletions() calls resolveTargetModel(body.model) itself,
      // so we just pass the body through. The previous block re-resolved the
      // model here AND in handleChatCompletions (double work) and left a
      // dangling `requestedModel` local; both are removed.
      return await handleChatCompletions(body, req, res);
    }

    // ─ OpenAI Responses API (codex >=0.144 wire_api="responses") ──
    if (method === 'POST' && path === '/v1/responses') {
      const raw = await readBody(req);
      // handleResponsesApi writes the response itself (SSE/JSON) or returns an
      // error object; jsonResp is only used for the parse/dispatch failures.
      const out = await responsesHandler.handleResponsesApi(req, res, raw);
      if (out && out.error) {
        const status = out.error.type === 'invalid_request_error' ? 400 : 502;
        return jsonResp(res, status, { error: out.error });
      }
      return;
    }

    // ─ Anthropic Messages ──
    if (method === 'POST' && path === '/v1/messages') {
      const raw = await readBody(req);
      return await handleAnthropicMessages(raw, req, res);
    }

    // ─ Anthropic Messages Token Count ──
    if (method === 'POST' && path === '/v1/messages/count_tokens') {
      const raw = await readBody(req);
      let body;
      try { body = JSON.parse(raw); } catch {
        return jsonResp(res, 400, { error: { message: 'Invalid JSON', type: 'invalid_request_error' } });
      }
      const count = estimateInputTokens(body);
      return jsonResp(res, 200, { input_tokens: count });
    }

    // ─ Embeddings ──
    if (method === 'POST' && path === '/v1/embeddings') {
      const raw = await readBody(req);
      let body;
      try { body = JSON.parse(raw); } catch {
        return jsonResp(res, 400, { error: { message: 'Invalid JSON', type: 'invalid_request_error' } });
      }
      if (!body.input_type) {
        if (typeof body.input === 'string') {
          body.input_type = 'query';
        } else if (Array.isArray(body.input)) {
          console.log('[embeddings] Auto-lengkapi input_type untuk array input');
        }
      }

      const modelId = resolveTargetModel(body.model || '');
      body.model = modelId;
      if (isModelUnavailable(modelId)) {
        return jsonResp(res, 404, { error: { message: `Model ${modelId} is retired or unavailable`, type: 'invalid_request_error' } });
      }

      return await proxyPost({
        req, res, body, rawBody: raw, modelId, path: '/v1/embeddings',
        getTargetUrl: (key) => {
          const baseUrl = resolveBase(modelId);
          const ep = (describe(modelId, BASE_LLM, BASE_GENAI).endpoints || [{}])[0];
          return ep.base_url ? `${ep.base_url}${ep.path || '/v1/embeddings'}` : `${baseUrl}/v1/embeddings`;
        }
      });
    }

// ─ Reranking ──
if (method === "POST" && path === "/v1/ranking") {
  const raw = await readBody(req);
  let body;
  try { body = JSON.parse(raw); } catch {
    return jsonResp(res, 400, { error: { message: "Invalid JSON", type: "invalid_request_error" } });
  }
  const modelId = resolveTargetModel(body.model || "");
  body.model = modelId;
  if (isModelUnavailable(modelId)) {
    return jsonResp(res, 404, { error: { message: `Model ${modelId} is retired or unavailable`, type: "invalid_request_error" } });
  }
  return await proxyPost({
    req, res, body, rawBody: raw, modelId, path: "/v1/ranking",
    getTargetUrl: (key) => {
      const baseUrl = resolveBase(modelId);
      return `${baseUrl}/v1/ranking`;
    }
  });
}

    // ─ Images Generations ──
    if (method === 'POST' && (path === '/v1/images/generations' || path === '/v1/infer')) {
      const raw = await readBody(req);
      let body;
      try { body = JSON.parse(raw); } catch {
        return jsonResp(res, 400, { error: { message: 'Invalid JSON', type: 'invalid_request_error' } });
      }

      // Image-gen models live on BASE_GENAI (ai.api.nvidia.com) and are NOT in
      // the chat catalog (pool.modelsCached), so resolveTargetModel() would
      // wrongly remap e.g. flux.1-dev → a chat model. Validate against the
      // curated genai list + live cache + heuristic classification instead,
      // and pass the model through verbatim to upstream.
      const requestedModel = body.model || '';
      const modelId = requestedModel;
      const knownImageModel =
        CURATED_GENAI.includes(modelId) ||
        pool.modelsCached.includes(modelId) ||
        classify(modelId).type === 'image';
      if (!modelId || !knownImageModel || isModelUnavailable(modelId)) {
        return jsonResp(res, 404, { error: { message: `Image model ${modelId || '(missing)'} is not available — must be a known NIM Visual GenAI model (e.g. black-forest-labs/flux.1-dev)`, type: 'invalid_request_error' } });
      }

      const nativeBody = { ...body };
      delete nativeBody.model;
      delete nativeBody.n;
      delete nativeBody.size;
      delete nativeBody.response_format;
      delete nativeBody.user;
      delete nativeBody.width;
      delete nativeBody.height;

      const isStability = modelId.toLowerCase().includes('stable-diffusion') || modelId.toLowerCase().includes('sdxl') || modelId.toLowerCase().includes('playground') || modelId.toLowerCase().includes('kandinsky');
      if (isStability) {
        nativeBody.text_prompts = [{ text: body.prompt || '', weight: 1 }];
        delete nativeBody.prompt;
      }

      return await proxyPost({
        req, res, body: nativeBody, rawBody: raw, modelId, path,
        getTargetUrl: () => `${BASE_GENAI}/v1/genai/${modelId}`,
      });
    }

    // ─ Images Edits ──
    if (method === 'POST' && path === '/v1/images/edits') {
      const raw = await readBody(req);
      let body;
      try { body = JSON.parse(raw); } catch {
        return jsonResp(res, 400, { error: { message: 'Invalid JSON', type: 'invalid_request_error' } });
      }

      const requestedModel = body.model || '';
      const modelId = requestedModel;
      const knownImageModel =
        CURATED_GENAI.includes(modelId) ||
        pool.modelsCached.includes(modelId) ||
        classify(modelId).type === 'image';
      if (!modelId || !knownImageModel || isModelUnavailable(modelId)) {
        return jsonResp(res, 404, { error: { message: `Image model ${modelId || '(missing)'} is not available`, type: 'invalid_request_error' } });
      }

      const nativeBody = { ...body };
      delete nativeBody.model;
      delete nativeBody.n;
      delete nativeBody.size;
      delete nativeBody.response_format;
      delete nativeBody.user;

      const isStability = modelId.toLowerCase().includes('stable-diffusion') || modelId.toLowerCase().includes('sdxl');
      if (isStability) {
        nativeBody.text_prompts = [{ text: body.prompt || '', weight: 1 }];
        delete nativeBody.prompt;
        if (body.image) {
          nativeBody.init_image = body.image;
          delete nativeBody.image;
        }
      }

      return await proxyPost({
        req, res, body: nativeBody, rawBody: raw, modelId, path,
        getTargetUrl: () => `${BASE_GENAI}/v1/genai/${modelId}`
      });
    }

    // ─ Ollama /api/chat — convert to OpenAI format ──
    if (method === 'POST' && path === '/api/chat') {
      const raw = await readBody(req);
      let oBody;
      try { oBody = JSON.parse(raw); } catch {
        return jsonResp(res, 400, { error: 'Invalid JSON' });
      }

      if (!oBody.model) return jsonResp(res, 400, { error: 'model is required' });
      oBody.model = resolveTargetModel(oBody.model);
      if (isModelUnavailable(oBody.model)) {
        return jsonResp(res, 404, { error: { message: `Model ${oBody.model} is retired or unavailable`, type: 'invalid_request_error' } });
      }

      // Convert Ollama options → OpenAI params
      const opts = oBody.options || {};
      const chatBody = {
        model: oBody.model,
        messages: oBody.messages || [],
        stream: oBody.stream === true,
        max_tokens: opts.num_predict || opts.max_tokens,
        temperature: opts.temperature,
        top_p: opts.top_p,
        top_k: opts.top_k,
        seed: opts.seed,
        stop: opts.stop,
        frequency_penalty: opts.frequency_penalty,
        presence_penalty: opts.presence_penalty,
      };
      for (const k of Object.keys(chatBody)) {
        if (chatBody[k] === undefined) delete chatBody[k];
      }

      const result = await proxyOpenai(chatBody, forwardHeaders(req), oBody.model, req, '/api/chat');

      if (result.stream) {
        res.writeHead(200, {
          'Content-Type': 'application/x-ndjson',
          'Cache-Control': 'no-cache',
          'Connection': 'keep-alive',
          'X-Accel-Buffering': 'no',
        });
        let ttftMs = 0;
        let isFirstRead = true;
        let reader = null;
        try {
          reader = result.stream.getReader();
          const decoder = new TextDecoder();
          let buffer = '';
          let fullContent = '';
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            if (isFirstRead) {
              ttftMs = Date.now() - result.startMs;
              isFirstRead = false;
            }
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';
            for (const line of lines) {
              const trimmed = line.trim();
              if (!trimmed || trimmed === 'data: [DONE]') continue;
              if (!trimmed.startsWith('data: ')) continue;
              try {
                const parsed = JSON.parse(trimmed.slice(6));
                const delta = parsed.choices?.[0]?.delta?.content || '';
                if (delta) fullContent += delta;
                const ollamaChunk = {
                  model: oBody.model,
                  created_at: new Date().toISOString(),
                  message: { role: 'assistant', content: delta },
                  done: false,
                };
                res.write(JSON.stringify(ollamaChunk) + '\n');
              } catch {}
            }
          }
          const finalChunk = {
            model: oBody.model,
            created_at: new Date().toISOString(),
            message: { role: 'assistant', content: '' },
            done: true,
            done_reason: 'stop',
            total_duration: (Date.now() - startTime) * 1e6,
            prompt_eval_count: 0,
            eval_count: fullContent.length,
          };
          res.write(JSON.stringify(finalChunk) + '\n');
        } catch (e) {
          console.error('[stream error] /api/chat:', e.message);
          try { res.end(); } catch {}
        } finally {
          // C3: cancel the upstream reader on disconnect so the undici socket
          // is returned to the pool instead of lingering until GC.
          if (reader) { try { await reader.cancel(); } catch {} }
          // FIX B3: Extract token usage from the last usage chunk in the buffer
          let lastUsage = null;
          try {
            const lines = buffer.split('\n');
            for (let i = lines.length - 1; i >= 0; i--) {
              const t = lines[i].trim();
              if (t.startsWith('data:') && t !== 'data: [DONE]' && t.includes('"usage"')) {
                const parsed = JSON.parse(t.slice(6));
                if (parsed && parsed.usage) { lastUsage = parsed.usage; break; }
              }
            }
          } catch {}
          const { pt, ct, tt, cacht } = extractUsageFields(lastUsage);
          metrics.recordRequest({
            method: 'POST', path: '/api/chat',
            model: oBody.model, keyLabel: result.key.label,
            streaming: true, statusCode: 200,
            latencyMs: Date.now() - result.startMs,
            ttftMs, promptTokens: pt, completionTokens: ct, cachedTokens: cacht, totalTokens: tt,
            wasRateLimited: false, pacingMs: result.pacingMs || 0,
          });
          // C2: cleanup MUST be in finally so a throw from res.end() (already
          // ended via res.on('close') on client disconnect) cannot leak
          // key.inFlight + global inFlight.
          pool.releaseSuccess(result.key);
          decInFlight();
        }
        return;
      }

      if (result.status === 200 && result.data) {
        const choice = result.data.choices?.[0];
        const content = choice?.message?.content || '';
        const usage = result.data.usage || {};
        const { pt, ct, tt, cacht } = extractUsageFields(usage);
        metrics.recordRequest({
          method: 'POST', path: '/api/chat',
          model: oBody.model, keyLabel: result.key.label,
          streaming: false, statusCode: 200,
          latencyMs: Date.now() - result.startMs,
          promptTokens: pt, completionTokens: ct, cachedTokens: cacht, totalTokens: tt,
          wasRateLimited: false, pacingMs: result.pacingMs || 0,
        });
        const ollamaResp = {
          model: oBody.model,
          created_at: new Date().toISOString(),
          message: { role: 'assistant', content },
          done: true,
          done_reason: 'stop',
          total_duration: (Date.now() - startTime) * 1e6,
          load_duration: 1,
          prompt_eval_count: usage.prompt_tokens || 0,
          eval_count: usage.completion_tokens || 0,
        };
        return jsonResp(res, 200, ollamaResp, result.key?.label);
      }

      return jsonResp(res, result.status, result.data, result.key?.label, result.retryAfter ? { "Retry-After": result.retryAfter } : undefined);
    }

    // ─ Ollama /api/generate — convert to OpenAI format ──
    if (method === 'POST' && path === '/api/generate') {
      const raw = await readBody(req);
      let oBody;
      try { oBody = JSON.parse(raw); } catch {
        return jsonResp(res, 400, { error: 'Invalid JSON' });
      }

      if (!oBody.model) return jsonResp(res, 400, { error: 'model is required' });
      oBody.model = resolveTargetModel(oBody.model);
      if (isModelUnavailable(oBody.model)) {
        return jsonResp(res, 404, { error: { message: `Model ${oBody.model} is retired or unavailable`, type: 'invalid_request_error' } });
      }

      // Convert Ollama generate → OpenAI chat format
      const opts = oBody.options || {};
      const chatBody = {
        model: oBody.model,
        messages: [{ role: 'user', content: oBody.prompt || '' }],
        stream: oBody.stream === true,
        max_tokens: opts.num_predict || opts.max_tokens,
        temperature: opts.temperature,
        top_p: opts.top_p,
        top_k: opts.top_k,
        seed: opts.seed,
        stop: opts.stop,
        frequency_penalty: opts.frequency_penalty,
        presence_penalty: opts.presence_penalty,
      };
      for (const k of Object.keys(chatBody)) {
        if (chatBody[k] === undefined) delete chatBody[k];
      }

      // Append context messages if provided (Ollama generate supports context)
      if (oBody.context && Array.isArray(oBody.context) && oBody.context.length > 0) {
        chatBody.messages.unshift({ role: 'system', content: 'Previous context tokens: ' + JSON.stringify(oBody.context) });
      }

      const result = await proxyOpenai(chatBody, forwardHeaders(req), oBody.model, req, '/api/generate');

      if (result.stream) {
        res.writeHead(200, {
          'Content-Type': 'application/x-ndjson',
          'Cache-Control': 'no-cache',
          'Connection': 'keep-alive',
          'X-Accel-Buffering': 'no',
        });
        let ttftMs = 0;
        let isFirstRead = true;
        let reader = null;
        try {
          reader = result.stream.getReader();
          const decoder = new TextDecoder();
          let buffer = '';
          let fullContent = '';
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            if (isFirstRead) {
              ttftMs = Date.now() - result.startMs;
              isFirstRead = false;
            }
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';
            for (const line of lines) {
              const trimmed = line.trim();
              if (!trimmed || trimmed === 'data: [DONE]') continue;
              if (!trimmed.startsWith('data: ')) continue;
              try {
                const parsed = JSON.parse(trimmed.slice(6));
                const delta = parsed.choices?.[0]?.delta?.content || '';
                if (delta) fullContent += delta;
                const ollamaChunk = {
                  model: oBody.model,
                  created_at: new Date().toISOString(),
                  response: delta,
                  done: false,
                };
                res.write(JSON.stringify(ollamaChunk) + '\n');
              } catch {}
            }
          }
          const finalChunk = {
            model: oBody.model,
            created_at: new Date().toISOString(),
            response: '',
            done: true,
            done_reason: 'stop',
            context: [],
            total_duration: (Date.now() - startTime) * 1e6,
            prompt_eval_count: 0,
            eval_count: fullContent.length,
          };
          res.write(JSON.stringify(finalChunk) + '\n');
        } catch (e) {
          console.error('[stream error] /api/generate:', e.message);
          try { res.end(); } catch {}
        } finally {
          // C3: cancel the upstream reader on disconnect so the undici socket
          // is returned to the pool instead of lingering until GC.
          if (reader) { try { await reader.cancel(); } catch {} }
          // FIX B5: Extract token usage from the last usage chunk in the buffer
          let lastUsage = null;
          try {
            const lines = buffer.split('\n');
            for (let i = lines.length - 1; i >= 0; i--) {
              const t = lines[i].trim();
              if (t.startsWith('data:') && t !== 'data: [DONE]' && t.includes('"usage"')) {
                const parsed = JSON.parse(t.slice(6));
                if (parsed && parsed.usage) { lastUsage = parsed.usage; break; }
              }
            }
          } catch {}
          const { pt, ct, tt, cacht } = extractUsageFields(lastUsage);
          metrics.recordRequest({
            method: 'POST', path: '/api/generate',
            model: oBody.model, keyLabel: result.key.label,
            streaming: true, statusCode: 200,
            latencyMs: Date.now() - result.startMs,
            ttftMs, promptTokens: pt, completionTokens: ct, cachedTokens: cacht, totalTokens: tt,
            wasRateLimited: false, pacingMs: result.pacingMs || 0,
          });
          // C2: cleanup MUST be in finally so a throw from res.end() (already
          // ended via res.on('close') on client disconnect) cannot leak
          // key.inFlight + global inFlight.
          pool.releaseSuccess(result.key);
          decInFlight();
        }
        return;
      }

      if (result.status === 200 && result.data) {
        const content = result.data.choices?.[0]?.message?.content || '';
        const usage = result.data.usage || {};
        const { pt, ct, tt, cacht } = extractUsageFields(usage);

        metrics.recordRequest({
          method: 'POST', path: '/api/generate',
          model: oBody.model, keyLabel: result.key.label,
          streaming: false, statusCode: 200,
          latencyMs: Date.now() - result.startMs,
          promptTokens: pt, completionTokens: ct, cachedTokens: cacht, totalTokens: tt,
          wasRateLimited: false, pacingMs: result.pacingMs || 0,
        });
        const ollamaResp = {
          model: oBody.model,
          created_at: new Date().toISOString(),
          response: content,
          done: true,
          done_reason: 'stop',
          context: [],
          total_duration: (Date.now() - startTime) * 1e6,
          load_duration: 1,
          prompt_eval_count: usage.prompt_tokens || 0,
          eval_count: usage.completion_tokens || 0,
        };
        return jsonResp(res, 200, ollamaResp, result.key?.label);
      }

      return jsonResp(res, result.status, result.data, result.key?.label, result.retryAfter ? { "Retry-After": result.retryAfter } : undefined);
    }

    // ─ Fallback to Catch-all Proxy ──
    return await handleCatchAll(req, res, path, url);
  } catch (e) {
    const duration = Date.now() - startTime;
    if (e.message === 'Request entity too large') {
      console.warn(`[${requestId}] ${method} ${path} 413 ${duration}ms`);
      return jsonResp(res, 413, { error: { message: 'Request entity too large', type: 'invalid_request_error' } });
    }
    // Safety net: if inFlight appears stuck from a leaked increment, clamp it
    const MAX_QUEUE_SIZE = parseInt(process.env.MAX_QUEUE_SIZE || '100', 10);
    const MAX_SANITY_INFLIGHT = Math.max(MAX_QUEUE_SIZE * 2, 500);
    if (inFlight > MAX_SANITY_INFLIGHT) {
      console.warn(`[${requestId}] inFlight counter stuck at ${inFlight}, clamping to 0`);
      inFlight = 0;
    }
    console.error(`[${requestId}] ${method} ${path} 500 ${duration}ms: ${e.message}`, e.stack);
    // Route-aware envelope: Anthropic Messages API wants
    // {"type":"error","error":{type:"api_error",message}}, OpenAI wants
    // {"error":{message,type:"server_error"}}.
    if (path === '/v1/messages' || path.startsWith('/v1/messages/')) {
      try { jsonResp(res, 500, { type: 'error', error: { type: 'api_error', message: 'Internal server error' } }); } catch {}
    } else {
      try { jsonResp(res, 500, { error: { message: 'Internal server error', type: 'server_error' } }); } catch {}
    }
  } finally {
    clearTimeout(preRespTimer);
    const duration = Date.now() - startTime;
    if (!res.writableEnded) {
      console.log(`[${requestId}] ${method} ${path} completed in ${duration}ms`);
    }
  }
}

// B8 FIX: Documented why this reads the .env file directly instead of using
// process.env like pool.loadFromEnv(). The background key reload needs to
// detect key ADDITIONS and REMOVALS without resetting per-key state (timestamps,
// blocks, inFlight). pool.syncKeys() diffs old vs new and only adds/removes
// changed keys. Reading the file directly ensures we see the raw configured
// keys even if dotenv hasn't reloaded yet (though reloadDotenv runs first).
// The parsing logic mirrors pool.loadFromEnv() — same key prefix matching,
// same validation (nvapi- prefix, length >= 10), same dedup.
function loadConfigFromEnvFile() {
  const fs = require('fs');
  const envPath = path.join(WRAPPER_DIR, '.env');
  const config = {
    keys: [],
    softLimit: undefined,
    hardLimit: undefined,
    queueLimit: undefined,
    maxQueueSize: undefined
  };
  if (!fs.existsSync(envPath)) {
    console.warn(`[wrapper-nvidia] .env file not found at ${envPath}`);
    return config;
  }
  try {
    const content = fs.readFileSync(envPath, 'utf8');
    const lines = content.split(/\r?\n/);
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#')) continue;
      const idx = trimmed.indexOf('=');
      if (idx === -1) continue;
      const key = trimmed.slice(0, idx).trim();
      let val = trimmed.slice(idx + 1).trim();
      // Strip surrounding quotes (single or double)
      if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
        val = val.slice(1, -1);
      }
      // Only strip inline comments when not inside quotes (API keys won't have #)
      const hashIdx = val.indexOf('#');
      if (hashIdx > 0) val = val.slice(0, hashIdx).trim();
      if (!key || !val) continue;

      if (key.startsWith('NVIDIA_API_KEY')) {
        // Match pool.loadFromEnv() validation: only check minimum length.
        // Do NOT require 'nvapi-' prefix — NVIDIA API keys can have different
        // formats and the prefix check would reject valid keys on reload that
        // were accepted on initial load (inconsistency bug C2).
        if (val.length >= 10) {
          config.keys.push(val);
        } else if (val.length > 0) {
          console.warn(`[wrapper-nvidia] Ignoring invalid or placeholder key in .env: ${key}`);
        }
      } else if (key === 'SOFT_LIMIT_RPM') {
        config.softLimit = parseInt(val, 10);
      } else if (key === 'HARD_LIMIT_RPM') {
        config.hardLimit = parseInt(val, 10);
      } else if (key === 'QUEUE_LIMIT_PER_KEY_PER_SEC' || key === 'QUEUE_LIMIT') {
        config.queueLimit = parseFloat(val);
      } else if (key === 'MAX_QUEUE_SIZE') {
        config.maxQueueSize = parseInt(val, 10);
      }
    }
    const seen = new Set();
    const deduped = [];
    for (const k of config.keys) {
      if (!seen.has(k)) {
        seen.add(k);
        deduped.push(k);
      }
    }
    config.keys = deduped;
    return config;
  } catch (e) {
    console.error(`[wrapper-nvidia] Error reading .env file for config reload:`, e.message);
    return config;
  }
}

function startKeyReload() {
  const keysReloadSec = parseInt(process.env.KEYS_RELOAD_SECONDS || '60', 10);
  if (keysReloadSec <= 0) return;

  setInterval(async () => {
    try {
      const config = loadConfigFromEnvFile();
      if (config.keys.length > 0) {
        await pool.syncKeys(config.keys);
      }
      await pool.syncLimits({
        soft: config.softLimit,
        hard: config.hardLimit,
        queueLimit: config.queueLimit,
        maxQueueSize: config.maxQueueSize
      });
    } catch (e) {
      console.error(`[wrapper-nvidia] Background config reload error:`, e.message);
    }
  }, keysReloadSec * 1000).unref();
}

// ── Start ───────────────────────────────────────────────────────────────
async function main() {
  pool.loadFromEnv();
  startKeyReload();

  // Alias config (Claude Code haiku/sonnet/opus + custom) — must load before
  // any request is routed.
  loadAliasConfig();

  // Dynamic NGC-synced authoritative context registry.
  registry.setExternalAgent(agent);
  await registry.refresh(true);
  registry.start();

  const dbPath = process.env.METRICS_DB || DB_PATH;
  metrics = new Metrics(dbPath);
  await metrics.ready();

  // Wire real-time push
  metrics.onRequest((req) => {
    broadcastSSE('activity', req);
  });
  metrics.onRateLimit((ev) => {
    broadcastSSE('rate-limit', ev);
  });

  await pool.refreshModels();
  refreshDiscoveryMap(pool.modelsCached);

  initUpstreamRoutes();

  await loadUnavailableModelsFromDb();

  metrics.prune(30);

  serverInstance = http.createServer(handleRequest);
  serverInstance.on('error', (err) => {
    // Any listen failure must terminate loudly instead of hanging silently.
    // The callback only fires on success, so an unhandled error (EADDRINUSE,
    // EACCES/EPERM, EINVAL) would otherwise leave the process alive but not
    // bound to any port -- every client gets ECONNREFUSED with no log/exit.
    console.error(`[FATAL] listen() error on ${BIND_HOST}:${LISTEN_PORT}: ${err.code || err.message}`);
    console.error(err.stack || err);
    process.exit(1);
  });
  // Server-level socket timeouts. Critical: must be >= TTFT_TIMEOUT_MS + a
  // safety margin, otherwise upstream hangs longer than that would be killed
  // at the HTTP layer and re-classified as 499 client-disconnect with
  // huge latency (visible as red rows in the dashboard). 110000ms default
  // TTFT + 30s slack → 140000ms timeout.
  const ttftMs = parseInt(process.env.TTFT_TIMEOUT_MS || '110000', 10);
  const antiSilence = parseInt(process.env.ANTI_SILENCE_TIMEOUT_MS || process.env.SERVER_REQUEST_TIMEOUT_MS || String(Math.max(ttftMs + 30000, 60000)), 10);
  serverInstance.timeout = antiSilence;
  // Default 65000 matches .env.example — 10s was dangerously low and would kill
// healthy streaming connections (reasoning models think silently for minutes).
serverInstance.keepAliveTimeout = parseInt(process.env.SERVER_KEEPALIVE_TIMEOUT_MS || '65000', 10);
  serverInstance.maxHeadersCount = 100;
  serverInstance.headersTimeout = parseInt(process.env.SERVER_HEADERS_TIMEOUT_MS || '15000', 10);

  serverInstance.listen(LISTEN_PORT, BIND_HOST, () => {
    console.log(`[wrapper-nvidia] v${VERSION} listening on ${BIND_HOST}:${LISTEN_PORT}`);
    console.log(`[wrapper-nvidia] Keys: ${pool.totalKeys} total, ${pool.availableKeys} available`);
    console.log(`[wrapper-nvidia] Models cached: ${pool.modelsCached.length}`);
    console.log(`[wrapper-nvidia] Upstream: LLM=${BASE_LLM}`);
    console.log(`[wrapper-nvidia] Metrics DB: ${dbPath}`);
  });

  // FIX P0: multi-port bind so every client config is satisfied without
  // editing client configs (which live outside this repo):
  //   - Hermes ILMA profile: base_url = http://127.0.0.1:9100 (main) AND
  //     custom_providers[wrapper-nvidia].base_url = http://127.0.0.1:9100
  //   - Claude Code settings: ANTHROPIC_BASE_URL = http://localhost:9100
  //     and CLADUE_CODE_GATEWAY_MODEL_DISCOVERY_URL = http://localhost:9910/v1/models
  // Previously this wrapper bound only LISTEN_PORT (9910 via .env + service), so
  // every 9100-targeting client got ECONNREFUSED. We keep LISTEN_PORT as the
  // primary and additionally bind ANY_ALSO_PORTS (comma-separated) on the SAME
  // http.Server/handler, so one OS process serves all expected addresses.
  const extraPorts = (process.env.ANY_ALSO_PORTS || '')
    .split(',').map(x => parseInt((x || '').trim(), 10))
    .filter(p => Number.isInteger(p) && p > 0 && p !== LISTEN_PORT)
    // de-dup
    .filter((p, i, arr) => arr.indexOf(p) === i);
  for (const p of extraPorts) {
    // A single http.Server can call .listen() multiple times; Node adds an
    // extra handle per call while reusing the same request handler.
    serverInstance.listen(p, BIND_HOST, () => {
      console.log(`[wrapper-nvidia] also listening on ${BIND_HOST}:${p}`);
    });
  }

  pool.startModelRefresh();

  // Keep the gateway discovery reverse-map in sync with the live catalog.
  const modelRefreshSec = parseInt(process.env.MODEL_REFRESH_SEC || '600', 10);
  setInterval(() => { try { refreshDiscoveryMap(pool.modelsCached); } catch {} }, modelRefreshSec * 1000).unref();

  if (VERIFY_ON_BOOT) {
    verifyLoop();
  }

  setInterval(() => metrics.prune(30), 6 * 3600 * 1000).unref();
  setInterval(() => pool.healInFlight(), 60000).unref();

  const shutdown = () => {
    console.log('[wrapper-nvidia] Shutting down...');
    // Stop accepting new connections
    serverInstance.close(() => {
      console.log('[wrapper-nvidia] Server closed, draining remaining requests...');
      // Give in-flight requests a chance to complete
      let drainAttempts = 0;
      const drainInterval = setInterval(() => {
        const remaining = inFlight;
        if (remaining <= 0 || drainAttempts >= 10) {
          clearInterval(drainInterval);
          metrics.close();
          process.exit(0);
        }
        console.log(`[wrapper-nvidia] Draining: ${remaining} in-flight requests...`);
        drainAttempts++;
      }, 1000);
    });
    // Force shutdown after timeout
    setTimeout(() => {
      console.log('[wrapper-nvidia] Force shutdown after timeout');
      metrics.close();
      process.exit(0);
    }, 15000);
  };
  process.on('SIGTERM', () => { console.log('[signal] Received SIGTERM'); shutdown(); });
  process.on('SIGINT',  () => { console.log('[signal] Received SIGINT'); shutdown(); });
  process.on('SIGHUP',  () => { console.log('[signal] Received SIGHUP (ignoring)'); });
  process.on('uncaughtException', (err) => {
    console.error('[FATAL] uncaughtException:', err.message, err.stack);
  });
  process.on('unhandledRejection', (reason) => {
    console.error('[FATAL] unhandledRejection:', reason);
  });
}

// INTEGRATION HOOK (audit only): under NODE_ENV=test_integration we let main()
// run for real (with listen neutralized by the harness + undici intercepted) so
// routing/auth/translation exercise the production code paths, then expose a
// `ready` promise and `handleRequest`. No behavior change in normal runs.
if (process.env.NODE_ENV === 'test_integration') {
  const _ready = main()
    .then(() => ({ handleRequest, metrics, pool, registry }))
    .catch(e => { console.error(`[wrapper-nvidia][test_integration] init error: ${e.message}`); throw e; });
  module.exports = { handleRequest, ready: _ready, translateThinkingToNim };
} else {
  main().catch(e => {
    console.error(`[wrapper-nvidia] Fatal: ${e.message}`);
    process.exit(1);
  });
}
