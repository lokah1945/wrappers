# Bug Analysis — `lokah1945/wrappers`

**Date:** 2026-07-31 · **Branch:** `arena/019fba14-wrappers` · **Base commit:** `bb53ad7`
**Scope:** static + behavioural analysis of all 5 wrappers (`nvidia-python`, `nous`, `opencode`, `blackbox`, `openrouter`), `common/`, `model-registry`, `runtime`, `tests`.
**No source files were modified.** Only this report was added.

---

## 0. Repo / build status

| Check | Result |
|---|---|
| `git fetch origin` | already up to date with `origin/main` (`bb53ad7`) |
| Test suite (`pytest tests`) | **79 passed** — but only after manually installing `python-dotenv`, `aiosqlite`, `uvicorn`, `pytest-asyncio` (see B-14) |
| `pyflakes` on all tracked `.py` | 1 hard error, ~60 warnings (see §4) |

> Note: the requested PAT was not needed and was not used — the sandbox already has GitHub auth. **Please revoke that token anyway**: it was pasted in plaintext into a chat transcript, which makes it compromised regardless of its 24h TTL.

---

## 1. Root causes of "proses berhenti di tengah jalan" (Claude Code / Codex hang or truncate)

### B-01 · CRITICAL — empty SSE payload treated as end-of-stream (blackbox, opencode)
`blackbox/src/main.py:1445`, `blackbox/src/main.py:1621`, `opencode/src/main.py:1633`, `opencode/src/main.py:1829`

```python
if payload in (b'[DONE]', b'', b'"[DONE]"'):
    for ev in state.force_done():
        yield ev
    return          # ← stream terminated
```

An SSE line `data:` with an empty value is a **legal keep-alive / empty event**, not a terminator. Many upstreams (and any proxy/CDN inserting padding) emit it. When it arrives, both wrappers emit `message_stop` and `return` — the model is still generating, but the client sees a complete turn. This is exactly the "process stops halfway" symptom in Claude Code and Codex.

`nous` already fixed this (`N-09`, `nous/src/main.py:1288-1291`: *"an empty `data:` payload is a valid (empty) SSE event, NOT end-of-stream"*). The fix was **never ported** to blackbox/opencode — a cross-wrapper parity violation under `CROSS_WRAPPER_BUG_POLICY.md`.

**Fix:** drop the `b''` case from the terminator tuple in all four sites; `continue` instead.

---

### B-02 · CRITICAL — openrouter drops every SSE line not prefixed `data: ` (with a space)
`openrouter/src/main.py:937` and `openrouter/src/main.py:1669`

```python
if not line_str.startswith('data: '):
    continue
...
data_str = line_str[6:].strip()
```

Every other wrapper matches `data:` and then strips. OpenRouter requires the **space**. Any upstream/relay emitting the equally-valid `data:{"id":...}` produces a stream where *100% of chunks are silently discarded*: the client gets `message_start` … `message_stop` with zero content, i.e. an empty answer or a run that appears to abort. There is no log line and no error event — completely silent.

Compounding it, `openrouter/src/main.py:980` also drops any chunk where `data.get('object') != 'chat.completion.chunk'`, which silently discards upstream **error chunks** (`{"error": {...}}` mid-stream) and any provider that omits the `object` field. The stream then just ends with a fabricated `stop_reason: end_turn`.

**Fix:** `startswith('data:')` + `line[5:].strip()`; and surface non-chunk payloads containing `error` as a terminal error event instead of dropping.

---

### B-03 · HIGH — openrouter tool-call translation emits duplicate `content_block_start` and loses arguments
`openrouter/src/main.py:1706-1735`

```python
for tc in delta['tool_calls']:
    tc_idx = tc.get('index', 0)
    fn = tc.get('function', {})
    if tc_idx not in tool_call_blocks:
        ...close text block...
        tool_call_blocks[tc_idx] = block_index
    yield _sse('content_block_start', {...})   # ← OUTSIDE the guard
    block_open = True
if fn.get('arguments'):                        # ← OUTSIDE the for loop
    yield _sse('content_block_delta', {... 'index': tool_call_blocks[tc_idx] ...})
```

