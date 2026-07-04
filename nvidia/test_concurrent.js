#!/usr/bin/env node
/**
 * Concurrent load test — simulates multiple AI agents hitting the wrapper simultaneously.
 *
 * Agents:
 *   - Claude Code      (Anthropic /v1/messages, streaming + non-streaming)
 *   - Kilo Code / OpenCode  (OpenAI /v1/chat/completions)
 *   - Hermes Agent     (Ollama /api/chat  +  /api/generate)
 *
 * Usage: node test_concurrent.js [url]
 *   default url: http://localhost:9100
 */

const http = require('http');

const BASE = process.argv[2] || 'http://localhost:9100';
const MODEL = 'nvidia/nemotron-mini-4b-instruct';

let passed = 0;
let failed = 0;
const errors = [];

function jsonReq(method, path, body) {
  return new Promise((resolve, reject) => {
    const url = new URL(path, BASE);
    const opts = {
      method,
      hostname: url.hostname,
      port: url.port,
      path: url.pathname,
      headers: {},
      timeout: 90000,
    };
    if (body) {
      opts.headers['Content-Type'] = 'application/json';
    }
    const req = http.request(opts, (res) => {
      let data = '';
      res.on('data', c => data += c);
      res.on('end', () => {
        try {
          const parsed = JSON.parse(data);
          resolve({ status: res.statusCode, headers: res.headers, data: parsed });
        } catch {
          resolve({ status: res.statusCode, headers: res.headers, data });
        }
      });
    });
    req.on('error', reject);
    req.on('timeout', () => { req.destroy(); reject(new Error('timeout')); });
    if (body) {
      req.write(JSON.stringify(body));
    }
    req.end();
  });
}

function check(label, condition, detail) {
  if (condition) {
    passed++;
    console.log(`  ✓ ${label}`);
  } else {
    failed++;
    const msg = `  ✗ ${label}: ${detail}`;
    console.error(msg);
    errors.push(msg);
  }
}

async function testOpenAIChat() {
  const res = await jsonReq('POST', '/v1/chat/completions', {
    model: MODEL,
    messages: [{ role: 'user', content: 'Count from 1 to 3' }],
    max_tokens: 30,
    temperature: 0.1,
  });
  check('OpenAI chat: status 200', res.status === 200, `got ${res.status}`);
  const content = res.data?.choices?.[0]?.message?.content || '';
  check('OpenAI chat: has content', content.length > 0, `empty content`);
  check('OpenAI chat: has usage', !!res.data?.usage, `no usage`);
  check('OpenAI chat: has rate-limit headers',
    res.headers['x-ratelimit-limit'] !== undefined, `no X-RateLimit-Limit`);
  check('OpenAI chat: has model in response', res.data?.model === MODEL,
    `model mismatch: ${res.data?.model}`);
}

async function testAnthropicMessages() {
  const res = await jsonReq('POST', '/v1/messages', {
    model: MODEL,
    messages: [{ role: 'user', content: 'Count from 1 to 3' }],
    max_tokens: 30,
  });
  check('Anthropic: status 200', res.status === 200, `got ${res.status}`);
  const content = res.data?.content?.[0]?.text || '';
  check('Anthropic: has content', content.length > 0, `empty content`);
  check('Anthropic: has stop_reason', res.data?.stop_reason === 'end_turn' ||
    res.data?.stop_reason === 'stop' || res.data?.stop_reason === 'max_tokens',
    `unexpected stop_reason: ${res.data?.stop_reason}`);
  check('Anthropic: has usage', !!res.data?.usage, `no usage`);
  check('Anthropic: content array format',
    Array.isArray(res.data?.content), `content not array`);
  check('Anthropic: has model', res.data?.model === MODEL,
    `model mismatch: ${res.data?.model}`);
}

async function testOllamaChat() {
  const res = await jsonReq('POST', '/api/chat', {
    model: MODEL,
    messages: [{ role: 'user', content: 'Count from 1 to 3' }],
    stream: false,
    options: { temperature: 0.1, num_predict: 30 },
  });
  check('Ollama chat: status 200', res.status === 200, `got ${res.status}`);
  const content = res.data?.message?.content || '';
  check('Ollama chat: has content', content.length > 0, `empty content`);
  check('Ollama chat: has message.role',
    res.data?.message?.role === 'assistant', `role: ${res.data?.message?.role}`);
  check('Ollama chat: done=true', res.data?.done === true,
    `done: ${res.data?.done}`);
  check('Ollama chat: has eval_count',
    typeof res.data?.eval_count === 'number' && res.data.eval_count > 0,
    `eval_count: ${res.data?.eval_count}`);
}

