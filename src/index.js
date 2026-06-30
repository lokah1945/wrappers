#!/usr/bin/env node
/**
 * wrapper-nvidia v4.5.0 — Node.js NVIDIA NIM API proxy
 * Ported from Python main.py (FastAPI) — functionally identical.
 */

const fs = require('fs');
const path = require('path');
const { join, dirname } = path;

// Load env from .env file FIRST (portable)
try {
  const dotenv = require('dotenv');
  dotenv.config({ path: join(__dirname, '..', '.env') });
} catch {}

// ── JSON Log Sink (Parity with Python backup) ───────────────────────────
const WRAPPER_JSON_LOG = (process.env.WRAPPER_JSON_LOG || '').toLowerCase();
const enableJsonLog = ['1', 'true', 'yes'].includes(WRAPPER_JSON_LOG);
const jsonlPath = '/root/wrapper/nvidia/metrics_data/wrapper-events.jsonl';

if (enableJsonLog) {
  try {
    fs.mkdirSync(dirname(jsonlPath), { recursive: true });
    
    const writeJsonEvent = (level, msg, extra = {}) => {
      const payload = {
        ts: new Date().toISOString(),
        level,
        logger: 'nvidia-proxy',
        msg,
        ...extra
      };
      
      // Extract structured context fields if matching patterns
      if (msg.includes('rate-limited') || msg.includes('429')) {
        payload.event = 'rate_limit';
        // Extract model
        const modelMatch = msg.match(/MODEL '([^']+)'/i);
        if (modelMatch) payload.model = modelMatch[1];
        // Extract key label
        const keyMatch = msg.match(/Key ([a-zA-Z0-9_-]+)/i);
        if (keyMatch) payload.key_label = keyMatch[1];
      } else if (msg.includes('exhausted') || msg.includes('exhaust')) {
        payload.event = 'exhaustion';
        payload.severity = 'critical';
      } else if (msg.includes('unavailable') || msg.includes('404')) {
        payload.event = 'model_unavailable';
        const modelMatch = msg.match(/model:?\s*([^\s—|]+)/i) || msg.match(/Model ([^\s—|]+)/i);
        if (modelMatch) payload.model = modelMatch[1];
      } else if (msg.includes('pacing') || msg.includes('throttle')) {
        payload.event = 'pacing';
      } else if (msg.includes('recovered')) {
        payload.event = 'model_recovered';
        const modelMatch = msg.match(/recovered:?\s*([^\s()]+)/i);
        if (modelMatch) payload.model = modelMatch[1];
      }
      
      try {
        fs.appendFileSync(jsonlPath, JSON.stringify(payload) + '\n');
      } catch (err) {
        process.stderr.write(`[logger] Failed to write JSON log: ${err.message}\n`);
      }
    };

    // Monkey-patch console methods
    const origLog = console.log;
    const origWarn = console.warn;
    const origError = console.error;

    console.log = (...args) => {
      origLog(...args);
      const msg = args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' ');
      writeJsonEvent('INFO', msg);
    };

    console.warn = (...args) => {
      origWarn(...args);
      const msg = args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' ');
      writeJsonEvent('WARNING', msg);
    };

    console.error = (...args) => {
      origError(...args);
      const msg = args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' ');
      writeJsonEvent('ERROR', msg);
    };

    console.log(`[logger] JSON log sink enabled -> ${jsonlPath}`);
  } catch (e) {
    console.warn('[logger] Failed to initialize JSON log sink:', e.message);
  }
}

const http = require('http');
const { URL } = require('url');
const { fetch: undiciFetch, Agent } = require('undici');

// Canonical wrapper dir
const WRAPPER_DIR = path.resolve(__dirname, '..');

// (PATCH-001/PATCH-004/PATCH-005) module imports — keep grouped for clarity
const { KeyPool, NVIDIA_BASE_URL, NVIDIA_GENAI_URL, NVIDIA_NVCF_URL } = require('./key_pool');
const { anthropicToOpenai, openaiToAnthropic, streamOpenaiToAnthropic, estimateInputTokens, anthropicError } = require('./anthropic_compat');
const { classify, describe, buildCatalog, summarize, CAPABILITY_PARAMS, RETIRED_MODELS } = require('./capabilities');
const { Metrics } = require('./metrics');
// (PATCH-004) provider circuit breaker classifier
const { ErrorTaxonomy } = require('./error_taxonomy');
const errorTaxonomy = new ErrorTaxonomy();
const PROVIDER_CIRCUIT_ENABLED = (process.env.PROVIDER_CIRCUIT_ENABLED || 'true').toLowerCase() !== 'false';
const PROVIDER_NAME = (process.env.PROVIDER_CIRCUIT_NAME || 'integrate.api.nvidia.com');
// (PATCH-005) stream heartbeat writer
const { installHeartbeatInterval } = require('./stream_heartbeat');

// ── Fault Tolerance (Enterprise & Military Grade Resilience) ─────────────
function safeInterval(fn, ms) {
  // (BUGFIX audit-2026-06-30 R6/R7: unhandledRejection in an async interval callback
  // would otherwise kill the wrapper process. Wrap so we log + swallow + keep ticking.)
  return setInterval(() => { Promise.resolve().then(fn).catch(e => console.error('[INTERVAL ERROR]', e?.message || e)); }, ms);
}

// (BUGFIX audit-2026-06-30 R9: log full context but NEVER exit on uncaught errors.
// ILMA/Hermes callers would otherwise see a process crash with no upstream signal.
// Stays alive unless an explicit fatal type is thrown.)
process.on('uncaughtException', (err) => {
  try {
    console.error('[CRITICAL ERROR] Uncaught Exception:', err?.stack || err?.message || err);
  } catch { /* never throw from the error handler */ }
});
process.on('unhandledRejection', (reason, promise) => {
  try {
    const e = reason instanceof Error ? reason : new Error(String(reason));
    console.error('[CRITICAL ERROR] Unhandled Rejection:', e?.stack || e?.message || e);
  } catch { /* never throw */ }
});
// Belt-and-braces: keep the loop alive if Node decides to emit a warning about
// an unhandled rejection; responses in flight should not be killed off.
process.on('warning', (w) => {
  try { console.warn('[NODE WARNING]', w?.name, w?.message); } catch {}
});

// ── Config ──────────────────────────────────────────────────────────────
const BETA_PORT   = parseInt(process.env.LISTEN_PORT || '9101', 10);
const BIND_HOST   = process.env.LISTEN_HOST || '0.0.0.0';
const BASE_LLM    = (process.env.NVIDIA_BASE_URL || NVIDIA_BASE_URL).replace(/\/+$/, '');
const BASE_GENAI  = (process.env.NVIDIA_GENAI_URL || NVIDIA_GENAI_URL).replace(/\/+$/, '');
const BASE_NVCF   = (process.env.NVIDIA_NVCF_URL || NVIDIA_NVCF_URL).replace(/\/+$/, '');
const DB_PATH     = process.env.METRICS_DB || path.join(WRAPPER_DIR, 'metrics.db');
const QUIET_RETRIED_429 = parseInt(process.env.QUIET_RETRIED_429 || '3', 10);
const MAX_RETRIES = QUIET_RETRIED_429;
const VERSION     = '4.6.0-node';

// (PATCH-006) retry budget — cap total retry walltime instead of bare attempt count.
// Default 15s. Combined with jittered exponential backoff.
const RETRY_BUDGET_MS = parseInt(process.env.RETRY_BUDGET_MS || '15000', 10);
const RETRY_BACKOFF_BASE_MS = parseInt(process.env.RETRY_BACKOFF_BASE_MS || '100', 10);
const RETRY_BACKOFF_CAP_MS = parseInt(process.env.RETRY_BACKOFF_CAP_MS || '1500', 10);

// Jittered exponential backoff helper (PATCH-006).
// attempt is 1-based: 1 -> ~100ms+jitter, 2 -> ~200ms+jitter, 3 -> ~400ms+jitter
function retryBackoffMs(attempt) {
  const exp = Math.min(RETRY_BACKOFF_BASE_MS * Math.pow(1.8, attempt - 1), RETRY_BACKOFF_CAP_MS);
  const jitter = Math.floor(Math.random() * Math.min(exp * 0.4, 200));
  return Math.round(exp + jitter);
}

// Metrics dashboard cache (avoid blocking DB reads on every poll)
const _metricsCache = { _ts: 0, _ttl: 3000, _data: null }; // 3s TTL

// Proactive parameter stripping — silently remove known-incompatible params
// before the first upstream call (e.g. "think" — an Ollama-ism Hermes injects).
// The reactive auto-strip still catches anything else NVIDIA rejects at runtime.
const PROACTIVE_DROP = new Set(
  (process.env.DROP_PARAMS || 'think').split(',').map(s => s.trim()).filter(Boolean)
);

// Concurrent verification config
// (BUGFIX audit-2026-06-30 R-verify: reduced default concurrency 16→8→4 and interval 600→1200
// to prevent verify sweep from monopolizing the key pool and causing 503 cascade.)
const VERIFY_CONCURRENCY = parseInt(process.env.VERIFY_CONCURRENCY || '4', 10);
const VERIFY_INTERVAL = parseInt(process.env.VERIFY_INTERVAL || '1200', 10) * 1000;
const VERIFY_ON_BOOT = process.env.VERIFY_ON_BOOT !== 'false';

