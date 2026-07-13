// Verify the Claude Code extension-field hygiene fixes (§6.11 / §6.13):
//  - cache_control is stripped recursively from system/content/tools
//  - tool_search_tool_* pseudo-tools are dropped, defer_loading cleared
const { anthropicToOpenai, stripCacheControl, sanitizeAnthropicTools } = require('../src/anthropic_compat');

let pass = 0, fail = 0;
function check(name, cond) {
  if (cond) { console.log('✔ ' + name); pass++; }
  else { console.log('❌ ' + name); fail++; }
}

// --- stripCacheControl recursion ---
const req = {
  system: [{ type: 'text', text: 'sys', cache_control: { type: 'ephemeral' } }],
  messages: [{
    role: 'user',
    content: [
      { type: 'text', text: 'hi', cache_control: { type: 'ephemeral' } },
      { type: 'tool_result', tool_use_id: 't1', content: 'ok' },
    ],
  }],
  tools: [{ name: 'X', description: 'd', input_schema: {}, cache_control: { type: 'ephemeral' } }],
};
stripCacheControl(req);
check('cache_control removed from system block', req.system[0].cache_control === undefined);
check('cache_control removed from content block', req.messages[0].content[0].cache_control === undefined);
check('cache_control removed from tool def', req.tools[0].cache_control === undefined);

// --- sanitizeAnthropicTools ---
const { tools: out, droppedSearchTool } = sanitizeAnthropicTools([
  { type: 'tool_search_tool_regex_20251119', name: 'tool_search_tool_regex' },
  { name: 'Read', description: 'r', input_schema: {}, defer_loading: true },
]);
check('tool_search_tool_* dropped', droppedSearchTool === true && out.length === 1);
check('defer_loading cleared on remaining tool', out[0].defer_loading === undefined);
check('remaining tool preserved', out[0].name === 'Read');

// --- end-to-end through anthropicToOpenai ---
const aBody = {
  model: 'meta/llama-3.3-70b-instruct',
  max_tokens: 64,
  system: [{ type: 'text', text: 'be helpful', cache_control: { type: 'ephemeral' } }],
  tools: [
    { type: 'tool_search_tool_bm25_20251119', name: 'tool_search_tool_bm25' },
    { name: 'Read', description: 'Read a file.', input_schema: { type: 'object', properties: { file_path: { type: 'string' } }, required: ['file_path'] }, defer_loading: true },
  ],
  messages: [{ role: 'user', content: 'Read /etc/hostname' }],
};
const oai = anthropicToOpenai(aBody, null);
check('e2e: no cache_control in translated system', !JSON.stringify(oai).includes('cache_control'));
check('e2e: tool_search pseudo-tool not forwarded', !JSON.stringify(oai.tools).includes('tool_search'));
check('e2e: real tool forwarded as function', oai.tools[0].function.name === 'Read');
check('e2e: defer_loading not forwarded', oai.tools[0].function.defer_loading === undefined);

console.log(`\n${fail === 0 ? '✔' : '❌'} compat-extension tests: ${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
