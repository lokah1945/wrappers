const assert = require('assert');
const path = require('path');
const fs = require('fs');

// Set mock env variables for testing
process.env.SOFT_LIMIT_RPM = '15';
process.env.HARD_LIMIT_RPM = '25';
process.env.MAX_QUEUE_SIZE = '10';

const srcDir = path.join(__dirname, '..', 'src');
const rootDir = path.join(__dirname, '..');
const { KeyPool } = require(path.join(rootDir, 'key_pool'));
const { estimateInputTokens, anthropicToOpenai, openaiToAnthropic, streamOpenaiToAnthropic } = require(path.join(srcDir, 'anthropic_compat'));
const { classify, describe } = require(path.join(srcDir, 'capabilities'));
const { Metrics } = require(path.join(srcDir, 'metrics'));

async function testKeyPool() {
  console.log('Testing KeyPool...');
  const pool = new KeyPool();
  const testKeys = [
    'nvapi-key-test-1-xxxxxxxxxxxxxxxxx',
    'nvapi-key-test-2-xxxxxxxxxxxxxxxxx'
  ];
  
  await pool.syncKeys(testKeys);
  assert.strictEqual(pool.keys.length, 2, 'Should load 2 mock keys');
  
  const modelId = 'meta/llama-3.1-8b-instruct';
  const { key, waitedMs } = await pool.acquire(modelId);
  assert.ok(key, 'Should acquire key');
  assert.strictEqual(key.inFlight, 1, 'In-flight count should increment');
  
  pool.releaseSuccess(key);
  assert.strictEqual(key.inFlight, 0, 'In-flight count should decrement');
  console.log('✔ KeyPool tests passed successfully.');
}

function testAnthropicCompat() {
  console.log('Testing Anthropic compatibility...');
  
  // Test token estimation
  const payload = {
    messages: [
      { role: 'user', content: 'Hello' },
      { role: 'assistant', content: 'Hi there' }
    ]
  };
  const count = estimateInputTokens(payload);
  assert.ok(count > 0, 'Token count estimate should be positive');

  // Test messages translation
  const anthropicRequest = {
    model: 'claude-3-5-sonnet',
    max_tokens: 100,
    messages: [{ role: 'user', content: 'What is 1+1?' }]
  };
  const openaiRequest = anthropicToOpenai(anthropicRequest);
  assert.strictEqual(openaiRequest.max_tokens, 100);
  assert.strictEqual(openaiRequest.messages[0].content, 'What is 1+1?');
  
  const openaiResponse = {
    id: 'chatcmpl-123',
    model: 'meta/llama-3.1-8b-instruct',
    choices: [{
      message: { role: 'assistant', content: '2' },
      finish_reason: 'stop'
    }],
    usage: { prompt_tokens: 10, completion_tokens: 5 }
  };
  const anthropicResponse = openaiToAnthropic(openaiResponse, 'meta/llama-3.1-8b-instruct');
  assert.strictEqual(anthropicResponse.content[0].text, '2');
  assert.strictEqual(anthropicResponse.usage.input_tokens, 10);

  // Test Anthropic -> OpenAI with thinking block
  const requestWithThinking = {
    model: 'claude-3-5-sonnet',
    messages: [{
      role: 'user',
      content: [
        { type: 'thinking', thinking: 'Let me think' },
        { type: 'text', text: 'Hello' }
      ]
    }]
  };
  const openaiReqThinking = anthropicToOpenai(requestWithThinking);
  assert.ok(openaiReqThinking.messages[0].content.includes('<thinking>'), 'Should convert thinking block');
  assert.ok(openaiReqThinking.messages[0].content.includes('Let me think'), 'Should preserve thinking content');

  // Test OpenAI -> Anthropic with reasoning_content
  const oaiResponseReasoning = {
    choices: [{
      message: {
        role: 'assistant',
        content: '42',
        reasoning_content: 'Calculating the ultimate answer'
      }
    }]
  };
  const anthropicRespReasoning = openaiToAnthropic(oaiResponseReasoning, 'model');
  assert.strictEqual(anthropicRespReasoning.content[0].type, 'thinking');
  assert.strictEqual(anthropicRespReasoning.content[0].thinking, 'Calculating the ultimate answer');
  assert.strictEqual(anthropicRespReasoning.content[1].type, 'text');
  assert.strictEqual(anthropicRespReasoning.content[1].text, '42');

  // Test OpenAI -> Anthropic with XML <think> tags in content
  const oaiResponseXML = {
    choices: [{
      message: {
        role: 'assistant',
        content: '<think>\nThinking about life\n</think>\nBe happy!'
      }
    }]
  };
  const anthropicRespXML = openaiToAnthropic(oaiResponseXML, 'model');
  assert.strictEqual(anthropicRespXML.content[0].type, 'thinking');
  assert.strictEqual(anthropicRespXML.content[0].thinking, 'Thinking about life');
  assert.strictEqual(anthropicRespXML.content[1].type, 'text');
  assert.strictEqual(anthropicRespXML.content[1].text, 'Be happy!');

  // Test streamOpenaiToAnthropic
  const makeMockStream = (chunks) => {
    let index = 0;
    return {
      getReader() {
        return {
          read() {
            if (index < chunks.length) {
              const val = chunks[index++];
              return Promise.resolve({ done: false, value: new TextEncoder().encode(val) });
            }
            return Promise.resolve({ done: true, value: undefined });
          },
          releaseLock() {}
        };
      }
    };
  };

  const runStreamTest = async () => {
    const mockChunks = [
      'data: {"choices": [{"delta": {"reasoning_content": "Think" }}]}\n',
      'data: {"choices": [{"delta": {"content": "Hello" }}]}\n'
    ];
    const capture = { _startMs: Date.now() };
    const sseGen = streamOpenaiToAnthropic(makeMockStream(mockChunks), 'model', capture);
    const events = [];
    for await (const chunk of sseGen) {
      events.push(chunk);
    }
    assert.ok(events.some(e => e.includes('content_block_start') && e.includes('thinking')), 'Should start thinking block in stream');
    assert.ok(events.some(e => e.includes('content_block_delta') && e.includes('Think')), 'Should yield thinking delta in stream');
    assert.ok(events.some(e => e.includes('content_block_start') && e.includes('text')), 'Should start text block in stream');
    assert.ok(events.some(e => e.includes('content_block_delta') && e.includes('Hello')), 'Should yield text delta in stream');
  };

  // Test malformed payload resilience
  const badRequest1 = anthropicToOpenai(null);
  assert.deepStrictEqual(badRequest1, { model: '', messages: [] });

  const badRequest2 = anthropicToOpenai({ messages: 'not-an-array' });
  assert.deepStrictEqual(badRequest2, { model: '', messages: [] });

  const badTokens = estimateInputTokens(null);
  assert.strictEqual(badTokens, 1);

  // Run async stream tests synchronously in a promise wait (or since this function is synchronous, we run it and handle completion in runAll)
  testAnthropicCompat.asyncTests = runStreamTest();

  console.log('✔ Anthropic compatibility tests passed successfully.');
}

