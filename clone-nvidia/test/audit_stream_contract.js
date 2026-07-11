// Synthetic streaming validation for thinking/reasoning/tool-call contract.
// Feeds realistic upstream OpenAI SSE chunks through streamOpenaiToAnthropic
// and asserts the emitted Anthropic event stream is well-formed and contract-
// compliant for Claude Code / agents.
const { streamOpenaiToAnthropic } = require('../src/anthropic_compat');

function makeStream(chunks) {
  // chunks: array of raw upstream SSE strings (data: {...}\n\n)
  let i = 0;
  return {
    getReader() {
      return {
        read() {
          if (i < chunks.length) return Promise.resolve({ value: Buffer.from(chunks[i++]), done: false });
          return Promise.resolve({ value: undefined, done: true });
        },
        cancel() { return Promise.resolve(); },
        releaseLock() {},
      };
    },
  };
}

function sseData(obj) { return `data: ${JSON.stringify(obj)}\n\n`; }
function doneEvent() { return `data: [DONE]\n\n`; }

// Parse emitted events into structured form
async function run(stream, expectThinking) {
  const events = [];
  const capture = {};
  for await (const ev of streamOpenaiToAnthropic(stream, 'test-model', capture, 10, 'req1', expectThinking)) {
    const m = ev.match(/^event: (\S+)\ndata: (.*)\n\n$/s);
    if (m) events.push({ event: m[1], data: JSON.parse(m[2]) });
    else if (ev.startsWith('event: ping')) events.push({ event: 'ping' });
    else events.push({ event: 'raw', raw: ev });
  }
  return { events, capture };
}

function checkContract(name, events) {
  const errs = [];
  if (events[0]?.event !== 'message_start') errs.push('first event not message_start');
  const last = events[events.length - 1];
  if (last.event !== 'message_stop') errs.push('last event not message_stop (got ' + last.event + ')');
  // block start/stop pairing
  const open = [];
  let sawThinking = false, sawTextOrTool = false;
  for (const e of events) {
    if (e.event === 'content_block_start') {
      open.push(e.data.index);
      const t = e.data.content_block.type;
      if (t === 'thinking') sawThinking = true;
      if (t === 'text' || t === 'tool_use') sawTextOrTool = true;
    } else if (e.event === 'content_block_stop') {
      const idx = open.indexOf(e.data.index);
      if (idx === -1) errs.push(`content_block_stop without matching start idx=${e.data.index}`);
      else open.pop();
    }
  }
  if (open.length) errs.push('unclosed content blocks: ' + JSON.stringify(open));
  // message_delta must appear before message_stop
  const di = events.findIndex(e => e.event === 'message_delta');
  const si = events.findIndex(e => e.event === 'message_stop');
  if (di === -1) errs.push('no message_delta');
  if (di > si) errs.push('message_delta after message_stop');
  return { errs, sawThinking, sawTextOrTool };
}

async function scenario(name, chunks, expectThinking, expectFirstThinking) {
  const stream = makeStream(chunks);
  const { events, capture } = await run(stream, expectThinking);
  const { errs, sawThinking, sawTextOrTool } = checkContract(name, events);
  let ok = errs.length === 0;
  if (expectThinking && !sawThinking && sawTextOrTool) {
    errs.push('expectThinking but no thinking block emitted before text/tool (CONTRACT VIOLATION)');
    ok = false;
  }
  if (expectFirstThinking) {
    const firstBlock = events.find(e => e.event === 'content_block_start');
    if (firstBlock?.data?.content_block?.type !== 'thinking') {
      errs.push('first content block is not thinking (got ' + firstBlock?.data?.content_block?.type + ')');
      ok = false;
    }
  }
  console.log(`${ok ? '✔' : '❌'} ${name}` + (errs.length ? '  ERRORS: ' + JSON.stringify(errs) : ''));
  return ok;
}

(async () => {
  let allOk = true;

  // 1) Plain text (no thinking)
  allOk &= await scenario('text-only (no thinking)',
    [sseData({ choices: [{ delta: { content: 'Hello' }, finish_reason: 'stop' }] }), doneEvent()],
    false);

  // 2) Reasoning model: reasoning_content then text, expectThinking ON
  allOk &= await scenario('reasoning_content + text (expectThinking)',
    [sseData({ choices: [{ delta: { reasoning_content: 'Let me think...' } }] }),
     sseData({ choices: [{ delta: { content: 'The answer is 42' }, finish_reason: 'stop' }] }),
     sseData({ usage: { prompt_tokens: 10, completion_tokens: 5 } }), doneEvent()],
    true, true);

  // 3) Native tool_calls, expectThinking ON, NO reasoning -> synthetic thinking required
  allOk &= await scenario('native tool_calls + expectThinking (no reasoning)',
    [sseData({ choices: [{ delta: { tool_calls: [{ index: 0, id: 'call1', function: { name: 'Bash', arguments: '{"cmd":"ls"}' } }] }, finish_reason: 'tool_calls' }] }),
     sseData({ usage: { prompt_tokens: 10, completion_tokens: 5 } }), doneEvent()],
    true, true);

  // 4) DSML tool_calls in content, expectThinking ON -> synthetic thinking required (regression for fix)
  allOk &= await scenario('DSML tool_calls + expectThinking (no reasoning)',
    [sseData({ choices: [{ delta: { content: '<｜DSML｜tool_calls>\n<｜DSML｜invoke name="Bash">\n<｜DSML｜parameter name="cmd" string="true">ls</｜DSML｜parameter>\n</｜DSML｜invoke>\n</｜DSML｜tool_calls>' }, finish_reason: 'tool_calls' }] }),
     sseData({ usage: { prompt_tokens: 10, completion_tokens: 5 } }), doneEvent()],
    true, true);

  // 5) <think> XML tag style reasoning then text (expectThinking ON)
  allOk &= await scenario('<think> XML + text (expectThinking)',
    [sseData({ choices: [{ delta: { content: '<think>I am reasoning</think>Final answer' }, finish_reason: 'stop' }] }),
     sseData({ usage: { prompt_tokens: 10, completion_tokens: 5 } }), doneEvent()],
    true, true);

  // 6) Reasoning only (thinking, max_tokens cut) -> must still emit text/tool or synthetic guard
  allOk &= await scenario('reasoning-only (no text, expectThinking)',
    [sseData({ choices: [{ delta: { reasoning_content: 'deep thought' }, finish_reason: 'length' }] }),
     sseData({ usage: { prompt_tokens: 10, completion_tokens: 5 } }), doneEvent()],
    true);

  // 7) Interleaved: text then reasoning then text (completedThinking logic)
  allOk &= await scenario('text then reasoning then text',
    [sseData({ choices: [{ delta: { content: 'start ' } }] }),
     sseData({ choices: [{ delta: { reasoning_content: 'mid thought' } }] }),
     sseData({ choices: [{ delta: { content: ' end' }, finish_reason: 'stop' }] }), doneEvent()],
    false);

  console.log(allOk ? '\n✔ ALL STREAMING CONTRACT SCENARIOS PASSED' : '\n❌ SOME SCENARIOS FAILED');
  process.exit(allOk ? 0 : 1);
})();