// Upstream routing config
let UPSTREAM_ROUTES_SORTED = [];

// ── Globals ────────────────────────────────────────────────────────────
const pool    = new KeyPool();   // keys loaded in main() after dotenv
let metrics;                      // initialized in main() after dotenv sets METRICS_DB
const MAX_CONNECTIONS = parseInt(process.env.MAX_CONNECTIONS || '200', 10);
const agent   = new Agent({ connections: MAX_CONNECTIONS, pipelining: 10 });
let inFlight  = 0;
const unavailableModels = new Set();

// ── Helpers ─────────────────────────────────────────────────────────────
function sseEvent(event, data) {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

function jsonResp(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(body),
  });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    const limit = 25 * 1024 * 1024; // 25MB limit
    const onData = (c) => {
      size += c.length;
      if (size > limit) {
        req.removeListener('data', onData);
        reject(new Error('Request entity too large'));
      } else {
        chunks.push(c);
      }
    };
    req.on('data', onData);
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

async function convertVisionImages(body) {
  if (!body || !Array.isArray(body.messages)) return;
  const model = (body.model || '').toLowerCase();
  const isVision = model.includes('vision') || model.includes('llava') || model.includes('vila') || model.includes('neva') || model.includes('paligemma');
  if (!isVision) return;

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
                const contentType = res.headers.get('content-type') || 'image/png';
                const buffer = Buffer.from(await res.arrayBuffer());
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

function clientIp(req) {
  return req.headers['x-forwarded-for']?.split(',')[0]?.trim()
      || req.headers['x-real-ip']
      || req.socket?.remoteAddress
      || '127.0.0.1';
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
    if (!SKIP_HEADERS.has(lk) && lk !== 'content-type' && lk !== 'accept' && !lk.startsWith('x-hermes')) {
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
  if (!msg.toLowerCase().includes('unsupported parameter') && !msg.toLowerCase().includes('extra fields')) {
    return [];
  }
  const matches = [];
  const regex = /`([^`]+)`|'([^']+)'/g;
  let m;
  while ((m = regex.exec(msg)) !== null) {
    const p = m[1] || m[2];
    if (p && p.trim()) matches.push(p.trim());
  }
  if (matches.length === 0) {
    const commonParams = ['think', 'top_k', 'frequency_penalty', 'presence_penalty', 'max_tokens', 'temperature', 'stream_options'];
    for (const p of commonParams) {
      if (msg.includes(p)) {
        matches.push(p);
      }
    }
  }
  return matches;
}

function isDegradedError(errBody, text) {
  const txt = (text || '').toLowerCase();
  if (txt.includes('degraded') || txt.includes('cannot be invoked') || (txt.includes('function id') && txt.includes('cannot'))) {
    return true;
  }
  if (errBody) {
    const detail = (errBody.detail || '').toLowerCase();
    const title = (errBody.title || '').toLowerCase();
    if (detail.includes('degraded') || detail.includes('cannot be invoked') || title.includes('degraded')) {
      return true;
    }
  }
  return false;
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
    '/v1/ranking': BASE_GENAI,
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
  const parts = path.split('/');
  for (const part of parts) {
    if (part.includes('llama') || part.includes('mixtral') || part.includes('mistral') || part.includes('phi') || part.includes('gemma') || part.includes('nv-embed') || part.includes('glm')) {
      return part;
    }
  }
  return null;
}

// ── Model Verification Sweep ───────────────────────────────────────────
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

function noteLiveResult(path, model, status) {
  const p = path.toLowerCase().replace(/\/+$/, '');
  if (!p.endsWith('chat/completions') && !p.endsWith('embeddings')) {
    return;
  }
  if (status === 200 && unavailableModels.has(model)) {
    markModel(model, true, 200, path, 'recovered via live traffic');
  } else if (status === 404) {
    markModel(model, false, 404, path, '404 on live traffic');
  }
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
  if (!key) return;  // acquireSlot already cleans up myTicket on null return

  const baseUrl = resolveBase(modelId);
  const url = `${baseUrl}/${path}`;

  // (BUGFIX audit-2026-06-29: defensive release tracking — releaseSuccess runs in finally
  // below only on the success path; ensure releaseRateLimited on every non-success outcome
  // including transport-level aborts to prevent inFlight counter leak during fast cancel)
  let probeReleased = false;
  const releaseKey = (mode) => {
    if (probeReleased) return;
    probeReleased = true;
    if (mode === 'failure') pool.releaseFailure(key);
    else if (mode === 'ratelimited') pool.releaseRateLimited(key, 10);
    else pool.releaseSuccess(key);
  };

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

    let respText = '';
    try { respText = await resp.text(); } catch {}

    if (resp.status === 404) {
      markModel(modelId, false, 404, path, '404 on verification probe');
    } else if (resp.status === 400) {
      let errBody = null;
      try { errBody = JSON.parse(respText); } catch {}
      if (isDegradedError(errBody, respText)) {
        markModel(modelId, false, 400, path, 'degraded on verification probe: ' + (errBody?.detail || respText));
      } else {
        markModel(modelId, true, 400, path, 'verified');
      }
    } else if (resp.status === 200 || resp.status === 422) {
      markModel(modelId, true, resp.status, path, 'verified');
    }
  } catch (e) {
    // transient — release as rate-limited (network blip)
    releaseKey('ratelimited');
    return;
  }
  // Good path — release success exactly once
  releaseKey('success');
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

async function verifyLoop() {
  await new Promise(resolve => setTimeout(resolve, 30000));
  while (true) {
    try {
      // (BUGFIX audit-2026-06-29: skip verification sweep if active traffic exceeds threshold.
      // Verification uses 16 concurrent probes competing for the same key pool used by live
      // traffic, which inflated queue depth and produced apparent hangs for Hermes users.)
      const liveReqRate = metrics.recentRequests(1, 0).length; // proxy: last 1 request = indicator
      const liveKeysInFlight = pool.allStats().reduce((s, k) => s + (k.in_flight || 0), 0);
      // Only run sweep when no active live requests AND key pool is mostly idle
      if (liveKeysInFlight > 5) {
        console.log(`[verify] Skipping sweep — ${liveKeysInFlight} in-flight keys (live traffic active)`);
      } else {
        await verifyModels();
      }
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
async function proxyOpenai(body, reqHeaders, model, req = null) {
  const modelId = body.model || model || '';
  if (modelId in RETIRED_MODELS || isModelUnavailable(modelId)) {
    return { status: 404, data: { error: { message: `Model ${modelId} is retired or unavailable`, type: 'invalid_request_error' } } };
  }

  await convertVisionImages(body);

  // Proactive drop: silently remove known-incompatible params before first call
  for (const p of PROACTIVE_DROP) { delete body[p]; }

  // Inject stream_options so NVIDIA NIM includes usage in last SSE chunk
  if (body.stream && !body.stream_options) {
    body.stream_options = { include_usage: true };
  }

  const strippedParams = new Set();
  let attempt = 0;
  // (BUGFIX audit-2026-06-30 R-maxretry: cap maxAttempts to MAX_RETRIES+1 only.
  // Using pool.totalKeys allowed 5× retries on 5xx/401, causing 20+ second
  // hangs when upstream is degraded. MAX_RETRIES=3 → maxAttempts=4 is enough.)
  const maxAttempts = MAX_RETRIES + 1;
  while (attempt < maxAttempts && (Date.now() - retryStartedAt) < RETRY_BUDGET_MS) {
    const keyResult = await pool.acquire(modelId, req?.clientAbortSignal);
    const key = keyResult ? keyResult.key : null;
    const pacingMs = keyResult ? keyResult.waitedMs : 0;

    if (!key) {
      return { status: 503, data: { error: { message: 'All API keys exhausted — no capacity available', type: 'server_error' } } };
    }

    const baseUrl = resolveBase(modelId);
    const url = `${baseUrl}/v1/chat/completions`;
    const headers = {
      'Authorization': `Bearer ${key.apiKey}`,
      'Content-Type': 'application/json',
      'Accept': body.stream ? 'text/event-stream' : 'application/json',
    };
    for (const [k, v] of Object.entries(reqHeaders)) {
      if (k.toLowerCase().startsWith('nv-') || k.toLowerCase().startsWith('x-nv-')) {
        headers[k] = v;
      }
    }

    const requestTimeoutMs = parseInt(process.env.REQUEST_TIMEOUT_SEC || process.env.REQUEST_TIMEOUT || '60', 10) * 1000;
    // Compose abort signals: timeout + client abort
    // (BUGFIX audit-2026-06-29: client abort was not propagated to upstream fetch,
    // causing 60s hangs after Hermes-side disconnect → in-flight stream blocked queue)
    const upstreamSignal = req?.clientAbortSignal
      ? AbortSignal.any([AbortSignal.timeout(requestTimeoutMs), req.clientAbortSignal])
      : AbortSignal.timeout(requestTimeoutMs);

    const startMs = Date.now();
    try {
      const resp = await undiciFetch(url, {
        method: 'POST',
        headers,
        body: JSON.stringify(body),
        dispatcher: agent,
        signal: upstreamSignal,
      });

      noteLiveResult('v1/chat/completions', modelId, resp.status);

      if (resp.status === 429) {
        pool.releaseSuccess(key);
        const ra = parseInt(resp.headers.get('retry-after') || '0', 10) || 65;
        let bodyText = '';
        try { bodyText = await resp.text(); } catch {}
        const [scope, reason] = await pool.registerRateLimit(key, modelId, ra, null, bodyText);
        metrics.recordRateLimitEvent({ keyLabel: key.label, model: modelId, retryAfterS: ra });
        // (PATCH-002) record model-level penalty so future selects avoid this model on this key
        if (scope === 'model') pool.recordModelPenalty(key.label, modelId);
        if (attempt < maxAttempts - 1 && _budgetLeft() > 200) {
          attempt++;
          // (PATCH-006) jittered exponential backoff
          await new Promise(resolve => setTimeout(resolve, retryBackoffMs(attempt)));
          continue;
        }
        return { status: 429, data: { error: { message: `Rate limited (retry-after ${ra}s). Scope: ${scope}, Reason: ${reason}`, type: 'rate_limit_error' } } };
      }

      if (resp.status === 400) {
        let respText = '';
        try { respText = await resp.text(); } catch { console.warn('[READ WARN] Failed to read upstream response body text'); }
        if (attempt < MAX_RETRIES) {
          const badParams = parseUnsupportedParams(respText);
          const toStrip = badParams.filter(p => body[p] !== undefined && !strippedParams.has(p));
          if (toStrip.length > 0) {
            for (const p of toStrip) {
              delete body[p];
              strippedParams.add(p);
            }
            console.warn(`[PARAM STRIP] Stripping unsupported params ${JSON.stringify(toStrip)} and retrying`);
            pool.releaseSuccess(key);
            attempt++;
            continue;
          }
        }
        // No strippable params or retries exhausted — return the 400 with original error body
        let errBody = null;
        try { errBody = JSON.parse(respText); } catch { console.warn('[PARSE WARN] Non-JSON error response body from upstream'); }
        
        if (isDegradedError(errBody, respText)) {
          markModel(modelId, false, 400, '/v1/chat/completions', 'degraded: ' + (errBody?.detail || respText));
          console.warn(`[MODEL DEGRADED] Marked model ${modelId} as unavailable due to upstream degradation`);
          metrics.recordRequest({
            method: 'POST', path: '/v1/chat/completions',
            model: modelId, keyLabel: key.label,
            streaming: !!body.stream, statusCode: 503, latencyMs: Date.now() - startMs,
            wasRateLimited: false, pacingMs
          });
          pool.releaseSuccess(key);
          return { status: 503, data: { error: { message: `Model ${modelId} is temporarily degraded or unavailable on upstream`, type: 'invalid_request_error' } } };
        }

        console.warn(`[UPSTREAM ERROR] status: 400 for model: ${modelId} | error: ${JSON.stringify(errBody)}`);
        metrics.recordRequest({
          method: 'POST', path: '/v1/chat/completions',
          model: modelId, keyLabel: key.label,
          streaming: !!body.stream, statusCode: 400, latencyMs: Date.now() - startMs,
          wasRateLimited: false, pacingMs
        });
        pool.releaseSuccess(key);
        return { status: 400, data: errBody || { error: { message: respText || 'Bad Request', type: 'invalid_request_error' } } };
      }

      // (BUGFIX audit-2026-06-30 R-404: 404 = model not found on upstream, NOT transient.
      // Retrying 404 across all keys wastes capacity and causes P95=41s latency spikes.)
      const isRetryableError = (resp.status >= 500) || [401, 403].includes(resp.status);
      // (BUGFIX audit-2026-06-30 R-404-mark: auto-mark 404 models unavailable so
      // subsequent requests skip immediately instead of retrying every key.)
      if (resp.status === 404) {
        let respText404 = '';
        try { respText404 = await resp.text(); } catch {}
        markModel(modelId, false, 404, '/v1/chat/completions', 'not_found: ' + (respText404 || '').slice(0, 200));
        console.warn(`[UPSTREAM 404] Model ${modelId} not found on upstream — marked unavailable, returning 404 fast`);
      }
      if (isRetryableError && attempt < maxAttempts - 1 && _budgetLeft() > 300) {
        console.warn(`[UPSTREAM ERROR] status: ${resp.status} for model: ${modelId} on key: ${key.label} — retrying next key`);
        const cooldown = [401, 403].includes(resp.status) ? 3600 : 15;
        pool.releaseRateLimited(key, cooldown);
        // (PATCH-004) classify — provider-level failures feed circuit breaker
        const _class = errorTaxonomy.classify({ status: resp.status, body: '', model: modelId, key: key.label, provider: PROVIDER_NAME });
        globalThis.__handleFailClassification?.(_class, { status: resp.status, model: modelId });
        attempt++;
        // (PATCH-006) jittered exponential backoff, capped
        await new Promise(resolve => setTimeout(resolve, retryBackoffMs(attempt)));
        continue;
      }

      if (!resp.ok && resp.status !== 200) {
        let errBody = null;
        let respText = '';
        try {
          respText = await resp.text();
          errBody = JSON.parse(respText);
        } catch {}
        console.warn(`[UPSTREAM ERROR] status: ${resp.status} for model: ${modelId} | error: ${JSON.stringify(errBody || respText)}`);
        const latencyMs = Date.now() - startMs;
        metrics.recordRequest({
          method: 'POST', path: '/v1/chat/completions',
          model: modelId, keyLabel: key.label,
          streaming: !!body.stream, statusCode: resp.status, latencyMs,
          wasRateLimited: false, pacingMs
        });
        pool.releaseSuccess(key);
        return { status: resp.status, data: errBody || { error: { message: respText || `Upstream ${resp.status}`, type: 'upstream_error' } } };
      }

      if (body.stream) {
        return { status: 200, stream: resp.body, key, model: modelId, startMs, pacingMs };
      }

      const data = await resp.json();
      const { pt, ct, tt, cacht } = extractUsageFields(data.usage);
      // (PATCH-004) success — clear provider circuit (half-open recovery)
      if (errorTaxonomy.isProviderOpen(PROVIDER_NAME)) {
        errorTaxonomy.providerProbeSucceeded(PROVIDER_NAME);
        console.info(`[CIRCUIT-CLOSE] ${PROVIDER_NAME} recovered (half-open probe succeeded)`);
      }
      metrics.recordRequest({
        method: 'POST', path: '/v1/chat/completions',
        model: modelId, keyLabel: key.label,
        streaming: false, statusCode: 200, latencyMs: Date.now() - startMs,
        promptTokens: pt, completionTokens: ct, cachedTokens: cacht, totalTokens: tt,
        wasRateLimited: false, pacingMs
      });
      pool.releaseSuccess(key);
      return { status: 200, data, key };
    } catch (e) {
      if (attempt < maxAttempts - 1 && _budgetLeft() > 250) {
        console.warn(`[NETWORK ERROR] ${e.message} — retrying next key (budget-rem=${_budgetLeft()}ms)`);
        pool.releaseRateLimited(key, 10);
        // (PATCH-004) classify network errors — they DON'T trigger provider circuit (only provider class does)
        const _class = errorTaxonomy.classify({ status: 0, body: e.message, model: modelId, key: key.label, provider: PROVIDER_NAME });
        if (_class !== 'network') globalThis.__handleFailClassification?.(_class, { status: 0, model: modelId });
        attempt++;
        // (PATCH-006) jittered exponential backoff
        await new Promise(resolve => setTimeout(resolve, retryBackoffMs(attempt)));
        continue;
      }
      const latencyMs = Date.now() - startMs;
      metrics.recordRequest({
        method: 'POST', path: '/v1/chat/completions',
        model: modelId, keyLabel: key.label,
        streaming: !!body.stream, statusCode: e.name === 'TimeoutError' ? 408 : 502, latencyMs,
        wasRateLimited: false, pacingMs
      });
      pool.releaseFailure(key);
      return { status: e.name === 'TimeoutError' ? 408 : 502, data: { error: { message: `Network error: ${e.message}`, type: 'upstream_error' } } };
    }
  }
}

// ── Generic POST Helper (embeddings, images, ranking) ─────────────────
async function proxyPost({ req, res, body, rawBody, modelId, path, getTargetUrl }) {
  const strippedParams = new Set();
  // (BUGFIX audit-2026-06-30 R-maxretry: cap maxAttempts to MAX_RETRIES+1 only.
  // Using pool.totalKeys allowed 5× retries on 5xx/401, causing 20+ second
  // hangs when upstream is degraded. MAX_RETRIES=3 → maxAttempts=4 is enough.)
  let attempt = 0;
  const maxAttempts = MAX_RETRIES + 1;
  // (PATCH-006) per-call retry budget — caps total walltime across attempts
  const retryStartedAt = Date.now();
  function _budgetLeft() { return RETRY_BUDGET_MS - (Date.now() - retryStartedAt); }
  while (attempt < maxAttempts && (Date.now() - retryStartedAt) < RETRY_BUDGET_MS) {
    const keyResult = await pool.acquire(modelId, req?.clientAbortSignal);
    const key = keyResult ? keyResult.key : null;
    const pacingMs = keyResult ? keyResult.waitedMs : 0;

    if (!key) {
      return jsonResp(res, 503, { error: { message: 'All API keys exhausted', type: 'server_error' } });
    }

    const targetUrl = getTargetUrl(key);
    const startMs = Date.now();
    // (BUGFIX audit-2026-06-29: same client-abort propagation as proxyOpenai/handleCatchAll.
    // Without this, embeddings/images/ranking/infer endpoints can hit the 60s upstream default
    // timeout even after the client disconnects.)
    const ppTimeoutMs = parseInt(process.env.REQUEST_TIMEOUT_SEC || process.env.REQUEST_TIMEOUT || '60', 10) * 1000;
    const ppSignal = req?.clientAbortSignal
      ? AbortSignal.any([AbortSignal.timeout(ppTimeoutMs), req.clientAbortSignal])
      : AbortSignal.timeout(ppTimeoutMs);
    try {
      const resp = await undiciFetch(targetUrl, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${key.apiKey}`,
          'Content-Type': 'application/json',
          ...forwardHeaders(req)
        },
        body: JSON.stringify(body),
        dispatcher: agent,
        signal: ppSignal,
      });

      noteLiveResult(path, modelId, resp.status);

      if (resp.status === 429) {
        pool.releaseSuccess(key);
        const ra = parseInt(resp.headers.get('retry-after') || '0', 10) || 65;
        let bodyText = '';
        try { bodyText = await resp.text(); } catch {}
        await pool.registerRateLimit(key, modelId, ra, null, bodyText);
        metrics.recordRateLimitEvent({ keyLabel: key.label, model: modelId, retryAfterS: ra });
        if (attempt < maxAttempts - 1) {
          attempt++;
          // Fast rotation on rate limits: retry immediately with 50ms delay
          await new Promise(resolve => setTimeout(resolve, 50));
          continue;
        }
        return jsonResp(res, 429, { error: { message: `Rate limited (retry-after ${ra}s)`, type: 'rate_limit_error' } });
      }

      if (resp.status === 400) {
        let respText = '';
        try { respText = await resp.text(); } catch { console.warn('[READ WARN] Failed to read upstream response body text'); }
        if (attempt < MAX_RETRIES) {
          const badParams = parseUnsupportedParams(respText);
          const toStrip = badParams.filter(p => body[p] !== undefined && !strippedParams.has(p));
          if (toStrip.length > 0) {
            for (const p of toStrip) {
              delete body[p];
              strippedParams.add(p);
            }
            console.warn(`[PARAM STRIP] Stripping unsupported params ${JSON.stringify(toStrip)} and retrying`);
            pool.releaseSuccess(key);
            attempt++;
            continue;
          }
        }
        // No strippable params or retries exhausted — return 400 with captured body
        let errBody = null;
        try { errBody = JSON.parse(respText); } catch { console.warn('[PARSE WARN] Non-JSON error response body from upstream'); }
        
        if (isDegradedError(errBody, respText)) {
          markModel(modelId, false, 400, path, 'degraded: ' + (errBody?.detail || respText));
          console.warn(`[MODEL DEGRADED] Marked model ${modelId} as unavailable due to upstream degradation`);
          metrics.recordRequest({
            method: 'POST', path, model: modelId, keyLabel: key.label,
            streaming: false, statusCode: 503, latencyMs: Date.now() - startMs,
            wasRateLimited: false, requestBytes: rawBody.length, pacingMs
          });
          pool.releaseSuccess(key);
          return jsonResp(res, 503, { error: { message: `Model ${modelId} is temporarily degraded or unavailable on upstream`, type: 'invalid_request_error' } });
        }

        metrics.recordRequest({
          method: 'POST', path, model: modelId, keyLabel: key.label,
          streaming: false, statusCode: 400, latencyMs: Date.now() - startMs,
          wasRateLimited: false, requestBytes: rawBody.length, pacingMs
        });
        pool.releaseSuccess(key);
        return jsonResp(res, 400, errBody || { error: { message: respText || 'Bad Request', type: 'invalid_request_error' } });
      }

      // (BUGFIX audit-2026-06-30 R-404: 404 = model not found on upstream, NOT transient.
      // Retrying 404 across all keys wastes capacity and causes P95=41s latency spikes.)
      const isRetryableError = (resp.status >= 500) || [401, 403].includes(resp.status);
      if (isRetryableError && attempt < maxAttempts - 1 && _budgetLeft() > 300) {
        console.warn(`[UPSTREAM ERROR] status: ${resp.status} for model: ${modelId} on key: ${key.label} — retrying next key`);
        const cooldown = [401, 403].includes(resp.status) ? 3600 : 15;
        pool.releaseRateLimited(key, cooldown);
        // (PATCH-004) classify — provider-level failures feed circuit breaker
        const _class = errorTaxonomy.classify({ status: resp.status, body: '', model: modelId, key: key.label, provider: PROVIDER_NAME });
        globalThis.__handleFailClassification?.(_class, { status: resp.status, model: modelId });
        attempt++;
        // (PATCH-006) jittered exponential backoff, capped
        await new Promise(resolve => setTimeout(resolve, retryBackoffMs(attempt)));
        continue;
      }

      const contentType = resp.headers.get('content-type') || '';
      if (contentType.includes('application/json')) {
        let responseData = await resp.json();
        // Normalize non-standard response from Flux models (artifacts -> data)
        if (path.includes('images') && responseData && Array.isArray(responseData.artifacts)) {
          responseData = {
            created: Math.floor(Date.now() / 1000),
            data: responseData.artifacts.map(art => ({
              b64_json: art.base64 || art.b64_json || '',
              revised_prompt: body.prompt || ''
            }))
          };
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
        return jsonResp(res, resp.status, responseData);
      } else {
        // Binary response (ASR/TTS, images, audio, video) in proxyPost
        res.writeHead(resp.status, {
          'Content-Type': contentType,
          'Content-Length': resp.headers.get('content-length') || undefined
        });
        const reader = resp.body.getReader();
        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            res.write(value);
          }
        } catch (e) {
          console.error('Binary stream error in proxyPost:', e);
        }
        res.end();
        metrics.recordRequest({
          method: 'POST', path, model: modelId, keyLabel: key.label,
          streaming: false, statusCode: resp.status, latencyMs: Date.now() - startMs,
          wasRateLimited: false, requestBytes: rawBody.length, pacingMs
        });
        pool.releaseSuccess(key);
        return;
      }
    } catch (e) {
      if (attempt < maxAttempts - 1 && _budgetLeft() > 250) {
        console.warn(`[NETWORK ERROR] ${e.message} — retrying next key (budget-rem=${_budgetLeft()}ms)`);
        pool.releaseRateLimited(key, 10);
        // (PATCH-004) classify network errors — they DON'T trigger provider circuit (only provider class does)
        const _class = errorTaxonomy.classify({ status: 0, body: e.message, model: modelId, key: key.label, provider: PROVIDER_NAME });
        if (_class !== 'network') globalThis.__handleFailClassification?.(_class, { status: 0, model: modelId });
        attempt++;
        // (PATCH-006) jittered exponential backoff
        await new Promise(resolve => setTimeout(resolve, retryBackoffMs(attempt)));
        continue;
      }
      metrics.recordRequest({
        method: 'POST', path, model: modelId, keyLabel: key.label,
        streaming: false, statusCode: e.name === 'TimeoutError' ? 408 : 502, latencyMs: Date.now() - startMs,
        wasRateLimited: false, requestBytes: rawBody.length, pacingMs
      });
      pool.releaseFailure(key);
      if (!res.headersSent) {
        return jsonResp(res, e.name === 'TimeoutError' ? 408 : 502, { error: { message: e.message, type: 'upstream_error' } });
      }
      return;
    }
  }
}