Three distinct defects in one block:

1. `content_block_start` is emitted **for every tool-call delta chunk**, not once per tool. The Anthropic SDK / Claude Code receives repeated `content_block_start` on the same index and either raises a protocol error or discards the block → tool call never executes, agent turn stalls.
2. `if fn.get('arguments')` sits **outside** the `for` loop, so with parallel tool calls only the **last** `tc` in the chunk contributes arguments; every other tool's argument fragments are dropped → truncated/invalid JSON in `input_json_delta` → tool invocation fails.
3. `block_index` is **never incremented** when a tool block opens, so two parallel tools collide on the same block index.

There is also a latent `NameError` on `fn`/`tc_idx` if `delta['tool_calls']` is a non-empty structure that iterates zero times.

---

### B-04 · HIGH — no `content_block_stop` before the tool block, and unbalanced blocks on error path (openrouter)
Same function: `_close_block` semantics don't exist here. The text block is closed only when `block_open and text_started`; a thinking/tool block opened earlier is never closed. On the `except` path (`openrouter/src/main.py:1758-1770`) the code emits `content_block_stop` for `block_index` even if the currently open block index differs (see B-03.3) → the client closes a block it never opened.

---

### B-05 · HIGH — `AnthropicStreamState.translate_chunk` drops trailing content after `finish_reason` (shared)
`common/translations/anthropic_stream.py:88-96`

```python
if self.finished:
    if chunk.get("usage"): self.last_usage = ...
    return []
```

Correct per Anthropic protocol, but several upstreams send `finish_reason` in the *same* chunk batch as the last content deltas, or send a final content chunk after a premature `finish_reason: null`→`stop`. Everything after the first `finish_reason` is discarded with no warning and no metric. Combined with B-01/B-02 this is the second largest source of truncated answers. At minimum this needs a counter/log so truncation is observable.

---

### B-06 · MEDIUM — `finish_reason` mapping forces `tool_use` whenever any tool was ever seen
`common/translations/anthropic_stream.py:174`, `nous/src/main.py:1512`

```python
stop = "tool_use" if (fr == "tool_calls" or self.tool_map) else {...}.get(fr, "end_turn")
```

If a turn emits a tool call *and then* the model finishes with `stop` (text after tool result, or `length`), the wrapper still reports `stop_reason: "tool_use"`. Claude Code then waits for a `tool_result` that will never come → the agent loop hangs. It also masks `max_tokens` truncation: a `length` finish is reported as `tool_use`, so the client never learns the output was cut. This is a direct "berhenti di tengah jalan" mechanism.

---

### B-07 · MEDIUM — `force_done()` fabricates a successful `end_turn` after an upstream failure
`blackbox/src/main.py:1633-1636`, `opencode/src/main.py:1843-1846`

```python
except Exception as e:
    logger.error(f'[anthropic stream] {e}')
    for ev in state.force_done():
        yield ev
```

A connection reset, read timeout, or JSON blow-up mid-generation is converted into a clean `message_delta{stop_reason: end_turn}` + `message_stop`. The client believes the (partial) answer is complete and **cannot retry**, because nothing looks like an error. `nous` handles this better (`N-05`, emits `: upstream-error <Type>` first, `nous/src/main.py:1401`), and `blackbox`'s Responses path handles it correctly too (`response.failed`, `blackbox/src/main.py:1510-1518`). The Anthropic paths in blackbox/opencode are the outliers. Emit an `error` SSE event (`event: error`) before the terminal events.

Same shape in `openrouter/src/main.py:1756` (bare `except` → fabricated `end_turn`) and `openrouter/src/main.py:1041` (fabricated `response.completed` with whatever partial text was accumulated).

---

### B-08 · MEDIUM — heartbeats can corrupt SSE frames in openrouter / base_wrapper
`openrouter/src/main.py:676-690`, `common/base_wrapper.py:556-571`

