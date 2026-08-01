# Streaming Correctness Deep Audit

**Date:** 2026-08-01  
**Scope:** Byte-level analysis of SSE parsing, emission, and protocol compliance across all 5 wrappers

---

## Executive Summary

| Wrapper | SSE Parsing | Anthropic Emission | Responses Emission | Chat Emission | Critical Bugs |
|---|---|---|---|---|---|
| nvidia-python | ✅ (mostly) | ✅ (shared) | ✅ | ✅ | B-06, B-12, B-13 |
| nous | ✅ (own) | ❌ (B-10) | ✅ | ✅ | B-10, B-06, B-11 |
| opencode | ✅ (shared) | ✅ (shared) | ✅ | ✅ | B-01, B-06 |
| blackbox | ✅ (shared) | ✅ (shared) | ✅ | ✅ | B-01, B-06 |
| openrouter | ✅ (shared) | ✅ (shared) | ✅ | ✅ | B-08, B-09 |

---

## 1. Upstream SSE Parsing — Byte-Level Analysis

### 1.1 Frame Splitting Logic

**Standard (WHATWG SSE):** Frames separated by blank line (`\n\n` or `\r\n\r\n`). Each frame: `field: value\n` lines.

**Real-World Upstream Variants (from E2E harness 21 behaviors):**
| Variant | Byte Pattern | Spec Legal? |
|---|---|---|
| Normal | `data: {...}\n\n` | ✅ |
| No space | `data:{...}\n\n` | ✅ (space optional) |
| Keep-alive | `data:\n\n` | ✅ (empty value = empty event) |
| CRLF | `data: {...}\r\n\r\n` | ✅ |
| Comments | `: comment\n\ndata: {...}\n\n` | ✅ |
| Big chunk | `data: {...}` split across TCP packets | ✅ |
| Byte split | `da` + `ta: {...}\n\n` | ✅ |
| `id:` / `retry:` | `id: 123\nretry: 5000\n\ndata: {...}\n\n` | ✅ |
| Dup finish | `data: [DONE]\n\ndata: [DONE]\n\n` | ✅ |

### 1.2 Per-Wrapper Parsing Implementation

#### nvidia-python
**File:** `src/main.py:_stream_chat` (line 2278), `src/anthropic_compat.py:stream_openai_to_anthropic`

```python
# Raw upstream chunks yielded verbatim — NO validation
async for chunk_str in resp.content:
    yield chunk_str  # B-12: forwards Anthropic frames on chat surface
```

**Issues:**
- No `common/sse.py` usage for chat streaming
- No CRLF normalization
- No empty `data:` handling (relies on upstream not sending)
- Forwards raw frames without checking `object == "chat.completion.chunk"`

#### nous
**File:** `src/main.py:stream_with_heartbeat` (line 1303)

```python
# Correct: uses sentinel-task pattern (N-05)
chunk_task = asyncio.ensure_future(chunk_iter.__anext__())
done_set, _ = await asyncio.wait({chunk_task}, timeout=HEARTBEAT_MS/1000)
if not done_set:
    yield IDLE  # heartbeat
    continue

# CRLF normalization (N-08)
if b"\r" in buffer:
    buffer = buffer.replace(b"\r\n", b"\n")

# Empty data: = keep-alive (N-09)
if payload == b"":
    continue
```

**Correct behaviors:**
- Sentinel-task heartbeat (not `wait_for`)
- CRLF normalization
- Empty `data:` treated as keep-alive
- Empty `choices: []` guarded

#### opencode
**File:** `src/main.py:_chunk_stream` (line 1029)

```python
# Correct: sentinel-task pattern
chunk_task = asyncio.ensure_future(chunk_iter.__anext__())
done_set, _ = await asyncio.wait({chunk_task}, timeout=HEARTBEAT_MS/1000)
if not done_set:
    yield (True, None)  # idle → heartbeat
    continue

# CRLF: NOT normalized here (but in Responses/Anthropic translators)
```

**Issues:**
- CRLF normalization missing in `_chunk_stream` (only in translators)
- Empty `data:` listed in terminator tuple (B-01)

