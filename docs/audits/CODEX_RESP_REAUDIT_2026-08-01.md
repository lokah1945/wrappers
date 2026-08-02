# Codex / OpenAI Responses Re-Audit — CODEX-RESP-01 Fix Verification

**Date:** 2026-08-01  \
**Branch:** `arena/019fbee0-wrappers`  \
**Scope:** openrouter Responses streaming fix + remaining technical-debt sweep across all 5 wrappers, `common/`, `model-registry/`, `tests/`

---

## 1. The Critical Bug (CODEX-RESP-01) — Fixed

**Symptom (user report):** *Codex tidak bisa menyelesaikan semua proses-nya dan
berhenti di tengah jalan dan tidak sampai final respon di hasilkan* — Codex
stops mid-process and never produces a final response.

**Root cause (from `audit_report/DEEP_AUDIT_REPORT_2026-08-01.md` §7b):**
`openrouter/src/main.py::_translate_openai_stream_to_responses` gated its
completion events behind `if text_started:`. When a model emitted **only**
`reasoning_content`/`thinking` deltas (no text content), `text_started` stayed
`False` and the wrapper skipped `response.output_text.done`,
`response.content_part.done` and `response.output_item.done`. The message item
was also never announced with `response.output_item.added`. Codex therefore
never saw its output items open/close and waited indefinitely for the terminal
events.

**Reproduced before the fix** (reasoning-only stream through the translator):

```
event types seen: ['response.created', 'response.in_progress',
                   'response.completed', '[DONE]']
item.done events: []
→ response.completed references a message item that was never added or closed
```

---

## 2. The Fix (`openrouter/src/main.py`)

`_translate_openai_stream_to_responses` was rewritten to mirror the proven
`nous::ResponsesStreamState.done()` and opencode inline `gen()`:

1. **Message item opened eagerly** — `response.output_item.added` (index 0) +
   `response.content_part.added` are emitted immediately after
   `response.in_progress`, so every stream (including reasoning-only) has an
   active item.
2. **Reasoning streamed** — `reasoning_content`/`reasoning` deltas open a
   `reasoning` output item and emit `response.reasoning_text.delta`; closed by
   `response.reasoning_text.done` + `response.output_item.done`.
3. **Tool calls streamed** — `tool_calls` deltas open `function_call` items
   with `response.function_call.delta` (name + argument fragments) and are
   closed with `response.output_item.done`; arguments reassemble to valid JSON
   (this surface previously **dropped** tool calls entirely).
4. **Completion events are unconditional** — the `if text_started:` guard is
   gone; `output_text.done` / `content_part.done` / `output_item.done` are
   always emitted, followed by `response.completed` (or `response.failed` on
   upstream error) and exactly one `data: [DONE]`.
5. **`response.completed` output array** — sorted by `output_index`: message
   (0), reasoning, then tool items — matching what was actually streamed.

**After the fix** (same reasoning-only stream):

```
['response.created', 'response.in_progress', 'response.output_item.added',
 'response.content_part.added', 'response.output_item.added',
 'response.reasoning_text.delta', 'response.reasoning_text.delta',
 'response.reasoning_text.done', 'response.output_item.done',
 'response.output_text.done', 'response.content_part.done',
 'response.output_item.done', 'response.completed', '[DONE]']
→ full lifecycle, terminal event present: Codex no longer hangs
```

---

## 3. Other Findings Resolved This Cycle

| ID | Finding | Fix |
|---|---|---|
| **B-39** | openrouter `record_error()` was dead code → local error responses never incremented the error counter (false health) | New `_error_response()` helper calls `metrics.record_error()` for every local error response (auth rejections, invalid JSON, FREE_ONLY blocks, call-plan errors, pool exhaustion, MCP 503s, management 501/502). |
| **B-20** | git identity resolution ran blocking subprocesses with no timeout (audit: "blocking git in /health") | `timeout=3` added to every git `subprocess.check_output` call in all 5 wrappers + model-registry. (The calls actually run once at import — not per-request — but they were unbounded.) |