// ── Route Handlers ──────────────────────────────────────────────────────

/** POST /v1/chat/completions */
async function handleChatCompletions(body, req, res) {
  let retries = 0;
  while (retries <= QUIET_RETRIED_429) {
    const result = await proxyOpenai(body, forwardHeaders(req), body.model, req);
    if (result.retry && retries < QUIET_RETRIED_429) {
      retries++;
      continue;
    }

    if (result.stream) {
      res.writeHead(200, {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no',
      });
      // (PATCH-005) install heartbeat writer on stream — OFF by default, env-gated
      installHeartbeatInterval(res);
      const reader = result.stream.getReader();
      const decoder = new TextDecoder();
      let lastUsage = null;
      const streamChunks = [];
      let seenDone = false;
      // (BUGFIX audit-2026-06-29: ensure key release on client abort / stream error)
      let streamReleased = false;
      const releaseStream = (mode) => {
        if (streamReleased) return;
        streamReleased = true;
        if (mode === 'failure') pool.releaseFailure(result.key);
        else if (mode === 'ratelimited') pool.releaseRateLimited(result.key, 10);
        else pool.releaseSuccess(result.key);
      };

      // Notify upstream abort signal chain (already on req.clientAbortSignal — see proxyOpenai)
      // so undici cancels as soon as the client disconnects. No need for an extra abort controller here.
      try {
        while (true) {
          if (req?.clientAbortSignal?.aborted) {
            // Client disappear — drain quietly and exit fast
            try { reader.cancel('client-aborted').catch(() => {}); } catch {}
            console.warn('[STREAM ABORT] client aborted, cancelling upstream reader');
            releaseStream('failure');
            if (!res.writableEnded) { try { res.end(); } catch {} }
            return;
          }
          const { done, value } = await reader.read();
          if (done) break;
          if (req?.clientAbortSignal?.aborted) {
            try { reader.cancel('client-aborted-late').catch(() => {}); } catch {}
            releaseStream('failure');
            if (!res.writableEnded) { try { res.end(); } catch {} }
            return;
          }
          const chunkStr = decoder.decode(value, { stream: true });
          try {
            res.write(chunkStr);
          } catch (writeErr) {
            // res destroyed by client — cancel upstream to free the key
            try { reader.cancel('client-write-failed').catch(() => {}); } catch {}
            console.warn('[STREAM WARN] res.write failed:', writeErr?.message);
            releaseStream('failure');
            return;
          }
          if (!seenDone && chunkStr.includes('data: [DONE]')) seenDone = true;
          streamChunks.push(chunkStr);
        }
        // Normal completion
        releaseStream('success');
      } catch (streamErr) {
        // Stream interrupted (likely upstream abortSignal fired because client disconnected
        // OR upstream timeout). Release key on transport-level abort.
        console.warn('[STREAM WARN] proxyOpenai stream interrupted:', streamErr?.message);
        try { reader.cancel('stream-err').catch(() => {}); } catch {}
        releaseStream(streamErr?.name === 'AbortError' ? 'failure' : 'ratelimited');
      }
      // Only write [DONE] if upstream didn't already send it
      if (!seenDone && !req?.clientAbortSignal?.aborted) {
        try { res.write('data: [DONE]\n\n'); } catch {}
      }
      try { res.end(); } catch {}

      try {
        try {
          const lines = streamChunks.join('').split('\n');
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
        } catch { console.warn('[STREAM WARN] proxyOpenai usage parse failed from stream buffer'); }

        const { pt, ct, tt, cacht } = extractUsageFields(lastUsage);
        metrics.recordRequest({
          method: 'POST',
          path: '/v1/chat/completions',
          model: result.model,
          keyLabel: result.key.label,
          streaming: true,
          statusCode: 200,
          latencyMs: Date.now() - result.startMs,
          promptTokens: pt,
          completionTokens: ct,
          cachedTokens: cacht,
          totalTokens: tt,
          wasRateLimited: false,
          retries,
          pacingMs: result.pacingMs || 0
        });
      } catch (err) {
        console.error('[METRICS ERROR] Failed to record request metrics in handleChatCompletions:', err.message);
      }
      // (BUGFIX audit-2026-06-29: release handled by releaseStream() above on all paths
      // incl. client-abort / write-failure / transport-timeout — no fallback finally needed.)
      return;
    }

    jsonResp(res, result.status, result.data);
    return;
  }
  jsonResp(res, 429, { error: { message: 'All retries exhausted — keys rate-limited', type: 'rate_limit_error' } });
}

