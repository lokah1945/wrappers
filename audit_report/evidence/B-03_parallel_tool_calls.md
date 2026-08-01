# Evidence Artifact: B-03 Parallel Tool Call Translation Bug

**Finding:** B-03 — openrouter tool-call translation loses arguments, injects phantoms, collides indices

## Source Code Evidence (openrouter/src/main.py:1706-1735)

### Defect 1: content_block_start OUTSIDE Guard
```python
# Line 1719 - OUTSIDE the "first time we see this tool" guard
for tc in (delta.get('tool_calls') or []):
    if not isinstance(tc, dict):
        continue
    tc_idx = tc.get('index', 0)
    fn = tc.get('function') or {}
    if tc_idx not in tool_call_blocks:
        # ...
    # BUG: content_block_start emitted HERE, outside guard!
    yield _sse('content_block_start', {...})  # Emits per chunk!
```

### Defect 2: Argument Emit OUTSIDE For Loop
```python
# Line 1728 - OUTSIDE the for tc loop!
for tc in (delta.get('tool_calls') or []):
    # ...
    # fn, tc_idx leak from loop scope
# BUG: if fn.get('arguments'): sits HERE, outside loop!
if fn.get('arguments'):  # Uses LAST fn/tc_idx from loop
    yield _sse('content_block_delta', {...})
```

### Defect 3: block_index Never Incremented
```python
# block_index initialized to 0, never incremented for tool blocks
# All tools collide on index 0
block_index = 0  # Line 1713
# No block_index += 1 when tool block opens
```

## Proof of Bug (Executable Reproduction)

### Test Script: `/tmp/prove3.py`
```python
import asyncio, json
from tests.test_sse_streaming_regressions import load_openrouter_translator, parse_frames, agen

f = load_openrouter_translator('_translate_openai_stream_to_anthropic')

async def run():
    lines = [
        # Chunk 1: Two tools start
        b'data: ' + json.dumps({
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {
                "tool_calls": [
                    {"index": 0, "id": "call_a", "function": {"name": "alpha", "arguments": '{"x'}},
                    {"index": 1, "id": "call_b", "function": {"name": "beta", "arguments": '{"y'}}
                ]
            }}]
        }).encode() + b'\n\n',
        # Chunk 2: Both tools continue
        b'data: ' + json.dumps({
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {
                "tool_calls": [
                    {"index": 0, "function": {"arguments": '":1}'}},
                    {"index": 1, "function": {"arguments": '":2}'}}
                ]
            }}]
        }).encode() + b'\n\n',
        # Chunk 3: Finish
        b'data: ' + json.dumps({
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]
        }).encode() + b'\n\n',
        b'data: [DONE]\n\n'
    ]
    return [e async for e in f(agen(lines), {'model': 'test'})]

frames = parse_frames(asyncio.run(run()))

starts = [p for _e, p in frames if isinstance(p, dict) and p.get('type') == 'content_block_start']
args = [p for _e, p in frames if isinstance(p, dict) 
        and p.get('type') == 'content_block_delta' 
        and p['delta'].get('type') == 'input_json_delta']

print(f"content_block_start count: {len(starts)}   (expected 2)")
for s in starts:
    print(f"    index {s['index']}  tool_use id={s['content_block']['id']}  name={s['content_block']['name']}")

print(f"\ninput_json_delta events: {len(args)}     (expected 4)")
for a in args:
    print(f"    index {a['index']}  partial_json '{a['delta']['partial_json']}'")

print(f"\nblock indices used by starts: {[s['index'] for s in starts]} -> distinct: {len(set(s['index'] for s in starts))}")

# Reassemble
by_idx = {}
for a in args:
    by_idx[a['index']] = by_idx.get(a['index'], '') + a['delta']['partial_json']
reassembled = [json.loads(v) for v in by_idx.values()]
print(f"\nreassembled args: {reassembled}")
```

### Output (Bug Reproduced)
```
content_block_start count: 4   (expected 2)
    index 0  tool_use id=call_a  name=alpha
    index 0  tool_use id=call_b  name=beta
    index 0  tool_use id=toolu_0  name=""     ← phantom block
    index 0  tool_use id=toolu_0  name=""     ← phantom block

input_json_delta events: 2     (expected 4)
    index 0  partial_json '{"y'      ← tool "alpha" arguments COMPLETELY LOST
    index 0  partial_json '":2}'

block indices used by starts: [0, 0, 0, 0] -> distinct: 1
```

## Impact on Agent
- Tool `alpha` loses ALL arguments → invalid JSON when reassembled
- Two phantom unnamed tool blocks injected
- Every block reuses index 0 → Anthropic SDK discards or raises
- Agent turn **stalls forever** waiting for tool_result

## Fix Reference (common/translations/anthropic_stream.py:146)
```python
# Move content_block_start INSIDE guard
if oi not in self.tool_map:
    events.extend(self._close_block())  # Close text/thinking ONLY
    self.index += 1
    self.tool_map[oi] = self.index
    self.open_tool_blocks.add(self.index)  # Track concurrent
    events.append(content_block_start(...))
    self.current_block = "tool_use"

# Argument emit INSIDE for loop
if fn.get('arguments'):
    events.append(content_block_delta(..., index=self.tool_map[oi], ...))

# Increment index for next tool
self.index += 1
```

## Test Verification
```bash
$ python -m pytest tests/test_sse_streaming_regressions.py::test_b03_parallel_tool_calls_distinct_blocks_and_all_arguments -v
PASSED

$ python -m pytest tests/test_sse_streaming_regressions.py::test_r02_parallel_tool_blocks_stay_open_concurrently -v
PASSED

$ python -m pytest tests/test_sse_streaming_regressions.py::test_r02_no_wrapper_closes_previous_tool_block_on_new_tool -v
PASSED (verifies NVIDIA stop_open() fixed)
```