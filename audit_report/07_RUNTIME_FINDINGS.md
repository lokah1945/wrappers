# Runtime Findings (R-01 through R-08) — Deep Analysis

**Date:** 2026-08-01  
**Source:** `tests/e2e_runtime/run_runtime_e2e.py` — Live E2E harness booting real uvicorn servers against mock upstream  
**Verification:** 420 checks (5 wrappers × 3 surfaces × 21 upstream behaviors) — **0 failures** after fixes  
**Key Insight:** Every one of these passed the 110-test unit suite **before being fixed** — only reachable by driving real servers with agent-shaped traffic.

---

## R-01 · CRITICAL — Non-Object JSON Body → HTTP 500

### Root Cause
```python
# Handler pattern in ALL wrappers:
body = await request.json()  # Returns list/str/int for non-object JSON
model = body.get('model')    # list.get() → AttributeError → HTTP 500
```

### Trigger Inputs (All Valid JSON)
```json
[1, 2, 3]      // array
"string"       // string
42             // number
true           // boolean
null           // null
```

### Impact
- SDK receives **500** → assumes **server broken** → **retries amplify load**
- Should be **400** with shaped error: "Request body must be a JSON object"

### Affected Wrappers
| Wrapper | Routes Guarded | Routes Unguarded | Fix |
|---|---|---|---|
| nvidia-python | 0 | All | `common/body_guard.py` middleware |
| nous | 0 | All | `common/body_guard.py` middleware |
| opencode | 3 (`/v1/chat/completions`, `/v1/responses`, `/v1/messages`) | Others | `common/body_guard.py` middleware |
| blackbox | 0 | All | `common/body_guard.py` middleware |
| openrouter | 0 | All | `common/body_guard.py` middleware |

### Fix: `common/body_guard.py` (ASGI Middleware)
```python
class JSONBodyGuard:
    async def __call__(self, scope, receive, send):
        # 1. Buffer request body (max 64 MB)
        # 2. Parse JSON
        # 3. If not dict → reject with shaped 400
        # 4. Replay body to downstream via modified receive()
        
    def _reject(send, path, got_type):
        if is_anthropic_surface(path):
            payload = {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': f'Request body must be a JSON object, got {got_type}.'}}
        else:
            payload = {'error': {'type': 'invalid_request_error', 'message': f'Request body must be a JSON object, got {got_type}.', 'code': 'invalid_body_shape'}}
```

### Verification
```bash
test_r01_non_object_json_body_guard_registered_everywhere  # All 5 have JSONBodyGuard
test_r01_body_guard_rejects_non_objects_and_passes_objects  # Unit test
test_r01_body_guard_replay_delegates_after_body  # Critical: replay delegates to original receive()
```

### Critical Replay Bug (Fixed in Middleware)
```python
# WRONG (initial implementation):
async def receive():
    if not state['sent_body']:
        state['sent_body'] = True
        return {'type': 'http.request', 'body': body, 'more_body': False}
    return {'type': 'http.disconnect'}  # BREAKS STREAMING!

# CORRECT (current):
async def receive():
    if not state['sent_body']:
        state['sent_body'] = True
        return {'type': 'http.request', 'body': body, 'more_body': False}
    return await original_receive()  # Delegates — real disconnects work
```

**Why it mattered:** `StreamingResponse` runs a disconnect-watcher that calls `receive()`. Synthetic `http.disconnect` made it conclude client went away → cancel stream after first event.

---

## R-02 · CRITICAL — Parallel Tool Calls Protocol-Corrupt

### Root Cause (Shared Translator + 3 Wrappers)
```python
# WRONG: Opening tool #2 CLOSES tool #1
for tc in delta.get('tool_calls', []):
    if oi not in self.tool_map:
        events.extend(self._close_block())  # Closes PREVIOUS block (including tool!)
        self.index += 1
        self.tool_map[oi] = self.index
    # ...
```

### OpenAI Behavior
OpenAI **interleaves** argument fragments across ALL active tool indices:
```
Chunk 1: tool_calls[0].arguments = '{"a'
Chunk 2: tool_calls[1].arguments = '{"b'
Chunk 3: tool_calls[0].arguments = ':1}'   ← arrives AFTER tool #1 "closed"
Chunk 4: tool_calls[1].arguments = ':2}'
```

