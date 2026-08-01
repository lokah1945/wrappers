# Deep Agent Runtime Re-Audit — All Wrapper Backends

**Date:** 2026-08-01  
**Branch:** `arena/019fbc7b-wrappers`  
**Scope:** `nvidia-python`, `nous`, `opencode`, `blackbox`, `openrouter`, shared `common/`, and runtime tests.  
**Goal:** Verify the wrappers can be used as backend endpoints by Claude Code, Codex, OpenClaw, Hermes Agent, OpenCode, OpenHands, generic OpenAI/Anthropic SDK clients, Ollama-style discovery clients, and MCP/catalog clients without runtime protocol failures.

---

## 1. Audit Method

This re-audit did **not** trust prior status claims. It combined:

1. **Contract review** against `WRAPPER_CONTRACT.md` and `README.md`.
2. **Audit evidence review** of every file under `audit_report/`.
3. **Static pattern re-checks** for previously proven failure classes.
4. **Live E2E execution**: all five wrappers booted as real `uvicorn` servers against `tests/e2e_runtime/mock_upstream.py`.
5. **Sustained soak** across all wrappers.
6. **Regression-test expansion** for gaps found during this re-audit.

---

## 2. Additional Findings Closed in This Pass

| ID / Class | Runtime Risk | Fix |
|---|---|---|
| B-06 non-streaming variant | Claude Code / Anthropic SDK can wait for a tool result if a non-streaming response includes tool calls but `finish_reason` is `stop`, `length`, or `content_filter`. | Strict `finish_reason` mapping in `common.translations.shared.openai_to_anthropic_response`, local `openai_to_anthropic` copies in `nous`, `opencode`, `blackbox`, and OpenRouter non-streaming conversion. |
| B-26 hardening gap | `DISABLE_AUTH=true` could still bypass OpenRouter management protection if management checks were nested under inference auth. | Management routes are checked before inference-auth bypass and never fail open. Non-loopback management is rejected. |
| B-31 catch-all POST gap | Unknown POST endpoints on per-route-auth wrappers could return unauthenticated 404, violating the "every POST surface is authenticated/rate-limited" rule and making future route drift risky. | `nous`, `opencode`, and `blackbox` catch-all POSTs now call `_auth_check()` and `check_rate_limit()`. |
| B-31 MCP transport gap | `/mcp/messages` is a POST agent surface and should not be public. | OpenRouter removed MCP POST from public paths; shared catalog MCP routes now call `common.auth.check_auth`. |
| B-37 model-block predicate side effects | Metrics/health calls could mutate model-scoped cooldown state outside pool locks. | `is_model_blocked()` is side-effect-free; explicit `expire_model_blocks()` runs under the acquire lock across all pool implementations. |
| B-38 Nous pool lock | Nous KeyPool used `threading.Lock` in async request paths. | Nous KeyPool now uses `asyncio.Lock`; acquire/release/mark_failure/heal/peek call sites were updated. |
| FREE_ONLY false positive | Substring matching allowed IDs like `freemium` under `FREE_ONLY`. | `nous`, `opencode`, and `blackbox` now use suffix matching (`:free` / `-free`) plus explicit allowlist. |
| BaseWrapper parity | Reference wrapper still had legacy auth/public-path semantics. | `common/base_wrapper.py` now uses `common.auth`, exact/method-gated public paths, and side-effect-free model blocks. |

---

## 3. Agent Compatibility Requirements Re-Checked

| Agent / Client | Required Surface(s) | Verification |
|---|---|---|
| Claude Code | Anthropic `/v1/messages`, streaming block lifecycle, tool_use, `stop_reason`, thinking, no frame leakage | Streaming regressions + live E2E Anthropic surface across 21 upstream modes. |
| Codex | OpenAI `/v1/responses`, Responses SSE lifecycle, `previous_response_id`, function calls, `response.failed` on error | Live E2E Responses surface + regression checks for non-streaming translation and error handling. |
| OpenClaw / Hermes / OpenHands | OpenAI `/v1/chat/completions`, streaming `[DONE]`, tool calls, shaped errors | Live E2E Chat surface across all wrappers and modes. |
| OpenCode | Chat + Responses + Anthropic parity | All three surfaces tested for every wrapper. |
| Generic OpenAI SDK | Chat, Responses, embeddings behavior, `/v1/models` | Contract tests + live E2E + regression suite. |
| Generic Anthropic SDK | Messages + count_tokens + event ordering | Contract tests + Anthropic stream validation. |
| Ollama clients | `/api/tags` model discovery | Contract tests verify route presence and E2E covers startup health/discovery assumptions. |
| MCP/catalog clients | `/mcp/sse`, `/mcp/messages` | Re-audit hardened MCP POST/auth behavior and added regression coverage. |

---

## 4. Verification Results

Commands executed after the final patch set:

```bash
/tmp/wrappers-venv/bin/python -m pytest tests/test_sse_streaming_regressions.py -q
# 57 passed

/tmp/wrappers-venv/bin/python -m pytest tests -q
# 136 passed, 1 warning

/tmp/wrappers-venv/bin/python tests/e2e_runtime/run_runtime_e2e.py
# checks passed: 420    failures: 0

/tmp/wrappers-venv/bin/python tests/e2e_runtime/soak.py --seconds 12 --concurrency 6
# soak PASSED for all wrappers
# requests ok: nvidia 3896, nous 4259, opencode 4580, blackbox 4334, openrouter 3737
# failures: 0 for every wrapper

/tmp/wrappers-venv/bin/python -m compileall -q common blackbox nous opencode openrouter nvidia-python model-registry tests
# OK

git diff --check
# OK

/tmp/wrappers-venv/bin/python /tmp/post_patch_static_audit.py
# post-patch static audit checks passed
```

---

## 5. Runtime Contract Status

| Contract Area | Status |
|---|---|
| Required surfaces | PASS |
| OpenAI Chat streaming | PASS |
| OpenAI Responses streaming | PASS |
| Anthropic Messages streaming | PASS |
| SSE framing (`data:`, keepalive, CRLF, comments, split bytes, duplicate finish) | PASS |
| Parallel tools | PASS |
| Mid-stream upstream errors | PASS |
| Non-object JSON guard | PASS |
| Fail-closed auth | PASS |
| OpenRouter management auth separation | PASS |
| POST catch-all auth/rate-limit | PASS |
| MCP POST auth hardening | PASS |
| Response-store bounds | PASS |
| Pool in-flight accounting | PASS |
| Side-effect-free block predicates | PASS |
| Soak stability | PASS |

---

## 6. Notes / Boundaries

- The live E2E harness validates protocol/runtime behavior with real HTTP servers and a deterministic mock upstream. It does not call paid external providers.
- Some legacy `pyflakes` hygiene warnings remain in untouched code paths (mostly unused imports / historical local fallback definitions). They are not part of the runtime failure classes closed here. The critical runtime gates above are green.
- The wrappers are still intentionally provider-specific internally; the client-facing behavior is what the contract standardizes.

---

## 7. Verdict

After this re-audit and patch pass, the wrapper fleet satisfies the runtime contract for the listed agents/SDKs under the tested conditions:

- **136/136 unit + regression tests passed**
- **420/420 live E2E checks passed**
- **~20,806 soak requests, 0 failures**
- **No stream lifecycle, auth, tool-call, or error-transparency regression detected**

The project is fit for use as a backend for Claude Code, Codex, OpenClaw, Hermes Agent, OpenCode, OpenHands, generic OpenAI/Anthropic clients, Ollama discovery clients, and authenticated MCP/catalog clients within the verified contract.
