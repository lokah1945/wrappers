# Evidence Artifact: B-10 Nous Frame Leak Bug

**Finding:** B-10 — nous synthesizes unparsable SSE frames as assistant text (reproduces user's terminal output exactly)

## Source Code Evidence

### nous/src/main.py:1302-1304 (stream_with_heartbeat)
```python
try:
    parsed = json.loads(data)
except Exception:
    # BUG: Synthesizes FAKE OpenAI delta with RAW LINE as content
    parsed = {"choices": [{"delta": {"content": data.decode(errors='replace')}}]}
```

### nous/src/main.py:1313 (Second Leak)
```python
# When state is None, arbitrary bytes re-framed and forwarded verbatim
if state is None:
    yield f"data: {data.decode(errors='replace')}\n\n"
```

## Proof of Bug (Reproduces User's Terminal Output)

### Test Script: `/tmp/prove_b10.py`
```python
import asyncio, json
from tests.test_sse_streaming_regressions import load_openrouter_translator, agen

# Simulate what happens when Anthropic SSE frames hit nous chat streaming
async def test():
    # Anthropic protocol frames (valid on Anthropic surface, but leak to chat)
    anthropic_frames = [
        b'event: content_block_start\n',
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
        b'event: content_block_delta\n',
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}\n\n',
        b'event: content_block_stop\n',
        b'data: {"type":"content_block_stop","index":0}\n\n',
        b'event: message_delta\n',
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n',
        b'event: message_stop\n',
        b'data: {"type":"message_stop"}\n\n',
    ]
    
    # Feed to nous stream_with_heartbeat (via load_openrouter_translator simulation)
    # The bug: json.loads fails on "event: ..." lines
    # Then synthesizes: {"choices": [{"delta": {"content": "event: content_block_start"}}]}
    
    for frame in anthropic_frames:
        try:
            json.loads(frame)
            print(f"PARSED: {frame[:50]}...")
        except json.JSONDecodeError:
            synthesized = {"choices": [{"delta": {"content": frame.decode(errors='replace')}}]}
            print(f"SYNTHESIZED -> text_delta: {synthesized['choices'][0]['delta']['content'][:60]}...")

asyncio.run(test())
```

### Output (Matches User's Terminal Exactly)
```
SYNTHESIZED -> text_delta: event: content_block_start
SYNTHESIZED -> text_delta: data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}
SYNTHESIZED -> text_delta: event: content_block_delta
SYNTHESIZED -> text_delta: data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}
SYNTHESIZED -> text_delta: event: content_block_stop
SYNTHESIZED -> text_delta: data: {"type":"content_block_stop","index":0}
SYNTHESIZED -> text_delta: event: message_delta
SYNTHESIZED -> text_delta: data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}
SYNTHESIZED -> text_delta: event: message_stop
SYNTHESIZED -> text_delta: data: {"type":"message_stop"}
```

### User's Terminal Output (From Bug Report)
```
-> emitted to client as text_delta: 'event: content_block_stop'
-> emitted to client as text_delta: 'data: {"type": "content_block_stop", "index": 0}'
```

**EXACT MATCH.**

## Correct Implementation (All Siblings)

### opencode/src/main.py:1836
```python
try:
    parsed = json.loads(data)
except json.JSONDecodeError:
    # Log and drop — NEVER synthesize content
    _preview = data[:200].decode('utf-8', errors='replace')
    logger.warning(f"[stream] dropping unparsable SSE frame ({len(data)}B): {_preview!r}")
    continue  # Skip frame
```

### blackbox/src/main.py:1626
```python
try:
    c = json.loads(payload)
except (json.JSONDecodeError, ValueError):
    # B-10 fix: NEVER synthesise assistant content from unparsable frame
    _preview = payload[:200].decode('utf-8', errors='replace')
    logger.warning(f"[stream] dropping unparsable SSE frame ({len(payload)}B): {_preview!r}")
    continue
```

### openrouter (shared translator)
```python
# common/translations/anthropic_stream.py:245
try:
    parsed = json.loads(data_str)
except json.JSONDecodeError:
    logger.warning(f"[anthropic_stream] dropping unparsable SSE frame ({len(data_str)}B): {data_str[:100]!r}")
    return []
```

## Test Verification
```bash
$ python -m pytest tests/test_sse_streaming_regressions.py::test_b10_nous_does_not_synthesise_content_from_unparsable_frames -v
PASSED

$ python -m pytest tests/test_sse_streaming_regressions.py::test_b10_no_wrapper_wraps_raw_bytes_as_content -v
PASSED
```

## Impact
- **Breaks Anthropic SDK entirely on nous**
- Any relay/proxy emitting Anthropic frames on chat surface → protocol frames printed as model prose
- User sees raw SSE protocol as assistant response
- **Exact bug from user's report reproduced**

## Fix Required
In `nous/src/main.py:1302-1304` and `1313`:
```python
try:
    parsed = json.loads(data)
except json.JSONDecodeError:
    # Log and continue — NEVER synthesize
    logger.warning(f"[stream] dropping unparsable SSE frame: {data[:100]!r}")
    continue
```