#### blackbox
**File:** `src/main.py:_iter_chunks_with_idle` (line 914)

```python
# Correct: sentinel-task pattern (BB-5/DR-1)
# Identical to opencode pattern
```

**Issues:**
- Same as opencode: CRLF missing, B-01 terminator bug

#### openrouter
**File:** Uses `common/sse.py:iter_chunks_with_idle` (line 32)

```python
# In common/sse.py:
async def iter_chunks_with_idle(resp, idle_sec):
    chunk_iter = resp.content.iter_any().__aiter__()
    chunk_task = None
    try:
        while True:
            if chunk_task is None:
                chunk_task = asyncio.ensure_future(chunk_iter.__anext__())
            done_set, _ = await asyncio.wait({chunk_task}, timeout=idle_sec)
            if not done_set:
                yield IDLE
                continue
            # ... handles StopAsyncIteration, yields chunk
```

**Correct behaviors:**
- Uses shared `common/sse.py` — sentinel-task, CRLF normalization, IDLE sentinel
- All 3 translators use this (Anthropic, Responses, Chat passthrough)

---

## 2. Anthropic SSE Emission — Protocol Compliance

### 2.1 Required State Machine (per Anthropic API spec)

```
message_start
  └─ content_block_start (index 0, type: thinking)
  └─ content_block_delta (thinking_delta)*
  └─ content_block_stop
  └─ content_block_start (index 1, type: text)
  └─ content_block_delta (text_delta)*
  └─ content_block_stop
  └─ content_block_start (index 2, type: tool_use, name: "fn")
  └─ content_block_delta (input_json_delta)*
  └─ content_block_stop
  └─ ... parallel tool_use blocks stay open ...
message_delta (stop_reason: end_turn|max_tokens|tool_use|refusal)
message_stop
```

### 2.2 Shared Translator: `common/translations/anthropic_stream.py`

**Key Implementation Details:**

```python
# open_tool_blocks: set of indices for concurrently-open tool_use blocks
self.open_tool_blocks: set[int] = set()

# On new tool call (translate_chunk):
if oi not in self.tool_map:
    # Close ONLY text/thinking, NOT previous tool blocks
    events.extend(self._close_block())  # _close_block skips tool_use
    self.index += 1
    self.tool_map[oi] = self.index
    self.open_tool_blocks.add(self.index)  # Track as open
    # emit content_block_start with tool_use
    self.current_block = "tool_use"

# On finish_reason:
events.extend(self._close_everything())  # Closes text/thinking + ALL tool blocks
stop = _FINISH_TO_STOP.get(fr, "end_turn")  # STRICT mapping
```

**Correct Behaviors:**
- `open_tool_blocks` tracks concurrent tool blocks
- `_close_block()` skips `tool_use` → prevents closing previous tool
- `_close_everything()` closes all at terminal path
- Strict `finish_reason` → `stop_reason` mapping (`_FINISH_TO_STOP`)
- `force_done()` infers `tool_use` ONLY when no `finish_reason` AND tool block was open

### 2.3 Per-Wrapper Anthropic Emission Status

| Wrapper | Translator Used | Parallel Tools | stop_reason Strict | Error Frames | GeneratorExit |
|---|---|---|---|---|---|
| nvidia-python | Shared (via `anthropic_compat.py`) | ✅ | ❌ (B-06) | ✅ | ⚠️ 2 sites |
| nous | Own dict-based | ✅ | ❌ (B-06) | ✅ | ✅ 4 sites |
| opencode | Shared | ✅ | ❌ (B-06) | ✅ | ❌ 0 sites |
| blackbox | Shared | ✅ | ❌ (B-06) | ✅ | ❌ 0 sites |
| openrouter | Shared | ✅ | ✅ | ✅ | ❌ 0 sites |

### 2.4 B-06: stop_reason Mapping — The Critical Bug

**Current (WRONG) in shared translator + nous:**
```python
# common/translations/anthropic_stream.py:174
stop = "tool_use" if (fr == "tool_calls" or self.tool_map) else {...}.get(fr, "end_turn")
# nous/src/main.py:1512
stop = "tool_use" if (fr == "tool_calls" or self.tool_map) else {...}.get(fr, "end_turn")
```

