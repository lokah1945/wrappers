/**
 * e2e-mock.js — Comprehensive end-to-end test against a LOCAL mock NVIDIA upstream.
 *
 * The live NVIDIA upstream (integrate.api.nvidia.com) is blackholed from this
 * environment, so true live E2E isn't possible. This harness spins up a faithful
 * mock of the NIM API surface (chat completions stream + non-stream, embeddings,
 * ranking, image generation, /v1/models) and drives the wrapper against it to
 * validate EVERY feature surface:
 *   - /health
 *   - /v1/models → exact NVIDIA NIM ids (clean list); ?gateway=1 → claude-* routing ids labelled with exact NIM names for Claude Code picker + NGC-synced context windows
 *   - OpenAI non-stream + stream (SSE)
 *   - Anthropic non-stream + stream (message_start/delta/stop)
 *   - Claude Code alias routing (haiku/sonnet/opus)
 *   - Gateway discovery alias (claude-<slug> → real NIM id)
 *   - Transparent error passthrough (real upstream status + type)
 *   - Tool calling (Anthropic tool_use ⇄ OpenAI tool_calls)
 *   - Extended thinking (Anthropic thinking block ⇄ OpenAI reasoning_content)
 *   - Embeddings
 *   - Ranking
 *   - Image generation passthrough (genai host)
 *   - Token counting (/v1/messages/count_tokens)
 *   - Context-length error verbatim passthrough (no custom envelope)
 *   - Ollama /api/tags discovery
 *   - Ollama /api/chat
 *   - Capabilities endpoint (/v1/capabilities)
 *
 * Run: node test/e2e-mock.js   (or: npm run test:e2e)
 */
const assert = require('assert');
const http = require('http');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '..');
const WRAPPER_PORT = 19941;
const MOCK_PORT = 19942; MOCK_HOST = "127.0.0.1";
const TOKEN = 'e2e-token';

// ── Mock NVIDIA upstream ────────────────────────────────────────────────────
function sseChunk(obj) { return `data: ${JSON.stringify(obj)}\n\n`; }

// Captured last translated request the wrapper forwarded to "NVIDIA" (so tests
// can assert the wrapper stripped/translated fields correctly before egress).
let lastChatBody = null;

