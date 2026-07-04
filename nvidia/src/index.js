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
function anySignal(signals) {
  const controller = new AbortController();
  for (const signal of signals) {
    if (signal.aborted) { controller.abort(); break; }
    signal.addEventListener('abort', () => controller.abort(), { once: true });
  }
  return controller.signal;
}

// Canonical wrapper dir
const WRAPPER_DIR = path.resolve(__dirname, '..');

const { KeyPool, NVIDIA_BASE_URL, NVIDIA_GENAI_URL, NVIDIA_NVCF_URL } = require('../key_pool');
const { anthropicToOpenai, openaiToAnthropic, streamOpenaiToAnthropic, estimateInputTokens, anthropicError } = require('./anthropic_compat');
const { classify, describe, buildCatalog, summarize, CAPABILITY_PARAMS, RETIRED_MODELS, CURATED_GENAI, getCapabilityParams } = require('./capabilities');
const { Metrics } = require('./metrics');

// ── Config ──────────────────────────────────────────────────────────────
const LISTEN_PORT = parseInt(process.env.LISTEN_PORT || '9100', 10);
const BIND_HOST   = process.env.LISTEN_HOST || '0.0.0.0';
const BASE_LLM    = (process.env.NVIDIA_BASE_URL || NVIDIA_BASE_URL).replace(/\/+$/, '');
const BASE_GENAI  = (process.env.NVIDIA_GENAI_URL || NVIDIA_GENAI_URL).replace(/\/+$/, '');
const BASE_NVCF   = (process.env.NVIDIA_NVCF_URL || NVIDIA_NVCF_URL).replace(/\/+$/, '');
const DB_PATH     = process.env.METRICS_DB || path.join(WRAPPER_DIR, 'metrics.db');
const QUIET_RETRIED_429 = parseInt(process.env.QUIET_RETRIED_429 || '3', 10);
const MAX_RETRIES = QUIET_RETRIED_429;
const VERSION     = '8.6.0-node'; // bumped 2026-07-05 forensic audit (REVISI): ReferenceError landmines + image-gen/ranking passthrough + /v1/models curated
const DEFAULT_CONTEXT_WINDOW = parseInt(process.env.DEFAULT_CONTEXT_WINDOW || '131072', 10);

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
let metrics;                      // initialized in main() after dotenv sets METRICS_DB
const MAX_CONNECTIONS = parseInt(process.env.MAX_CONNECTIONS || '200', 10);
const agent   = new Agent({ connections: MAX_CONNECTIONS, pipelining: 10 });
pool.setExternalAgent(agent);
let inFlight  = 0;

// ── SSE Real-time Push ────────────────────────────────
const sseClients = new Set();

