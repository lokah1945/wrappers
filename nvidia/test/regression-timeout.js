/**
 * regression-timeout.js — REVISI audit regression test.
 *
 * Proves the wrapper fails FAST (clean 504) when the upstream accepts the TCP
 * connection but never sends an HTTP response (the exact blackhole condition
 * that previously hung the wrapper for STREAM_REQUEST_TIMEOUT_SEC = 900s and
 * only surfaced as a client-disconnect `[close-abort]`).
 *
 * Strategy: spin up a local "blackhole" upstream that accepts connections but
 * never writes a response, point the wrapper at it via NVIDIA_BASE_URL, and
 * assert the wrapper returns HTTP 504 within PRE_RESPONSE_TIMEOUT_MS (+ slack)
 * instead of hanging.
 *
 * Run: node test/regression-timeout.js
 */
const assert = require('assert');
const http = require('http');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '..');
const WRAPPER_PORT = 9931;
const BLACKHOLE_PORT = 9932;
const PRE_RESP_MS = 5000; // short so the test is fast

function startBlackhole() {
  // Accept connections. Answer /v1/models quickly (so the wrapper's startup
  // model-refresh doesn't hang and block listening), but BLACKHOLE every other
  // path — i.e. accept the connection and never send a response. That is the
  // exact condition that previously hung the wrapper for 900s.
  return new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      if (req.url.startsWith('/v1/models')) {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ data: [] }));
        return;
      }
      /* intentionally never respond */
    });
    srv.listen(BLACKHOLE_PORT, () => resolve(srv));
  });
}

function waitForHealth(port, token, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tryOnce = async () => {
      try {
        const r = await fetch(`http://127.0.0.1:${port}/health`, { headers: { Authorization: `Bearer ${token}` } });
        if (r.ok) return resolve(true);
      } catch {}
      if (Date.now() > deadline) return reject(new Error('wrapper did not become healthy in time'));
      setTimeout(tryOnce, 300);
    };
    tryOnce();
  });
}

async function main() {
  const blackhole = await startBlackhole();
  const token = 'test-token-regression';
  const tmpDb = path.join(ROOT, `metrics-regression-${process.pid}.db`);
  const env = {
    ...process.env,
    LISTEN_PORT: String(WRAPPER_PORT),
    BEARER_TOKEN: token,
    NVIDIA_API_KEY_1: 'nvapi-dummy-key-1',
    NVIDIA_API_KEY_2: 'nvapi-dummy-key-2',
    NVIDIA_BASE_URL: `http://127.0.0.1:${BLACKHOLE_PORT}`,
    NVIDIA_GENAI_URL: `http://127.0.0.1:${BLACKHOLE_PORT}`,
    NVIDIA_NVCF_URL: `http://127.0.0.1:${BLACKHOLE_PORT}`,
    METRICS_DB: tmpDb,
    VERIFY_ON_BOOT: 'false',
    VERIFY_INTERVAL: '3600',
    PRE_RESPONSE_TIMEOUT_MS: String(PRE_RESP_MS),
    READ_BODY_TIMEOUT_MS: '5000',
  };

  const wrapper = spawn('node', [path.join(ROOT, 'src', 'index.js')], { env, stdio: 'ignore' });

  let failed = false;
  try {
    await waitForHealth(WRAPPER_PORT, token, 15000);

    const t0 = Date.now();
    const resp = await fetch(`http://127.0.0.1:${WRAPPER_PORT}/v1/chat/completions`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: 'meta/llama-3.1-8b-instruct',
        messages: [{ role: 'user', content: 'hi' }],
        max_tokens: 5,
      }),
    });
    const elapsed = Date.now() - t0;
    const body = await resp.json().catch(() => ({}));

    console.log(`  wrapper responded in ${elapsed}ms with HTTP ${resp.status}`);
    console.log(`  body: ${JSON.stringify(body)}`);

    assert.strictEqual(resp.status, 504, `expected 504, got ${resp.status}`);
    assert.strictEqual(body?.error?.type, 'upstream_error', `expected upstream_error, got ${body?.error?.type}`);
    assert.ok(elapsed < PRE_RESP_MS + 8000, `should fail fast (<${PRE_RESP_MS + 8000}ms), took ${elapsed}ms`);

    console.log('✔ regression-timeout: wrapper fails fast on blackholed upstream (504 within budget)');
  } catch (e) {
    failed = true;
    console.error('✗ regression-timeout FAILED:', e.message);
  } finally {
    wrapper.kill('SIGKILL');
    blackhole.close();
    try { fs.unlinkSync(tmpDb); } catch {}
  }

  process.exit(failed ? 1 : 0);
}

main();