### Corrupt Output (Before Fix)
```
content_block_start (index 0, tool_use: alpha)
content_block_delta (index 0, '{"a')
content_block_stop  (index 0)  ← TOOL #1 CLOSED PREMATURELY
content_block_start (index 0, tool_use: beta)  ← INDEX REUSED!
content_block_delta (index 0, '{"b')
content_block_delta (index 0, ':1}')  ← ORPHANED on closed index
content_block_delta (index 0, ':2}')
content_block_stop (index 0)
```

### Impact on Claude Code
- Tool #1 arguments **never complete** → invalid JSON
- Tool #2 opens on **same index** → protocol violation
- Agent turn **stalls forever** waiting for tool_result

### Fix: `common/translations/anthropic_stream.py`
```python
# Track concurrently-open tool blocks
self.open_tool_blocks: set[int] = set()

def _close_block(self):  # Closes ONLY text/thinking
    if self.current_block is None or self.current_block == "tool_use":
        return []
    # ... emit content_block_stop ...

def _close_all_tool_blocks(self):  # Closes ALL tool blocks at terminal
    for idx in sorted(self.open_tool_blocks):
        events.append(content_block_stop(index=idx))
    self.open_tool_blocks.clear()

def translate_chunk(self, chunk):
    # On new tool:
    if oi not in self.tool_map:
        events.extend(self._close_block())  # Close text/thinking ONLY
        self.index += 1
        self.tool_map[oi] = self.index
        self.open_tool_blocks.add(self.index)  # Track as OPEN
        self.current_block = "tool_use"
    
    # On finish_reason:
    events.extend(self._close_everything())  # Close text + ALL tools
```

### NVIDIA-Specific Extra Bug (B-18)
```python
# nvidia-python/src/anthropic_compat.py: stop_open()
async def stop_open(self):
    for k in list(self.tool_map.keys()):
        # DELETES tool from map!
        del self.tool_map[k]  
        # Next fragment re-creates PHANTOM unnamed block
```

**Result:** 4 `content_block_start` events, all index 0, 2 phantom unnamed blocks.

### Verification
```bash
test_b03_parallel_tool_calls_distinct_blocks_and_all_arguments  # 2 tools, 3 chunks, all 4 args delivered
test_r02_parallel_tool_blocks_stay_open_concurrently  # No delta on closed index
test_r02_no_wrapper_closes_previous_tool_block_on_new_tool  # NVIDIA stop_open() fixed
```

### Proof Output (Fixed)
```
content_block_start count: 2   ✅
    index 0  tool_use id=call_a  name=alpha
    index 1  tool_use id=call_b  name=beta
input_json_delta events: 4     ✅
    index 0  partial_json '{"a'
    index 0  partial_json ':1}'
    index 1  partial_json '{"b'
    index 1  partial_json ':2}'
block indices used by starts: [0, 1] -> distinct: 2  ✅
reassembled args: {'a': 1}, {'b': 2}  ✅ valid JSON
```

---

## R-03 · CRITICAL — Mid-Stream Upstream Error Frames Swallowed

### Root Cause
```python
# Pattern in ALL translators:
if "choices" not in chunk:
    continue  # DROPS upstream {"error": {...}} frames!

# Stream then closes with fabricated end_turn
```

### Upstream Error Frame (Legal SSE)
```
data: {"error": {"message": "rate limit exceeded", "type": "rate_limit_error"}}
```

### Before Fix: Client Sees
```json
{"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
{"type": "message_stop"}
```
**Client persists truncated answer as successful turn — cannot retry.**

### After Fix: Client Sees (Anthropic)
```json
{"type": "error", "error": {"type": "rate_limit_error", "message": "rate limit exceeded"}}
{"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
{"type": "message_stop"}
```

### After Fix: Client Sees (Responses)
```json
{"type": "response.failed", "response": {"status": "failed", "error": {"code": "upstream_error", "message": "rate limit exceeded"}}}
data: [DONE]
```

### Fix Pattern (All 5 Wrappers + 2 NVIDIA Modules)
```python
# In translate_chunk / stream processor:
if isinstance(chunk, dict) and chunk.get("error") is not None and "choices" not in chunk:
    # Surface as REAL error event
    self.upstream_error = chunk["error"].get("message", "upstream error")
    events.extend(self._close_everything())
    events.append(error_event)
    events.extend(terminal_events)
    self.finished = True
    return events
```

