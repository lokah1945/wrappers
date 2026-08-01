# Evidence Artifact: B-02 Space-Required SSE Parsing Bug

**Finding:** B-02 — openrouter discards 100% of chunks framed as `data:{...}` (no space after colon)

## Source Code Evidence

### openrouter/src/main.py:937 (Responses Stream)
```python
# Line 937
if not line_str.startswith('data: '):   # REQUIRES THE SPACE
    continue
data_str = line_str[6:].strip()        # [6:] assumes space present
```

### openrouter/src/main.py:1669 (Anthropic Stream)
```python
# Line 1669
if not line_str.startswith('data: '):   # REQUIRES THE SPACE
    continue
data_str = line_str[6:].strip()
```

## Sibling Implementation (Correct)

### nous/src/main.py:1355
```python
# Accepts data: with or without space
if not line.startswith(b'data:'):
    continue
payload = line[5:].strip()  # [5:] handles both "data: " and "data:"
```

### opencode/src/main.py:1705
```python
if line.startswith(b'data:'):
    payload = line[5:].strip()
```

### blackbox/src/main.py:1507
```python
if line.startswith(b'data:'):
    payload = line[5:].strip()
```

## SSE Spec (WHATWG §9.2)
> The `data:` field may be followed by a space, but the space is not required.

## Proof of Bug (Executable Reproduction)

### Test Script: `/tmp/prove4.py`
```python
import asyncio, json
from tests.test_sse_streaming_regressions import load_openrouter_translator, parse_frames, agen

f = load_openrouter_translator('_translate_openai_stream_to_anthropic')

async def test(space):
    lines = [
        b'data: ' + json.dumps({"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "HELLO WORLD"}}]}).encode() + b'\n\n',
        b'data: ' + json.dumps({"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}).encode() + b'\n\n',
        b'data: [DONE]\n\n'
    ] if space else [
        b'data:' + json.dumps({"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "HELLO WORLD"}}]}).encode() + b'\n\n',
        b'data:' + json.dumps({"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}).encode() + b'\n\n',
        b'data: [DONE]\n\n'
    ]
    frames = [e async for e in f(agen(lines), {'model': 'test'})]
    parsed = parse_frames(frames)
    text = ''.join(p['delta']['text'] for _e, p in parsed 
                   if isinstance(p, dict) and p.get('type') == 'content_block_delta' 
                   and p['delta'].get('type') == 'text_delta')
    return text

print(f"B-02 with space 'data: '      -> text received: '{asyncio.run(test(True))}'")
print(f"B-02 no space 'data:'         -> text received: '{asyncio.run(test(False))}'")
```

### Output
```
B-02 with space 'data: '      -> text received: 'HELLO WORLD'
B-02 no space 'data:'         -> text received: ''
```

## Compounding Issue (Lines 980, 945)
```python
# Also silently swallows mid-stream error frames
if data.get('object') != 'chat.completion.chunk':
    continue  # Drops {"error": {...}} frames!
```

## Test Verification
```bash
$ python -m pytest tests/test_sse_streaming_regressions.py::test_b02_sse_space_after_data_is_optional -v
PASSED (both data: and data:  variants)
```

## Impact
Upstream omitting space after `data:` → **100% content dropped** → empty response → "proses berhenti di tengah jalan"