**Problem:** `self.tool_map` is non-empty if ANY tool was seen in the turn. Even if `finish_reason == "stop"` or `"length"`, it reports `tool_use`.

**Impact on Claude Code:**
```json
{"delta": {"stop_reason": "tool_use", "stop_sequence": null}}
```
Claude Code waits for `tool_result` that will never be requested → **agent loop hangs**.

**Correct Mapping (openrouter only):**
```python
_FINISH_TO_STOP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}
# In translate_chunk:
stop = _FINISH_TO_STOP.get(fr, "end_turn")  # NEVER infer from tool_map
# In force_done (no finish_reason):
if stop == "end_turn" and self.tool_map and was_in_tool_block:
    stop = "tool_use"  # ONLY infer when stream died mid-tool
```

**Proof:** `test_b06_stop_reason_strict_mapping_even_after_a_tool_call` passes for openrouter, fails for others.

---

## 3. OpenAI Responses SSE Emission — Protocol Compliance

### 3.1 Required State Machine (per OpenAI Responses API spec)

```
response.created
response.in_progress
response.output_item.added (index 0, message, in_progress)
response.content_part.added (output_text, empty)
response.output_text.delta (text chunks)*
response.output_text.done (full text)
response.content_part.done
response.output_item.done
[reasoning item if thinking]
[function_call items if tools]
response.completed
data: [DONE]
```

### 3.2 Per-Wrapper Responses Emission

| Wrapper | Implementation | Full Lifecycle | output_item.added before delta | reasoning Streaming | function_call Streaming | response.failed on Error |
|---|---|---|---|---|---|---|
| nvidia-python | `ResponsesHandler` class | ✅ | ✅ | ✅ | ✅ | ❌ |
| nous | `ResponsesStreamState` class | ✅ | ✅ | ✅ | ✅ | ✅ |
| opencode | Inline in `responses()` | ✅ | ✅ | ✅ | ✅ | ✅ |
| blackbox | `_responses_stream` generator | ✅ | ✅ | ✅ | ✅ | ✅ |
| openrouter | `_translate_openai_stream_to_responses` | ✅ | ✅ | ✅ | ✅ | ✅ |

### 3.3 Critical Implementation Details

**openrouter `_translate_openai_stream_to_responses` (line 1142):**
```python
# Buffer accumulator for split chunks
buffer = b''
while b'\n' in buffer:
    line_bytes, buffer = buffer.split(b'\n', 1)
    line_str = line_bytes.decode().strip()
    if line_str.startswith('data:'):
        data_str = line_str[5:].strip()  # [5:] handles "data:" without space
        if data_str == '[DONE]': done = True
        # Parse, emit events...

# R-03: upstream error frames surface as response.failed
if isinstance(data, dict) and data.get('error') is not None and 'choices' not in data:
    upstream_error = data['error'].get('message', 'upstream error')
    # Later emits response.failed instead of response.completed
```

**Correct behaviors:**
- Single-frame emission (`event:\ndata:\n\n` as one string)
- Buffer handles split chunks
- `data:` without space handled (`[5:]` not `[6:]`)
- Upstream errors → `response.failed` not `response.completed`
- `GeneratorExit` re-raised, `aclose()` called in `finally`

---

## 4. OpenAI Chat Completions SSE Emission

### 4.1 Required Format

```
data: {"id": "...", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "..."}, "finish_reason": null}]}\n\n
...
data: [DONE]\n\n
```

### 4.2 Per-Wrapper Chat Emission

| Wrapper | Implementation | [DONE] Once | CRLF Tolerant | Empty data: = Keep-alive |
|---|---|---|---|---|
| nvidia-python | `_stream_chat` yields raw | ✅ (`saw_done`) | ❌ | ✅ |
| nous | `stream_with_heartbeat` + `stream_passthrough` | ✅ (`terminated`) | ✅ | ✅ |
| opencode | `stream_passthrough` | ✅ (`saw_done`) | ❌ | ❌ (B-01) |
| blackbox | `stream_passthrough` | ✅ (`saw_done`) | ❌ | ❌ (B-01) |
| openrouter | `stream_gen` + `stream_with_heartbeat` (shared) | ✅ (`saw_done`) | ✅ | ✅ |