/** POST /v1/messages — Anthropic-compatible endpoint */
async function handleAnthropicMessages(rawBody, req, res) {
  let aBody;
  try { aBody = JSON.parse(rawBody); } catch (e) {
    console.error('[JSON PARSE ERROR] messages raw:', JSON.stringify(rawBody), 'err:', e.message);
    return jsonResp(res, 400, anthropicError('invalid_request_error', 'Invalid JSON body: ' + e.message));
  }

  if (!aBody || typeof aBody !== 'object' || Array.isArray(aBody)) {
    return jsonResp(res, 400, anthropicError('invalid_request_error', 'Invalid request: body must be a JSON object'));
  }

  if (!aBody.model) {
    return jsonResp(res, 400, anthropicError('invalid_request_error', 'model is required'));
  }

  if (aBody.model in RETIRED_MODELS || isModelUnavailable(aBody.model)) {
    return jsonResp(res, 404, anthropicError('not_found_error', `Model ${aBody.model} is retired or unavailable`));
  }

  const oaiBody = anthropicToOpenai(aBody);
  // Proactive drop for Anthropic path too
  for (const p of PROACTIVE_DROP) { delete oaiBody[p]; }
  const inputTokens = estimateInputTokens(aBody);

  let retries = 0;
  while (retries <= QUIET_RETRIED_429) {
    const result = await proxyOpenai(oaiBody, forwardHeaders(req), aBody.model, req);

    if (result.retry && retries < QUIET_RETRIED_429) {
      retries++;
      continue;
    }

    if (result.stream) {
      res.writeHead(200, {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no',
      });
      // (PATCH-005) install heartbeat writer on stream — OFF by default, env-gated
      installHeartbeatInterval(res);
      const capture = {};
      try {
        for await (const chunk of streamOpenaiToAnthropic(result.stream, aBody.model, capture)) {
          res.write(chunk);
        }
      } catch (streamErr) {
        // FIX I1-anthropic: Ensure message_stop is sent even if stream breaks,
        // otherwise Anthropic SDK clients hang waiting for it.
        console.warn('[STREAM ERROR] Anthropic stream interrupted:', streamErr?.message);
        if (!capture.stop) {
          try {
            res.write(sseEvent('message_delta', {
              type: 'message_delta',
              delta: { stop_reason: 'end_turn', stop_sequence: null },
              usage: { input_tokens: 0, output_tokens: 0 },
            }));
            res.write(sseEvent('message_stop', { type: 'message_stop' }));
          } catch { console.warn('[STREAM WARN] Failed to write message_stop event'); }
        }
      }
      res.end();

      try {
        const { pt, ct, tt, cacht } = extractUsageFields(capture.usage);
        metrics.recordRequest({
          method: 'POST',
          path: '/v1/messages',
          model: aBody.model,
          keyLabel: result.key.label,
          streaming: true,
          statusCode: 200,
          latencyMs: Date.now() - result.startMs,
          promptTokens: pt,
          completionTokens: ct,
          cachedTokens: cacht,
          totalTokens: tt,
          wasRateLimited: false,
          retries,
          pacingMs: result.pacingMs || 0
        });
      } catch (err) {
        console.error('[METRICS ERROR] Failed to record request metrics in handleAnthropicMessages:', err.message);
      } finally {
        pool.releaseSuccess(result.key);
      }
      return;
    }

    if (result.status === 200 && result.data) {
      const anthroResp = openaiToAnthropic(result.data, aBody.model);
      jsonResp(res, 200, anthroResp);
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
    return;
  }
  jsonResp(res, 429, anthropicError('rate_limit_error', 'All retries exhausted — keys rate-limited'));
}

/** GET /health */
function handleHealth(res) {
  jsonResp(res, 200, pool.healthJson());
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

/** GET /v1/models */
async function handleModels(res, url = null) {
  const force = url?.searchParams?.get('refresh') === 'true';
  const ids = await pool.refreshModels(force);
  const data = ids.map(id => {
    const desc = describe(id, BASE_LLM, BASE_GENAI);
    return {
      id,
      object: 'model',
      owned_by: id.split('/')[0] || 'nvidia',
      created: 1715632124,
      context_window: desc.context_window || 4096,
      context_len: desc.context_window || 4096,
      max_position_embeddings: desc.context_window || 4096,
      capabilities: desc.capabilities || ['chat'],
      type: desc.type || 'chat'
    };
  });
  jsonResp(res, 200, { object: 'list', data });
}

/** GET /v1/models/:model */
async function handleModelInfo(modelId, res) {
  const desc = describe(modelId, BASE_LLM, BASE_GENAI);
  if (modelId in RETIRED_MODELS) desc.availability = RETIRED_MODELS[modelId];
  jsonResp(res, 200, { id: modelId, object: 'model', ...desc });
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

  let modelId = body.model || modelFromPath(path) || 'unknown';
  if (modelId in RETIRED_MODELS || isModelUnavailable(modelId)) {
    return jsonResp(res, 404, { error: { message: `Model ${modelId} is retired or unavailable`, type: 'invalid_request_error' } });
  }
  
  const targetHost = routeUpstream(path);
  const targetUrl = targetHost + path + (url.search ? url.search : '');
  const isStreaming = !!body.stream || (req.headers['accept'] && req.headers['accept'].includes('text/event-stream'));

  let attempt = 0;
  const strippedParams = new Set();
  const maxAttempts = MAX_RETRIES + 1;
  // (PATCH-006) per-call retry budget — caps total walltime across attempts
  const retryStartedAt = Date.now();
  function _budgetLeft() { return RETRY_BUDGET_MS - (Date.now() - retryStartedAt); }
  while (attempt < maxAttempts && (Date.now() - retryStartedAt) < RETRY_BUDGET_MS) {
    const keyResult = await pool.acquire(modelId, req?.clientAbortSignal);
    const key = keyResult ? keyResult.key : null;
    const pacingMs = keyResult ? keyResult.waitedMs : 0;

    if (!key) {
      return jsonResp(res, 503, { error: { message: 'All API keys exhausted', type: 'server_error' } });
    }

    const headers = {
      'Authorization': `Bearer ${key.apiKey}`,
      ...forwardHeaders(req)
    };
    if (isPost) {
      headers['Content-Type'] = 'application/json';
    }

    const startMs = Date.now();
    const ctRequestTimeoutMs = parseInt(process.env.REQUEST_TIMEOUT_SEC || process.env.REQUEST_TIMEOUT || '60', 10) * 1000;
    // (BUGFIX audit-2026-06-29: same abort propagation as proxyOpenai)
    const ctUpstreamSignal = req?.clientAbortSignal
      ? AbortSignal.any([AbortSignal.timeout(ctRequestTimeoutMs), req.clientAbortSignal])
      : AbortSignal.timeout(ctRequestTimeoutMs);
    try {
      const resp = await undiciFetch(targetUrl, {
        method,
        headers,
        body: isPost ? JSON.stringify(body) : undefined,
        dispatcher: agent,
        signal: ctUpstreamSignal,
      });

      noteLiveResult(path, modelId, resp.status);

      if (resp.status === 429) {
        pool.releaseSuccess(key);
        const ra = parseInt(resp.headers.get('retry-after') || '0', 10) || 65;
        let bodyText = '';
        try { bodyText = await resp.text(); } catch {}
        await pool.registerRateLimit(key, modelId, ra, null, bodyText);
        metrics.recordRateLimitEvent({ keyLabel: key.label, model: modelId, retryAfterS: ra });
        if (attempt < maxAttempts - 1) {
          attempt++;
          // Fast rotation on rate limits: retry immediately with 50ms delay
          await new Promise(resolve => setTimeout(resolve, 50));
          continue;
        }
        return jsonResp(res, 429, { error: { message: `Rate limited (retry-after ${ra}s)`, type: 'rate_limit_error' } });
      }

      if (resp.status === 400 && isPost) {
        let respText = '';
        try { respText = await resp.text(); } catch { console.warn('[READ WARN] Failed to read upstream response body text'); }
        if (attempt < MAX_RETRIES) {
          const badParams = parseUnsupportedParams(respText);
          const toStrip = badParams.filter(p => body[p] !== undefined && !strippedParams.has(p));
          if (toStrip.length > 0) {
            for (const p of toStrip) {
              delete body[p];
              strippedParams.add(p);
            }
            console.warn(`[PARAM STRIP] Stripping unsupported params ${JSON.stringify(toStrip)} and retrying`);
            pool.releaseSuccess(key);
            attempt++;
            continue;
          }
        }
        // No strippable params or retries exhausted — return 400 with captured body
        let errBody = null;
        try { errBody = JSON.parse(respText); } catch { console.warn('[PARSE WARN] Non-JSON error response body from upstream'); }
        
        if (isDegradedError(errBody, respText)) {
          markModel(modelId, false, 400, path, 'degraded: ' + (errBody?.detail || respText));
          console.warn(`[MODEL DEGRADED] Marked model ${modelId} as unavailable due to upstream degradation`);
          metrics.recordRequest({
            method, path, model: modelId, keyLabel: key.label,
            streaming: false, statusCode: 503, latencyMs: Date.now() - startMs,
            wasRateLimited: false, requestBytes: rawBody.length, pacingMs
          });
          pool.releaseSuccess(key);
          return jsonResp(res, 503, { error: { message: `Model ${modelId} is temporarily degraded or unavailable on upstream`, type: 'invalid_request_error' } });
        }

        metrics.recordRequest({
          method, path, model: modelId, keyLabel: key.label,
          streaming: false, statusCode: 400, latencyMs: Date.now() - startMs,
          wasRateLimited: false, requestBytes: rawBody.length, pacingMs
        });
        pool.releaseSuccess(key);
        return jsonResp(res, 400, errBody || { error: { message: respText || 'Bad Request', type: 'invalid_request_error' } });
      }

      // (BUGFIX audit-2026-06-30 R-404: 404 = model not found on upstream, NOT transient.
      // Retrying 404 across all keys wastes capacity and causes P95=41s latency spikes.)
      const isRetryableError = (resp.status >= 500) || [401, 403].includes(resp.status);
      if (isRetryableError && attempt < maxAttempts - 1 && _budgetLeft() > 300) {
        console.warn(`[UPSTREAM ERROR] status: ${resp.status} for model: ${modelId} on key: ${key.label} — retrying next key`);
        const cooldown = [401, 403].includes(resp.status) ? 3600 : 15;
        pool.releaseRateLimited(key, cooldown);
        // (PATCH-004) classify — provider-level failures feed circuit breaker
        const _class = errorTaxonomy.classify({ status: resp.status, body: '', model: modelId, key: key.label, provider: PROVIDER_NAME });
        globalThis.__handleFailClassification?.(_class, { status: resp.status, model: modelId });
        attempt++;
        // (PATCH-006) jittered exponential backoff, capped
        await new Promise(resolve => setTimeout(resolve, retryBackoffMs(attempt)));
        continue;
      }

      const contentType = resp.headers.get('content-type') || '';
      if (isStreaming || contentType.includes('text/event-stream')) {
        res.writeHead(resp.status, {
          'Content-Type': contentType,
          'Cache-Control': 'no-cache',
          'Connection': 'keep-alive',
          'X-Accel-Buffering': 'no',
        });
        // (PATCH-005) install heartbeat writer on catchAll stream — OFF by default, env-gated
        installHeartbeatInterval(res);

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        const streamChunks = [];
        let seenDone = false;
        // (BUGFIX audit-2026-06-29: release guard for stream — see proxyOpenai pattern.)
        let ctStreamReleased = false;
        const releaseCatchStream = (mode) => {
          if (ctStreamReleased) return;
          ctStreamReleased = true;
          if (mode === 'failure') pool.releaseFailure(key);
          else if (mode === 'ratelimited') pool.releaseRateLimited(key, 10);
          else pool.releaseSuccess(key);
        };
        try {
          while (true) {
            if (req?.clientAbortSignal?.aborted) {
              try { reader.cancel('client-aborted').catch(() => {}); } catch {}
              releaseCatchStream('failure');
              if (!res.writableEnded) { try { res.end(); } catch {} }
              return;
            }
            const { done, value } = await reader.read();
            if (done) break;
            if (req?.clientAbortSignal?.aborted) {
              try { reader.cancel('client-aborted-late').catch(() => {}); } catch {}
              releaseCatchStream('failure');
              if (!res.writableEnded) { try { res.end(); } catch {} }
              return;
            }
            const chunkStr = decoder.decode(value, { stream: true });
            try {
              res.write(chunkStr);
            } catch (writeErr) {
              try { reader.cancel('client-write-failed').catch(() => {}); } catch {}
              console.warn('[STREAM WARN] CatchAll res.write failed:', writeErr?.message);
              releaseCatchStream('failure');
              return;
            }
            if (!seenDone && chunkStr.includes('data: [DONE]')) seenDone = true;
            streamChunks.push(chunkStr);
          }
          releaseCatchStream('success');
        } catch (streamErr) {
          console.warn('[STREAM ERROR] CatchAll stream interrupted:', streamErr?.message);
          try { reader.cancel('stream-err').catch(() => {}); } catch {}
          releaseCatchStream(streamErr?.name === 'AbortError' ? 'failure' : 'ratelimited');
        }
        // Ensure [DONE] sentinel if upstream didn't send it
        if (!seenDone && !req?.clientAbortSignal?.aborted) {
          try { res.write('data: [DONE]\n\n'); } catch {}
        }
        try { res.end(); } catch {}

        let lastUsage = null;
        try {
          try {
            const lines = streamChunks.join('').split('\n');
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
          } catch { console.warn('[STREAM WARN] handleCatchAll usage parse failed from stream buffer'); }

          const { pt, ct, tt, cacht } = extractUsageFields(lastUsage);
          metrics.recordRequest({
            method, path, model: modelId, keyLabel: key.label,
            streaming: true, statusCode: resp.status, latencyMs: Date.now() - startMs,
            promptTokens: pt, completionTokens: ct, cachedTokens: cacht, totalTokens: tt,
            wasRateLimited: false, requestBytes: rawBody.length, pacingMs
          });
        } catch (err) {
          console.error('[METRICS ERROR] Failed to record request metrics in handleCatchAll:', err.message);
        }
        // (BUGFIX: release handled by releaseCatchStream above — do not double release.)
        return;
      }

      if (contentType.includes('application/json')) {
        let responseData;
        const respText = await resp.text();
        try {
          responseData = JSON.parse(respText);
        } catch {
          responseData = respText;
        }

        // Normalize 404 errors from upstream to our standard error format
        if (resp.status === 404) {
          const errorMessage = responseData?.error?.message || responseData?.message || `Model ${modelId} not found`;
          metrics.recordRequest({
            method, path, model: modelId, keyLabel: key.label,
            streaming: false, statusCode: 404, latencyMs: Date.now() - startMs,
            wasRateLimited: false, requestBytes: rawBody.length, pacingMs
          });
          pool.releaseSuccess(key);
          return jsonResp(res, 404, { error: { message: errorMessage, type: 'invalid_request_error' } });
        }

        const { pt, ct, tt, cacht } = extractUsageFields(responseData ? responseData.usage : null);
        metrics.recordRequest({
          method, path, model: modelId, keyLabel: key.label,
          streaming: false, statusCode: resp.status, latencyMs: Date.now() - startMs,
          promptTokens: pt, completionTokens: ct, cachedTokens: cacht, totalTokens: tt,
          wasRateLimited: false, requestBytes: rawBody.length, pacingMs
        });
        pool.releaseSuccess(key);
        return jsonResp(res, resp.status, responseData);
      } else {
        // Binary response (ASR/TTS, images, audio, video) in catch-all
        res.writeHead(resp.status, {
          'Content-Type': contentType,
          'Content-Length': resp.headers.get('content-length') || undefined
        });
        const reader = resp.body.getReader();
        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            res.write(value);
          }
        } catch (e) {
          console.error('Binary stream error in handleCatchAll:', e);
        }
        res.end();
        metrics.recordRequest({
          method, path, model: modelId, keyLabel: key.label,
          streaming: false, statusCode: resp.status, latencyMs: Date.now() - startMs,
          wasRateLimited: false, requestBytes: rawBody.length, pacingMs
        });
        pool.releaseSuccess(key);
        return;
      }
    } catch (e) {
      if (attempt < maxAttempts - 1 && _budgetLeft() > 250) {
        console.warn(`[NETWORK ERROR] ${e.message} — retrying next key (budget-rem=${_budgetLeft()}ms)`);
        pool.releaseRateLimited(key, 10);
        // (PATCH-004) classify network errors — they DON'T trigger provider circuit (only provider class does)
        const _class = errorTaxonomy.classify({ status: 0, body: e.message, model: modelId, key: key.label, provider: PROVIDER_NAME });
        if (_class !== 'network') globalThis.__handleFailClassification?.(_class, { status: 0, model: modelId });
        attempt++;
        // (PATCH-006) jittered exponential backoff
        await new Promise(resolve => setTimeout(resolve, retryBackoffMs(attempt)));
        continue;
      }
      metrics.recordRequest({
        method, path, model: modelId, keyLabel: key.label,
        streaming: isStreaming, statusCode: e.name === 'TimeoutError' ? 408 : 502, latencyMs: Date.now() - startMs,
        wasRateLimited: false, requestBytes: rawBody.length, pacingMs
      });
      pool.releaseFailure(key);
      if (!res.headersSent) {
        return jsonResp(res, e.name === 'TimeoutError' ? 408 : 502, { error: { message: e.message, type: 'upstream_error' } });
      }
      return;
    }
  }
}