### Verification
```bash
test_r03_upstream_error_frame_surfaces_not_swallowed  # Shared translator
test_r03_all_wrappers_handle_upstream_error_frames  # All 5 + 2 NVIDIA modules
```

### Wrappers Fixed
| Wrapper | Module | Status |
|---|---|---|
| nous | `src/main.py` | ✅ |
| opencode | `src/main.py` | ✅ |
| blackbox | `src/main.py` | ✅ |
| openrouter | `src/main.py` | ✅ |
| nvidia-python | `src/anthropic_compat.py` | ✅ |
| nvidia-python | `src/responses_compat.py` | ✅ |

---

## R-04 · CRITICAL — Loop Variable Shadows Function Parameter

### Root Cause (NVIDIA)
```python
# nvidia-python/src/anthropic_compat.py
async def stop_open(self, chunk):  # PARAMETER named 'chunk'
    # ...
    async for chunk in self.stop_events():  # LOOP VAR shadows parameter!
        yield chunk
    # After loop, 'chunk' = last SSE frame string
    # Downstream receives: "event: content_block_stop\ndata: {...}"
    # Rendered as ASSISTANT TEXT in Claude Code
```

### Same Class: 3 More Latent Instances in Same File
```python
# Line 706: async for chunk in ...
# Line 797: async for chunk in ...
# Line 1113: async for chunk in ...
```

### Impact
Only triggers on **reasoning models transitioning to text** — why unit tests missed it.

### Fix
```python
# Rename loop variable
async for ev in self.stop_events():
    yield ev
```

### Verification
```bash
test_r04_no_loop_variable_shadows_a_function_parameter  # AST scan across ALL 5 wrappers
```

**AST Scan Implementation:**
```python
def test_r04_no_loop_variable_shadows_a_function_parameter():
    import ast
    offenders = []
    for wrapper in ('nvidia-python', 'nous', 'opencode', 'blackbox', 'openrouter'):
        for py in (ROOT / wrapper / 'src').glob('*.py'):
            tree = ast.parse(py.read_text())
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                params = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
                for node in ast.walk(fn):
                    if isinstance(node, (ast.For, ast.AsyncFor)) and isinstance(node.target, ast.Name):
                        if node.target.id in params:
                            offenders.append(f'{py.name}:{node.lineno} {fn.name}() loop var shadows parameter')
    assert not offenders
```

---

## R-05 · CRITICAL — Raw OpenAI JSON Returned on Anthropic/Responses

### Root Cause (openrouter)
```python
# openrouter/src/main.py:1528
async def messages(request: Request):
    response = await _proxy_request(...)  # Returns JSONResponse
    
    if isinstance(response, JSONResponse):  # ALWAYS TRUE for non-streaming!
        return response  # RETURNS BEFORE TRANSLATION!
    
    # Translation code UNREACHABLE
    try:
        payload = json.loads(response.body)
        if isinstance(payload, dict) and 'choices' in payload:
            return JSONResponse(_openai_to_anthropic_response(payload, body))
```

### Before Fix: Client Receives
```json
{"object": "chat.completion", "choices": [{"message": {"role": "assistant", "content": "..."}}]}
```

### After Fix: Client Receives
```json
{"type": "message", "role": "assistant", "content": [{"type": "text", "text": "..."}], "stop_reason": "end_turn"}
```

### Impact
**Claude Code and Codex cannot parse the "before" shape at all** — every non-streaming turn broken.

### Verification
```bash
test_r05_openrouter_translates_non_streaming_responses
# Verifies translation call exists in both messages() and responses() handlers
```

---

## R-06 · HIGH — Duplicate `[DONE]` Terminator

### Root Cause
```python
# Appends [DONE] unconditionally
yield "data: [DONE]\n\n"

# But upstream ALREADY sent one WITHOUT trailing blank line
# Result: "[DONE]data: [DONE]" — corrupt frame, not valid JSON
```

### Upstream Pattern (Real)
```
data: [DONE]          ← no trailing \n\n
EOF
```

### Corrupt Output
```
data: [DONE]
data: [DONE]          ← appended
```
Combined: `data: [DONE]data: [DONE]` — strict SDK parsers error at end of turn.

