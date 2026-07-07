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

const { KeyPool, NVIDIA_BASE_URL, NVIDIA_GENAI_URL, NVIDIA_NVCF_URL } = require('./key_pool');
const { anthropicToOpenai, openaiToAnthropic, streamOpenaiToAnthropic, estimateInputTokens, anthropicError } = require('./anthropic_compat');
const { classify, describe, buildCatalog, summarize, CAPABILITY_PARAMS, RETIRED_MODELS } = require('./capabilities');
const { Metrics } = require('./metrics');

// ── Fault Tolerance (Enterprise & Military Grade Resilience) ─────────────
process.on('uncaughtException', (err) => {
  console.error('[CRITICAL ERROR] Uncaught Exception:', err?.stack || err?.message || err);
});
process.on('unhandledRejection', (reason, promise) => {
  console.error('[CRITICAL ERROR] Unhandled Rejection at:', promise, 'reason:', reason?.stack || reason?.message || reason);
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

// Metrics dashboard cache (avoid blocking DB reads on every poll)
const _metricsCache = { _ts: 0, _ttl: 3000, _data: null }; // 3s TTL

// Proactive parameter stripping — silently remove known-incompatible params
// before the first upstream call (e.g. "think" — an Ollama-ism Hermes injects).
// The reactive auto-strip still catches anything else NVIDIA rejects at runtime.
const PROACTIVE_DROP = new Set(
  (process.env.DROP_PARAMS || 'think').split(',').map(s => s.trim()).filter(Boolean)
);

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
    // transient
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

async function verifyLoop() {
  await new Promise(resolve => setTimeout(resolve, 30000));
  while (true) {
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
  const maxAttempts = Math.max(MAX_RETRIES + 1, pool.totalKeys);
  while (attempt < maxAttempts) {
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

    const startMs = Date.now();
    try {
      const resp = await undiciFetch(url, {
        method: 'POST',
        headers,
        body: JSON.stringify(body),
        dispatcher: agent,
        signal: AbortSignal.timeout(parseInt(process.env.REQUEST_TIMEOUT_SEC || process.env.REQUEST_TIMEOUT || '60', 10) * 1000),
      });

      noteLiveResult('v1/chat/completions', modelId, resp.status);

      if (resp.status === 429) {
        pool.releaseSuccess(key);
        const ra = parseInt(resp.headers.get('retry-after') || '0', 10) || 65;
        let bodyText = '';
        try { bodyText = await resp.text(); } catch {}
        const [scope, reason] = await pool.registerRateLimit(key, modelId, ra, null, bodyText);
        metrics.recordRateLimitEvent({ keyLabel: key.label, model: modelId, retryAfterS: ra });
        if (attempt < maxAttempts - 1) {
          attempt++;
          // Fast rotation on rate limits: retry immediately with 50ms delay
          await new Promise(resolve => setTimeout(resolve, 50));
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

      const isRetryableError = (resp.status >= 500) || [401, 403, 404].includes(resp.status);
      if (isRetryableError && attempt < maxAttempts - 1) {
        console.warn(`[UPSTREAM ERROR] status: ${resp.status} for model: ${modelId} on key: ${key.label} — retrying next key`);
        const cooldown = [401, 403].includes(resp.status) ? 3600 : 15;
        pool.releaseRateLimited(key, cooldown);
        attempt++;
        await new Promise(resolve => setTimeout(resolve, Math.min(200 * attempt, 2000)));
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
      if (attempt < maxAttempts - 1) {
        console.warn(`[NETWORK ERROR] ${e.message} — retrying next key`);
        pool.releaseRateLimited(key, 10);
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
      pool.releaseFailure(key);
      return { status: e.name === 'TimeoutError' ? 408 : 502, data: { error: { message: `Network error: ${e.message}`, type: 'upstream_error' } } };
    }
  }
}

// ── Generic POST Helper (embeddings, images, ranking) ─────────────────
async function proxyPost({ req, res, body, rawBody, modelId, path, getTargetUrl }) {
  const strippedParams = new Set();
  let attempt = 0;
  const maxAttempts = Math.max(MAX_RETRIES + 1, pool.totalKeys);
  while (attempt < maxAttempts) {
    const keyResult = await pool.acquire(modelId, req?.clientAbortSignal);
    const key = keyResult ? keyResult.key : null;
    const pacingMs = keyResult ? keyResult.waitedMs : 0;

    if (!key) {
      return jsonResp(res, 503, { error: { message: 'All API keys exhausted', type: 'server_error' } });
    }

    const targetUrl = getTargetUrl(key);
    const startMs = Date.now();
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
        signal: AbortSignal.timeout(parseInt(process.env.REQUEST_TIMEOUT_SEC || process.env.REQUEST_TIMEOUT || '60', 10) * 1000),
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

      const isRetryableError = (resp.status >= 500) || [401, 403, 404].includes(resp.status);
      if (isRetryableError && attempt < maxAttempts - 1) {
        console.warn(`[UPSTREAM ERROR] status: ${resp.status} for model: ${modelId} on key: ${key.label} — retrying next key`);
        const cooldown = [401, 403].includes(resp.status) ? 3600 : 15;
        pool.releaseRateLimited(key, cooldown);
        attempt++;
        await new Promise(resolve => setTimeout(resolve, Math.min(200 * attempt, 2000)));
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
      if (attempt < maxAttempts - 1) {
        console.warn(`[NETWORK ERROR] ${e.message} — retrying next key`);
        pool.releaseRateLimited(key, 10);
        attempt++;
        await new Promise(resolve => setTimeout(resolve, Math.min(200 * attempt, 2000)));
        continue;
      }
      metrics.recordRequest({
        method: 'POST', path, model: modelId, keyLabel: key.label,
        streaming: false, statusCode: e.name === 'TimeoutError' ? 408 : 502, latencyMs: Date.now() - startMs,
        wasRateLimited: false, requestBytes: rawBody.length, pacingMs
      });
      pool.releaseFailure(key);
      return jsonResp(res, e.name === 'TimeoutError' ? 408 : 502, { error: { message: e.message, type: 'upstream_error' } });
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
      const reader = result.stream.getReader();
      const decoder = new TextDecoder();
      let lastUsage = null;
      const streamChunks = [];
      let seenDone = false;
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          const chunkStr = decoder.decode(value, { stream: true });
          res.write(chunkStr);
          if (!seenDone && chunkStr.includes('data: [DONE]')) seenDone = true;
          streamChunks.push(chunkStr);
        }
      } catch (streamErr) { console.warn('[STREAM WARN] proxyOpenai stream interrupted:', streamErr?.message); }
      // Only write [DONE] if upstream didn't already send it
      if (!seenDone) {
        res.write('data: [DONE]\n\n');
      }
      res.end();

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
      } finally {
        pool.releaseSuccess(result.key);
      }
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
  const maxAttempts = Math.max(MAX_RETRIES + 1, pool.totalKeys);
  while (attempt < maxAttempts) {
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
    try {
      const resp = await undiciFetch(targetUrl, {
        method,
        headers,
        body: isPost ? JSON.stringify(body) : undefined,
        dispatcher: agent,
        signal: AbortSignal.timeout(parseInt(process.env.REQUEST_TIMEOUT_SEC || process.env.REQUEST_TIMEOUT || '60', 10) * 1000),
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

      const isRetryableError = (resp.status >= 500) || [401, 403, 404].includes(resp.status);
      if (isRetryableError && attempt < maxAttempts - 1) {
        console.warn(`[UPSTREAM ERROR] status: ${resp.status} for model: ${modelId} on key: ${key.label} — retrying next key`);
        const cooldown = [401, 403].includes(resp.status) ? 3600 : 15;
        pool.releaseRateLimited(key, cooldown);
        attempt++;
        await new Promise(resolve => setTimeout(resolve, Math.min(200 * attempt, 2000)));
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

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        const streamChunks = [];
        let seenDone = false;
        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            const chunkStr = decoder.decode(value, { stream: true });
            res.write(chunkStr);
            if (!seenDone && chunkStr.includes('data: [DONE]')) seenDone = true;
            streamChunks.push(chunkStr);
          }
        } catch (streamErr) {
          console.warn('[STREAM ERROR] CatchAll stream interrupted:', streamErr?.message);
        }
        // Ensure [DONE] sentinel if upstream didn't send it
        if (!seenDone) {
          try { res.write('data: [DONE]\n\n'); } catch {}
        }
        res.end();

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
        } finally {
          pool.releaseSuccess(key);
        }
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
      if (attempt < maxAttempts - 1) {
        console.warn(`[NETWORK ERROR] ${e.message} — retrying next key`);
        pool.releaseRateLimited(key, 10);
        attempt++;
        await new Promise(resolve => setTimeout(resolve, Math.min(200 * attempt, 2000)));
        continue;
      }
      metrics.recordRequest({
        method, path, model: modelId, keyLabel: key.label,
        streaming: isStreaming, statusCode: e.name === 'TimeoutError' ? 408 : 502, latencyMs: Date.now() - startMs,
        wasRateLimited: false, requestBytes: rawBody.length, pacingMs
      });
      pool.releaseFailure(key);
      return jsonResp(res, e.name === 'TimeoutError' ? 408 : 502, { error: { message: e.message, type: 'upstream_error' } });
    }
  }
}

// ── Router ──────────────────────────────────────────────────────────────
async function handleRequest(req, res) {
  const controller = new AbortController();
  req.clientAbortSignal = controller.signal;
  let aborted = false;
  res.on('close', () => {
    if (!res.writableEnded && !aborted) {
      aborted = true;
      controller.abort();
    }
  });
  // Cleanup: null-ify signal after response to allow GC of AbortController
  res.on('finish', () => {
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

  setInterval(async () => {
    try {
      const config = loadConfigFromEnvFile();
      if (config.keys.length > 0) {
        await pool.syncKeys(config.keys);
        try {
          const historicalRequests = metrics.getAllTimeKeyRequests();
          pool.initializeKeyRequests(historicalRequests);
        } catch { console.warn('[INIT WARN] Failed to load historical key requests from DB'); }
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
  server.timeout = 300000;
  server.keepAliveTimeout = 75000;
  server.maxHeadersCount = 100;

  server.listen(BETA_PORT, BIND_HOST, () => {
    console.log(`[wrapper-nvidia] v${VERSION} listening on ${BIND_HOST}:${BETA_PORT}`);
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
    metrics.close();
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 5000);
  };
  process.on('SIGTERM', shutdown);
  process.on('SIGINT',  shutdown);
}

main().catch(e => {
  console.error(`[wrapper-nvidia] Fatal: ${e.message}`);
  process.exit(1);
});