// ── Router ──────────────────────────────────────────────────────────────
// Track response lifecycle cleanly so client-disconnect abort is never duplicated
// or fired after the response has already finished. Multiple close listeners on the
// same res can otherwise cause AbortController.abort() to no-op silently.
// (BUGFIX audit-2026-06-30 R8: deduplicate lifecycle signals with a single shot state guard.)
async function handleRequest(req, res) {
  const controller = new AbortController();
  req.clientAbortSignal = controller.signal;
  let aborted = false;
  let resClosed = false;
  const onResClose = () => {
    if (resClosed) return;
    resClosed = true;
    if (!res.writableEnded && !aborted) {
      aborted = true;
      try { controller.abort(); } catch {}
    }
  };
  res.on('close', onResClose);
  // Cleanup: null-ify signal after response to allow GC of AbortController
  res.on('finish', () => {
    if (!resClosed) { resClosed = true; aborted = true; }
    req.clientAbortSignal = null;
  });

  // Bearer token auth removed — open access for local usage

  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  const path = url.pathname;
  const method = req.method;

  try {
    // ─ Health / Meta ──
    if (method === 'GET' && path === '/health')        return handleHealth(res);
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
    if (method === 'GET' && (path === '/version' || path === '/api/version')) {
      return jsonResp(res, 200, { version: `wrapper-nvidia-${VERSION}` });
    }
    if (method === 'GET' && path === '/api/tags') {
      const models = pool.modelsCached.map(mid => ({
        name: mid,
        model: mid,
        modified_at: '1970-01-01T00:00:00Z',
        size: 0,
        digest: '',
        details: {
          family: mid.includes('/') ? mid.split('/')[0] : mid,
          parameter_size: '',
          quantization_level: ''
        }
      }));
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
      const now = Date.now();
      if (_metricsCache._data && (now - _metricsCache._ts) < _metricsCache._ttl) {
        return jsonResp(res, 200, _metricsCache._data);
      }
      const summary = metrics.summary(windowStr);
      const totals = metrics.getTotalCounts();
      const data = { ...summary, ...totals, live_keys: pool.allStats ? pool.allStats() : [] };
      _metricsCache._data = data;
      _metricsCache._ts = now;
      return jsonResp(res, 200, data);
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
        return jsonResp(res, 200, d);
      }
      const catalog = buildCatalog(pool.modelsCached, BASE_LLM, BASE_GENAI);
      return jsonResp(res, 200, {
        object: 'list', models: catalog,
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
      if (!body || typeof body !== 'object' || Array.isArray(body)) {
        return jsonResp(res, 400, { error: { message: 'Invalid request: body must be a JSON object', type: 'invalid_request_error' } });
      }
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
      if (!body || typeof body !== 'object' || Array.isArray(body)) {
        return jsonResp(res, 400, { error: { message: 'Invalid request: body must be a JSON object', type: 'invalid_request_error' } });
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
      if (!body || typeof body !== 'object' || Array.isArray(body)) {
        return jsonResp(res, 400, { error: { message: 'Invalid request: body must be a JSON object', type: 'invalid_request_error' } });
      }
      
      // Avoid mutating caller's body object — use shallow clone
      if (!body.input_type) {
        body = { ...body, input_type: 'query' };
      }

      const modelId = body.model || '';
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
      if (!body || typeof body !== 'object' || Array.isArray(body)) {
        return jsonResp(res, 400, { error: { message: 'Invalid request: body must be a JSON object', type: 'invalid_request_error' } });
      }

      const modelId = body.model || '';
      if (modelId in RETIRED_MODELS || isModelUnavailable(modelId)) {
        return jsonResp(res, 404, { error: { message: `Model ${modelId} is retired or unavailable`, type: 'invalid_request_error' } });
      }

      const minDim = modelId.toLowerCase().includes('flux') ? 768 : 256;
      let w = body.width;
      let h = body.height;
      if (body.size && typeof body.size === 'string') {
        const parts = body.size.split('x');
        if (parts.length === 2) {
          w = parseInt(parts[0], 10) || w;
          h = parseInt(parts[1], 10) || h;
        }
      }
      w = parseInt(w, 10) || 1024;
      h = parseInt(h, 10) || 1024;

      if (w < minDim || h < minDim) {
        body.width = Math.max(w, minDim);
        body.height = Math.max(h, minDim);
        if (body.size) {
          body.size = `${body.width}x${body.height}`;
        }
      }

      return await proxyPost({
        req, res, body, rawBody: raw, modelId, path,
        getTargetUrl: (key) => {
          const desc = describe(modelId, BASE_LLM, BASE_GENAI);
          const ep = (desc.endpoints || [{}])[0];
          const targetBase = ep.base_url || BASE_GENAI;
          const targetPath = ep.path || '/v1/images/generations';
          return `${targetBase}${targetPath}`;
        }
      });
    }

    // ─ Ranking (rerank) ──
    if (method === 'POST' && path === '/v1/ranking') {
      const raw = await readBody(req);
      let body;
      try { body = JSON.parse(raw); } catch {
        return jsonResp(res, 400, { error: { message: 'Invalid JSON', type: 'invalid_request_error' } });
      }
      if (!body || typeof body !== 'object' || Array.isArray(body)) {
        return jsonResp(res, 400, { error: { message: 'Invalid request: body must be a JSON object', type: 'invalid_request_error' } });
      }

      const modelId = body.model || '';
      if (modelId in RETIRED_MODELS || isModelUnavailable(modelId)) {
        return jsonResp(res, 404, { error: { message: `Model ${modelId} is retired or unavailable`, type: 'invalid_request_error' } });
      }

      return await proxyPost({
        req, res, body, rawBody: raw, modelId, path: '/v1/ranking',
        getTargetUrl: () => `${BASE_GENAI}/v1/ranking`
      });
    }

    // ─ Fallback to Catch-all Proxy ──
    return await handleCatchAll(req, res, path, url);
  } catch (e) {
    if (e.message === 'Request entity too large') {
      jsonResp(res, 413, { error: { message: 'Request entity too large', type: 'invalid_request_error' } });
      try { req.destroy(); } catch {}
      return;
    }
    console.error(`[ERROR] ${e.message}`);
    jsonResp(res, 500, { error: { message: 'Internal server error', type: 'server_error' } });
  }
}

function loadConfigFromEnvFile() {
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
      const val = trimmed.slice(idx + 1).trim().replace(/['"]/g, '').split('#')[0].trim();
      if (!key || !val) continue;

      if (key.startsWith('NVIDIA_API_KEY')) {
        if (val.length >= 10) {
          config.keys.push(val);
        }
      } else if (key === 'SOFT_LIMIT_RPM') {
        const v = parseInt(val, 10); if (!isNaN(v)) config.softLimit = v;
      } else if (key === 'HARD_LIMIT_RPM') {
        const v = parseInt(val, 10); if (!isNaN(v)) config.hardLimit = v;
      } else if (key === 'QUEUE_LIMIT_PER_KEY_PER_SEC' || key === 'QUEUE_LIMIT') {
        const v = parseFloat(val); if (!isNaN(v)) config.queueLimit = v;
      } else if (key === 'MAX_QUEUE_SIZE') {
        const v = parseInt(val, 10); if (!isNaN(v)) config.maxQueueSize = v;
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

  // safeInterval is defined at file scope (above) — uses the same Relay loop guard.

  safeInterval(async () => {
    try {
      const config = loadConfigFromEnvFile();
      if (config.keys.length > 0) {
        try {
          await pool.syncKeys(config.keys);
          try {
            const historicalRequests = metrics.getAllTimeKeyRequests();
            pool.initializeKeyRequests(historicalRequests);
          } catch { console.warn('[INIT WARN] Failed to load historical key requests from DB'); }
        } catch (e) {
          console.error('[wrapper-nvidia] Background key reload error:', e?.message || e);
        }
      }
      try {
        await pool.syncLimits({
          soft: config.softLimit,
          hard: config.hardLimit,
          queueLimit: config.queueLimit,
          maxQueueSize: config.maxQueueSize
        });
      } catch (e) {
        console.error('[wrapper-nvidia] Background limit sync error:', e?.message || e);
      }
    } catch (e) {
      console.error(`[wrapper-nvidia] Background config reload error:`, e?.message || e);
    }
  }, keysReloadSec * 1000);
}

// (PATCH-004) provider circuit breaker helper — referenced by key_pool.acquireSlot
// Returns true if circuit is currently open (refuse traffic). Reads from errorTaxonomy.
globalThis.acquireProviderCircuitCheck = () => {
  if (!PROVIDER_CIRCUIT_ENABLED) return false;
  return errorTaxonomy.isProviderOpen(PROVIDER_NAME);
};

// (PATCH-004) — called from retry loops on each classifier result
function handleFailClassification(kind, ev) {
  if (!PROVIDER_CIRCUIT_ENABLED) return;
  if (kind !== 'provider') return;
  const opened = errorTaxonomy.recordProviderFail(PROVIDER_NAME);
  if (opened) {
    console.error(`[CIRCUIT-OPEN] ${PROVIDER_NAME} circuit OPEN for ${parseInt(process.env.PROVIDER_OPEN_MS || '120000', 10)}ms after ${errorTaxonomy._recentFails.get(PROVIDER_NAME)?.length} fails`);
  }
}
globalThis.__handleFailClassification = handleFailClassification;

// ── Start ───────────────────────────────────────────────────────────────
async function main() {
  pool.loadFromEnv();
  startKeyReload();

  const dbPath = process.env.METRICS_DB || DB_PATH;
  metrics = new Metrics(dbPath);
  await metrics.ready();

  try {
    const historicalRequests = metrics.getAllTimeKeyRequests();
    pool.initializeKeyRequests(historicalRequests);
  } catch (e) {
    console.error(`[wrapper-nvidia] Error initializing historical key request counts:`, e.message);
  }

  await pool.refreshModels();

  initUpstreamRoutes();

  await loadUnavailableModelsFromDb();

  metrics.prune(30);

  const server = http.createServer(handleRequest);
  // (BUGFIX audit-2026-06-30 R-handle: tighten timeouts to expose stalls fast.
  // Previous defaults: timeout=300000 (5 min) + keepAliveTimeout=75000 (75s).
  // These defaults caused Hermes / ILMA to silently hang for tens of seconds
  // waiting for the wrapper to respond after an aborted upstream call.
  // New defaults: server.timeout hard cap at SERVER_REQUEST_TIMEOUT_MS,
  // keepAliveTimeout short enough to recover sockets but long enough for
  // streaming responses, headersTimeout bounds slow-client hangs.)
  const SERVER_REQUEST_TIMEOUT_MS = parseInt(process.env.SERVER_REQUEST_TIMEOUT_MS || '60000', 10);
  const SERVER_KEEPALIVE_TIMEOUT_MS = parseInt(process.env.SERVER_KEEPALIVE_TIMEOUT_MS || '10000', 10);
  const SERVER_HEADERS_TIMEOUT_MS = parseInt(process.env.SERVER_HEADERS_TIMEOUT_MS || '15000', 10);
  server.timeout = SERVER_REQUEST_TIMEOUT_MS;
  server.keepAliveTimeout = SERVER_KEEPALIVE_TIMEOUT_MS;
  server.headersTimeout = Math.max(SERVER_HEADERS_TIMEOUT_MS, SERVER_KEEPALIVE_TIMEOUT_MS + 1000);
  server.maxHeadersCount = 100;
  server.requestTimeout = Math.max(SERVER_REQUEST_TIMEOUT_MS, 1000);

  // Anti-silence guard: respond 504 fast if a handler hasn't started writing
  // anything within ANTI_SILENCE_TIMEOUT_MS. Without this, a hung upstream
  // fetch silently parked the response and Hermes tool wait loops spin forever.
  const ANTI_SILENCE_TIMEOUT_MS = parseInt(process.env.ANTI_SILENCE_TIMEOUT_MS || '45000', 10);
  const originalHandleRequest = handleRequest;
  const handleRequestWithSilenceGuard = (req, res) => {
    const silenceTimer = setTimeout(() => {
      if (!res.headersSent && !res.writableEnded) {
        console.error(`[ANTI-SILENCE] No response in ${ANTI_SILENCE_TIMEOUT_MS}ms for ${req.method} ${req.url} from ${req.socket?.remoteAddress}`);
        try {
          res.writeHead(504, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({
            error: {
              message: `wrapper-nvidia silence watchdog fired after ${ANTI_SILENCE_TIMEOUT_MS}ms`,
              type: 'timeout_error',
              evidence: 'ANTI_SILENCE_TIMEOUT_MS env',
            },
          }));
        } catch {}
      }
    }, ANTI_SILENCE_TIMEOUT_MS);
    res.on('close', () => clearTimeout(silenceTimer));
    res.on('finish', () => clearTimeout(silenceTimer));
    return originalHandleRequest(req, res);
  };
  // Replace server handler with guarded version — wrap createServer around this.
  const guardedServer = http.createServer(handleRequestWithSilenceGuard);
  guardedServer.timeout = SERVER_REQUEST_TIMEOUT_MS;
  guardedServer.keepAliveTimeout = SERVER_KEEPALIVE_TIMEOUT_MS;
  guardedServer.headersTimeout = Math.max(SERVER_HEADERS_TIMEOUT_MS, SERVER_KEEPALIVE_TIMEOUT_MS + 1000);
  guardedServer.maxHeadersCount = 100;
  guardedServer.requestTimeout = SERVER_REQUEST_TIMEOUT_MS;

  guardedServer.listen(BETA_PORT, BIND_HOST, () => {
    console.log(`[wrapper-nvidia] v${VERSION} listening on ${BIND_HOST}:${BETA_PORT}`);
    console.log(`[wrapper-nvidia] Keys: ${pool.totalKeys} total, ${pool.availableKeys} available`);
    console.log(`[wrapper-nvidia] Models cached: ${pool.modelsCached.length}`);
    console.log(`[wrapper-nvidia] Upstream: LLM=${BASE_LLM}`);
    console.log(`[wrapper-nvidia] Metrics DB: ${dbPath}`);
    console.log(`[wrapper-nvidia] Hardening: server.timeout=${SERVER_REQUEST_TIMEOUT_MS}ms keepAlive=${SERVER_KEEPALIVE_TIMEOUT_MS}ms headers=${SERVER_HEADERS_TIMEOUT_MS}ms silenceGuard=${ANTI_SILENCE_TIMEOUT_MS}ms`);
  });

  // Process banner — proves stdout is wired to journalctl for ops visibility.
  console.log(`[wrapper-nvidia] PID=${process.pid} listening — startup OK`);

  pool.startModelRefresh();

  if (VERIFY_ON_BOOT) {
    verifyLoop();
  }

  // Guard async setTimeout — unhandled rejection in metrics.prune would kill the wrapper.
  // (BUGFIX audit-2026-06-30 R7: pass a sync arrow so setInterval never sees a rejected promise.)
  safeInterval(() => {
    try { metrics.prune(30); } catch (e) { console.error('[METRICS PRUNE ERROR]', e?.message || e); }
  }, 6 * 3600 * 1000);

  const shutdown = () => {
    console.log('[wrapper-nvidia] Shutting down...');
    try { metrics?.close(); } catch (e) { console.error('[shutdown] metrics.close err:', e?.message); }
    try {
      guardedServer.close(() => process.exit(0));
    } catch (e) {
      console.error('[shutdown] server.close err:', e?.message);
      process.exit(0);
    }
    // Hard exit if connections don't drain in time
    setTimeout(() => {
      console.warn('[wrapper-nvidia] Shutdown hard-exiting after 10s timeout');
      process.exit(0);
    }, 10000).unref();
  };
  process.on('SIGTERM', shutdown);
  process.on('SIGINT',  shutdown);
}

main().catch(e => {
  console.error(`[wrapper-nvidia] Fatal: ${e.message}`);
  process.exit(1);
});