### Fix: `saw_done` Guard (All 4 Wrappers + common/base_wrapper.py)
```python
saw_done = False
async for raw in upstream:
    if b'[DONE]' in raw:
        saw_done = True
    yield raw

if not saw_done:
    yield b'data: [DONE]\n\n'
elif not ends_with_newline:
    yield b'\n\n'  # Complete upstream's frame
```

### nVIDIA `common/base_wrapper.py` Also Fixed
```python
# Same pattern in shared base class
```

### Verification
```bash
test_r06_no_duplicate_done_terminator  # saw_done guard present in all 4 + base_wrapper
```

### Nous Exemption (Verified Separately)
```bash
# Nous only emits [DONE] inside `if state is None:` pass-through branch
# And uses `terminated` flag making path single-shot
test_r06_no_duplicate_done_terminator  # Checks nous explicitly
```

---

## R-07 · HIGH — Upstream Generator Never Closed

### Root Cause (NVIDIA Responses)
```python
# responses_compat.py:stream_wrapper()
async for raw in stream:
    yield raw
# Breaking out leaves generator SUSPENDED
# finally block (release response + pool key) NEVER RUNS
```

### Impact Under Load
- `MAX_CONNECTIONS` exhausted (200 default)
- Every request blocks forever
- Key pool still reports `available` (keys never released)

### Fix
```python
_ac = getattr(stream, 'aclose', None)
if _ac is not None:
    try:
        await _ac()  # Runs generator's finally
    except Exception:
        pass
```

### Verification
```bash
test_r07_nvidia_responses_closes_upstream_generator
# Checks for "_ac = getattr(stream, 'aclose', None)" pattern
```

---

## R-08 · CRITICAL — Empty `choices: []` Crashes Stream

### Root Cause
```python
# Multiple sites across wrappers:
chunk = chunk["choices"][0]  # IndexError on legal empty choices
```

### Legal Upstream Frames with Empty Choices
- Usage-only frames (no delta, just usage)
- Provider keep-alives
- Model loading responses

### Before Fix: HTTP 500 Mid-Stream
```
File "nous/src/main.py", line 1845, in translate_chunk
IndexError: list index out of range
```

### Fix: Guard Before Index
```python
# Shared translator (correct):
ch = (chunk.get("choices") or [{}])[0]  # Default empty dict

# NVIDIA fixed 3 sites:
choices = chunk.get("choices") or []
if not choices:
    return events
ch = choices[0]
```

### Verification
```bash
test_r08_empty_choices_array_does_not_crash  # Shared translator handles correctly
test_r08_no_unguarded_choices_indexing  # Regex scan across ALL 5 wrappers
```

**Regex Scan:**
```python
pattern = re.compile(r"""\[["']choices["']\]\[0\]""")
for wrapper in all_wrappers:
    for py in wrapper.glob('*.py'):
        for n, line in enumerate(lines):
            if pattern.search(line):
                # Check for inline guard: `or [{}]` or `or []`
                # Check for explicit length check above
                offenders.append(...)
```

**Found by Guard (Not Manual Review):** 2 unguarded sites in NVIDIA that manual review missed.

---

## Summary: Runtime Findings Impact

| Finding | Severity | Wrappers Affected | Agent Impact | Fixed? |
|---|---|---|---|---|
| R-01 | CRITICAL | 5 (opencode partial) | SDK retry storms | ✅ (middleware) |
| R-02 | CRITICAL | 4 (shared + nous) | Claude Code hangs | ✅ |
| R-03 | CRITICAL | 5 + 2 NVIDIA modules | Silent truncation | ✅ |
| R-04 | CRITICAL | 1 (4 latent) | SSE frames as text | ✅ |
| R-05 | CRITICAL | 1 (openrouter) | Anth/Resp broken | ✅ |
| R-06 | HIGH | 4 + base_wrapper | SDK parse error | ✅ |
| R-07 | HIGH | 1 (nvidia responses) | Pool exhaustion | ✅ |
| R-08 | CRITICAL | 2 (nous + nvidia) | Stream crash | ✅ |

**All 8 runtime findings FIXED and verified by:**
- 420/420 E2E checks passing
- 10 regression tests in `test_sse_streaming_regressions.py`
- 4 CI parity guards preventing regression

---

*Every finding reproduced with live server + mock upstream, fixed, and verified against all 5 wrappers. Code evidence at commit `4a0485d`.*