# Audit Report Index

**Date:** 2026-08-01  \
**Re-audit date:** 2026-08-01 (fix + re-verification cycle for **CODEX-RESP-01** and remaining technical debt)

---

## Reports

| File | Description |
|---|---|
| `DEEP_AUDIT_REPORT_2026-08-01.md` | **Main comprehensive audit report** — post-fix verification across all 5 wrappers, common/, tests/ (updated with the CODEX-RESP-01 fix + re-audit §7c) |
| `docs/audits/CODEX_RESP_REAUDIT_2026-08-01.md` | **Tracked re-audit report** — the fix cycle for the critical Codex/Responses bug and the debt items, with reproducible gates |

---

## Verification Gates Passed (after fix + re-audit)

| Gate | Command | Result |
|---|---|---|
| Unit + Regression Suite | `python -m pytest tests -q` | **204 passed** (142 + 62 new translation-matrix cases) |
| Streaming Regression Suite | `python -m pytest tests/test_sse_streaming_regressions.py -q` | **63 passed** |
| **AI Gateway Translation Layer** | `python -m pytest tests/test_translation_matrix.py -q` | **58 passed** (all 5 wrappers incl. nvidia) |
| Runtime E2E (5 wrappers × 3 surfaces × 22 modes + cross-translation probes) | `python tests/e2e_runtime/run_runtime_e2e.py` | **445/445 checks passed** (incl. `reasoning_only`, `anthropic_tools`, `responses_tools`, thinking-block assertion) |
| Sustained Soak | `python tests/e2e_runtime/soak.py --seconds 12 --concurrency 6` | **~19,600 requests, 0 failures**, flat RSS/latency |
| Contract Conformance | `pytest tests/test_sse_streaming_regressions.py::test_contract_all_wrappers_expose_required_surfaces` | **All 10 surfaces on all 5 wrappers** |
| Cross-Wrapper Parity Guards | `pytest tests/test_sse_streaming_regressions.py -k parity` | **4/4 pass** |
| Codex reasoning-only regression | `pytest tests/test_sse_streaming_regressions.py -k codex_resp01` | **3/3 pass** (openrouter emits full item lifecycle + `response.completed` for reasoning-only streams) |

---

## Agent Compatibility Verified

| Agent / SDK | Status |
|---|---|
| Claude Code (Anthropic SDK) | ✅ — thinking blocks, parallel tools, strict stop_reason, tool_use round trips (F1–F7) |
| Codex (OpenAI Responses API) | ✅ **all wrappers** — openrouter reasoning-only hang **fixed** (CODEX-RESP-01); reasoning-input items skipped (F2); `usage.total_tokens` present (F7) |
| OpenClaw / Hermes / OpenHands | ✅ — tool_choice mapped (F3/F4), stop_sequences forwarded (F4) |
| OpenCode | ✅ |
| Generic OpenAI SDK | ✅ — passthrough verbatim |
| Generic Anthropic SDK | ✅ — full request/response translation incl. images (F1/F6), reasoning (F5) |
| Ollama clients | ✅ |
| MCP/catalog clients | ✅ |

---

## Critical Findings Closed (Verified Fixed)

| ID | Risk | Fix |
|---|---|---|
| **B-06** | Claude Code hangs on non-streaming tool calls | Strict `finish_reason` mapping in shared + local translators |
| **B-26** | OpenRouter mgmt API unauthenticated | Loopback-only, separate token, checked before inference auth |
| **B-27** | Prefix-match public path bypass | Exact-match + method-gated public paths |
| **B-28** | Fail-open when `BEARER_TOKEN` unset | `REQUIRE_AUTH=true` default → 503 |
| **B-29** | Token rotation ineffective (nous) | Per-request `os.environ` read |
| **B-30** | Non-ASCII token → 500 | `hmac.compare_digest(bytes, bytes)` |
| **B-31** | Catch-all POST unauthenticated | Auth + rate-limit on all POST paths |
| **B-31** | MCP POST public | Auth on `/mcp/messages` |
| **B-37** | Model-block predicate side effects | Side-effect-free `is_model_blocked()` + explicit `expire_model_blocks()` |
| **B-38** | Nous `threading.Lock` | `asyncio.Lock` in KeyPool |
| **FREE_ONLY** false positive | Substring "free" | Suffix matching (`:free`/`-free`) + allowlist |
| **CODEX-RESP-01** | **Codex hangs indefinitely on reasoning-only outputs (openrouter)** | `if text_started:` guard removed; completion events always emitted; reasoning + tool-call deltas streamed as proper output items (eager `output_item.added`, unconditional `output_item.done` × reasoning/text/tools, `response.completed` with full output array) |

---

## NEW: Critical Issue Found in This Audit — RESOLVED