```python
chunk = await asyncio.wait_for(inner.__anext__(), timeout=hb_interval)
except asyncio.TimeoutError:
    if at_line_boundary:
        yield b': heartbeat\n\n'
```

Two problems:

1. `asyncio.wait_for` **cancels** the pending `__anext__()` on timeout. On the next loop iteration a *new* `__anext__()` is started on the same iterator. For `aiohttp`'s `content` iterator this is usually survivable, but any partially-consumed read is lost and a genuine `aiohttp` `ServerTimeoutError`/`asyncio.TimeoutError` from the socket is **indistinguishable** from an idle tick — so a dead upstream is heartbeated forever and the client hangs until *its own* timeout. `nous` (`N-05`) and `blackbox`/`opencode` (`_iter_chunks_with_idle`) already use the correct sentinel-task + `asyncio.wait` pattern. openrouter and `common/base_wrapper.py` were never migrated.
2. In `base_wrapper.py:571`, `last_hb` is assigned but never read — the heartbeat interval is effectively unthrottled (pyflakes confirms the dead variable).

This is the most likely cause of Codex sessions that sit "thinking" indefinitely against openrouter.

---

### B-09 · MEDIUM — openrouter Anthropic/Responses translators never release the upstream key on client disconnect
`openrouter/src/main.py:861` and `openrouter/src/main.py:1217` wrap `response.body_iterator` in a **second** generator. Key/connection release lives in the *inner* `stream_gen().finally` (`openrouter/src/main.py:668-675`). When the outer translator raises or the client disconnects, the outer generator's `except Exception` swallows it and returns normally — but nothing guarantees the inner generator is closed promptly; it is left to GC. Under load this leaks in-flight slots (`pool.release` never called) and the key pool starves → later requests 429/hang. The `try/except Exception` at `openrouter/src/main.py:1756` explicitly catches and *does not re-raise*, which also swallows `GeneratorExit`-adjacent cancellation semantics.

---

## 2. Root cause of the raw SSE text leaking into the Claude Code terminal

The transcript you pasted shows literal frames rendered as assistant text:

```
● event: content_block_stop
  data: {"type": "content_block_stop", "index": 0}
```

### B-10 · CRITICAL — non-JSON SSE payloads are wrapped as model content (nous)
`nous/src/main.py:1302-1304`

```python
try:
    parsed = json.loads(data)
except Exception:
    parsed = {"choices": [{"delta": {"content": data.decode(errors='replace')}}]}
```

**This is the bug.** Any `data:` line the wrapper cannot parse as JSON is converted into a fake OpenAI delta whose `content` is the *raw line text*, which `AnthropicStreamState.translate_chunk` then re-emits as a `text_delta`. If the upstream is already speaking Anthropic SSE (or a relay re-frames the stream), the wrapper ingests `event: content_block_stop` / `data: {...}` lines, fails to interpret them as chat chunks, and **renders them as assistant prose** — precisely the output you saw, including the leading `event:` line and the indented `data:` line.

The same class of leak exists at `nous/src/main.py:1313` (`state is None` passthrough re-frames arbitrary bytes as `data: ...`).

**Fix:** never synthesise content from unparsable frames. Log + drop (`continue`), matching blackbox/opencode/openrouter which all `continue` on `json.JSONDecodeError`.

### B-11 · HIGH — `str(dict)` serialisation fallback (nous Responses path)
`nous/src/main.py:2473`

```python
async for line in stream_with_heartbeat(result, lambda x: x if isinstance(x, str) else str(x), state=state, ...)
```

If `ResponsesStreamState` ever returns a dict event (it is documented at `nous/src/main.py:1639` that it "MUST return a list" of strings, but nothing enforces it), the serializer emits the Python `repr` of the dict — `{'type': 'response...'}` with single quotes — straight into the SSE body. Codex then either ignores the frame or surfaces it as text. Contrast with `nous/src/main.py:2561`, where the Anthropic path builds a proper `event:/data:` frame. The two serializers on the same helper are inconsistent.