async function testOllamaGenerate() {
  const res = await jsonReq('POST', '/api/generate', {
    model: MODEL,
    prompt: 'Count from 1 to 3',
    stream: false,
    options: { temperature: 0.1, num_predict: 30 },
  });
  check('Ollama gen: status 200', res.status === 200, `got ${res.status}`);
  const content = res.data?.response || '';
  check('Ollama gen: has content', content.length > 0, `empty content`);
  check('Ollama gen: done=true', res.data?.done === true,
    `done: ${res.data?.done}`);
  check('Ollama gen: has eval_count',
    typeof res.data?.eval_count === 'number' && res.data.eval_count > 0,
    `eval_count: ${res.data?.eval_count}`);
}

async function testHealth() {
  const res = await jsonReq('GET', '/health');
  check('Health: status 200', res.status === 200, `got ${res.status}`);
  check('Health: status=ok', res.data?.status === 'ok',
    `status: ${res.data?.status}`);
  check('Health: has keys array', Array.isArray(res.data?.keys),
    `no keys array`);
  check('Health: keys array non-empty', res.data?.keys?.length > 0,
    `empty keys`);
  check('Health: has models_cached',
    typeof res.data?.models_cached === 'number', `missing models_cached`);
}

async function testModels() {
  const res = await jsonReq('GET', '/v1/models');
  check('Models: status 200', res.status === 200, `got ${res.status}`);
  check('Models: has data array', Array.isArray(res.data?.data),
    `no data array`);
  check('Models: non-empty', res.data?.data?.length > 0,
    `empty`);
}

async function testStats() {
  const res = await jsonReq('GET', '/stats');
  check('Stats: status 200', res.status === 200, `got ${res.status}`);
  check('Stats: has total_keys', typeof res.data?.total_keys === 'number',
    `missing total_keys`);
  check('Stats: has keys array', Array.isArray(res.data?.keys),
    `no keys`);
}

async function concurrentBurst(count = 5) {
  const tasks = [];
  for (let i = 0; i < count; i++) {
    tasks.push(testOpenAIChat());
    tasks.push(testOllamaChat());
    tasks.push(testOllamaGenerate());
  }
  await Promise.all(tasks);
  console.log(`  Concurrent burst: ${count} rounds × 3 agent types = ${count * 3} requests done`);
}

// ── Main ──
async function main() {
  console.log(`\n=== Concurrent Agent Load Test ===`);
  console.log(`Target: ${BASE}`);
  console.log(`Model:  ${MODEL}\n`);

  // Warmup — single request to wake up caches
  console.log('Warmup...');
  try {
    const w = await jsonReq('POST', '/v1/chat/completions', {
      model: MODEL,
      messages: [{ role: 'user', content: 'ping' }],
      max_tokens: 1,
      temperature: 0.1,
    });
    if (w.status === 200) console.log('Server is alive.');
    else { console.error(`Server returned ${w.status}. Aborting.`); process.exit(1); }
  } catch (e) {
    console.error(`Cannot connect to ${BASE}: ${e.message}. Is the server running?`);
    process.exit(1);
  }

  // Sequential tests (single agent)
  console.log('\n--- Sequential Single-Agent Tests ---');
  await testHealth();
  await testModels();
  await testStats();
  await testOpenAIChat();
  await testAnthropicMessages();
  await testOllamaChat();
  await testOllamaGenerate();

  // Concurrent: burst from all agent types at once
  console.log('\n--- Concurrent Multi-Agent Burst ---');
  await concurrentBurst(8);

  // Concurrent: mixed chat + generate + streaming from all agents
  console.log('\n--- Mixed Workload Burst ---');
  const mixed = [];
  for (let i = 0; i < 6; i++) {
    mixed.push(testOpenAIChat());
    mixed.push(testAnthropicMessages());
    mixed.push(testOllamaChat());
    mixed.push(testOllamaGenerate());
  }
  await Promise.all(mixed);
  console.log('  Mixed burst complete.');

  // Summary
  console.log(`\n=== Results ===`);
  console.log(`  Passed: ${passed}`);
  console.log(`  Failed: ${failed}`);
  if (errors.length > 0) {
    console.error(`\nErrors:`);
    for (const e of errors) console.error(`  ${e}`);
    process.exit(1);
  }
  console.log('  All concurrent tests passed!');
}

main().catch(e => {
  console.error('Fatal:', e.message);
  process.exit(1);
});
