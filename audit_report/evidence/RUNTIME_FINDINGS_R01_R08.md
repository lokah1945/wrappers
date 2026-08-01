# Evidence Artifact: Runtime Findings (R-01 through R-08) Summary

**Source:** `tests/e2e_runtime/run_runtime_e2e.py` — Live E2E Harness  
**Date:** 2026-08-01  
**Result:** 420/420 checks passed after fixes

---

## Harness Overview

### Mock Upstream (`tests/e2e_runtime/mock_upstream.py`)
Produces 21 pathological-but-legal upstream behaviors:
```
normal      nospace     keepalive    crlf       tools       reasoning
nofinish    noterminator midstream_error abrupt  slow       usage_after
empty       unicode     bigchunk     bytesplit  comments    dupfinish
nullcontent emptychoices toolnoid     longtool   http500    http429
```

### Test Runner (`tests/e2e_runtime/run_runtime_e2e.py`)
- Boots each wrapper as real `uvicorn` server
- Drives it exactly as Claude Code / Codex / OpenAI SDK would
- Validates: event ordering, block lifecycle, terminal events, malformed input → 4xx, client disconnect handling, 12 concurrent streams

---

## The 8 Runtime Findings

### R-01 · CRITICAL · All 5 Wrappers — HTTP 500 on Non-Object JSON Body
**Trigger:** Valid JSON that isn't an object: `[1,2,3]`, `"str"`, `42`, `true`, `null`

**Root Cause:**
```python
body = await request.json()  # Returns list/str/int
model = body.get('model')    # list.get() → AttributeError → HTTP 500
```

**Impact:** SDK receives 500 → assumes server broken → retries amplify load

**Fix:** `common/body_guard.py` ASGI middleware on all JSON routes

**Pre-Fix:** opencode guarded 3 routes; other 4 had NO guard

---

### R-02 · CRITICAL · Shared Translator + 3 Wrappers — Parallel Tool Calls Corrupt
**Root Cause:** Opening tool #2 **closes** tool #1
```python
# WRONG:
if oi not in self.tool_map:
    events.extend(self._close_block())  # Closes PREVIOUS tool!
    self.index += 1
    self.tool_map[oi] = self.index
```

**OpenAI Behavior:** Interleaves argument fragments across ALL active tool indices

**Corrupt Output:**
```
start(0) delta(0) STOP(0) start(1) delta(1) delta(0) delta(1) stop(1)
            ↑ closed too early      ↑ orphaned
```

**NVIDIA Extra Bug:** `stop_open()` deletes tool from `tool_map` → phantom unnamed blocks

**Fix:** Track `open_tool_blocks`, close all at terminal path

---

### R-03 · CRITICAL · All 5 — Mid-Stream `{"error":...}` Frames Swallowed
**Root Cause:**
```python
if "choices" not in chunk:
    continue  # DROPS upstream error frames!
```
Upstream error frame has no `"choices"` key → discarded → stream closes with fabricated `end_turn`

**Client Impact:** Persists truncated answer as successful turn → **cannot retry**

**Fix:** Surface as real Anthropic `error` event / `response.failed`

---

### R-04 · CRITICAL · NVIDIA — Loop Variable Shadows Parameter
```python
async def stop_open(self, chunk):  # PARAMETER 'chunk'
    async for chunk in self.stop_events():  # LOOP VAR shadows parameter!
        yield chunk
    # 'chunk' now = last SSE frame string
    # Rendered as ASSISTANT TEXT in Claude Code
```

**3 More Latent Instances** in same file (found by AST scan)

**Fix:** Rename loop variable

---

### R-05 · CRITICAL · OpenRouter — Raw OpenAI JSON on Anth/Resp Surfaces
```python
async def messages(request):
    response = await _proxy_request(...)  # Returns JSONResponse
    if isinstance(response, JSONResponse):  # ALWAYS TRUE for non-streaming
        return response  # RETURNS BEFORE TRANSLATION!
    # Translation code UNREACHABLE
```

**Before:** `{"object":"chat.completion","choices":[...]}`  
**After:** `{"type":"message","role":"assistant","content":[...],"stop_reason":"end_turn"}`

**Claude Code/Codex cannot parse "before" shape**

---

### R-06 · HIGH · OpenRouter + common/base_wrapper.py — Duplicate `[DONE]`
```python
# Appends unconditionally
yield "data: [DONE]\n\n"
# But upstream already sent one WITHOUT trailing blank line
# Result: "data: [DONE]data: [DONE]" — corrupt frame
```

**Fix:** `saw_done` guard + line-boundary completion

---

### R-07 · HIGH · NVIDIA Responses — Upstream Generator Never Closed
```python
async for raw in stream:
    yield raw
# Breaking out leaves generator SUSPENDED
# finally (release response + pool key) NEVER RUNS
```

**Under Load:** `MAX_CONNECTIONS` exhausted → every request blocks forever

**Fix:** `await stream.aclose()` in `finally`

---

### R-08 · CRITICAL · Nous + NVIDIA — Empty `choices: []` Crashes Stream
```python
chunk = chunk["choices"][0]  # IndexError on legal empty choices
```

**Legal Upstream Frames:** Usage-only frames, provider keep-alives

**Error:** `IndexError: list index out of range` → HTTP 500 mid-stream

**Fix:** Guard before index: `choices = chunk.get("choices") or [{}]`

**Found by Guard:** 2 unguarded sites in NVIDIA that manual review missed

---

## Cross-Wrapper Verification Table

| Finding | Found In | Also Fixed In | Already Correct |
|---|---|---|---|
| R-01 | nvidia | nous, blackbox, openrouter, opencode ungated | opencode (3 routes) |
| R-02 | shared translator | nous, nvidia, openrouter | opencode, blackbox |
| R-03 | opencode | nous, blackbox, openrouter, nvidia (×2) | — |
| R-04 | nvidia | 3 more latent in same file | others |
| R-05 | openrouter | — | others translate correctly |
| R-06 | openrouter | **common/base_wrapper.py** | nvidia, opencode, blackbox, nous |
| R-07 | nvidia responses | — | anthropic_compat correct |
| R-08 | nous | nvidia ×3 (2 found by guard) | opencode, blackbox, openrouter |

**6 of 8 findings existed in >1 wrapper**  
**2 found ONLY by automated guards after manual review missed them**

---

## CI Parity Guards (Now Enforced)

| Guard | Test | Prevents |
|---|---|---|
| No loop var shadows parameter | `test_r04_no_loop_variable_shadows_a_function_parameter` | R-04 class |
| No unguarded `choices[0]` | `test_r08_no_unguarded_choices_indexing` | R-08 |
| Sentinel-task heartbeat | `test_parity_all_wrappers_use_sentinel_heartbeat_not_wait_for` | B-08 |
| No shadowing shared cooldown | `test_parity_no_wrapper_shadows_shared_cooldown_helper` | B-21 |

---

## Verification Commands
```bash
# Unit + regression suite (127 tests)
python -m pytest tests -q

# Live E2E (420 checks)
python tests/e2e_runtime/run_runtime_e2e.py

# Soak (10k+ requests)
python tests/e2e_runtime/soak.py --seconds 12 --concurrency 6

# Streaming regressions (48 tests)
python -m pytest tests/test_sse_streaming_regressions.py -v
```