### B-12 · MEDIUM — nvidia passthrough forwards upstream frames verbatim
`nvidia-python/src/main.py:2278` (`_stream_chat`) yields `chunk_str` unchanged. If NIM (or a proxy) returns Anthropic-style frames on the chat endpoint, they reach the OpenAI-SSE client raw. There is no validation that a forwarded frame is a `chat.completion.chunk`.

### B-13 · LOW — the same generator also fabricates an error *as content*
`nvidia-python/src/main.py:2306-2312` yields a friendly "context too large…" message as a `data:` **error** object, which is correct, but `nvidia-python/src/responses_compat.py:676-682` and `nvidia-python/src/anthropic_compat.py:1113` inject `[upstream stream error: …]` as an **`output_text.delta` / `text_delta`** — i.e. transport errors are persisted as model output. `blackbox` explicitly documents fixing this (`B20`); nvidia still does it in two places.

---

## 3. Other defects found

| ID | Sev | Location | Issue |
|---|---|---|---|
| **B-14** | HIGH | repo root | No `requirements.txt` / lockfile for the test suite. `pytest tests` fails at collection until `python-dotenv`, `aiosqlite`, `uvicorn`, `pytest-asyncio` are installed by hand. CI on a clean runner cannot pass. |
| **B-15** | HIGH | `nvidia-python/src/main.py:3098` | `if __name__ == "__main__": main()` — **`main` is undefined** (pyflakes hard error). Running the module directly raises `NameError`. Also `app = create_app()` sits *after* the `__main__` guard, which is fragile ordering. |
| **B-16** | MED | `tests/test_protocol_conversion_matrix.py:110` | `@pytest.mark.asyncio` with no `asyncio_mode`/plugin registered → the async test is **silently skipped/no-op** while reporting "passed". The 79/79 green result overstates real coverage. |
| **B-17** | MED | `common/base_wrapper.py:387` | `method` assigned and never used — the request method is likely hard-coded downstream; verify non-POST verbs are not silently converted. |
| **B-18** | MED | `nvidia-python/src/anthropic_compat.py:706,797` | 10 × `nonlocal X` where `X` is never assigned in that scope. The intended state mutation (`sent_content_block_start`, `real_thinking_emitted`, `next_index`, `open_idx`, …) is **not happening**, so thinking-block bookkeeping and block indices can desync → duplicated/missing `content_block_start` on the NVIDIA Anthropic surface. Same family as B-03. |
| **B-19** | MED | `blackbox/src/main.py:1723`, `nous/src/main.py:2638` | `body = await request.json()` assigned but never used in a handler — request validation is being skipped on that route. |
| **B-20** | MED | 5 × `_resolve_git_root()` (`blackbox:235`, `nous:452`, `nvidia:735`, `opencode:172`, `openrouter:231`, `model-registry/service.py:49`) | Synchronous `subprocess.check_output('git …')` called from async request handlers (health/version endpoints). Blocks the event loop for the duration of a fork+exec; under load this stalls *all* in-flight streams on that worker. Cache the SHA once at startup. |
| **B-21** | MED | `blackbox:513`, `nous:757` | `_should_cooldown_key` is defined twice; the local redefinition shadows the shared `common.translations` implementation, so cooldown policy silently diverges from the other wrappers. Same for `sanitize_header_value` (`blackbox:72` vs `:47`) and `free_only_enabled` (`blackbox:1707` vs `:290`, `nous:2620` vs `:475`). |
| **B-22** | LOW | `blackbox:1027`, `nous:1977` | `global _session` / `global _SESSION` declared but never assigned in scope — the session is never actually replaced on reconnect. |
| **B-23** | LOW | `blackbox:1310-1311` | `request_id` / `start_time` computed then discarded → no per-request latency metric or correlation ID on the busiest endpoint, despite the code implying otherwise. |
| **B-24** | LOW | `nvidia-python/src/main.py:166` | `global _unavailable_models, _retired_models, _model_status` never assigned → the model-availability refresh mutates nothing (or relies on in-place mutation that isn't obvious). Verify retired-model filtering actually updates. |
| **B-25** | INFO | root | 20+ `AUDIT_*.md` / `*_REPORT.md` files and stray artifacts (`nvidia_*_test_report.json`, `test_nvidia_llms.py`, `retry_nvidia_failed.py`) committed at repo root. Several claim "100% perfect / zero bug", which conflicts with the findings above and makes the docs untrustworthy as a source of truth. |

---

## 4. Cross-wrapper parity matrix (streaming correctness)

| Behaviour | nvidia | nous | opencode | blackbox | openrouter |
|---|---|---|---|---|---|
| Empty `data:` ≠ DONE | ✅ | ✅ (N-09) | ❌ **B-01** | ❌ **B-01** | ✅ |
| Accepts `data:` without space | ✅ | ✅ | ✅ | ✅ | ❌ **B-02** |
| Idle heartbeat via sentinel task (not `wait_for`) | n/a | ✅ | ✅ | ✅ | ❌ **B-08** |
| Heartbeat only at line boundary | n/a | ⚠️ | ✅ (OC-5) | ✅ (BB-6) | ⚠️ partial |
| CRLF SSE framing tolerated | ⚠️ | ✅ (N-08) | ❌ | ❌ | ❌ |
| Upstream error surfaced (not faked `end_turn`) | ⚠️ | ✅ (N-05) | ❌ **B-07** | ❌ (Anthropic) / ✅ (Responses) | ❌ **B-07** |
| Tail flush without trailing blank line | ✅ | ✅ | ✅ | ✅ | ❌ |
| Unparsable frame dropped (not rendered as text) | ⚠️ **B-12** | ❌ **B-10** | ✅ | ✅ | ✅ |
| Exactly-once key release on stream | ✅ | ✅ | ✅ | ✅ | ⚠️ **B-09** |

Legend: ✅ correct · ⚠️ partial/unverified · ❌ defect.

---

## 5. Recommended fix order

1. **B-10** — stop rendering unparsable SSE as assistant text (nous). *One-line fix, directly explains your terminal artifact.*
2. **B-01** — remove `b''` from the DONE terminator tuple (4 sites, blackbox + opencode). *Directly explains mid-run stops.*
3. **B-02** — accept `data:` without the space (openrouter, 2 sites).
4. **B-03 / B-04** — rewrite openrouter's tool-call block: move `content_block_start` inside the `not in tool_call_blocks` guard, move the arguments emit inside the `for`, increment `block_index`.
5. **B-06** — only force `tool_use` when `finish_reason == 'tool_calls'`.
6. **B-07** — emit `event: error` (Anthropic) / `response.failed` (Responses) before terminal events on exception.
7. **B-08 / B-09** — port the `_iter_chunks_with_idle` sentinel pattern to openrouter + `common/base_wrapper.py`; add explicit `aclose()` of the inner iterator in the translator `finally`.
8. **B-18** — fix the 10 no-op `nonlocal` declarations in `anthropic_compat.py`.
9. **B-14 / B-15 / B-16** — pin deps, fix the `main` NameError, register `pytest-asyncio` so async tests actually run.
10. Consolidate the duplicated helpers (B-21) into `common/translations` so parity bugs stop reappearing per-wrapper.

### Suggested regression tests (currently missing)
- SSE stream containing a bare `data:` keep-alive line mid-generation → full content must arrive.
- SSE stream using `data:{...}` (no space) → full content must arrive.
- Two parallel tool calls in one delta → exactly two `content_block_start`, distinct indices, both argument streams complete.
- Upstream socket reset mid-stream → client receives an error event, **not** `stop_reason: end_turn`.
- Upstream emits Anthropic frames on the chat surface → wrapper must not echo them as text.

---

*Analysis only — no wrapper source was modified.*