function testCapabilities() {
  console.log('Testing Capabilities classification...');
  
  const c1 = classify('meta/llama-3.1-8b-instruct');
  assert.strictEqual(c1.type, 'chat');
  assert.strictEqual(c1.context_window, 131072, 'Llama 3.1 should have 128k context');
  
  const c2 = classify('nvidia/nv-embed-v1');
  assert.strictEqual(c2.type, 'embedding');
  assert.strictEqual(c2.context_window, 32768, 'NV-Embed-v1 should have 32k context');

  const c3 = classify('meta/llama-3.2-11b-vision-instruct');
  assert.strictEqual(c3.type, 'vision_chat');
  assert.strictEqual(c3.context_window, 131072, 'Llama 3.2 should have 128k context');

  const c4 = classify('google/gemma-3-12b-it');
  assert.strictEqual(c4.context_window, 131072, 'Gemma 3 12B should have 128k context');

  const c5 = classify('google/gemma-3-1b-it');
  assert.strictEqual(c5.context_window, 32768, 'Gemma 3 1B should have 32k context');

  const c6 = classify('mistralai/mistral-small-4-119b-2603');
  assert.strictEqual(c6.context_window, 262144, 'Mistral Small v4 should have 256k context');

  const c7 = classify('baai/bge-m3');
  assert.strictEqual(c7.context_window, 8192, 'BGE-M3 should have 8k context');

  console.log('✔ Capabilities tests passed successfully.');
}

async function testMetrics() {
  console.log('Testing Metrics database...');
  const testDbPath = path.join(__dirname, 'test-metrics.db');
  
  // Clean up if previous test run left it
  if (fs.existsSync(testDbPath)) {
    try { fs.unlinkSync(testDbPath); } catch {}
  }
  
  const metrics = new Metrics(testDbPath);
  await metrics.ready();
  
  await metrics.recordRequest({
    method: 'POST',
    path: '/v1/chat/completions',
    model: 'meta/llama-3.1-8b-instruct',
    keyLabel: 'key1',
    streaming: false,
    statusCode: 200,
    latencyMs: 150.5,
    promptTokens: 15,
    completionTokens: 20,
    cachedTokens: 0,
    totalTokens: 35,
    wasRateLimited: false,
    retries: 0,
    requestBytes: 500,
    pacingMs: 0.0
  });

  const totals = metrics.getTotalCounts();
  assert.strictEqual(totals.all_time_requests, 1, 'Should record 1 request');
  assert.strictEqual(totals.all_time_tokens, 35, 'Should record 35 tokens');
  
  metrics.close();
  
  // Wait a small moment for async writes to finish and close cleanly
  await new Promise(resolve => setTimeout(resolve, 500));
  
  // Clean up test DB files
  const filesToClean = [testDbPath, testDbPath + '.tmp', testDbPath + '.db-shm', testDbPath + '.db-wal'];
  for (const f of filesToClean) {
    if (fs.existsSync(f)) {
      try { fs.unlinkSync(f); } catch {}
    }
  }
  
  console.log('✔ Metrics tests passed successfully.');
}

async function runAll() {
  console.log('=== Starting wrapper-nvidia unit tests ===\n');
  try {
    await testKeyPool();
    testAnthropicCompat();
    if (testAnthropicCompat.asyncTests) {
      await testAnthropicCompat.asyncTests;
    }
    testCapabilities();
    await testMetrics();
    console.log('\n=== All unit tests passed successfully! ===');
  } catch (err) {
    console.error('\n❌ Test Failure:', err.message);
    console.error(err.stack);
    process.exit(1);
  }
}

runAll();