function broadcastSSE(event, data) {
  const msg = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  for (const client of sseClients) {
    try {
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

// Generate unique request ID for tracing
function generateRequestId() {
  return `req_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
}

// Map external model request names (Claude, GPT, etc.) to available local NIM equivalents
function resolveTargetModel(requestedModel) {
  if (!requestedModel || typeof requestedModel !== 'string') return requestedModel;

  // 1. If it's directly available and not marked unavailable, use it!
  if (pool.modelsCached.includes(requestedModel) && !unavailableModels.has(requestedModel)) {
    return requestedModel;
  }

  // 2. Try exact/predefined mappings
  const mapping = {
    'claude-3-5-sonnet': ['mistralai/mistral-large', 'meta/llama-3.3-70b-instruct', 'nvidia/llama-3.1-nemotron-70b-instruct'],
    'claude-3-5-sonnet-20241022': ['mistralai/mistral-large', 'meta/llama-3.3-70b-instruct', 'nvidia/llama-3.1-nemotron-70b-instruct'],
    'claude-3-5-haiku': ['meta/llama-3.1-8b-instruct', 'google/gemma-3-4b-it'],
    'claude-3-haiku': ['meta/llama-3.1-8b-instruct', 'google/gemma-3-4b-it'],
    'claude-3-haiku-20240307': ['meta/llama-3.1-8b-instruct', 'google/gemma-3-4b-it'],
    'claude-3-opus': ['mistralai/mistral-large', 'meta/llama-3.3-70b-instruct'],
    'gpt-4o': ['meta/llama-3.3-70b-instruct', 'nvidia/llama-3.1-nemotron-70b-instruct', 'mistralai/mistral-large'],
    'gpt-4o-mini': ['meta/llama-3.1-8b-instruct', 'google/gemma-3-4b-it'],
    'gpt-4': ['meta/llama-3.3-70b-instruct', 'nvidia/llama-3.1-nemotron-70b-instruct'],
    'gpt-3.5-turbo': ['meta/llama-3.1-8b-instruct', 'google/gemma-3-4b-it'],
    'o1': ['meta/llama-3.3-70b-instruct'],
    'o1-preview': ['meta/llama-3.3-70b-instruct'],
    'o1-mini': ['meta/llama-3.1-8b-instruct'],
    'o3-mini': ['meta/llama-3.3-70b-instruct']
  };

  const lower = requestedModel.toLowerCase();
  let candidates = mapping[requestedModel] || mapping[lower] || [];

  // 3. Heuristic matching by family
  if (candidates.length === 0) {
    if (lower.includes('sonnet') || lower.includes('opus') || lower.includes('gpt-4') || lower.includes('mistral-large') || lower.includes('mixtral-8x22b')) {
      candidates = ['mistralai/mistral-large', 'meta/llama-3.3-70b-instruct', 'nvidia/llama-3.1-nemotron-70b-instruct'];
    } else if (lower.includes('haiku') || lower.includes('mini') || lower.includes('gpt-3.5') || lower.includes('gemma-3-4b') || lower.includes('llama-3.1-8b')) {
      candidates = ['meta/llama-3.1-8b-instruct', 'google/gemma-3-4b-it'];
    } else if (lower.includes('claude') || lower.includes('gpt') || lower.includes('gemini')) {
      candidates = ['meta/llama-3.3-70b-instruct', 'nvidia/llama-3.1-nemotron-70b-instruct', 'mistralai/mistral-large'];
    }
  }

  for (const cand of candidates) {
    if (pool.modelsCached.includes(cand) && !unavailableModels.has(cand)) {
      console.log(`[resolveModel] Mapping "${requestedModel}" to available fallback "${cand}"`);
      return cand;
    }
  }

  // 4. Default fallback: Pick the first available chat model in cache
  const availableChatModels = pool.modelsCached.filter(m => !unavailableModels.has(m));
  if (availableChatModels.length > 0) {
    const preferred = ['meta/llama-3.3-70b-instruct', 'nvidia/llama-3.1-nemotron-70b-instruct', 'meta/llama-3.1-8b-instruct'];
    for (const p of preferred) {
      if (availableChatModels.includes(p)) {
        console.log(`[resolveModel] Fallback to preferred: ${p}`);
        return p;
      }
    }
    // Hardcode fallback to meta/llama-3.1-70b-instruct to prevent z-ai/glm-5.2 rate limit loop and 8B thought loops
    console.log(`[resolveModel] Preferred models not available. Forcing meta/llama-3.1-70b-instruct instead of ${availableChatModels[0]}`);
    return 'meta/llama-3.1-70b-instruct';
  }

  return requestedModel;
}

// ── Helpers ─────────────────────────────────────────────────────────────
function clientIp(req) {
  return req.headers['x-forwarded-for']?.split(',')[0]?.trim()
    || req.headers['x-real-ip']
    || req.socket?.remoteAddress
    || 'unknown';
}

function jsonResp(res, code, obj, keyLabel) {
  const body = JSON.stringify(obj);
  const respHeaders = {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(body),
  };
  if (code < 400 && keyLabel) {
    addRateLimitHeaders(respHeaders, keyLabel, pool);
  }
  res.writeHead(code, respHeaders);
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    let settled = false;
    const limit = 25 * 1024 * 1024; // 25MB limit
    const onData = (c) => {
      size += c.length;
      if (size > limit) {
        req.off('data', onData);
        if (!settled) {
          settled = true;
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
        resolve(Buffer.concat(chunks).toString('utf8'));
      }
    });
    req.on('error', (err) => {
      if (!settled) {
        settled = true;
        reject(err);
      }
    });
  });
}

async function convertVisionImages(body) {
  if (!body || !Array.isArray(body.messages)) return;
  const model = (body.model || '').toLowerCase();
  const isVision = model.includes('vision') || model.includes('llava') || model.includes('vila') || model.includes('neva') || model.includes('paligemma');
  if (!isVision) return;

  // Configurable limits
  const MAX_IMAGE_SIZE = parseInt(process.env.MAX_IMAGE_SIZE_MB || '10', 10) * 1024 * 1024; // default 10MB
  const ALLOWED_IMAGE_TYPES = (process.env.ALLOWED_IMAGE_TYPES || 'image/jpeg,image/png,image/webp,image/gif').split(',');

  for (const msg of body.messages) {
    if (!msg || !msg.content) continue;
    if (Array.isArray(msg.content)) {
      for (const item of msg.content) {
        if (item && item.type === 'image_url' && item.image_url && typeof item.image_url.url === 'string') {
          const imgUrl = item.image_url.url;
          if (imgUrl.startsWith('http://') || imgUrl.startsWith('https://')) {
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
function validateRequestBody(body, path) {
  if (!body || typeof body !== 'object') {
    return { valid: false, error: 'Request body must be a JSON object' };
  }

  if (path === '/v1/chat/completions' || path === '/v1/complete') {
    if (!body.model) return { valid: false, error: 'Model is required' };
    if (!body.messages || !Array.isArray(body.messages) || body.messages.length === 0) {
      return { valid: false, error: 'Messages array is required and must not be empty' };
    }
    for (const msg of body.messages) {
      if (!msg.role || !['system', 'user', 'assistant', 'tool'].includes(msg.role)) {
        return { valid: false, error: `Invalid message role: ${msg.role}` };
      }
      if (msg.content !== undefined && msg.content !== null && typeof msg.content !== 'string' && !Array.isArray(msg.content)) {
        return { valid: false, error: 'Message content must be string or array' };
      }
    }
  }

  if (path === '/v1/embeddings') {
    if (!body.model) return { valid: false, error: 'Model is required' };
    if (!body.input || (typeof body.input !== 'string' && !Array.isArray(body.input))) {
      return { valid: false, error: 'Input must be string or array of strings' };
    }
  }

  if (path === '/v1/messages') {
    if (!body.model) return { valid: false, error: 'Model is required' };
    if (!body.messages || !Array.isArray(body.messages) || body.messages.length === 0) {
      return { valid: false, error: 'Messages array is required and must not be empty' };
    }
  }

  return { valid: true };
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

function extractUsageFields(usage) {
  const u = usage || {};
  const pt = u.prompt_tokens || 0;
  const ct = u.completion_tokens || 0;
  const tt = u.total_tokens || 0;
  const cacht = u.prompt_tokens_details?.cached_tokens || 0;
  return { pt, ct, tt, cacht };
}

function resolveBase(modelId) {
  const desc = describe(modelId, BASE_LLM, BASE_GENAI);
  const ep = (desc.endpoints || [])[0];
  return ep?.base_url || BASE_LLM;
}

const SKIP_HEADERS = new Set([
  'host','connection','content-length','transfer-encoding',
  'accept-encoding','x-forwarded-for','x-real-ip',
]);

function forwardHeaders(req) {
  const h = {};
  h['Accept'] = 'application/json, text/event-stream';
  if (req.headers['content-type']) h['Content-Type'] = req.headers['content-type'];
  for (const [k, v] of Object.entries(req.headers)) {
    const lk = k.toLowerCase();
    if (!SKIP_HEADERS.has(lk) && lk !== 'content-type' && lk !== 'accept' && !lk.startsWith('x-hermes') && !lk.startsWith('anthropic-')) {
      h[k] = v;
    }
  }
  return h;
}

function parseUnsupportedParams(bodyText) {
  let msg = '';
  try {
    const d = JSON.parse(bodyText);
    msg = d.message || d.detail || (d.error && d.error.message) || '';
  } catch {
    msg = bodyText || '';
  }
  if (!msg.toLowerCase().includes('unsupported parameter') && !msg.toLowerCase().includes('extra fields') && !msg.toLowerCase().includes('unexpected')) {
    return [];
  }
  const matches = [];
  const regex = /`([^`]+)`|'([^']+)'/g;
  let m;
  while ((m = regex.exec(msg)) !== null) {
    const p = m[1] || m[2];
    if (p && p.trim()) matches.push(p.trim());
  }
  return matches;
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
const MODEL_GRACE_FAILS = 2;
const modelFailCount = {};

function isModelUnavailable(modelId) {
  return unavailableModels.has(modelId);
}

function markModel(modelId, ok, status, path, reason) {
  if (ok) {
    if (unavailableModels.has(modelId)) {
      console.log(`[verify] Model recovered: ${modelId} (${reason})`);
      unavailableModels.delete(modelId);
    }
  } else {
    if (!unavailableModels.has(modelId)) {
      console.warn(`[verify] Model marked unavailable: ${modelId} (${reason})`);
      unavailableModels.add(modelId);
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

function getFriendlyContextLimitError(modelId, rawMsg) {
  const tokensMatch = rawMsg.match(/\d+/g);
  const tokenDetail = tokensMatch ? ` (${tokensMatch[1] || tokensMatch[0]} tokens)` : '';
  return `The context/history for model "${modelId}" is too large${tokenDetail} and exceeds the model's limit. This typically happens if the model entered a repetition loop or if the session has become too long. Please start a clean session (e.g., exit and run 'claude' again if using Claude Code).`;
}

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

  const keyResult = await pool.acquire(modelId);
  const key = keyResult ? keyResult.key : null;
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
      signal: AbortSignal.timeout(15000)
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
  } finally {
    pool.releaseSuccess(key);
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
  console.log(`[verify] Model verification done: ${unavailableModels.size} unavailable models.`);
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
    const unav = metrics.getUnavailableModels();
    for (const m of unav) {
      unavailableModels.add(m);
    }
    console.log(`[verify] Loaded ${unavailableModels.size} unavailable models from database.`);
  } catch (e) {
    console.error('[verify] Failed to load unavailable models:', e.message);
  }
}

// ── Upstream Proxy (OpenAI format) ─────────────────────────────────────
function sanitizeNvidiaPayload(body) {
  if (!body || !Array.isArray(body.messages)) return;
  const model = (body.model || '').toLowerCase();
  const isVision = model.includes('vision') || model.includes('llava') || model.includes('vila') || model.includes('neva') || model.includes('paligemma');

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
      for (let k = 0; k < originalToolCalls.length; k++) {
        const tc = originalToolCalls[k];
        const matchingResult = toolResults.find(r => r.tool_call_id === tc.id);
        newMessages.push({
          role: 'assistant',
          content: k === 0 ? msg.content : "",
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

async function proxyOpenai(body, reqHeaders, model, req = null) {
  sanitizeNvidiaPayload(body);
  const modelId = body.model || model || '';
  if (modelId in RETIRED_MODELS || isModelUnavailable(modelId)) {
    return { status: 404, data: { error: { message: `Model ${modelId} is retired or unavailable`, type: 'invalid_request_error' } } };
  }

  await convertVisionImages(body);

  // Map max_completion_tokens → max_tokens for OpenAI-compatible clients
  if (body.max_completion_tokens !== undefined && body.max_tokens === undefined) {
    body.max_tokens = body.max_completion_tokens;
    delete body.max_completion_tokens;
  }

  const headers = { ...reqHeaders };

  // Inject DEFAULT_ params from .env — only fills gap if client didn't send
  for (const [p, v] of Object.entries(DEFAULT_PARAMS)) {
    if (body[p] === undefined) {
      const num = Number(v);
      body[p] = Number.isFinite(num) ? num : v;
    }
  }

  // Proactive drop: silently remove known-incompatible params (after defaults so the drop always wins)
  for (const p of PROACTIVE_DROP) { delete body[p]; }

  // Inject stream_options so NVIDIA NIM includes usage in last SSE chunk
  if (body.stream && !body.stream_options) {
    body.stream_options = { include_usage: true };
  }

  const strippedParams = new Set();
  let attempt = 0;
  const maxAttempts = Math.max(MAX_RETRIES + 1, pool.totalKeys);
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

      // Client disconnected — no point retrying
      if (req?.clientAbortSignal?.aborted) {
        if (cycles === 0) {
          return { status: 499, data: { error: { message: 'Client disconnected', type: 'client_error' } } };
        }
        break;
      }

      cycles++;
      if (cycles >= 3) break;
      console.warn(`[RETRY-CYCLE] All keys exhausted for model: ${modelId}. Cycle ${cycles}/3: Waiting for adaptive revalidation...`);
      await new Promise(resolve => setTimeout(resolve, cycles * 1500));
      await pool.healInFlight();
      
      // Revalidate: unblock keys/models that are close to unblocking early to retry
      for (const s of pool.keys) {
        if (s.isHardBlocked() && s.hardBlockedUntil - (Date.now() / 1000) < 45) {
          s.hardBlockedUntil = 0;
        }
        if (modelId && s.modelBlocks[modelId]) {
          const rem = s.modelBlocks[modelId] - (Date.now() / 1000);
          if (rem < 30) {
            delete s.modelBlocks[modelId];
          }
        }
      }
    }

    if (!key) {
      return { status: 503, data: { error: { message: 'All API keys exhausted — no capacity available after revalidation cycles', type: 'server_error' } } };
    }

    const startMs = Date.now();
    try {
      incInFlight();
      const baseUrl = resolveBase(modelId);
      const url = `${baseUrl}/v1/chat/completions`;
      const h = {
        'Authorization': `Bearer ${key.apiKey}`,
        'Content-Type': 'application/json',
        'Accept': body.stream ? 'text/event-stream' : 'application/json',
      };
      for (const [k, v] of Object.entries(headers)) {
        if (k.toLowerCase().startsWith('nv-') || k.toLowerCase().startsWith('x-nv-')) {
          h[k] = v;
        }
      }
      const timeoutSec = parseInt(process.env.REQUEST_TIMEOUT || process.env.REQUEST_TIMEOUT_SEC || '600', 10);
      const timeoutMs = (body.stream ? Math.max(timeoutSec, 600) : timeoutSec) * 1000;
      const abortController = new AbortController();
      const clientAbortSignal = req?.clientAbortSignal;
      if (clientAbortSignal) {
        if (clientAbortSignal.aborted) {
          abortController.abort();
        } else {
          const onAbort = () => abortController.abort();
          clientAbortSignal.addEventListener('abort', onAbort, { once: true });
          // Clean up the listener after timeout to prevent memory leaks
          setTimeout(() => clientAbortSignal.removeEventListener('abort', onAbort), timeoutMs + 1000).unref();
        }
      }
      const timeoutSignal = AbortSignal.timeout(timeoutMs);
      const resp = await undiciFetch(url, {
        method: 'POST',
        headers: h,
        body: JSON.stringify(body),
        dispatcher: agent,
        signal: anySignal([abortController.signal, timeoutSignal]),
      });

      let _400respText = '';
      if (resp.status === 400) {
        try { _400respText = await resp.clone().text(); } catch {}
      }
      noteLiveResult('v1/chat/completions', modelId, resp.status, _400respText);

      if (resp.status === 429) {
        const ra = parseInt(resp.headers.get('retry-after') || '0', 10) || 65;
        decInFlight();
        key.decrementInFlight();
        let bodyText = '';
        try { bodyText = await resp.text(); } catch {}
        const [scope, reason] = await pool.registerRateLimit(key, modelId, ra, null, bodyText);
        metrics.recordRateLimitEvent({ keyLabel: key.label, model: modelId, retryAfterS: ra });
        if (attempt < maxAttempts - 1) {
          attempt++;
          await new Promise(resolve => setTimeout(resolve, 50));
          continue;
        }
        return { status: 429, data: { error: { message: `Rate limited (retry-after ${ra}s). Scope: ${scope}, Reason: ${reason}`, type: 'rate_limit_error' } } };
      }

      if (resp.status === 400 || resp.status === 413) {
        let respText = '';
        try { respText = await resp.text(); } catch {}
        if (isDegradedResponse(respText || '')) {
          console.warn(`[DEGRADED] Model ${modelId} is DEGRADED upstream on key ${key.label} — trying next key`);
          let errBody = null;
          try { errBody = JSON.parse(respText); } catch {}
          metrics.recordRequest({
            method: 'POST', path: '/v1/chat/completions',
            model: modelId, keyLabel: key.label,
            streaming: !!body.stream, statusCode: resp.status, latencyMs: Date.now() - startMs,
            wasRateLimited: false, pacingMs
          });
          pool.releaseSuccess(key);
          decInFlight();
          attempt++;
          await new Promise(resolve => setTimeout(resolve, 50));
          continue;
        }
        if (isContextLengthError(resp.status, respText)) {
          let errBody = null;
          try { errBody = JSON.parse(respText); } catch {}
          const rawMsg = errBody?.error?.message || errBody?.message || respText || '';
          const friendlyMsg = getFriendlyContextLimitError(modelId, rawMsg);
          errBody = { error: { message: friendlyMsg, type: 'invalid_request_error' } };
          metrics.recordRequest({
            method: 'POST', path: '/v1/chat/completions',
            model: modelId, keyLabel: key.label,
            streaming: !!body.stream, statusCode: resp.status, latencyMs: Date.now() - startMs,
            wasRateLimited: false, pacingMs
          });
          pool.releaseSuccess(key);
          decInFlight();
          return { status: resp.status, data: errBody };
        }
        if (resp.status === 400 && attempt < MAX_RETRIES) {
          const badParams = parseUnsupportedParams(respText);
          const toStrip = badParams.filter(p => body[p] !== undefined && !strippedParams.has(p));
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
        let errBody = null;
        try { errBody = JSON.parse(respText); } catch {}
        console.warn(`[UPSTREAM ERROR] status: ${resp.status} for model: ${modelId} | error: ${JSON.stringify(errBody)}`);
        metrics.recordRequest({
          method: 'POST', path: '/v1/chat/completions',
          model: modelId, keyLabel: key.label,
          streaming: !!body.stream, statusCode: resp.status, latencyMs: Date.now() - startMs,
          wasRateLimited: false, pacingMs
        });
        pool.releaseSuccess(key);
        decInFlight();
        return { status: resp.status, data: errBody || { error: { message: respText || 'Bad Request', type: 'invalid_request_error' } } };
      }

      if (resp.status >= 500 && attempt < MAX_RETRIES) {
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
            method: 'POST', path: '/v1/chat/completions',
            model: modelId, keyLabel: key.label,
            streaming: !!body.stream, statusCode: 400, latencyMs: Date.now() - startMs,
            wasRateLimited: false, pacingMs
          });
          pool.releaseSuccess(key);
          decInFlight();
          return { status: 400, data: errBody };
        }

        console.warn(`[UPSTREAM ERROR] status: ${resp.status} for model: ${modelId} — retrying next key`);
        pool.releaseSuccess(key);
        decInFlight();
        attempt++;
        await new Promise(resolve => setTimeout(resolve, Math.min(200 * attempt, 2000)));
        continue;
      }

      if (!resp.ok) {
        let errBody = null;
        try { errBody = await resp.json(); } catch {}
        const showStatus = resp.status >= 500 ? 503 : resp.status;
        const showMsg = resp.status >= 500
          ? 'Upstream server error — all retries exhausted'
          : `Upstream ${resp.status}`;
        console.warn(`[UPSTREAM ERROR] status: ${resp.status} for model: ${modelId} | error: ${JSON.stringify(errBody)}`);
        const latencyMs = Date.now() - startMs;
        metrics.recordRequest({
          method: 'POST', path: '/v1/chat/completions',
          model: modelId, keyLabel: key.label,
          streaming: !!body.stream, statusCode: showStatus, latencyMs,
          wasRateLimited: false, pacingMs
        });
        pool.releaseSuccess(key);
        decInFlight();
        return { status: showStatus, data: errBody || { error: { message: showMsg, type: 'upstream_error' } } };
      }

      if (body.stream) {
        // For streaming, we'll decInFlight in handleChatCompletions after streaming completes
        return { status: 200, stream: resp.body, key, model: modelId, startMs, pacingMs };
      }

      const data = await resp.json();
      // Normalize model name in response — NVIDIA sometimes prefixes with stg/
      if (data.model && modelId && data.model !== modelId) {
        data.model = modelId;
      }
      const { pt, ct, tt, cacht } = extractUsageFields(data.usage);
      metrics.recordRequest({
        method: 'POST', path: '/v1/chat/completions',
        model: modelId, keyLabel: key.label,
        streaming: false, statusCode: 200, latencyMs: Date.now() - startMs,
        promptTokens: pt, completionTokens: ct, cachedTokens: cacht, totalTokens: tt,
        wasRateLimited: false, pacingMs
      });
      pool.releaseSuccess(key);
      decInFlight();
      return { status: 200, data, key };
    } catch (e) {
      if (attempt < MAX_RETRIES) {
        console.warn(`[NETWORK ERROR] ${e.message} — retrying`);
        pool.releaseSuccess(key);
        decInFlight();
        attempt++;
        await new Promise(resolve => setTimeout(resolve, Math.min(200 * attempt, 2000)));
        continue;
      }
      const latencyMs = Date.now() - startMs;
      metrics.recordRequest({
        method: 'POST', path: '/v1/chat/completions',
        model: modelId, keyLabel: key.label,
        streaming: !!body.stream, statusCode: e.name === 'TimeoutError' ? 408 : 502, latencyMs,
        wasRateLimited: false, pacingMs
      });
      pool.releaseSuccess(key);
      decInFlight();
      return { status: e.name === 'TimeoutError' ? 408 : 502, data: { error: { message: `Network error: ${e.message}`, type: 'upstream_error' } } };
    }
  }
  return { status: 503, data: { error: { message: 'All attempts failed due to upstream degradation or network errors', type: 'server_error' } } };
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
          return jsonResp(res, 499, { error: { message: 'Client disconnected', type: 'client_error' } });
        }
        break;
      }

      cycles++;
      if (cycles >= 3) break;
      console.warn(`[RETRY-CYCLE] All keys exhausted for model: ${modelId} in proxyPost. Cycle ${cycles}/3: Waiting for adaptive revalidation...`);
      await new Promise(resolve => setTimeout(resolve, cycles * 1500));
      await pool.healInFlight();
      
      // Revalidate: unblock keys/models that are close to unblocking early to retry
      for (const s of pool.keys) {
        if (s.isHardBlocked() && s.hardBlockedUntil - (Date.now() / 1000) < 45) {
          s.hardBlockedUntil = 0;
        }
        if (modelId && s.modelBlocks[modelId]) {
          const rem = s.modelBlocks[modelId] - (Date.now() / 1000);
          if (rem < 30) {
            delete s.modelBlocks[modelId];
          }
        }
      }
    }

    if (!key) {
      return jsonResp(res, 503, { error: { message: 'All API keys exhausted — no capacity available after revalidation cycles', type: 'server_error' } });
    }

    const startMs = Date.now();
    try {
      incInFlight();
      const targetUrl = getTargetUrl ? getTargetUrl(key) : `${resolveBase(modelId)}${path || '/v1/chat/completions'}`;
      const ppTimeoutSec = parseInt(process.env.REQUEST_TIMEOUT || process.env.REQUEST_TIMEOUT_SEC || '600', 10);
      const ppTimeoutMs = (body.stream ? Math.max(ppTimeoutSec, 600) : ppTimeoutSec) * 1000;
      const ppAbortController = new AbortController();
      const ppClientSignal = req?.clientAbortSignal;
      if (ppClientSignal) {
        if (ppClientSignal.aborted) { ppAbortController.abort(); }
        else {
          const onAbort = () => ppAbortController.abort();
          ppClientSignal.addEventListener('abort', onAbort, { once: true });
          setTimeout(() => ppClientSignal.removeEventListener('abort', onAbort), ppTimeoutMs + 1000).unref();
        }
      }
      const resp = await undiciFetch(targetUrl, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${key.apiKey}`,
          'Content-Type': 'application/json',
          ...forwardHeaders(req)
        },
        body: JSON.stringify(body),
        dispatcher: agent,
        signal: anySignal([ppAbortController.signal, AbortSignal.timeout(ppTimeoutMs)]),
      });

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

      if (resp.status === 400 || resp.status === 413) {
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
          let errBody = null;
          try { errBody = JSON.parse(respText); } catch {}
          const rawMsg = errBody?.error?.message || errBody?.message || respText || '';
          const friendlyMsg = getFriendlyContextLimitError(modelId, rawMsg);
          errBody = { error: { message: friendlyMsg, type: 'invalid_request_error' } };
          metrics.recordRequest({
            method: 'POST', path, model: modelId, keyLabel: key.label,
            streaming: false, statusCode: resp.status, latencyMs: Date.now() - startMs,
            wasRateLimited: false, requestBytes: rawBody.length, pacingMs
          });
          pool.releaseSuccess(key);
          decInFlight();
          return jsonResp(res, resp.status, errBody);
        }
        if (resp.status === 400 && attempt < MAX_RETRIES) {
          const badParams = parseUnsupportedParams(respText);
          const toStrip = badParams.filter(p => body[p] !== undefined && !strippedParams.has(p));
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

      if (resp.status >= 500 && attempt < MAX_RETRIES) {
        console.warn(`[UPSTREAM ERROR] status: ${resp.status} for model: ${modelId} — retrying next key`);
        pool.releaseSuccess(key);
        decInFlight();
        attempt++;
        await new Promise(resolve => setTimeout(resolve, Math.min(200 * attempt, 2000)));
        continue;
      }

      if (resp.status >= 500) {
        console.warn(`[UPSTREAM ERROR] status: ${resp.status} for model: ${modelId} — all retries exhausted`);
        metrics.recordRequest({
          method: 'POST', path, model: modelId, keyLabel: key.label,
          streaming: false, statusCode: 503, latencyMs: Date.now() - startMs,
          wasRateLimited: false, requestBytes: rawBody.length, pacingMs
        });
        pool.releaseSuccess(key);
        decInFlight();
        return jsonResp(res, 503, { error: { message: 'Upstream server error — all retries exhausted', type: 'server_error' } });
      }

      const contentType = resp.headers.get('content-type') || '';
      let responseData;
      if (contentType.includes('application/json')) {
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
      if (attempt < MAX_RETRIES) {
        console.warn(`[NETWORK ERROR] ${e.message} — retrying`);
        pool.releaseSuccess(key);
        decInFlight();
        attempt++;
        await new Promise(resolve => setTimeout(resolve, Math.min(200 * attempt, 2000)));
        continue;
      }
      metrics.recordRequest({
        method: 'POST', path, model: modelId, keyLabel: key.label,
        streaming: false, statusCode: e.name === 'TimeoutError' ? 408 : 502, latencyMs: Date.now() - startMs,
        wasRateLimited: false, requestBytes: rawBody.length, pacingMs
      });
      pool.releaseSuccess(key);
      decInFlight();
      return jsonResp(res, e.name === 'TimeoutError' ? 408 : 502, { error: { message: e.message, type: 'upstream_error' } });
    }
  }
  return jsonResp(res, 503, { error: { message: 'All attempts failed due to upstream degradation or network errors', type: 'server_error' } });
}

// ── Route Handlers ──────────────────────────────────────────────────────

/** POST /v1/chat/completions */
async function handleChatCompletions(body, req, res) {
  body.model = resolveTargetModel(body.model);
  const result = await proxyOpenai(body, forwardHeaders(req), body.model, req);

  if (result.stream) {
    let streamError;
    try {
      const respHeaders = {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no',
      };
      addRateLimitHeaders(respHeaders, result.key.label, pool);
      res.writeHead(200, respHeaders);
      const reader = result.stream.getReader();
      const decoder = new TextDecoder();
      let lastUsage = null;
      let ttftMs = 0;
      const MAX_STREAM_BUFFER = 128 * 1024;
      let streamBuffer = '';
      let isFirstChunk = true;
      let hasContent = false;
      let lastUsageSnippet = '';
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          if (isFirstChunk) {
            ttftMs = Date.now() - result.startMs;
            isFirstChunk = false;
          }
          const chunkStr = decoder.decode(value, { stream: true });
          res.write(chunkStr);
          if (chunkStr.includes('choices') || chunkStr.includes('content') || chunkStr.includes('text')) {
            hasContent = true;
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
      } catch (e) { streamError = e; console.error('[stream error] handleChatCompletions:', e.message); }
      if (!hasContent) {
        const friendlyMsg = `The context/history for model "${body.model}" is too large and exceeds the model's limit (or the upstream connection closed immediately). Please exit the current session and start a clean one.`;
        const errChunk = `data: ${JSON.stringify({ error: { message: friendlyMsg, type: 'invalid_request_error' } })}\n\n`;
        res.write(errChunk);
      }
      if (!streamBuffer.includes('data: [DONE]')) {
        res.write('data: [DONE]\n\n');
      }
      res.end();

      try {
        // First try the preserved usage snippet
        if (lastUsageSnippet) {
          const lines2 = lastUsageSnippet.split('\n');
          for (const line of lines2) {
            const t = line.trim();
            if (t.startsWith('data: ') && t.includes('"usage"')) {
              const parsed = JSON.parse(t.slice(6));
              if (parsed && parsed.usage) { lastUsage = parsed.usage; }
            }
          }
        }
        if (!lastUsage) {
          const lines = streamBuffer.split('\n');
          for (let i = lines.length - 1; i >= 0; i--) {
            const line = lines[i].trim();
            if (line.startsWith('data: ') && line !== 'data: [DONE]' && line.includes('"usage"')) {
              const parsed = JSON.parse(line.slice(6));
              if (parsed && parsed.usage) {
                lastUsage = parsed.usage;
                break;
              }
            }
          }
        }
      } catch {}

      const { pt, ct, tt, cacht } = extractUsageFields(lastUsage);
      metrics.recordRequest({
        method: 'POST',
        path: '/v1/chat/completions',
        model: result.model,
        keyLabel: result.key.label,
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
      pool.releaseSuccess(result.key);
      decInFlight();
    }
    return;
  }

  jsonResp(res, result.status, result.data, result.key?.label);
}

/** POST /v1/messages — Anthropic-compatible endpoint */
async function handleAnthropicMessages(rawBody, req, res) {
  let aBody;
  try { aBody = JSON.parse(rawBody); } catch (e) {
    console.error('[JSON PARSE ERROR] messages raw:', JSON.stringify(rawBody), 'err:', e.message);
    return jsonResp(res, 400, anthropicError('invalid_request_error', 'Invalid JSON body: ' + e.message));
  }

  if (!aBody.model) {
    return jsonResp(res, 400, anthropicError('invalid_request_error', 'model is required'));
  }

  const requestedModel = aBody.model;
  aBody.model = resolveTargetModel(aBody.model);
  console.log(`[ROUTE-DEBUG] Claude requested: ${requestedModel} -> Resolved to: ${aBody.model}`);

  // Dump payload for debugging loop
  require('fs').writeFileSync('/tmp/claude_payload.json', JSON.stringify(aBody, null, 2));

  if (aBody.model in RETIRED_MODELS || isModelUnavailable(aBody.model)) {
    return jsonResp(res, 404, anthropicError('not_found_error', `Model ${aBody.model} is retired or unavailable`));
  }

  const oaiBody = anthropicToOpenai(aBody);
  // Proactive drop for Anthropic path too
  for (const p of PROACTIVE_DROP) { delete oaiBody[p]; }
  const inputTokens = estimateInputTokens(aBody);

  const result = await proxyOpenai(oaiBody, forwardHeaders(req), aBody.model, req);

  if (result.stream) {
    let streamError;
    try {
      const respHeaders = {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no',
      };
      addRateLimitHeaders(respHeaders, result.key.label, pool);
      res.writeHead(200, respHeaders);
      const capture = { _startMs: result.startMs };
      let hasContent = false;
      try {
        for await (const chunk of streamOpenaiToAnthropic(result.stream, aBody.model, capture)) {
          res.write(chunk);
          if (chunk.includes('content_block_delta') || chunk.includes('text_delta') || chunk.includes('input_json_delta')) {
            hasContent = true;
          }
        }
      } catch (e) { streamError = e; console.error('[stream error] handleAnthropicMessages:', e.message); }
      if (!hasContent) {
        const friendlyMsg = `The Claude Code session history is too large and exceeds the model's context limit (or the upstream connection was closed immediately). Please exit the current Claude session (type /exit or Ctrl+D) and run 'claude' again to start a clean session.`;
        const errEvent = `event: error\ndata: ${JSON.stringify({ type: 'error', error: { type: 'api_error', message: friendlyMsg } })}\n\n`;
        res.write(errEvent);
      }
      res.end();

      const { pt, ct, tt, cacht } = extractUsageFields(capture.usage);
      metrics.recordRequest({
        method: 'POST',
        path: '/v1/messages',
        model: aBody.model,
        keyLabel: result.key.label,
        streaming: true,
        statusCode: 200,
        latencyMs: Date.now() - result.startMs,
        ttftMs: capture.ttftMs || 0,
        promptTokens: pt,
        completionTokens: ct,
        cachedTokens: cacht,
        totalTokens: tt,
        wasRateLimited: false,
        retries: 0,
        pacingMs: result.pacingMs || 0
      });
    } finally {
      pool.releaseSuccess(result.key);
      decInFlight();
    }
    return;
  }

  if (result.status === 200 && result.data) {
    const anthroResp = openaiToAnthropic(result.data, aBody.model);
    jsonResp(res, 200, anthroResp, result.key?.label);
    return;
  }

  const errData = result.data || {};
  const errMsg = errData?.error?.message || `Upstream error ${result.status}`;
  const errType = result.status === 429 ? 'rate_limit_error' :
                  result.status === 401 ? 'authentication_error' :
                  result.status === 403 ? 'permission_error' :
                  result.status === 404 ? 'not_found_error' :
                  'api_error';
  jsonResp(res, result.status, anthropicError(errType, errMsg));
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
    context_window: desc.context_window ?? DEFAULT_CONTEXT_WINDOW,
    max_output_tokens: desc.max_output_tokens ?? 4096,
    supports_vision: isVision,
    supports_function_calling: isChat,
    supports_parallel_tool_calls: isChat,
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
 */
async function handleModels(res, url = null) {
  const force = url?.searchParams?.get('refresh') === 'true';
  const ids = await pool.refreshModels(force);
  const catalog = buildCatalog(ids, BASE_LLM, BASE_GENAI);
  const data = catalog.map(d => enrichModelMetadata(d.id, d));
  jsonResp(res, 200, { object: 'list', data });
}

/** GET /v1/models/:model */
async function handleModelInfo(modelId, res) {
  const desc = describe(modelId, BASE_LLM, BASE_GENAI);
  if (modelId in RETIRED_MODELS) desc.availability = RETIRED_MODELS[modelId];
  jsonResp(res, 200, enrichModelMetadata(modelId, desc));
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
    const html = fs.readFileSync(DASHBOARD_PATH, 'utf8');
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
  if (modelId in RETIRED_MODELS || isModelUnavailable(modelId) || modelId === 'unknown') {
    return jsonResp(res, 404, { error: { message: modelId === 'unknown' ? 'Unknown model — cannot route request' : `Model ${modelId} is retired or unavailable`, type: 'invalid_request_error' } });
  }
  
  const targetHost = routeUpstream(path);
  const targetUrl = targetHost + path + (url.search ? url.search : '');
  const isStreaming = !!body.stream || (req.headers['accept'] && req.headers['accept'].includes('text/event-stream'));

  let attempt = 0;
  const strippedParams = new Set();
  const maxAttempts = Math.max(MAX_RETRIES + 1, pool.totalKeys);
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
          return jsonResp(res, 499, { error: { message: 'Client disconnected', type: 'client_error' } });
        }
        break;
      }

      cycles++;
      if (cycles >= 3) break;
      console.warn(`[RETRY-CYCLE] All keys exhausted for model: ${modelId} in handleCatchAll. Cycle ${cycles}/3: Waiting for adaptive revalidation...`);
      await new Promise(resolve => setTimeout(resolve, cycles * 1500));
      await pool.healInFlight();
      
      // Revalidate: unblock keys/models that are close to unblocking early to retry
      for (const s of pool.keys) {
        if (s.isHardBlocked() && s.hardBlockedUntil - (Date.now() / 1000) < 45) {
          s.hardBlockedUntil = 0;
        }
        if (modelId && s.modelBlocks[modelId]) {
          const rem = s.modelBlocks[modelId] - (Date.now() / 1000);
          if (rem < 30) {
            delete s.modelBlocks[modelId];
          }
        }
      }
    }

    if (!key) {
      return jsonResp(res, 503, { error: { message: 'All API keys exhausted — no capacity available after revalidation cycles', type: 'server_error' } });
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
      const caTimeoutSec = parseInt(process.env.REQUEST_TIMEOUT || process.env.REQUEST_TIMEOUT_SEC || '600', 10);
      const caTimeoutMs = (isStreaming ? Math.max(caTimeoutSec, 600) : caTimeoutSec) * 1000;
      const caAbortController = new AbortController();
      const caClientSignal = req?.clientAbortSignal;
      if (caClientSignal) {
        if (caClientSignal.aborted) { caAbortController.abort(); }
        else {
          const onAbort = () => caAbortController.abort();
          caClientSignal.addEventListener('abort', onAbort, { once: true });
          setTimeout(() => caClientSignal.removeEventListener('abort', onAbort), caTimeoutMs + 1000).unref();
        }
      }
      const resp = await undiciFetch(targetUrl, {
        method,
        headers,
        body: isPost ? JSON.stringify(body) : undefined,
        dispatcher: agent,
        signal: anySignal([caAbortController.signal, AbortSignal.timeout(caTimeoutMs)]),
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
          let errBody = null;
          try { errBody = JSON.parse(respText); } catch {}
          const rawMsg = errBody?.error?.message || errBody?.message || respText || '';
          const friendlyMsg = getFriendlyContextLimitError(modelId, rawMsg);
          errBody = { error: { message: friendlyMsg, type: 'invalid_request_error' } };
          metrics.recordRequest({
            method, path, model: modelId, keyLabel: key.label,
            streaming: false, statusCode: resp.status, latencyMs: Date.now() - startMs,
            wasRateLimited: false, requestBytes: rawBody.length, pacingMs
          });
          pool.releaseSuccess(key);
          decInFlight();
          return jsonResp(res, resp.status, errBody);
        }
        if (resp.status === 400 && attempt < MAX_RETRIES) {
          const badParams = parseUnsupportedParams(respText);
          const toStrip = badParams.filter(p => body[p] !== undefined && !strippedParams.has(p));
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

      if (resp.status >= 500 && attempt < MAX_RETRIES) {
        console.warn(`[UPSTREAM ERROR] status: ${resp.status} for model: ${modelId} — retrying next key`);
        pool.releaseSuccess(key);
        decInFlight();
        attempt++;
        await new Promise(resolve => setTimeout(resolve, Math.min(200 * attempt, 2000)));
        continue;
      }

      if (resp.status >= 500) {
        console.warn(`[UPSTREAM ERROR] status: ${resp.status} for model: ${modelId} — all retries exhausted`);
        metrics.recordRequest({
          method, path, model: modelId, keyLabel: key.label,
          streaming: isStreaming, statusCode: 503, latencyMs: Date.now() - startMs,
          wasRateLimited: false, requestBytes: rawBody.length, pacingMs
        });
        pool.releaseSuccess(key);
        decInFlight();
        return jsonResp(res, 503, { error: { message: 'Upstream server error — all retries exhausted', type: 'server_error' } });
      }

      const contentType = resp.headers.get('content-type') || '';
      if (isStreaming || contentType.includes('text/event-stream')) {
        res.writeHead(resp.status, {
          'Content-Type': contentType,
          'Cache-Control': 'no-cache',
          'Connection': 'keep-alive',
          'X-Accel-Buffering': 'no',
        });

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        const MAX_STREAM_BUFFER = 128 * 1024;
        let streamBuffer = '';
        let ttftMs = 0;
        let isFirstChunk = true;
        let hasContent = false;
        let lastUsageSnippet = '';
        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            if (isFirstChunk) {
              ttftMs = Date.now() - startMs;
              isFirstChunk = false;
            }
            const chunkStr = decoder.decode(value, { stream: true });
            res.write(chunkStr);
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
        } catch {}
        if (!hasContent) {
          const friendlyMsg = `The context/history for model "${modelId}" is too large and exceeds the model's limit (or the upstream connection closed immediately). Please exit the current session and start a clean one.`;
          const errChunk = `data: ${JSON.stringify({ error: { message: friendlyMsg, type: 'invalid_request_error' } })}\n\n`;
          res.write(errChunk);
        }
        res.end();

        let lastUsage = null;
        try {
          if (lastUsageSnippet) {
            const lines2 = lastUsageSnippet.split('\n');
            for (const line of lines2) {
              const t = line.trim();
              if (t.startsWith('data: ') && t.includes('"usage"')) {
                const parsed = JSON.parse(t.slice(6));
                if (parsed && parsed.usage) { lastUsage = parsed.usage; }
              }
            }
          }
          if (!lastUsage) {
            const lines = streamBuffer.split('\n');
            for (let i = lines.length - 1; i >= 0; i--) {
              const line = lines[i].trim();
              if (line.startsWith('data: ') && line !== 'data: [DONE]' && line.includes('"usage"')) {
                const parsed = JSON.parse(line.slice(6));
                if (parsed && parsed.usage) {
                  lastUsage = parsed.usage;
                  break;
                }
              }
            }
          }
        } catch {}

        const { pt, ct, tt, cacht } = extractUsageFields(lastUsage);
        metrics.recordRequest({
          method, path, model: modelId, keyLabel: key.label,
          streaming: true, statusCode: resp.status, latencyMs: Date.now() - startMs,
          ttftMs,
          promptTokens: pt, completionTokens: ct, cachedTokens: cacht, totalTokens: tt,
          wasRateLimited: false, requestBytes: rawBody.length, pacingMs
        });
        pool.releaseSuccess(key);
        decInFlight();
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
        const { pt, ct, tt, cacht } = extractUsageFields(responseData.usage);
        metrics.recordRequest({
          method, path, model: modelId, keyLabel: key.label,
          streaming: false, statusCode: resp.status, latencyMs: Date.now() - startMs,
          promptTokens: pt, completionTokens: ct, cachedTokens: cacht, totalTokens: tt,
          wasRateLimited: false, requestBytes: rawBody.length, pacingMs
        });
        pool.releaseSuccess(key);
        decInFlight();
        return jsonResp(res, resp.status, responseData);
      } else {
        metrics.recordRequest({
          method, path, model: modelId, keyLabel: key.label,
          streaming: false, statusCode: resp.status, latencyMs: Date.now() - startMs,
          wasRateLimited: false, requestBytes: rawBody.length, pacingMs
        });
        pool.releaseSuccess(key);
        decInFlight();
        res.writeHead(resp.status, { 'Content-Type': contentType });
        return res.end(responseData);
      }
    } catch (e) {
      if (attempt < MAX_RETRIES) {
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
  return jsonResp(res, 503, { error: { message: 'All attempts failed due to upstream degradation or network errors', type: 'server_error' } });
}

// ── Router ──────────────────────────────────────────────────────────────
async function handleRequest(req, res) {
  const requestId = generateRequestId();
  req.requestId = requestId;
  res.setHeader('X-Request-ID', requestId);

  const controller = new AbortController();
  req.clientAbortSignal = controller.signal;
  res.on('close', () => {
    if (!res.writableEnded) {
      controller.abort();
    }
  });

  let url, path, method;
  try {
    url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
    path = url.pathname;
    method = req.method;
  } catch (e) {
    console.warn(`[${requestId}] Malformed request URL: ${req.url}`);
    return jsonResp(res, 400, { error: { message: 'Invalid request URL', type: 'invalid_request_error' } });
  }

  // Log incoming request
  const startTime = Date.now();
  console.log(`[${requestId}] ${method} ${path} from ${clientIp(req)}`);

  // ── CORS headers for all responses ──
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Request-ID');
  if (method === 'OPTIONS') {
    res.writeHead(204);
    return res.end();
  }

  // ── Bearer Token Auth (if configured) ──
  const BEARER_TOKEN = process.env.BEARER_TOKEN?.trim();
  const publicPaths = ['/health', '/metrics/prom', '/', '/dashboard.html', '/dashboard', '/favicon.ico', '/events'];
  if (BEARER_TOKEN && !publicPaths.includes(path) && !path.startsWith('/metrics')) {
    const auth = (req.headers.authorization || '').trim();
    if (auth.replace(/^Bearer\s+/i, '') !== BEARER_TOKEN) {
      console.warn(`[${requestId}] Auth failed for ${method} ${path}`);
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
        blocked_models: pool.blockedModels ? pool.blockedModels() : []
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
        blocked_models: pool.blockedModels ? pool.blockedModels() : [],
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
        console.error('[JSON PARSE ERROR] completions raw:', JSON.stringify(raw), 'err:', e.message);
        return jsonResp(res, 400, { error: { message: 'Invalid JSON: ' + e.message, type: 'invalid_request_error' } });
      }
      // handleChatCompletions() calls resolveTargetModel(body.model) itself,
      // so we just pass the body through. The previous block re-resolved the
      // model here AND in handleChatCompletions (double work) and left a
      // dangling `requestedModel` local; both are removed.
      return await handleChatCompletions(body, req, res);
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
      
      if (!body.input_type && body.input && typeof body.input === 'string') {
        body.input_type = 'query';
      }

      const modelId = resolveTargetModel(body.model || '');
      body.model = modelId;
      if (modelId in RETIRED_MODELS || isModelUnavailable(modelId)) {
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
      if (!modelId || !knownImageModel || modelId in RETIRED_MODELS || isModelUnavailable(modelId)) {
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
      if (!modelId || !knownImageModel || modelId in RETIRED_MODELS || isModelUnavailable(modelId)) {
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

    // ─ Ranking (rerank) ──
    // NVIDIA NIM rerank models live on BASE_LLM (integrate.api.nvidia.com/v1/ranking),
    // NOT on BASE_GENAI. The capabilities.js rerank endpoint definition points at
    // GENAI, which is wrong — the live upstream endpoint is on the LLM host. We
    // route to BASE_LLM here and pass the model through verbatim (rerank models
    // like nvidia/nv-rerankqa-mistral4b-v3 are in the chat catalog, so
    // resolveTargetModel() handles them correctly).
    if (method === 'POST' && path === '/v1/ranking') {
      const raw = await readBody(req);
      let body;
      try { body = JSON.parse(raw); } catch {
        return jsonResp(res, 400, { error: { message: 'Invalid JSON', type: 'invalid_request_error' } });
      }

      const modelId = resolveTargetModel(body.model || '');
      body.model = modelId;
      if (modelId in RETIRED_MODELS || isModelUnavailable(modelId)) {
        return jsonResp(res, 404, { error: { message: `Model ${modelId} is retired or unavailable`, type: 'invalid_request_error' } });
      }

      return await proxyPost({
        req, res, body, rawBody: raw, modelId, path: '/v1/ranking',
        getTargetUrl: () => `${BASE_LLM}/v1/ranking`,
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
      if (oBody.model in RETIRED_MODELS || isModelUnavailable(oBody.model)) {
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

      const result = await proxyOpenai(chatBody, forwardHeaders(req), oBody.model, req);

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
          metrics.recordRequest({
            method: 'POST', path: '/api/chat',
            model: oBody.model, keyLabel: result.key.label,
            streaming: true, statusCode: 200,
            latencyMs: Date.now() - result.startMs,
            ttftMs, wasRateLimited: false, pacingMs: result.pacingMs || 0,
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

      return jsonResp(res, result.status, result.data, result.key?.label);
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
      if (oBody.model in RETIRED_MODELS || isModelUnavailable(oBody.model)) {
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

      const result = await proxyOpenai(chatBody, forwardHeaders(req), oBody.model, req);

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
          metrics.recordRequest({
            method: 'POST', path: '/api/generate',
            model: oBody.model, keyLabel: result.key.label,
            streaming: true, statusCode: 200,
            latencyMs: Date.now() - result.startMs,
            ttftMs, wasRateLimited: false, pacingMs: result.pacingMs || 0,
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

      return jsonResp(res, result.status, result.data, result.key?.label);
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
    console.error(`[${requestId}] ${method} ${path} 500 ${duration}ms: ${e.message}`);
    jsonResp(res, 500, { error: { message: 'Internal server error', type: 'server_error' } });
  } finally {
    const duration = Date.now() - startTime;
    if (!res.writableEnded) {
      console.log(`[${requestId}] ${method} ${path} completed in ${duration}ms`);
    }
  }
}

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
        if (val.length >= 10 && val.startsWith('nvapi-')) {
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
  }, keysReloadSec * 1000);
}

// ── Start ───────────────────────────────────────────────────────────────
async function main() {
  pool.loadFromEnv();
  startKeyReload();

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

  initUpstreamRoutes();

  await loadUnavailableModelsFromDb();

  metrics.prune(30);

  serverInstance = http.createServer(handleRequest);
  const antiSilence = parseInt(process.env.ANTI_SILENCE_TIMEOUT_MS || process.env.SERVER_REQUEST_TIMEOUT_MS || '60000', 10);
  serverInstance.timeout = antiSilence;
  serverInstance.keepAliveTimeout = parseInt(process.env.SERVER_KEEPALIVE_TIMEOUT_MS || '10000', 10);
  serverInstance.maxHeadersCount = 100;
  serverInstance.headersTimeout = parseInt(process.env.SERVER_HEADERS_TIMEOUT_MS || '15000', 10);

  serverInstance.listen(LISTEN_PORT, BIND_HOST, () => {
    console.log(`[wrapper-nvidia] v${VERSION} listening on ${BIND_HOST}:${LISTEN_PORT}`);
    console.log(`[wrapper-nvidia] Keys: ${pool.totalKeys} total, ${pool.availableKeys} available`);
    console.log(`[wrapper-nvidia] Models cached: ${pool.modelsCached.length}`);
    console.log(`[wrapper-nvidia] Upstream: LLM=${BASE_LLM}`);
    console.log(`[wrapper-nvidia] Metrics DB: ${dbPath}`);
  });

  pool.startModelRefresh();

  if (VERIFY_ON_BOOT) {
    verifyLoop();
  }

  setInterval(() => metrics.prune(30), 6 * 3600 * 1000);

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
  process.on('SIGTERM', shutdown);
  process.on('SIGINT',  shutdown);
}

main().catch(e => {
  console.error(`[wrapper-nvidia] Fatal: ${e.message}`);
  process.exit(1);
});
