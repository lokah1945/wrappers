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
  // The thinking block is converted to "  thinking\n...\n  response\n" format
  // (deepseek-style), NOT "<thinking>...</thinking>" XML tags.
  assert.ok(openaiReqThinking.messages[0].content.includes('thinking'), 'Should convert thinking block');
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
          releaseLock() {},
          // Stream-protocol cancel is optional; the generator's finally
          // gate now checks `typeof reader.cancel === 'function'`. Provide it
          // anyway so any other consumer that calls it on a mock doesn't crash.
          cancel() {}
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
    const sseGen = streamOpenaiToAnthropic(makeMockStream(mockChunks), 'model', capture, 15, 'test_req_001');
    const events = [];
    for await (const chunk of sseGen) {
      events.push(chunk);
    }
    assert.ok(events.some(e => e.includes('content_block_start') && e.includes('thinking')), 'Should start thinking block in stream');
    assert.ok(events.some(e => e.includes('content_block_delta') && e.includes('Think')), 'Should yield thinking delta in stream');
    assert.ok(events.some(e => e.includes('content_block_start') && e.includes('text')), 'Should start text block in stream');
    assert.ok(events.some(e => e.includes('content_block_delta') && e.includes('Hello')), 'Should yield text delta in stream');
  };

  // Synthetic thinking shim: when extended thinking is requested but the model
  // returns only plain text (no reasoning), a `thinking` block must precede the
  // text block so Claude Code's content-ordering contract holds.
  const runSyntheticThinkingTest = async () => {
    const mockChunks = [
      'data: {"choices": [{"delta": {"content": "Direct answer" }}]}\n'
    ];
    const capture = { _startMs: Date.now() };
    const sseGen = streamOpenaiToAnthropic(makeMockStream(mockChunks), 'model', capture, 15, 'test_req_002', true);
    const events = [];
    for await (const chunk of sseGen) events.push(chunk);
    const all = events.join('');
    const thinkingPos = all.indexOf('"type":"thinking"');
    const textPos = all.indexOf('"type":"text"');
    assert.ok(thinkingPos > -1, 'Synthetic thinking block must be present');
    assert.ok(textPos > -1, 'Text block must be present');
    assert.ok(thinkingPos < textPos, 'Thinking block must come before text block');
    // The FIRST content_block_start event must carry a thinking block.
    const firstBlockLine = events.find(e => e.includes('event: content_block_start'));
    const firstData = JSON.parse(firstBlockLine.split('data: ')[1]);
    assert.strictEqual(firstData.content_block.type, 'thinking', 'First content block must be the synthetic thinking block');
    assert.ok(all.includes('responding directly'), 'Synthetic thinking note present');

    // And the non-streaming shim:
    const oaiResp = {
      choices: [{ message: { role: 'assistant', content: 'Direct answer' }, finish_reason: 'stop' }],
      usage: { prompt_tokens: 10, completion_tokens: 3 }
    };
    const anthro = openaiToAnthropic(oaiResp, 'model', 'reqX', true);
    assert.strictEqual(anthro.content[0].type, 'thinking', 'Non-stream first block must be synthetic thinking');
    assert.strictEqual(anthro.content[1].type, 'text', 'Non-stream second block must be text');
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

  // Verify the generator terminates cleanly on upstream mid-stream error:
  // it must NOT emit message_delta/message_stop (would produce a silent
  // truncation or an `event: error` after `message_stop`), and must flag
  // capture.errored so the HTTP handler can emit a single `event: error`.
  const makeErrStream = (chunks) => {
    let index = 0;
    return {
      getReader() {
        return {
          read() {
            if (index < chunks.length) {
              const val = chunks[index++];
              return Promise.resolve({ done: false, value: new TextEncoder().encode(val) });
            }
            return Promise.reject(new Error('socket hang up'));
          },
          releaseLock() {},
          cancel() { return Promise.resolve(); }
        };
      }
    };
  };
  const runStreamErrTest = async () => {
    const mockChunks = [
      'data: {"choices": [{"delta": {"content": "hello" }}]}\n',
      'data: {"choices": [{"delta": {"content": " world" }}]}\n'
    ];
    const capture = { _startMs: Date.now() };
    const sseGen = streamOpenaiToAnthropic(makeErrStream(mockChunks), 'model', capture, 5, 'req_err_001');
    const events = [];
    let threw = false;
    try {
      for await (const chunk of sseGen) events.push(chunk);
    } catch (e) {
      threw = true;
    }
    const all = events.join('');
    assert.strictEqual(threw, false, 'Generator must not throw the read error to the consumer');
    assert.strictEqual(capture.errored, true, 'capture.errored must be set on upstream read failure');
    assert.strictEqual(all.includes('message_stop'), false, 'Must NOT emit message_stop on error');
    assert.strictEqual(all.includes('message_delta'), false, 'Must NOT emit message_delta on error');
    assert.strictEqual(all.includes('content_block_stop'), true, 'Open block must still be closed');
    assert.strictEqual(all.includes('hello') && all.includes('world'), true, 'Content deltas before the error must be preserved');
  };
  testAnthropicCompat.asyncTests = Promise.all([runStreamTest(), runStreamErrTest(), runSyntheticThinkingTest()]);

  console.log('✔ Anthropic compatibility tests passed successfully.');
}

function testCapabilities() {
  console.log('Testing Capabilities classification...');
  
  const c1 = classify('meta/llama-3.1-8b-instruct');
  assert.strictEqual(c1.type, 'chat');
  // classify() returns the bare capability definition without context_window;
  // the production /v1/models & /v1/capabilities endpoints enrich via
  // enrichModelMetadata() which defaults context_window to 131072. The
  // classifier itself must NOT invent a context window (it has no per-model
  // knowledge of upstream limits), so we assert the classifier contract here.
  assert.strictEqual(c1.context_window, undefined, 'classify() must not fabricate context_window');

  const c2 = classify('nvidia/nv-embed-v1');
  assert.strictEqual(c2.type, 'embedding');
  assert.strictEqual(c2.context_window, undefined, 'embedding must not expose context_window');

  const c3 = classify('meta/llama-3.2-11b-vision-instruct');
  assert.strictEqual(c3.type, 'vision_chat');
  assert.strictEqual(c3.context_window, undefined, 'classify() must not fabricate context_window');

  const c4 = classify('google/gemma-3-12b-it');
  assert.strictEqual(c4.context_window, undefined, 'no context_window field');

  const c5 = classify('google/gemma-3-1b-it');
  assert.strictEqual(c5.context_window, undefined, 'no context_window field');

  const c6 = classify('mistralai/mistral-small-4-119b-2603');
  assert.strictEqual(c6.context_window, undefined, 'no context_window field');

  const c7 = classify('baai/bge-m3');
  assert.strictEqual(c7.context_window, undefined, 'no context_window field');

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
