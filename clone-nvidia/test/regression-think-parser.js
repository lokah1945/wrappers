/**
 * test/regression-think-parser.js
 *
 * Unit test for the robust stateful thinking/text parser streamOpenaiToAnthropic.
 * Verifies that tool calls and text content that mistakenly appear inside
 * reasoning blocks are correctly split out into text blocks so Claude Code
 * is never desynced or crashed.
 */
const assert = require('assert');
const { streamOpenaiToAnthropic } = require('../src/anthropic_compat');

const MODEL = 'deepseek-ai/deepseek-v4-pro';

async function collectEvents(generator) {
  const events = [];
  for await (const chunk of generator) {
    // Parse SSE format:
    // event: <event_name>
    // data: <json_string>
    const lines = chunk.split('\n');
    let eventType = null;
    let eventData = null;
    for (const line of lines) {
      if (line.startsWith('event:')) {
        eventType = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        eventData = JSON.parse(line.slice(5).trim());
      }
    }
    if (eventType) {
      events.push({ type: eventType, data: eventData });
    }
  }
  return events;
}

function makeMockStream(chunks) {
  let index = 0;
  return {
    getReader() {
      return {
        async read() {
          if (index >= chunks.length) {
            return { done: true, value: undefined };
          }
          const val = chunks[index++];
          const bytes = typeof val === 'string' ? new TextEncoder().encode(val) : val;
          return { done: false, value: bytes };
        },
        releaseLock() {}
      };
    }
  };
}

function makeOpenAIChoiceChunk(content, reasoningContent, finishReason = null) {
  const delta = {};
  if (content !== undefined) delta.content = content;
  if (reasoningContent !== undefined) delta.reasoning_content = reasoningContent;
  
  return `data: ${JSON.stringify({
    choices: [{
      delta,
      finish_reason: finishReason,
      index: 0
    }]
  })}\n\n`;
}

async function testReasoningOnlyTransition() {
  console.log('Testing: Model outputs tool calls inside reasoning_content stream...');
  
  const chunks = [
    makeOpenAIChoiceChunk(undefined, 'Thinking... Let me check the directory contents.'),
    makeOpenAIChoiceChunk(undefined, ' <think>\nLet me run ls.\n</think>\n'),
    makeOpenAIChoiceChunk(undefined, 'Final response: here are the files: <｜DSML｜tool_calls><｜DSML｜invoke name="Edit"><｜DSML｜parameter name="file_path">src/capabilities.js</｜DSML｜parameter></｜DSML｜invoke></｜DSML｜tool_calls>'),
    makeOpenAIChoiceChunk(undefined, ' The edit has been completed successfully.'),
    makeOpenAIChoiceChunk(undefined, undefined, 'stop')
  ];

  const mockStream = makeMockStream(chunks);
  const capture = {};
  const generator = streamOpenaiToAnthropic(mockStream, MODEL, capture, 100, 'test-req-1', true);
  const events = await collectEvents(generator);

  // Assert events structure
  // 1. Thinking block starts and contains thinking content
  const contentBlockStarts = events.filter(e => e.type === 'content_block_start');
  const contentBlockDeltas = events.filter(e => e.type === 'content_block_delta');

  console.log('  Content blocks started:', contentBlockStarts.map(e => e.data.content_block.type));
  
  // The first content block must be thinking because expectThinking = true
  assert.strictEqual(contentBlockStarts[0].data.content_block.type, 'thinking');
  
  // We must have transitioned and started a text block
  const hasText = contentBlockStarts.some(e => e.data.content_block.type === 'text');
  assert.ok(hasText, 'Should have started a text block when tool calls or final text appeared');

  // Verify that final text is sent as text_delta
  const textDeltas = contentBlockDeltas.filter(e => e.data.delta.type === 'text_delta');
  const textContent = textDeltas.map(e => e.data.delta.text).join('');
  console.log('  Collected text content:', JSON.stringify(textContent));
  assert.ok(textContent.includes('Final response:'), 'Text content should contain the final response text');
  assert.ok(textContent.includes('The edit has been completed successfully.'), 'Subsequent text should flow into text block');

  console.log('✔ testReasoningOnlyTransition PASSED');
}

async function testNormalTransition() {
  console.log('Testing: Normal model transition from reasoning_content to content...');
  
  const chunks = [
    makeOpenAIChoiceChunk(undefined, 'Hmm, let me calculate 2+2.'),
    makeOpenAIChoiceChunk(undefined, ' The calculation is simple.'),
    makeOpenAIChoiceChunk('The answer is 4.', undefined),
    makeOpenAIChoiceChunk(undefined, undefined, 'stop')
  ];

  const mockStream = makeMockStream(chunks);
  const capture = {};
  const generator = streamOpenaiToAnthropic(mockStream, MODEL, capture, 100, 'test-req-2', true);
  const events = await collectEvents(generator);

  const contentBlockStarts = events.filter(e => e.type === 'content_block_start');
  console.log('  Content blocks started:', contentBlockStarts.map(e => e.data.content_block.type));
  
  assert.strictEqual(contentBlockStarts[0].data.content_block.type, 'thinking');
  assert.strictEqual(contentBlockStarts[1].data.content_block.type, 'text');

  const textDeltas = events.filter(e => e.type === 'content_block_delta' && e.data.delta.type === 'text_delta');
  const textContent = textDeltas.map(e => e.data.delta.text).join('');
  assert.strictEqual(textContent, 'The answer is 4.');

  console.log('✔ testNormalTransition PASSED');
}

async function runAll() {
  try {
    await testReasoningOnlyTransition();
    await testNormalTransition();
    console.log('\n✔ All thinking parser regression tests PASSED!');
  } catch (err) {
    console.error('\n✗ Test failed:', err);
    process.exit(1);
  }
}

runAll();
