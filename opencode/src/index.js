#!/usr/bin/env node
const http = require('http');
const { URL } = require('url');
const path = require('path');
const fs = require('fs');

const WRAPPER_DIR = path.resolve(__dirname, '..');
const ENV_PATH = path.join(WRAPPER_DIR, '.env');

function loadDotenv() {
  if (!fs.existsSync(ENV_PATH)) return;
  const content = fs.readFileSync(ENV_PATH, 'utf8');
  for (const line of content.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const idx = trimmed.indexOf('=');
    if (idx === -1) continue;
    const key = trimmed.slice(0, idx).trim();
    const val = trimmed.slice(idx + 1).trim().replace(/['"]/g, '').split('#')[0].trim();
    if (key && val) process.env[key] = val;
  }
}

loadDotenv();

const { KeyPool } = require('./key_pool');

const pool = new KeyPool();
pool.loadFromEnv();

const HOST = process.env.LISTEN_HOST || '0.0.0.0';
let PORT = parseInt(process.env.LISTEN_PORT || '9101', 10);
const VERSION = '2.0.0';
const MAX_BODY_SIZE = parseInt(process.env.MAX_BODY_SIZE_MB || '10', 10) * 1024 * 1024;
const MODEL_REFRESH_SEC = parseInt(process.env.MODEL_REFRESH_SEC || '300', 10);
const MAX_RETRIES = parseInt(process.env.MAX_RETRIES || '5', 10);
const BEARER_TOKEN = (process.env.BEARER_TOKEN || '').trim();
const FREE_ONLY = process.env.FREE_ONLY?.toLowerCase() === 'true';

let inFlight = 0;
const unavailableModels = new Set();

function incInFlight() { inFlight++; }
function decInFlight() { if (inFlight > 0) inFlight--; }

function clientIp(req) {
  return req.headers['x-forwarded-for']?.split(',')[0]?.trim()
    || req.headers['x-real-ip']
    || req.socket?.remoteAddress
    || 'unknown';
}

function jsonResp(res, code, obj, keyLabel) {
  const body = JSON.stringify(obj);
  const headers = {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(body),
    'Access-Control-Allow-Origin': '*',
  };
  if (code < 400 && keyLabel) {
    pool.addRateLimitHeaders(headers, keyLabel);
  }
  res.writeHead(code, headers);
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    const limit = MAX_BODY_SIZE;
    const onData = (c) => {
      size += c.length;
      if (size > limit) {
        req.off('data', onData);
        req.destroy(new Error('Request entity too large'));
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

function validateChatBody(body) {
  if (!body || typeof body !== 'object') return 'Request body must be a JSON object';
  if (!body.model || typeof body.model !== 'string') return 'Model is required';
  if (!body.messages || !Array.isArray(body.messages) || body.messages.length === 0) return 'Messages array is required';
  for (const msg of body.messages) {
    if (!msg.role || !['system', 'user', 'assistant', 'tool'].includes(msg.role)) return `Invalid message role: ${msg.role}`;
  }
  return null;
}

function extractUsage(usage) {
  const u = usage || {};
  return {
    prompt_tokens: u.prompt_tokens || 0,
    completion_tokens: u.completion_tokens || 0,
    total_tokens: u.total_tokens || 0,
    cached_tokens: u.prompt_tokens_details?.cached_tokens || 0,
  };
}

async function handleChatCompletions(body, req, res) {
  const modelId = body.model || '';

  if (FREE_ONLY && !pool.isFreeModel(modelId)) {
    return jsonResp(res, 403, {
      error: {
        message: `Model '${modelId}' is not available. FREE_ONLY mode enabled. Available free models: ${pool.freeModelIds.join(', ')}`,
        type: 'invalid_request_error',
      },
    });
  }

  if (unavailableModels.has(modelId)) {
    return jsonResp(res, 404, {
      error: { message: `Model '${modelId}' is currently unavailable`, type: 'invalid_request_error' },
    });
  }

  const maxAttempts = Math.max(MAX_RETRIES, pool.totalKeys);

  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    const { key, retryAfter } = await pool.acquire(modelId);

    if (!key) {
      return jsonResp(res, 503, {
        error: {
          message: retryAfter ? `All API keys rate-limited. Retry after ${retryAfter}s` : 'No API keys available',
          type: 'server_error',
        },
      });
    }

    const startMs = Date.now();
    incInFlight();

    try {
      const result = await pool.proxyChat(body, key);

      if (result.status === 429) {
        const ra = result.retryAfter || 65;
        let bodyText = '';
        if (result.data) {
          try { bodyText = JSON.stringify(result.data); } catch {}
        }
        await pool.registerRateLimit(key, modelId, ra, null, bodyText);
        decInFlight();
        if (attempt < maxAttempts - 1) {
          await new Promise(r => setTimeout(r, 100));
          continue;
        }
        return jsonResp(res, 429, {
          error: { message: `Rate limited (retry-after ${ra}s)`, type: 'rate_limit_error' },
        });
      }

      if (result.stream) {
        try {
          const respHeaders = {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
            'Access-Control-Allow-Origin': '*',
          };
          pool.addRateLimitHeaders(respHeaders, key.label);
          res.writeHead(200, respHeaders);

          const upstream = result.stream;
          upstream.on('data', (chunk) => {
            try { res.write(chunk); } catch {}
          });
          upstream.on('end', () => {
            try { res.end(); } catch {}
            pool.release(key);
            decInFlight();
          });
          upstream.on('error', (e) => {
            console.error(`[wrapper-zen] Stream error: ${e.message}`);
            try { res.end(); } catch {}
            pool.release(key);
            decInFlight();
          });
          req.on('close', () => {
            upstream.destroy();
            pool.release(key);
            decInFlight();
          });
          return;
        } catch (e) {
          pool.release(key);
          decInFlight();
          console.error(`[wrapper-zen] Stream setup error: ${e.message}`);
          return jsonResp(res, 500, { error: { message: 'Stream error', type: 'server_error' } });
        }
      }

      decInFlight();

      if (result.status >= 500 && attempt < maxAttempts - 1) {
        console.warn(`[wrapper-zen] Upstream ${result.status} for ${modelId} — retrying`);
        pool.release(key);
        await new Promise(r => setTimeout(r, Math.min(200 * (attempt + 1), 2000)));
        continue;
      }

      if (!result.status || result.status >= 400) {
        const errMsg = result.data?.error?.message || `Upstream error ${result.status}`;
        const showStatus = result.status >= 500 ? 503 : result.status;
        pool.release(key);
        return jsonResp(res, showStatus, result.data || { error: { message: errMsg, type: 'upstream_error' } });
      }

      pool.release(key);
      return jsonResp(res, result.status, result.data, key.label);

    } catch (e) {
      decInFlight();
      if (attempt < maxAttempts - 1) {
        console.warn(`[wrapper-zen] Network error on ${key.label}: ${e.message} — retrying`);
        pool.release(key);
        await new Promise(r => setTimeout(r, Math.min(200 * (attempt + 1), 2000)));
        continue;
      }
      pool.release(key);
      return jsonResp(res, 502, {
        error: { message: `Upstream error: ${e.message}`, type: 'upstream_error' },
      });
    }
  }
}

function handleHealth(res) {
  jsonResp(res, 200, pool.healthJson());
}

function handleStats(res) {
  jsonResp(res, 200, {
    ...pool.healthJson(),
    keys: pool.allStats(),
    models_cached: pool.cachedModels.length,
    blocked_models: pool.blockedModels(),
    in_flight: inFlight,
  });
}

async function handleModels(res, url) {
  const force = url?.searchParams?.get('refresh') === 'true';
  await pool.refreshModels(force);
  let models = pool.cachedModels;
  if (FREE_ONLY) models = pool.filterModels(models);
  jsonResp(res, 200, { object: 'list', data: models });
}

function handleModelInfo(modelId, res) {
  const model = pool.cachedModels.find(m => m.id === modelId);
  if (model) return jsonResp(res, 200, model);
  const freeModel = pool.freeModelIds.find(m => m === modelId);
  if (freeModel) return jsonResp(res, 200, { id: freeModel, object: 'model' });
  jsonResp(res, 404, { error: { message: `Model '${modelId}' not found`, type: 'not_found' } });
}

function handleMetricsSummary(res) {
  const stats = pool.allStats();
  const totalReqs = stats.reduce((s, k) => s + k.total_requests, 0);
  const total429s = stats.reduce((s, k) => s + k.total_429s, 0);
  jsonResp(res, 200, {
    total_requests: totalReqs,
    total_rate_limits: total429s,
    in_flight: inFlight,
    available_keys: pool.availableKeys,
    total_keys: pool.totalKeys,
    blocked_models: pool.blockedModels(),
    keys: stats,
  });
}

function handleMetricsKeys(res) {
  jsonResp(res, 200, { keys: pool.allStats() });
}

function handleMetricsModels(res) {
  jsonResp(res, 200, {
    models_cached: pool.cachedModels.length,
    blocked_models: pool.blockedModels(),
    free_only: FREE_ONLY,
    free_models: FREE_ONLY ? pool.freeModelIds : [],
  });
}

function loadConfigFromEnvFile() {
  if (!fs.existsSync(ENV_PATH)) return { keys: [] };
  const content = fs.readFileSync(ENV_PATH, 'utf8');
  const config = { keys: [] };
  const seen = new Set();
  for (const line of content.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const idx = trimmed.indexOf('=');
    if (idx === -1) continue;
    const key = trimmed.slice(0, idx).trim();
    const val = trimmed.slice(idx + 1).trim().replace(/['"]/g, '').split('#')[0].trim();
    if (!key || !val) continue;
    if (key.startsWith('OPENCODE-ZEN_API_KEY') && val.length >= 5 && !seen.has(val)) {
      seen.add(val);
      config.keys.push(val);
    }
  }
  return config;
}

async function handleRequest(req, res) {
  const method = req.method;
  let url, pathname;
  try {
    url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
    pathname = url.pathname;
  } catch {
    return jsonResp(res, 400, { error: 'Invalid request URL' });
  }

  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS, PUT, DELETE');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Request-ID');

  if (method === 'OPTIONS') {
    res.writeHead(204);
    return res.end();
  }

  const startMs = Date.now();
  const requestId = `zen_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
  const ip = clientIp(req);
  console.log(`[${requestId}] ${method} ${pathname} from ${ip}`);

  const publicPaths = ['/health', '/metrics', '/', '/favicon.ico'];
  if (BEARER_TOKEN && !publicPaths.includes(pathname) && !pathname.startsWith('/metrics/')) {
    const auth = (req.headers.authorization || '').trim();
    if (auth.replace(/^Bearer\s+/i, '') !== BEARER_TOKEN) {
      console.warn(`[${requestId}] Auth failed`);
      return jsonResp(res, 401, { error: { message: 'Unauthorized', type: 'authentication_error' } });
    }
  }

  try {
    if (pathname === '/health') {
      return handleHealth(res);
    }

    if (pathname === '/stats') {
      return handleStats(res);
    }

    if (pathname === '/metrics') {
      return handleMetricsSummary(res);
    }
    if (pathname === '/metrics/keys') {
      return handleMetricsKeys(res);
    }
    if (pathname === '/metrics/models') {
      return handleMetricsModels(res);
    }
    if (method === 'POST' && pathname === '/metrics/reset') {
      pool.resetCounters();
      return jsonResp(res, 200, { status: 'ok' });
    }
    if (method === 'POST' && pathname === '/admin/heal-in-flight') {
      pool.healInFlight();
      inFlight = 0;
      return jsonResp(res, 200, { status: 'ok' });
    }

    if (pathname === '/v1/models') {
      if (method === 'GET') return await handleModels(res, url);
      return jsonResp(res, 405, { error: { message: 'Method not allowed', type: 'invalid_request_error' } });
    }

    if (pathname.startsWith('/v1/models/') && method === 'GET') {
      const modelId = decodeURIComponent(pathname.slice('/v1/models/'.length));
      return handleModelInfo(modelId, res);
    }

    if (method === 'POST' && pathname === '/v1/chat/completions') {
      const raw = await readBody(req);
      let body;
      try { body = JSON.parse(raw); } catch (e) {
        return jsonResp(res, 400, { error: { message: 'Invalid JSON body: ' + e.message, type: 'invalid_request_error' } });
      }

      const validationError = validateChatBody(body);
      if (validationError) {
        return jsonResp(res, 400, { error: { message: validationError, type: 'invalid_request_error' } });
      }

      return await handleChatCompletions(body, req, res);
    }

    jsonResp(res, 404, {
      error: { message: `Not found: ${method} ${pathname}`, type: 'not_found' },
    });

  } catch (e) {
    if (e.message === 'Request entity too large') {
      return jsonResp(res, 413, { error: { message: 'Request entity too large', type: 'invalid_request_error' } });
    }
    if (inFlight > 100) {
      console.warn(`[wrapper-zen] inFlight stuck at ${inFlight}, clamping`);
      inFlight = 0;
    }
    console.error(`[${requestId}] Error: ${e.message}`);
    jsonResp(res, 500, { error: { message: 'Internal server error', type: 'server_error' } });
  }
}

function startKeyReload() {
  const keysReloadSec = parseInt(process.env.KEYS_RELOAD_SECONDS || '60', 10);
  if (keysReloadSec <= 0) return;

  setInterval(async () => {
    try {
      const config = loadConfigFromEnvFile();
      loadDotenv();
      pool.freeOnly = process.env.FREE_ONLY?.toLowerCase() === 'true';
      if (config.keys.length > 0) {
        await pool.syncKeys(config.keys);
      }
    } catch (e) {
      console.error(`[wrapper-zen] Config reload error: ${e.message}`);
    }
  }, keysReloadSec * 1000);
}

async function main() {
  console.log(`[wrapper-zen] v${VERSION} starting...`);

  await pool.refreshModels();

  startKeyReload();

  const server = http.createServer(handleRequest);
  server.timeout = 300000;
  server.keepAliveTimeout = 75000;
  server.maxHeadersCount = 100;

  server.listen(PORT, HOST, () => {
    console.log(`[wrapper-zen] v${VERSION} listening on ${HOST}:${PORT}`);
    console.log(`[wrapper-zen] Keys: ${pool.totalKeys} total, ${pool.availableKeys} available`);
    console.log(`[wrapper-zen] Models cached: ${pool.cachedModels.length}`);
    console.log(`[wrapper-zen] FREE_ONLY: ${FREE_ONLY}`);
    console.log(`[wrapper-zen] BEARER_TOKEN auth: ${BEARER_TOKEN ? 'enabled' : 'disabled'}`);
  });

  setInterval(() => pool.refreshModels(), MODEL_REFRESH_SEC * 1000);

  const shutdown = () => {
    console.log('[wrapper-zen] Shutting down...');
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 5000);
  };
  process.on('SIGTERM', shutdown);
  process.on('SIGINT', shutdown);
}

main().catch(e => {
  console.error(`[wrapper-zen] Fatal: ${e.message}`);
  process.exit(1);
});