function mockChat(body, res) {
  const model = body.model || 'unknown';
  lastChatBody = body;
  // Only the explicit error-passthrough test model 404s; everything else is served.
  if (model === 'does-not-exist/model') {
    res.writeHead(404, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify({ error: { message: 'Model not found', type: 'not_found_error' } }));
  }
  // Context-length error passthrough test: return real upstream-style error verbatim
  if (model === 'context-limit/model') {
    res.writeHead(400, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify({ error: { message: 'Requested context length of 200001 exceeds the limit of 131072 tokens. Please reduce the input size.', type: 'invalid_request_error', param: 'messages' } }));
  }
  // 422 "Unknown parameter" test (mirrors nvidia/gliner-pii, which rejects top_p):
  // first request (still carrying the wrapper-injected top_p) gets a 422; once the
  // wrapper strips the offending param and retries, we serve 200. This proves the
  // proxyOpenai param-strip + retry path works for HTTP 422.
  if (model === 'reject-top_p/model') {
    if (body.top_p !== undefined) {
      res.writeHead(422, { 'Content-Type': 'application/json' });
      return res.end(JSON.stringify({
        detail: "Unknown parameter 'top_p' is not allowed. Please check the API documentation for valid parameters.",
        error: 'Invalid request parameters',
        details: [{ field: 'top_p', message: "Unknown parameter 'top_p' is not allowed." }],
      }));
    }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify({ id: 'chatcmpl-e2e', choices: [{ message: { role: 'assistant', content: 'stripped ok' }, finish_reason: 'stop' }], usage: { prompt_tokens: 1, completion_tokens: 2, total_tokens: 3 }, model }));
  }
  const usage = { prompt_tokens: 7, completion_tokens: 12, total_tokens: 19 };
  const hasTools = Array.isArray(body.tools) && body.tools.length > 0;
  const isReasoning = /deepseek/.test(model) || body.chat_template_kwargs || body.reasoning_effort;
  // Deterministic reasoning-only case (Nemotron-style): upstream emits ONLY a
  // reasoning_content delta and a blank final content delta — no assistant
  // text. The wrapper MUST inject a non-empty placeholder so OpenAI-compatible
  // clients never receive a hollow message (fix 2026-07-20, index.js:2355).
  const isReasoningOnly = model === 'reasoning-only/model';

  if (body.stream) {
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'Connection': 'keep-alive' });
    if (isReasoningOnly) {
      res.write(sseChunk({ choices: [{ delta: { reasoning_content: 'Let me reason about this carefully. ' }, finish_reason: null }] }));
      res.write(sseChunk({ choices: [{ delta: { content: '', finish_reason: null } }] }));
      res.write(sseChunk({ choices: [{ delta: {}, finish_reason: 'stop' }], usage }));
      res.write(sseChunk('[DONE]'));
      return res.end();
    }
    if (isReasoning) {
      res.write(sseChunk({ choices: [{ delta: { reasoning_content: 'Let me reason about this carefully. ' }, finish_reason: null }] }));
      res.write(sseChunk({ choices: [{ delta: { content: 'The answer is 42.' }, finish_reason: null }] }));
    } else if (hasTools) {
      res.write(sseChunk({ choices: [{ delta: { tool_calls: [{ index: 0, id: 'call_e2e1', type: 'function', function: { name: 'get_weather', arguments: '' } }] }, finish_reason: null }] }));
      res.write(sseChunk({ choices: [{ delta: { tool_calls: [{ index: 0, function: { arguments: '{"location":"NYC"}' } }] }, finish_reason: null }] }));
      res.write(sseChunk({ choices: [{ delta: {}, finish_reason: 'tool_calls' }], usage }));
    } else {
      res.write(sseChunk({ choices: [{ delta: { content: 'Hello' }, finish_reason: null }] }));
      res.write(sseChunk({ choices: [{ delta: { content: ' world' }, finish_reason: null }] }));
      res.write(sseChunk({ choices: [{ delta: {}, finish_reason: 'stop' }], usage }));
    }
    res.write(sseChunk('[DONE]'));
    return res.end();
  }

  // non-streaming
  let data;
  if (isReasoning) {
    data = { id: 'chatcmpl-e2e', choices: [{ message: { role: 'assistant', reasoning_content: 'Let me reason about this carefully.', content: 'The answer is 42.', finish_reason: 'stop' } }], usage, model };
  } else if (hasTools) {
    data = { id: 'chatcmpl-e2e', choices: [{ message: { role: 'assistant', content: null, tool_calls: [{ id: 'call_e2e1', type: 'function', function: { name: 'get_weather', arguments: '{"location":"NYC"}' } }], finish_reason: 'tool_calls' } }], usage, model };
  } else {
    data = { id: 'chatcmpl-e2e', choices: [{ message: { role: 'assistant', content: 'Hello world' }, finish_reason: 'stop' }], usage, model };
  }
  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(data));
}

