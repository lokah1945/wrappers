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

async function testDsmlToolCallsParsing() {
  console.log('Testing: Streaming DSML tool call parsing and translation...');
  
  const chunks = [
    makeOpenAIChoiceChunk(undefined, 'Hmm, let me run a check.'),
    makeOpenAIChoiceChunk(undefined, '</think>\n<｜DSML｜tool_calls>\n<｜DSML｜invoke name="Bash">\n'),
    makeOpenAIChoiceChunk(undefined, '<｜DSML｜parameter name="command" string="true">node --version</｜DSML｜parameter>\n'),
    makeOpenAIChoiceChunk(undefined, '</｜DSML｜invoke>\n</｜DSML｜tool_calls>\nAll done!'),
    makeOpenAIChoiceChunk(undefined, undefined, 'stop')
  ];

  const mockStream = makeMockStream(chunks);
  const capture = {};
  const generator = streamOpenaiToAnthropic(mockStream, MODEL, capture, 100, 'test-req-3', true);
  const events = await collectEvents(generator);

  const contentBlockStarts = events.filter(e => e.type === 'content_block_start');
  console.log('  Content blocks started:', contentBlockStarts.map(e => e.data.content_block.type));
  
  assert.strictEqual(contentBlockStarts[0].data.content_block.type, 'thinking');
  assert.strictEqual(contentBlockStarts[1].data.content_block.type, 'text');
  assert.strictEqual(contentBlockStarts[2].data.content_block.type, 'tool_use');
  assert.strictEqual(contentBlockStarts[2].data.content_block.name, 'Bash');
  assert.strictEqual(contentBlockStarts[3].data.content_block.type, 'text');

  // Verify tool delta json input
  const toolDeltas = events.filter(e => e.type === 'content_block_delta' && e.data.delta.type === 'input_json_delta');
  const inputStr = toolDeltas.map(e => e.data.delta.partial_json).join('');
  console.log('  Parsed Tool Input:', inputStr);
  const parsedInput = JSON.parse(inputStr);
  assert.strictEqual(parsedInput.command, 'node --version');

  // Verify trailing text delta
  const textDeltas = events.filter(e => e.type === 'content_block_delta' && e.data.delta.type === 'text_delta');
  const textContent = textDeltas.map(e => e.data.delta.text).join('');
  console.log('  Trailing text content:', JSON.stringify(textContent));
  assert.strictEqual(textContent.trim(), 'All done!');

  console.log('✔ testDsmlToolCallsParsing PASSED');
}

async function testDsmlNonStreaming() {
  console.log('Testing: Non-streaming DSML tool call parsing...');
  
  const { openaiToAnthropic } = require('../src/anthropic_compat');
  const mockOpenaiResponse = {
    choices: [{
      message: {
        role: 'assistant',
        content: 'Pre-text\n<｜DSML｜tool_calls>\n<｜DSML｜invoke name="Bash">\n<｜DSML｜parameter name="command" string="true">node -v</｜DSML｜parameter>\n</｜DSML｜invoke>\n</｜DSML｜tool_calls>\nPost-text',
        reasoning_content: 'Thinking details'
      }
    }]
  };
  
  const result = openaiToAnthropic(mockOpenaiResponse, MODEL);
  console.log('  Parsed Content Blocks:', result.content);
  
  assert.strictEqual(result.content[0].type, 'thinking');
  assert.strictEqual(result.content[0].thinking, 'Thinking details');
  
  assert.strictEqual(result.content[1].type, 'tool_use');
  assert.strictEqual(result.content[1].name, 'Bash');
  assert.strictEqual(result.content[1].input.command, 'node -v');
  
  assert.strictEqual(result.content[2].type, 'text');
  assert.strictEqual(result.content[2].text, 'Pre-text');
  
  assert.strictEqual(result.content[3].type, 'text');
  assert.strictEqual(result.content[3].text, 'Post-text');

  console.log('✔ testDsmlNonStreaming PASSED');
}

async function testHistorySelfHealing() {
  console.log('Testing: History translation self-healing guard (alternating plaintext model)...');
  
  const { anthropicToOpenai } = require('../src/anthropic_compat');
  
  const mockAnthropicRequest = {
    model: 'deepseek-ai/deepseek-v4-pro',
    messages: [
      {
        role: 'user',
        content: 'Please list files.'
      },
      {
        role: 'assistant',
        content: [
          {
            type: 'text',
            text: 'Pre-text'
          },
          {
            type: 'tool_use',
            id: 'toolu_dsml_12345',
            name: 'Bash',
            input: { command: 'ls' }
          }
        ]
      },
      {
        role: 'user',
        content: [
          {
            type: 'tool_result',
            tool_use_id: 'toolu_dsml_12345',
            content: 'src/ package.json'
          }
        ]
      }
    ]
  };
  
  const result = anthropicToOpenai(mockAnthropicRequest);
  console.log('  OpenAI Translated Messages:', JSON.stringify(result.messages, null, 2));
  
  assert.strictEqual(result.messages.length, 3);
  
  assert.strictEqual(result.messages[0].role, 'user');
  assert.strictEqual(result.messages[0].content, 'Please list files.');
  
  assert.strictEqual(result.messages[1].role, 'assistant');
  assert.ok(result.messages[1].content.includes('Pre-text'));
  assert.ok(result.messages[1].content.includes('<｜DSML｜tool_calls>'));
  assert.ok(result.messages[1].content.includes('<｜DSML｜invoke name="Bash">'));
  assert.ok(result.messages[1].content.includes('ls'));
  
  assert.strictEqual(result.messages[2].role, 'user');
  assert.ok(result.messages[2].content.includes('<tool_result id="toolu_dsml_12345">'));
  assert.ok(result.messages[2].content.includes('src/ package.json'));

  console.log('✔ testHistorySelfHealing PASSED');
}

async function runAll() {
  try {
    await testReasoningOnlyTransition();
    await testNormalTransition();
    await testDsmlToolCallsParsing();
    await testDsmlNonStreaming();
    await testHistorySelfHealing();
    console.log('\n✔ All thinking parser regression tests PASSED!');
  } catch (err) {
    console.error('\n✗ Test failed:', err);
    process.exit(1);
  }
}

runAll();