| ID | Wrapper | Risk | Fix (this cycle) |
|---|---|---|---|
| **CODEX-RESP-01** | **openrouter** | **Codex hangs indefinitely for reasoning-only model outputs** — `if text_started:` guard at `openrouter/src/main.py` skipped completion events when model emits only reasoning/thinking | Rewrote `_translate_openai_stream_to_responses`: message item opened eagerly, `reasoning_content`/`reasoning` streamed as a reasoning output item, tool-call deltas streamed as `function_call` items, completion events emitted **unconditionally**, `response.completed` carries the full sorted output array. Mirrors `nous` `ResponsesStreamState.done()` / `opencode` inline `gen()`. **Verified by 3 new unit tests + new `reasoning_only` mock/E2E mode + strengthened E2E lifecycle check.** |

**Impact (before fix):** Codex hangs indefinitely waiting for `response.completed` when a model outputs only reasoning/thinking — the exact "Codex stops mid-process, no final response" symptom. Not caught by the old test suite because the mock upstream always emitted text.

---

## Technical Debt — Re-verified After This Cycle

All items previously listed as remaining debt were **re-verified against the code**:

| Issue | Status after this cycle |
|---|---|
| B-36: `record()` conflates telemetry with in-flight | **Already fixed** (blackbox/nous `record()` is telemetry-only; `acquire()` calls `record()` + `increment_in_flight()` separately). **New regression guard added:** `test_b36_pool_record_is_telemetry_only_not_in_flight`. |
| B-33: Response store missing TTL/byte cap | **Already fixed** (blackbox store bounded on count + bytes + TTL; covered by `test_b33_*`). |
| B-34: No graceful shutdown drain | **Already fixed** — openrouter + nvidia-python have drain loops in lifespan (`SHUTDOWN_DRAIN_SEC`). |
| B-35: BG task registry incomplete | **Already fixed** — openrouter `_spawn_background` registry + `_drain_background_tasks()`. |
| B-39: Metrics divergence — openrouter `record_error()` dead | **Fixed this cycle**: every local error response now routes through `_error_response()` (auth rejections, invalid JSON, FREE_ONLY blocks, pool exhaustion, MCP 503s) which calls `metrics.record_error()` before returning — error counter now increments for local errors too. **New guard:** `test_b39_openrouter_local_error_responses_count_in_metrics`. |
| B-20: Blocking `subprocess` git calls | **Hardened this cycle**: every git subprocess call in all 5 wrappers + model-registry now carries `timeout=3` (the calls run once at import, but were unbounded). **New guard:** `test_b20_git_subprocess_calls_are_timeout_bounded`. |

---

## Second Deep Pass — AI Gateway Translation Layer (2026-08-01)

See `docs/audits/TRANSLATION_LAYER_AUDIT_2026-08-01.md` for the full
bit-level findings. This pass verified OpenAI↔Anthropic cross-translation
(both directions, streaming + non-streaming) and Responses↔Chat round trips
across all 5 wrappers and fixed 7 findings:

| # | Finding | Wrapper(s) | Impact |
|---|---|---|---|
| F1 | single-image message → `KeyError: 'text'` crash | nous, blackbox | vision requests HTTP 500 |
| F2 | `reasoning` input items → empty user message | nous, opencode, blackbox, openrouter | Codex multi-turn 400 |
| F3 | `tool_choice` dropped | nous | forced tool choice silently auto |
| F4 | `stop_sequences` dropped; `tool_choice` verbatim Anthropic shape | opencode | stops lost / upstream 400 |
| F5 | thinking dropped in request + non-stream + streaming response | openrouter | reasoning vanished on Anthropic surface |
| F6 | URL-source images broken/dropped | nous, openrouter | vision with URL images broken |
| F7 | reasoning item + `total_tokens` missing in `chat_to_responses`; `output_text` parts dropped; `str()` repr tool output | nous | Codex SDK usage error / continuity loss |

**New gate:** `tests/test_translation_matrix.py` — 58 tests locking every
round trip (incl. nvidia via its own modules).

## Reproduction Commands

```bash
# Unit + regression suite (204 tests)
python -m pytest tests -q

# AI Gateway Translation Layer gate (58 tests)
python -m pytest tests/test_translation_matrix.py -q

# Streaming regressions (63 tests)
python -m pytest tests/test_sse_streaming_regressions.py -q

# Codex reasoning-only regression (3 tests)
python -m pytest tests/test_sse_streaming_regressions.py -k codex_resp01 -v

# Live agent-traffic E2E (445 checks)
python tests/e2e_runtime/run_runtime_e2e.py

# Sustained load (~20k requests)
python tests/e2e_runtime/soak.py --seconds 12 --concurrency 6

# Contract conformance
pytest tests/test_sse_streaming_regressions.py::test_contract_all_wrappers_expose_required_surfaces -v
```