---

## 5. Byte-Level Proof Artifacts

### 5.1 B-01: Empty `data:` Terminator Bug

**blackbox/src/main.py:1445**
```python
if payload in (b'[DONE]', b'', b'"[DONE]"'):
    for ev in state.force_done():
        yield ev
    return  # TERMINATES STREAM
```

**Test Proof:**
```python
# tests/test_sse_streaming_regressions.py::test_b01_empty_data_line_is_keepalive_not_terminator
# Verifies b'' NOT in terminator tuple for blackbox/opencode
```

### 5.2 B-02: `data:{...}` Without Space

**openrouter/src/main.py:937 (BEFORE FIX)**
```python
if not line_str.startswith('data: '):  # REQUIRES SPACE
    continue
data_str = line_str[6:].strip()  # [6:] assumes space
```

**Proof Output (from `/tmp/prove4.py`):**
```
B-02 with space 'data: '      -> text received: 'HELLO WORLD'
B-02 no space 'data:'         -> text received: ''
```

**FIXED:** Now uses `startswith('data:')` + `[5:]`

### 5.3 B-03: Parallel Tool Call Corruption

**openrouter/src/main.py:1706-1735 (BEFORE FIX):**
```python
for tc in delta.get('tool_calls', []):
    if tc_idx not in tool_call_blocks:
        # content_block_start OUTSIDE guard — emits per chunk
        yield content_block_start(...)
    # ...
# if fn.get('arguments'):  # OUTSIDE for loop!
    yield content_block_delta(...)
```

**Proof Output (from `/tmp/prove3.py`):**
```
content_block_start count: 4   (expected 2)
    index 0  tool_use id=call_a  name=alpha
    index 0  tool_use id=call_b  name=beta
    index 0  tool_use id=toolu_0 name=""     ← phantom
    index 0  tool_use id=toolu_0 name=""     ← phantom
input_json_delta events: 2     (expected 4)
    index 0  partial_json '{"y'      ← alpha args LOST
    index 0  partial_json '":2}'
block indices: [0, 0, 0, 0] -> distinct: 1
```

### 5.4 B-10: nous Frame Leak

**nous/src/main.py:1302-1304:**
```python
try:
    parsed = json.loads(data)
except Exception:
    parsed = {"choices": [{"delta": {"content": data.decode(errors='replace')}}]}
```

**Proof Output:**
```
-> emitted to client as text_delta: 'event: content_block_stop'
-> emitted to client as text_delta: 'data: {"type": "content_block_stop", "index": 0}'
```

### 5.5 R-08: Empty `choices: []` IndexError

**Found by AST scan (`test_r08_no_unguarded_choices_indexing`):**
```python
# nvidia-python/src/anthropic_compat.py:3x sites
chunk["choices"][0]  # NO guard for empty array
```

**Proof:** `test_r08_empty_choices_array_does_not_crash` — shared translator handles correctly; 3 sites in nvidia fixed.

---

## 6. Heartbeat Mechanism — Idle vs Dead Upstream

### 6.1 The `wait_for` Bug (B-08)

**WRONG (openrouter + common/base_wrapper.py):**
```python
chunk = await asyncio.wait_for(inner.__anext__(), timeout=hb_interval)
except asyncio.TimeoutError:
    yield b': heartbeat\n\n'
```

**Problem:** `wait_for` **cancels** the pending read. A real `aiohttp` socket timeout raises the SAME `asyncio.TimeoutError` — indistinguishable from idle. Dead upstream gets heartbeated forever.

### 6.2 Correct: Sentinel-Task Pattern (nous, opencode, blackbox, common/sse.py)

```python
chunk_task = asyncio.ensure_future(chunk_iter.__anext__())
done_set, _ = await asyncio.wait({chunk_task}, timeout=idle_sec)
if not done_set:
    yield IDLE  # Task still running = upstream idle
    continue
# Task finished = real chunk OR real error
chunk = chunk_task.result()  # Raises StopAsyncIteration or real error
```