Re-verified as **already fixed** in the current code (guards added where missing):

| ID | Finding | Status |
|---|---|---|
| B-36 | `record()` conflates telemetry with in-flight (blackbox, nous) | Fixed in code; **new guard** `test_b36_*` added |
| B-33 | blackbox response store missing TTL/byte cap | Fixed in code; covered by `test_b33_*` |
| B-34 | no graceful shutdown drain (openrouter, nvidia-python) | Fixed in code (lifespan drain loops) |
| B-35 | BG task registry incomplete (openrouter) | Fixed in code (`_spawn_background` + `_drain_background_tasks`) |

---

## 4. New Regression Coverage

| Test / harness | What it locks in |
|---|---|
| `test_codex_resp01_reasoning_only_stream_emits_full_completion_lifecycle` | reasoning-only stream → full added/done lifecycle + `response.completed` + `[DONE]`; completed output contains the reasoning item |
| `test_codex_resp01_reasoning_stream_with_text_still_completes` | reasoning+text stream unchanged; exactly one `response.completed`; text preserved |
| `test_codex_resp01_openrouter_no_text_started_guard_on_completion_events` | static guard — the Responses translator must never gate completion events on `text_started` again |
| `test_b39_openrouter_local_error_responses_count_in_metrics` | no `return JSONResponse(… status_code=4xx/5xx)` may bypass `_error_response()` |
| `test_b20_git_subprocess_calls_are_timeout_bounded` | every git subprocess call carries `timeout=` |
| `test_b36_pool_record_is_telemetry_only_not_in_flight` | `record()` must not touch `in_flight`; `acquire()` calls both separately |
| mock upstream `reasoning_only` mode | E2E now drives a real reasoning-only upstream |
| `check_responses_stream` lifecycle check | any completed turn missing `output_item.added`/`output_item.done` pairing fails the E2E |

The strengthened E2E check was **proven** to reject the pre-fix event sequence
and accept the post-fix sequence before/after the change.

---

## 5. Verification Gates (all green after fix)

```bash
python -m pytest tests -q                                   # 142 passed  (was 136)
python -m pytest tests/test_sse_streaming_regressions.py -q  # 63 passed   (was 57)
python tests/e2e_runtime/run_runtime_e2e.py                  # 435/435 checks (was 420)
python tests/e2e_runtime/soak.py --seconds 12 --concurrency 6
#   nvidia-python ~3,942 · nous ~3,952 · opencode ~4,366 · blackbox ~4,081 · openrouter ~3,366
#   all 0 failures, flat RSS/p95, 0 server-log issues
python -m compileall -q common blackbox nous opencode openrouter nvidia-python model-registry tests
```

---

## 6. Files Changed

| File | Change |
|---|---|
| `openrouter/src/main.py` | CODEX-RESP-01 fix (rewritten Responses translator), `_error_response()` B-39 wiring, git `timeout=3` |
| `nous/src/main.py`, `blackbox/src/main.py`, `opencode/src/main.py`, `nvidia-python/src/main.py`, `model-registry/service.py` | git `timeout=3` (B-20) |
| `tests/test_sse_streaming_regressions.py` | +6 regression tests (CODEX-RESP-01 ×3, B-39, B-20, B-36) |
| `tests/e2e_runtime/mock_upstream.py` | new `reasoning_only` upstream mode |
| `tests/e2e_runtime/run_runtime_e2e.py` | `reasoning_only` in STREAM_MODES + item-lifecycle check in `check_responses_stream` |

---

## 7. Conclusion

The critical Codex hang on openrouter (reasoning-only model outputs) is fixed,
covered by unit + static + live-E2E regression tests, and the remaining audit
debt items are either already fixed (B-33/B-34/B-35/B-36, now guarded) or
fixed this cycle (B-39, B-20). All 142 unit tests, 63 streaming regression
tests, 435 live E2E checks, and the sustained soak pass with zero failures.
