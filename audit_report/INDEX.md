# Audit Report Index

**Date:** 2026-08-01  
**Commit:** `40f2d69` (merged PR #5 — "Harden agent runtime contract across wrappers")

---

## Reports

| File | Description |
|---|---|
| `DEEP_AUDIT_REPORT_2026-08-01.md` | **Main comprehensive audit report** — post-fix verification across all 5 wrappers, common/, tests/ |

---

## Verification Gates Passed

| Gate | Command | Result |
|---|---|---|
| Unit + Regression Suite | `python -m pytest tests -q` | **136 passed** |
| Streaming Regression Suite | `python -m pytest tests/test_sse_streaming_regressions.py -q` | **57 passed** |
| Runtime E2E (5 wrappers × 3 surfaces × 21 modes) | `python tests/e2e_runtime/run_runtime_e2e.py` | **420/420 checks passed** |
| Sustained Soak | `python tests/e2e_runtime/soak.py --seconds 12 --concurrency 6` | **~20,806 requests, 0 failures** |
| Contract Conformance | `pytest tests/test_sse_streaming_regressions.py::test_contract_all_wrappers_expose_required_surfaces` | **All 10 surfaces on all 5 wrappers** |
| Cross-Wrapper Parity Guards | `pytest tests/test_sse_streaming_regressions.py -k parity` | **4/4 pass** |

---

## Agent Compatibility Verified

| Agent / SDK | Status |
|---|---|
| Claude Code (Anthropic SDK) | ✅ |
| Codex (OpenAI Responses API) | ✅ |
| OpenClaw / Hermes / OpenHands | ✅ |
| OpenCode | ✅ |
| Generic OpenAI SDK | ✅ |
| Generic Anthropic SDK | ✅ |
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

---

## Remaining Technical Debt (Non-Blocking)

| Issue | Wrapper(s) | Severity |
|---|---|---|
| B-36: `record()` conflates telemetry with in-flight | blackbox, nous | MEDIUM |
| B-33: Response store missing TTL/byte cap | blackbox | MEDIUM |
| B-34: No graceful shutdown drain | openrouter, nvidia-python | MEDIUM |
| B-35: BG task registry incomplete | openrouter | LOW |
| B-39: Metrics divergence | openrouter (`record_error` dead) | MEDIUM |
| B-20: Blocking `subprocess` git in `/health` | all 5 + model-registry | LOW |

---

## Reproduction Commands

```bash
# Unit + regression suite (136 tests)
python -m pytest tests -q

# Streaming regressions (57 tests)
python -m pytest tests/test_sse_streaming_regressions.py -q

# Live agent-traffic E2E (420 checks)
python tests/e2e_runtime/run_runtime_e2e.py

# Sustained load (20k requests)
python tests/e2e_runtime/soak.py --seconds 12 --concurrency 6

# Contract conformance
pytest tests/test_sse_streaming_regressions.py::test_contract_all_wrappers_expose_required_surfaces -v
```