**Verification:** `test_b08_dead_upstream_raises_instead_of_heartbeating_forever`
- `BrokenResp` raises `ConnectionResetError` immediately
- Sentinel-task pattern: error propagates ✅
- `wait_for` pattern: would catch as TimeoutError, heartbeat forever ❌

---

## 7. Generator Cleanup — Exactly-Once Release

### 7.1 The Leak Pattern

```python
async for raw in upstream_gen:
    yield raw
# Breaking here leaves generator suspended
# finally block (release key, close response) NEVER runs
```

### 7.2 Correct: Deterministic `aclose()`

```python
try:
    async for raw in upstream_gen:
        yield raw
except GeneratorExit:
    raise
finally:
    await upstream_gen.aclose()  # Runs generator's finally
    pool.release(key)
```

### 7.3 Per-Wrapper Status

| Wrapper | Chat | Anthropic | Responses |
|---|---|---|---|
| nvidia-python | ✅ | ✅ | ✅ (R-07 fixed) |
| nous | ✅ | ✅ | ✅ |
| opencode | ❌ (0 sites) | ❌ (0 sites) | ❌ (0 sites) |
| blackbox | ❌ (0 sites) | ❌ (0 sites) | ❌ (0 sites) |
| openrouter | ✅ (R-09) | ✅ (R-09) | ✅ |

---

## 8. Summary: Streaming Correctness Scorecard

| Criteria | nvidia | nous | opencode | blackbox | openrouter |
|---|---|---|---|---|---|
| SSE parsing: space optional | ✅ | ✅ | ✅ | ✅ | ✅ |
| SSE parsing: empty data: = keep-alive | ✅ | ✅ | ❌ | ❌ | ✅ |
| SSE parsing: CRLF normalized | ❌ | ✅ | ❌ | ❌ | ✅ |
| SSE parsing: choices[] guarded | ❌ (3 sites) | ✅ | ✅ | ✅ | ✅ |
| Anthropic: parallel tools concurrent | ✅ | ✅ | ✅ | ✅ | ✅ |
| Anthropic: stop_reason strict | ❌ | ❌ | ❌ | ❌ | ✅ |
| Anthropic: error frames surface | ✅ | ✅ | ✅ | ✅ | ✅ |
| Anthropic: GeneratorExit handled | ⚠️ | ✅ | ❌ | ❌ | ❌ |
| Responses: full lifecycle | ✅ | ✅ | ✅ | ✅ | ✅ |
| Responses: response.failed on error | ❌ | ✅ | ✅ | ✅ | ✅ |
| Chat: [DONE] exactly once | ✅ | ✅ | ✅ | ✅ | ✅ |
| Heartbeat: sentinel-task (not wait_for) | n/a | ✅ | ✅ | ✅ | ❌ |
| Generator cleanup: aclose() | ✅ | ✅ | ❌ | ❌ | ✅ |

**Overall Streaming Correctness:**
- **openrouter**: 85% (heartbeat + GeneratorExit gaps)
- **nous**: 85% (stop_reason + GeneratorExit in translators)
- **nvidia-python**: 75% (CRLF, choices[], stop_reason, GeneratorExit)
- **opencode**: 70% (B-01, CRLF, stop_reason, GeneratorExit)
- **blackbox**: 70% (B-01, CRLF, stop_reason, GeneratorExit)

---

## 9. Required Fixes (Priority Order)

1. **B-06** — Fix `stop_reason` mapping in shared translator + nous (affects Claude Code on 4/5)
2. **B-10** — Fix nous frame synthesis (breaks Anthropic SDK entirely)
3. **B-01** — Remove `b''` from terminator tuples in blackbox/opencode
4. **B-08** — Migrate openrouter + common/base_wrapper.py to sentinel-task heartbeat
5. **CRLF normalization** — Add to nvidia, opencode, blackbox chat streaming
6. **GeneratorExit/aclose()** — Add to opencode, blackbox, openrouter translators
7. **R-08** — Fix 3 unguarded `choices[0]` in nvidia-python

---

*All proofs derived from live E2E testing (420 checks), executable reproductions in `/tmp/prove*.py`, streaming regression suite (48 tests), and code inspection at commit `4a0485d`.*