function startMock() {
  return new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      const url = new URL(req.url, `http://127.0.0.1:${MOCK_PORT}`);
      let body = '';
      req.on('data', (c) => (body += c));
      req.on('end', () => {
        const json = body ? JSON.parse(body) : {};
        if (req.method === 'GET' && url.pathname === '/v1/models') {
          const models = [
            'meta/llama-3.1-8b-instruct', 'deepseek-ai/deepseek-v4-pro',
            'nvidia/nemotron-3-ultra-550b-a55b', 'black-forest-labs/flux.1-dev',
            'nvidia/nv-embedqa-e5-v5', 'nvidia/rerank-qa-mistral-4b',
          ];
          res.writeHead(200, { 'Content-Type': 'application/json' });
          return res.end(JSON.stringify({ data: models.map((id) => ({ id })) }));
        }
        if (req.method === 'POST' && url.pathname === '/v1/chat/completions') return mockChat(json, res);
        if (req.method === 'POST' && url.pathname === '/v1/embeddings') {
          const dim = 4096;
          const embedding = Array.from({ length: dim }, (_, i) => Math.sin(i) * 0.5);
          res.writeHead(200, { 'Content-Type': 'application/json' });
          return res.end(JSON.stringify({ data: [{ embedding, index: 0 }], model: json.model, usage: { prompt_tokens: 5, total_tokens: 5 } }));
        }
        if (req.method === 'POST' && url.pathname === '/v1/ranking') {
          res.writeHead(200, { 'Content-Type': 'application/json' });
          return res.end(JSON.stringify({ results: [{ index: 0, relevance_score: 0.91 }, { index: 1, relevance_score: 0.42 }], model: json.model }));
        }
        if (req.method === 'POST' && url.pathname.startsWith('/v1/genai/')) {
          res.writeHead(200, { 'Content-Type': 'application/json' });
          return res.end(JSON.stringify({ artifacts: [{ base64: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==', mime_type: 'image/png' }], model: json.model }));
        }
        // Default 404 for anything unexpected (used to test error passthrough)
        res.writeHead(404, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: { message: 'Model not found', type: 'not_found_error' } }));
      });
    });
    srv.listen(MOCK_PORT, "127.0.0.1", () => resolve(srv));
  });
}

// ── Helpers ────────────────────────────────────────────────────────────────
function startWrapper() {
  const env = {
    ...process.env,
    LISTEN_PORT: String(WRAPPER_PORT),
    BEARER_TOKEN: TOKEN,
    NVIDIA_API_KEY_1: 'nvapi-dummy-1',
    NVIDIA_BASE_URL: `http://127.0.0.1:${MOCK_PORT}`,
    NVIDIA_GENAI_URL: `http://127.0.0.1:${MOCK_PORT}`,
    NVIDIA_NVCF_URL: `http://127.0.0.1:${MOCK_PORT}`,
    METRICS_DB: path.join(ROOT, `metrics-e2e-${process.pid}.db`),
    VERIFY_ON_BOOT: 'false',
    VERIFY_INTERVAL: '3600',
    REGISTRY_REFRESH_SEC: '3600',
    CLAUDE_CODE_DEFAULT_HAIKU_MODEL: 'meta/llama-3.1-8b-instruct',
    CLAUDE_CODE_DEFAULT_SONNET_MODEL: 'deepseek-ai/deepseek-v4-pro',
    CLAUDE_CODE_DEFAULT_OPUS_MODEL: 'nvidia/nemotron-3-ultra-550b-a55b',
    PRE_RESPONSE_TIMEOUT_MS: '20000',
    // Exercise the DEFAULT_PARAMS injection + 422 param-strip retry path:
    WRAPPER_PARAMS: 'temperature,top_p',
    DEFAULT_TEMPERATURE: '0.7',
    DEFAULT_TOP_P: '1.0',
  };
  const child = spawn('node', [path.join(ROOT, 'src', 'index.js')], { env, stdio: 'ignore' });
  return child;
}

function waitHealthy(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tryOnce = async () => {
      try {
        const r = await fetch(`http://127.0.0.1:${WRAPPER_PORT}/health`, { headers: { Authorization: `Bearer ${TOKEN}` } });
        if (r.ok) return resolve();
      } catch {}
      if (Date.now() > deadline) return reject(new Error('wrapper not healthy'));
      setTimeout(tryOnce, 300);
    };
    tryOnce();
  });
}

