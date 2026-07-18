const assert = require('assert');

const BASE_URL = 'http://127.0.0.1:9910';
const AUTH_HEADER = 'Bearer bearer-token-local';

async function request(path, options = {}) {
  const url = `${BASE_URL}${path}`;
  const headers = {
    'Authorization': AUTH_HEADER,
    'Content-Type': 'application/json',
    ...(options.headers || {})
  };
  const response = await fetch(url, {
    ...options,
    headers
  });
  return response;
}

const testResults = [];

function recordResult(name, status, details = '') {
  console.log(`[E2E] ${name}: ${status ? '✔ PASS' : '❌ FAIL'} ${details}`);
  testResults.push({ name, status, details });
}

async function testHealth() {
  try {
    const res = await request('/health');
    assert.strictEqual(res.status, 200, 'Health endpoint status should be 200');
    const data = await res.json();
    assert.strictEqual(data.status, 'ok', 'Health status should be ok');
    recordResult('Health Check', true);
  } catch (e) {
    recordResult('Health Check', false, e.message);
  }
}

async function testModelsList() {
  try {
    const res = await request('/v1/models');
    assert.strictEqual(res.status, 200, 'Models status should be 200');
    const data = await res.json();
    assert.ok(Array.isArray(data.data), 'Models data should be an array');
    const modelIds = data.data.map(m => m.id);
    assert.ok(modelIds.includes('meta/llama-3.1-8b-instruct'), 'Should include meta/llama-3.1-8b-instruct');
    
    // Verify dynamic context window mapping
    const llama = data.data.find(m => m.id === 'meta/llama-3.1-8b-instruct');
    assert.strictEqual(llama.context_window, 128000, 'meta/llama-3.1-8b-instruct context window should be 128000');
    
    recordResult('Models List & Context Window Heuristic', true, `Llama 3.1 8B context window: ${llama.context_window}`);
  } catch (e) {
    recordResult('Models List & Context Window Heuristic', false, e.message);
  }
}

async function testOpenAIChatNonStream() {
  try {
    const res = await request('/v1/chat/completions', {
      method: 'POST',
      body: JSON.stringify({
        model: 'meta/llama-3.1-8b-instruct',
        messages: [{ role: 'user', content: 'Respond with exactly the word SUCCESS' }],
        max_tokens: 10,
        temperature: 0.1
      })
    });
    assert.strictEqual(res.status, 200, 'Chat completions status should be 200');
    const data = await res.json();
    assert.ok(data.choices[0].message.content.includes('SUCCESS'), 'Response should contain SUCCESS');
    recordResult('OpenAI Chat Completion Non-Stream', true, `Response: ${data.choices[0].message.content.trim()}`);
  } catch (e) {
    recordResult('OpenAI Chat Completion Non-Stream', false, e.message);
  }
}

async function testOpenAIChatStream() {
  try {
    const res = await request('/v1/chat/completions', {
      method: 'POST',
      body: JSON.stringify({
        model: 'meta/llama-3.1-8b-instruct',
        messages: [{ role: 'user', content: 'Respond with exactly the word SUCCESS' }],
        max_tokens: 10,
        temperature: 0.1,
        stream: true
      })
    });
    assert.strictEqual(res.status, 200, 'Chat completions stream status should be 200');
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let text = '';
    let hasDone = false;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = decoder.decode(value);
      text += chunk;
      if (chunk.includes('[DONE]')) {
        hasDone = true;
      }
    }
    assert.ok(text.includes('SUCCESS'), 'Stream should contain SUCCESS');
    assert.ok(hasDone, 'Stream should end with [DONE]');
    recordResult('OpenAI Chat Completion Stream', true);
  } catch (e) {
    recordResult('OpenAI Chat Completion Stream', false, e.message);
  }
}

async function testAnthropicMessageNonStream() {
  try {
    const res = await request('/v1/messages', {
      method: 'POST',
      headers: {
        'x-api-key': 'bearer-token-local',
        'anthropic-version': '2023-06-01'
      },
      body: JSON.stringify({
        model: 'meta/llama-3.1-8b-instruct',
        messages: [{ role: 'user', content: 'Respond with exactly the word SUCCESS' }],
        max_tokens: 10,
        temperature: 0.1
      })
    });
    assert.strictEqual(res.status, 200, 'Messages completions status should be 200');
    const data = await res.json();
    assert.ok(data.content[0].text.includes('SUCCESS'), 'Response should contain SUCCESS');
    recordResult('Anthropic Messages Non-Stream', true, `Response: ${data.content[0].text.trim()}`);
  } catch (e) {
    recordResult('Anthropic Messages Non-Stream', false, e.message);
  }
}

async function testAnthropicMessageStream() {
  try {
    const res = await request('/v1/messages', {
      method: 'POST',
      headers: {
        'x-api-key': 'bearer-token-local',
        'anthropic-version': '2023-06-01'
      },
      body: JSON.stringify({
        model: 'meta/llama-3.1-8b-instruct',
        messages: [{ role: 'user', content: 'Respond with exactly the word SUCCESS' }],
        max_tokens: 10,
        temperature: 0.1,
        stream: true
      })
    });
    assert.strictEqual(res.status, 200, 'Messages stream status should be 200');
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let text = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      text += decoder.decode(value);
    }
    assert.ok(text.includes('message_start'), 'Stream should contain message_start event');
    assert.ok(text.includes('message_stop'), 'Stream should contain message_stop event');
    assert.ok(text.includes('SUCCESS'), 'Stream should contain SUCCESS content');
    recordResult('Anthropic Messages Stream', true);
  } catch (e) {
    recordResult('Anthropic Messages Stream', false, e.message);
  }
}

async function testEmbeddings() {
  try {
    const res = await request('/v1/embeddings', {
      method: 'POST',
      body: JSON.stringify({
        model: 'nvidia/nv-embed-v1',
        input: ['Hello world'],
        input_type: 'query'
      })
    });
    assert.strictEqual(res.status, 200, 'Embeddings status should be 200');
    const data = await res.json();
    assert.ok(data.data && data.data[0] && Array.isArray(data.data[0].embedding), 'Should return embedding vector');
    recordResult('Embeddings', true, `Vector size: ${data.data[0].embedding.length}`);
  } catch (e) {
    recordResult('Embeddings', false, e.message);
  }
}

async function generateReport() {
  const fs = require('fs');
  let md = '# TEST REPORT - E2E Integration Testing\n\n';
  md += `Date: ${new Date().toISOString()}\n\n`;
  md += '## Test Results\n\n';
  md += '| Test Case | Status | Details |\n';
  md += '| --- | --- | --- |\n';
  for (const r of testResults) {
    md += `| ${r.name} | ${r.status ? '✅ PASS' : '❌ FAIL'} | ${r.details} |\n`;
  }
  md += '\n\n## Conclusion\n\n';
  const allPass = testResults.every(r => r.status);
  md += allPass
    ? 'All integration tests passed successfully. The wrapper proxy is functioning as a robust, fully compatible transparent proxy for NVIDIA NIM.'
    : 'Some integration tests failed. Please check the logs above to identify and resolve issues.';
  
  fs.writeFileSync('TEST_REPORT.md', md);
  console.log('\n[E2E] Generated TEST_REPORT.md');
}

async function runAll() {
  console.log('=== Running Wrapper-NVIDIA E2E Tests ===\n');
  await testHealth();
  await testModelsList();
  await testOpenAIChatNonStream();
  await testOpenAIChatStream();
  await testAnthropicMessageNonStream();
  await testAnthropicMessageStream();
  await testEmbeddings();
  await generateReport();
}

runAll();