const W = `http://127.0.0.1:${WRAPPER_PORT}`;
const post = (path, body, headers = {}) => fetch(W + path, {
  method: 'POST',
  headers: { Authorization: `Bearer ${TOKEN}`, 'Content-Type': 'application/json', ...headers },
  body: JSON.stringify(body),
});

// ── Test runner ────────────────────────────────────────────────────────────
const results = [];
async function check(name, fn) {
  try { await fn(); results.push([true, name]); console.log(`  ✔ ${name}`); }
  catch (e) { results.push([false, name, e.message]); console.log(`  ✗ ${name}: ${e.message}`); }
}

async function main() {
  const mock = await startMock();
  const wrapper = startWrapper();
  let failed = false;
  try {
    await waitHealthy(35000);
    // Allow the async model-cache refresh (from the mock /v1/models) to settle
    // so /v1/models and discovery aliases reflect the full catalog.
    await new Promise((r) => setTimeout(r, 3000));

    await check('Health check', async () => {
      const r = await fetch(`${W}/health`, { headers: { Authorization: `Bearer ${TOKEN}` } });
      assert.strictEqual(r.status, 200);
    });

    await check('/v1/models default: clean list (no claude-* duplicates)', async () => {
      const r = await fetch(`${W}/v1/models`, { headers: { Authorization: `Bearer ${TOKEN}` } });
      const j = await r.json();
      const ids = j.data.map((m) => m.id);
      // Default must NOT contain claude-* duplicates — clean NIM IDs only.
      assert.ok(!ids.some((id) => id.startsWith('claude-')), 'claude-* ids leaked in default response');
      assert.ok(ids.includes('meta/llama-3.1-8b-instruct'), 'original NIM id missing');
    });

    await check('/v1/models?gateway=1 emits claude-* routing ids labelled with exact NIM name', async () => {
      const r = await fetch(`${W}/v1/models?gateway=1`, { headers: { Authorization: `Bearer ${TOKEN}` } });
      const j = await r.json();
      const ids = j.data.map((m) => m.id);
      // Claude Code's picker ONLY displays ids beginning with "claude"/"anthropic"
      // and sends the selected id back as the model. So gateway mode must emit a
      // "claude-<slug>" routing id per model, labelled with display_name = exact NIM id.
      const alias = j.data.find((m) => m.id === 'claude-meta-llama-3.1-8b-instruct');
      assert.ok(alias, 'gateway mode must emit a claude-* routing id');
      assert.strictEqual(alias.display_name, 'meta/llama-3.1-8b-instruct', 'alias missing exact NIM display_name');
      assert.strictEqual(alias.original_id, 'meta/llama-3.1-8b-instruct', 'alias missing original_id');
      // The exact NIM id is still present (first entry) for OpenAI-compatible clients.
      assert.ok(ids.includes('meta/llama-3.1-8b-instruct'), 'exact NIM id missing in gateway mode');
    });

    await check('NGC-synced context window on /v1/models (deepseek-v4-pro=262144)', async () => {
      const r = await fetch(`${W}/v1/models`, { headers: { Authorization: `Bearer ${TOKEN}` } });
      const j = await r.json();
      // Default response now uses original NIM IDs (no claude-* prefix).
      const d = j.data.find((m) => m.id === 'deepseek-ai/deepseek-v4-pro' || m.original_id === 'deepseek-ai/deepseek-v4-pro');
      assert.ok(d, 'deepseek model not in catalog');
      // Registry seeds deepseek-v4-pro context=262144 (or live NGC); enrichModelMetadata
      // is invoked with the REAL id before the alias is applied, so it must propagate.
      assert.ok((d.context_window || d.contextWindow) >= 200000, `context_window too small: ${d.context_window}`);
    });

    await check('Keyless /v1/models (NO auth header) returns full catalog', async () => {
      // Discovery must work WITHOUT any API key/auth: the wrapper fetches
      // NVIDIA's /v1/models keyless-first, and /v1/models is a public endpoint.
      const r = await fetch(`${W}/v1/models`);
      assert.strictEqual(r.status, 200, 'keyless /v1/models should return 200');
      const j = await r.json();
      assert.ok(j.data && j.data.length > 0, 'keyless discovery returned no models');
    });

    await check('/v1/models exposes the correct call method per model', async () => {
      // Each model must advertise HOW to call it (base_url + endpoint + auth +
      // protocol) so clients can route correctly without hardcoding.
      const r = await fetch(`${W}/v1/models`, { headers: { Authorization: `Bearer ${TOKEN}` } });
      const j = await r.json();
      const chat = j.data.find((m) => m.id === 'meta/llama-3.1-8b-instruct');
      assert.ok(chat, 'llama model missing from catalog');
      assert.ok(chat.call, 'model is missing the call field');
      assert.strictEqual(chat.call.protocol, 'openai-compatible');
      assert.strictEqual(chat.call.endpoint, '/v1/chat/completions');
      assert.strictEqual(chat.call.method, 'POST');
      assert.strictEqual(chat.call.auth, 'Bearer');
      assert.ok(chat.call.base_url && chat.call.base_url.length > 0, 'call.base_url missing');
      const missing = j.data.filter((m) => !m.call || !m.call.endpoint);
      assert.strictEqual(missing.length, 0, `${missing.length} models missing call method`);
    });

    await check('OpenAI chat non-stream', async () => {
      const r = await post('/v1/chat/completions', { model: 'meta/llama-3.1-8b-instruct', messages: [{ role: 'user', content: 'hi' }] });
      const j = await r.json();
      assert.strictEqual(r.status, 200);
      assert.strictEqual(j.choices[0].message.content, 'Hello world');
      assert.ok(j.usage && j.usage.completion_tokens > 0, 'no usage');
    });

    await check('OpenAI chat stream (SSE)', async () => {
      const r = await post('/v1/chat/completions', { model: 'meta/llama-3.1-8b-instruct', messages: [{ role: 'user', content: 'hi' }], stream: true });
      assert.strictEqual(r.status, 200);
      const text = await r.text();
      assert.ok(text.includes('data:') && text.includes('[DONE]'), 'not SSE');
      assert.ok(text.includes('Hello') && text.includes('world'), 'content missing');
    });

    await check('OpenAI chat: developer role normalized to system (no NIM 500)', async () => {
      // OpenAI SDK / Codex / Hermes can send a `developer` role. NVIDIA NIM chat
      // templates reject it (HTTP 500, worse when combined with reasoning
      // toggles). The OpenAI-path sanitizer must fold developer->system and
      // merge consecutive system/developer content before egress.
      const r = await post('/v1/chat/completions', {
        model: 'meta/llama-3.1-8b-instruct',
        messages: [
          { role: 'developer', content: 'Be terse.' },
          { role: 'system', content: 'You are helpful.' },
          { role: 'user', content: 'hi' },
        ],
      });
      assert.strictEqual(r.status, 200);
      assert.ok(lastChatBody, 'mock never received a forwarded chat body');
      const roles = lastChatBody.messages.map((m) => m.role);
      assert.ok(!roles.includes('developer'), 'developer role leaked to upstream: ' + JSON.stringify(roles));
      const sysIdx = roles.indexOf('system');
      assert.ok(sysIdx >= 0 && roles[sysIdx + 1] !== 'system', 'consecutive system blocks not merged');
      const merged = roles.filter((x) => x === 'system').length;
      assert.strictEqual(merged, 1, 'expected a single merged system message, got ' + merged);
    });

    await check('OpenAI chat stream: reasoning-only upstream -> non-empty placeholder injected', async () => {
      // Upstream returns ONLY reasoning_content + empty content (Nemotron-style
      // reasoning-only). The proxy's streaming passthrough must append a final
      // content chunk with the placeholder so clients never get a hollow message.
      const r = await post('/v1/chat/completions', { model: 'reasoning-only/model', messages: [{ role: 'user', content: 'hi' }], max_tokens: 50, stream: true, chat_template_kwargs: { enable_thinking: true } });
      assert.strictEqual(r.status, 200);
      const text = await r.text();
      assert.ok(text.includes('[DONE]'), 'missing [DONE]');
      assert.ok(text.includes('No text response; the model returned reasoning only.'), 'reasoning-only stream did not get non-empty placeholder');
      assert.ok(text.includes('reasoning_content'), 'reasoning_content not forwarded');
    });

    await check('Anthropic messages non-stream', async () => {
      const r = await post('/v1/messages', { model: 'claude-llama-3-1-8b-instruct', messages: [{ role: 'user', content: 'hi' }], max_tokens: 50 });
      const j = await r.json();
      assert.strictEqual(r.status, 200);
      assert.strictEqual(j.type, 'message');
      const txt = j.content.filter((b) => b.type === 'text').map((b) => b.text).join('');
      assert.strictEqual(txt, 'Hello world');
    });

    await check('Anthropic messages stream (message_start/delta/stop)', async () => {
      const r = await post('/v1/messages', { model: 'claude-llama-3-1-8b-instruct', messages: [{ role: 'user', content: 'hi' }], max_tokens: 50, stream: true });
      assert.strictEqual(r.status, 200);
      const text = await r.text();
      assert.ok(text.includes('message_start'), 'no message_start');
      assert.ok(text.includes('content_block_delta'), 'no content_block_delta');
      assert.ok(text.includes('message_stop'), 'no message_stop');
    });

    await check('Claude Code alias routing (haiku→llama)', async () => {
      const r = await post('/v1/chat/completions', { model: 'haiku', messages: [{ role: 'user', content: 'hi' }] });
      const j = await r.json();
      assert.strictEqual(r.status, 200);
      assert.strictEqual(j.model, 'meta/llama-3.1-8b-instruct', `alias not resolved, got ${j.model}`);
    });

    await check('Gateway discovery alias (claude-<slug>→real id)', async () => {
      const r = await post('/v1/chat/completions', { model: 'claude-meta-llama-3.1-8b-instruct', messages: [{ role: 'user', content: 'hi' }] });
      const j = await r.json();
      assert.strictEqual(r.status, 200);
      assert.strictEqual(j.model, 'meta/llama-3.1-8b-instruct', 'discovery alias not resolved');
    });

    await check('Transparent error passthrough (404 not_found_error)', async () => {
      const r = await post('/v1/chat/completions', { model: 'does-not-exist/model', messages: [{ role: 'user', content: 'hi' }] });
      const j = await r.json();
      assert.strictEqual(r.status, 404, `expected 404, got ${r.status}`);
      assert.strictEqual(j.error.type, 'not_found_error', `expected not_found_error, got ${j.error?.type}`);
    });

    await check('Tool calling (Anthropic tool_use)', async () => {
      const r = await post('/v1/messages', {
        model: 'claude-llama-3-1-8b-instruct',
        messages: [{ role: 'user', content: 'weather?' }],
        max_tokens: 100,
        tools: [{ name: 'get_weather', description: 'g', input_schema: { type: 'object', properties: { location: { type: 'string' } } } }],
        tool_choice: { type: 'auto' },
      });
      const j = await r.json();
      assert.strictEqual(r.status, 200);
      const toolUse = j.content.find((b) => b.type === 'tool_use');
      assert.ok(toolUse, 'no tool_use block');
      assert.strictEqual(toolUse.name, 'get_weather');
      assert.strictEqual(toolUse.input.location, 'NYC');
    });

    await check('Claude Code extensions stripped before NIM (cache_control + tool_search)', async () => {
      // Claude Code (or any modern Anthropic client) routinely sends cache_control
      // and, when ENABLE_TOOL_SEARCH is on, tool_search_tool_* pseudo-tools. None
      // of these are understood by NVIDIA NIM. The wrapper must strip them so the
      // translated request that reaches NIM only contains fields it understands.
      const r = await post('/v1/messages', {
        model: 'claude-llama-3-1-8b-instruct',
        system: [{ type: 'text', text: 'You are helpful.', cache_control: { type: 'ephemeral' } }],
        messages: [{ role: 'user', content: 'weather?' }],
        max_tokens: 100,
        tools: [
          { type: 'tool_search_tool_regex_20251119', name: 'tool_search_tool_regex' },
          { name: 'get_weather', description: 'g', input_schema: { type: 'object', properties: { location: { type: 'string' } } }, defer_loading: true },
        ],
        tool_choice: { type: 'auto' },
      });
      const j = await r.json();
      assert.strictEqual(r.status, 200);
      // The mock NIM recorded the *translated* body it received:
      assert.ok(lastChatBody, 'mock never received a chat body');
      assert.ok(!JSON.stringify(lastChatBody).includes('cache_control'), 'cache_control leaked to upstream');
      assert.ok(!JSON.stringify(lastChatBody.tools || []).includes('tool_search'), 'tool_search_tool_* leaked to upstream');
      const toolUse = j.content.find((b) => b.type === 'tool_use');
      assert.ok(toolUse, 'no tool_use block returned to client');
      assert.strictEqual(toolUse.name, 'get_weather');
    });

    await check('Extended thinking (Anthropic thinking block)', async () => {
      const r = await post('/v1/messages', {
        model: 'claude-deepseek-v4-pro',
        messages: [{ role: 'user', content: 'what is 6*7?' }],
        max_tokens: 200,
        thinking: { type: 'enabled', budget_tokens: 100 },
      });
      const j = await r.json();
      assert.strictEqual(r.status, 200);
      const thinking = j.content.find((b) => b.type === 'thinking');
      assert.ok(thinking, 'no thinking block');
      assert.ok(thinking.thinking.includes('reason'), 'thinking empty');
    });

    await check('Embeddings (4096 dims)', async () => {
      const r = await post('/v1/embeddings', { model: 'nvidia/nv-embedqa-e5-v5', input: 'hello world' });
      const j = await r.json();
      assert.strictEqual(r.status, 200);
      assert.strictEqual(j.data[0].embedding.length, 4096);
    });

    await check('Ranking passthrough', async () => {
      const r = await post('/v1/ranking', { model: 'nvidia/rerank-qa-mistral-4b', query: 'q', passages: ['a', 'b'] });
      const j = await r.json();
      assert.strictEqual(r.status, 200);
      assert.ok(Array.isArray(j.results) && j.results.length === 2);
    });

    await check('Image generation passthrough (genai host)', async () => {
      const r = await post('/v1/images/generations', { model: 'black-forest-labs/flux.1-dev', prompt: 'a cat' });
      const j = await r.json();
      if (r.status !== 200) console.log('   [debug] image status', r.status, JSON.stringify(j).slice(0, 200));
      assert.strictEqual(r.status, 200);
      // Wrapper normalizes NIM genai {artifacts:[{base64}]} -> OpenAI {data:[{b64_json}]}
      const img = j.data && j.data[0];
      assert.ok(img && img.b64_json, 'no b64_json in image response: ' + JSON.stringify(j).slice(0, 200));
    });

    await check('Token counting (/v1/messages/count_tokens)', async () => {
      const r = await post('/v1/messages/count_tokens', {
        model: 'claude-llama-3-1-8b-instruct',
        messages: [{ role: 'user', content: 'hello world, this is a test message with some length' }],
      });
      const j = await r.json();
      assert.strictEqual(r.status, 200);
      assert.ok(j.input_tokens > 0, `expected positive input_tokens, got ${j.input_tokens}`);
    });

    await check('Context-length error verbatim passthrough (no custom envelope)', async () => {
      const r = await post('/v1/chat/completions', { model: 'context-limit/model', messages: [{ role: 'user', content: 'x'.repeat(200000) }] });
      const j = await r.json();
      assert.strictEqual(r.status, 400);
      // Must pass through the REAL upstream error message verbatim, not a wrapper-generated one.
      assert.ok(j.error.message.includes('exceeds the limit'), `expected verbatim upstream message, got: ${j.error.message}`);
      // Must NOT contain wrapper-generated friendly text.
      assert.ok(!j.error.message.includes('start a clean session'), `wrapper envelope leaked: ${j.error.message}`);
    });

    await check('422 unsupported-param stripped + retried (gliner-pii top_p)', async () => {
      // The wrapper injects top_p (WRAPPER_PARAMS) by default. The mock returns a
      // NVIDIA-style 422 "Unknown parameter 'top_p'" on the first attempt, then 200
      // once top_p has been stripped. This proves proxyOpenai handles HTTP 422 the
      // same as 400: parse the offending field, strip it, and retry transparently.
      const r = await post('/v1/chat/completions', { model: 'reject-top_p/model', messages: [{ role: 'user', content: 'hi' }] });
      const j = await r.json();
      assert.strictEqual(r.status, 200, `expected 200 after param strip+retry, got ${r.status}: ${JSON.stringify(j).slice(0, 160)}`);
      assert.strictEqual(j.choices[0].message.content, 'stripped ok');
      // The body that finally reached the mock must NOT contain top_p anymore.
      assert.ok(lastChatBody && lastChatBody.top_p === undefined, 'top_p was not stripped before retry');
    });


    await check('Ollama /api/tags discovery', async () => {
      const r = await fetch(`${W}/api/tags`, { headers: { Authorization: `Bearer ${TOKEN}` } });
      const j = await r.json();
      assert.strictEqual(r.status, 200);
      assert.ok(Array.isArray(j.models) && j.models.length > 0, 'no models in /api/tags');
      assert.ok(j.models.some(m => m.name.includes('llama')), 'llama model missing from tags');
    });

    await check('Ollama /api/chat non-stream', async () => {
      const r = await post('/api/chat', { model: 'meta/llama-3.1-8b-instruct', messages: [{ role: 'user', content: 'hi' }] });
      const j = await r.json();
      assert.strictEqual(r.status, 200);
      assert.strictEqual(j.message.role, 'assistant');
      assert.ok(j.message.content.length > 0, 'empty content');
      assert.strictEqual(j.done, true);
    });

    await check('Capabilities endpoint (/v1/capabilities)', async () => {
      const r = await fetch(`${W}/v1/capabilities`, { headers: { Authorization: `Bearer ${TOKEN}` } });
      const j = await r.json();
      assert.strictEqual(r.status, 200);
      assert.ok(Array.isArray(j.models) && j.models.length > 0, 'no models in capabilities');
      assert.ok(j.summary && j.summary.total > 0, 'no summary');
      // Verify a chat model has supports_parallel_tool_calls: false (NIM limitation)
      const chatModel = j.models.find(m => m.type === 'chat');
      assert.ok(chatModel, 'no chat model in capabilities');
      assert.strictEqual(chatModel.supports_parallel_tool_calls, false, 'parallel tool calls must be false for NIM');
    });

  } catch (e) {
    failed = true;
    console.error('FATAL:', e.message);
  } finally {
    wrapper.kill('SIGKILL');
    mock.close();
    try { fs.unlinkSync(path.join(ROOT, `metrics-e2e-${process.pid}.db`)); } catch {}
  }

  const passed = results.filter((r) => r[0]).length;
  console.log(`\n${passed}/${results.length} E2E checks passed.`);
  process.exit(failed || passed !== results.length ? 1 : 0);
}

main();
