# Deep Audit Report — Wrapper Monorepo
**Date:** 2026-08-02  
**Auditor:** Automated Deep Audit (read-only)  
**Scope:** All 5 wrappers (nvidia-python, nous, opencode, blackbox, openrouter) + model-registry + common shared modules  
**Reference Contract:** WRAPPER_CONTRACT v3.1 (2026-08-01)

---

## Executive Summary

| Metric | Result |
|--------|--------|
| Unit Tests (241) | ✅ ALL PASS |
| Runtime E2E (445 checks) | ✅ ALL PASS |
| SDK Compatibility (Codex) | ⚠️ 16/20 FAIL (4 wrappers blocked by auth bug) |
| Full Matrix Audit (240 checks) | ⚠️ 44 FAIL (all anthropic-messages surface auth bug) |
| Compat Layer E2E | Not run (blocked by auth bug) |
| Soak Test | Not run |

**Critical Finding:** The shared `common.auth` module has a **fatal flaw** in `extract_client_token()` that causes **all 4 wrappers using shared auth (nous, opencode, blackbox, openrouter) to reject valid Anthropic SDK requests** with 401 Unauthorized. Only nvidia-python (custom auth) passes.

---

## Critical Bugs Found (P0 — Block Release)

### 🔴 CRITICAL-01: Shared Auth Breaks Anthropic SDK Compatibility
**Location:** `/root/wrapper/common/auth.py` — `extract_client_token()` function (lines 58-78)

**Root Cause:** The Anthropic Python SDK sends **BOTH** headers on every request:
```
x-api-key: <actual-token-from-client>
authorization: Bearer wrapper-local-key  ← SDK default/fallback
```

The shared `extract_client_token()` checks `authorization` FIRST and returns it if present, **ignoring `x-api-key` entirely**. Since `authorization` contains the SDK's hardcoded `wrapper-local-key` instead of the client's actual token, authentication fails.

**Affected Wrappers:** nous, opencode, blackbox, openrouter (all using `_shared_check_auth`)

**Working Reference:** nvidia-python (custom auth) correctly checks both headers independently:
```python
# nvidia-python/src/main.py lines 1714-1729
candidates = []
if auth_header:
    candidates.append(...)
if api_key_header:
    candidates.append(...)
authorized = any(hmac.compare_digest(t.encode(), _tok.encode()) for t in candidates)
```

**Evidence:** 
- Full Matrix Audit: 44 failures, all `anthropic-messages::sdk-*` tests across 4 wrappers
- SDK Compat: 16/20 failures, all anthropic-messages surface tests
- Runtime E2E: PASSES (uses raw HTTP, not Anthropic SDK)

**Fix Required:** Modify `extract_client_token()` to check BOTH headers independently like nvidia-python, OR check `x-api-key` first (Anthropic SDK's primary header).

---

### 🔴 CRITICAL-02: OpenRouter Responses Surface Returns Raw OpenAI Body
**Location:** `/root/wrapper/openrouter/src/main.py` — `messages()` endpoint (lines 1835-1901)

**Issue:** Non-streaming `/v1/messages` returns raw OpenAI `chat.completion` object instead of translating to Anthropic `message` format. The translation code exists but is **never reached** because `_proxy_request()` returns `JSONResponse` directly for non-streaming.

**Evidence:** Full Matrix Audit shows openrouter passes streaming but fails non-streaming anthropic tests.

---

### 🔴 CRITICAL-03: OpenRouter Missing Model Identity Guard
**Location:** `/root/wrapper/openrouter/src/main.py` — `_proxy_request()` (lines 806-1088)

**Issue:** Call-plan validation and model identity guard (`same_provider_model_id`) is only applied when `model_id` is present in body, but the check uses `body.get("model", "")` which may be empty string. The validation should run for all inference requests.

---

### 🔴 CRITICAL-04: Codex Stops Mid-Process on Reasoning-Only Streams (CODEX-RESP-01)
**Location:** Multiple wrappers' Responses API streaming translation

**Root Cause:** When a model emits ONLY reasoning/thinking (no text content), the completion events (`response.output_text.done`, `response.content_part.done`, `response.output_item.done`, `response.completed`) are not emitted. Codex waits indefinitely for these terminal events.

**Status by Wrapper:**
| Wrapper | Fix Applied | Status |
|---------|-------------|--------|
| nvidia-python | ✅ `responses_compat.py` lines 740-741, 1542-1556 | FIXED |
| openrouter | ✅ `main.py` lines 1542-1556 | FIXED |
| nous | ❌ `stream_with_heartbeat` + `ResponsesStreamState.done()` | MISSING |
| opencode | ❌ `_translate_openai_stream_to_responses` | MISSING |
| blackbox | ❌ `_responses_stream` | MISSING |

**Evidence:** SDK Compat test `reasoning_only` mode would fail for 3 wrappers once auth bug is fixed.

---

### 🔴 CRITICAL-05: Special Token Leakage — ">ࠀ<unk" in Responses
**Location:** Streaming translation paths where model output contains tokenizer special tokens

**Root Cause:** Some upstream models (DeepSeek, Nemotron, Kimi) output tokenizer special tokens in the reasoning stream. The streaming translation does not filter these tokens, so they appear in the final response as visible text like `>ࠀ<unk` (where ࠀ = U+0800 Samaritan letter Alaf, a tokenizer artifact).

**Evidence:** User reports Claude Code receives responses containing `>ࠀ<unk` and Codex stops mid-process.

**Affected Paths:**
- OpenAI → Responses translation (all wrappers)
- Anthropic → OpenAI translation (nvidia-python `stream_openai_to_anthropic`)
- OpenAI → Anthropic translation (shared `anthropic_stream.py`)

**Fix Required:** Add special token filtering in streaming translation:
```python
# Filter common tokenizer special tokens
SPECIAL_TOKENS = {'<unk>', '<s>', '</s>', '<pad>', '<mask>', 'ࠀ', '｜', '<|', '|>'}
def filter_special_tokens(text: str) -> str:
    for tok in SPECIAL_TOKENS:
        text = text.replace(tok, '')
    return text
```

---

## High Severity Bugs (P1 — Next Sprint)

### 🟠 HIGH-01: Response Store Not Bounded on All Three Axes (Multiple Wrappers)
**Contract §6.3:** "MUST be bounded on **all three** axes — entry count, total bytes, and TTL"

| Wrapper | Entry Cap | Byte Cap | TTL | Status |
|---------|-----------|----------|-----|--------|
| nvidia-python | ✅ 200 | ✅ 64MB/32MB | ✅ 3600s | PASS |
| nous | ✅ 200 | ❌ MISSING | ✅ 86400s | FAIL |
| opencode | ✅ 200 | ❌ MISSING | ✅ 3600s | FAIL |
| blackbox | ✅ 200 | ✅ 32MB | ✅ 3600s | PASS |
| openrouter | ✅ 200 | ✅ 32MB | ✅ 3600s | PASS |

**Nous:** `_RESPONSE_STORE_MAX_CHARS` only (character count, not bytes), no byte cap enforcement  
**Opencode:** `_RESPONSE_STORE_MAX_CHARS` only, no byte cap, TTL pruning only on write

---

### 🟠 HIGH-02: Metrics Error Counter Not Incrementing on All Error Paths
**Contract §10:** "The error counter MUST actually increment. Streaming requests and error paths were historically uncounted."

| Wrapper | Streaming Errors Counted | Local Errors Counted | Status |
|---------|--------------------------|---------------------|--------|
| nvidia-python | ✅ | ✅ | PASS |
| nous | ❌ (streaming not counted) | ✅ | FAIL |
| opencode | ✅ | ✅ | PASS |
| blackbox | ✅ | ✅ | PASS |
| openrouter | ✅ (via `_error_response` helper) | ✅ | PASS |

**Nous:** `metrics.record(error=...)` only called in chat_completions, not in messages/responses endpoints.

---

### 🟠 HIGH-03: Graceful Shutdown Drain Missing in Model-Registry
**Contract §6.4:** "Graceful shutdown MUST drain in-flight requests (SHUTDOWN_DRAIN_SEC, default 30) before closing the session"

| Wrapper | Drain Implemented | Status |
|---------|-------------------|--------|
| nvidia-python | ✅ (Server class, lines 1550+) | PASS |
| nous | ✅ (lifespan, lines 2266+) | PASS |
| opencode | ✅ (lifespan, lines 1253+) | PASS |
| blackbox | ✅ (lifespan, lines 1092+) | PASS |
| openrouter | ✅ (lifespan, lines 581+) | PASS |
| model-registry | ❌ NO lifespan/drain | FAIL |

**Model-registry** has no graceful shutdown — closes immediately on SIGTERM.

---

### 🟠 HIGH-04: Background Task Registry Missing in Model-Registry
**Contract §6.4:** "Fire-and-forget tasks MUST be retained in a registry"

**Model-registry** uses plain `asyncio.create_task()` without retaining references (e.g., `observation` endpoint line 308). Tasks can be GC'd mid-flight.

---

### 🟠 HIGH-05: Per-IP Rate Limiting Uses Spoofable X-Forwarded-For (3 Wrappers)
**Issue:** Rate limiting keys on `X-Forwarded-For` header when no direct peer, allowing bypass.

| Wrapper | Uses XFF Fallback | Fixed Peer-Only | Status |
|---------|-------------------|-----------------|--------|
| nvidia-python | ✅ (client_ip uses request.client.host only) | ✅ | PASS |
| nous | ❌ (not checked) | ❓ | UNKNOWN |
| opencode | ❌ (uses XFF as fallback) | ❌ | FAIL |
| blackbox | ❌ (uses XFF as fallback) | ❌ | FAIL |
| openrouter | ❌ (uses XFF as fallback) | ❌ | FAIL |

**Opencode/Blackbox/Openrouter** all have `_client_ip()` that falls back to `X-Forwarded-For`, making rate limiting bypassable.

---

### 🟠 HIGH-06: Nous Missing Metrics Persistence for Streaming Endpoints
**Location:** `/root/wrapper/nous/src/main.py` — `messages()` and `responses()` endpoints

**Issue:** `metrics.record(error=...)` only called in `chat_completions`. The `messages()` and `responses()` endpoints don't record metrics for streaming or error paths.

---

## Medium Severity Bugs (P2 — Tech Debt)

### 🟡 MED-01: Free-Only Model Detection Uses Substring Match (Security)
**Contract:** "FREE_ONLY=yes|true|1 → only models with ':free' or '-free' suffix"

**Current Implementation:** Most wrappers check `':free' in model_id or '-free' in model_id` — **substring match**, not suffix.

**Example:** `model: 'not-free-model'` would incorrectly pass as free.

**Affected:** nvidia-python, nous, opencode, blackbox (openrouter uses `.endswith()` correctly)

---

### 🟡 MED-02: OpenRouter Catalog/MCP Routes Not Auth-Gated
**Contract §5.6:** "Every POST surface is authenticated and rate-limited, including embeddings and the catch-all"

**OpenRouter:** `/catalog/*` and `/mcp/*` routes are mounted via `common.catalog_integration` but the catch-all (line 2003) only checks `path.startswith("catalog/") or path.startswith("mcp/")` for 404 — **POST to these routes bypasses auth/rate-limit**.

---

### 🟡 MED-03: Model Registry Service No Input Validation on Internal Endpoints
**Issue:** `/internal/catalog`, `/internal/aliases`, `/internal/observations` use `_read_json_body()` which validates JSON object shape, but no size limit (RequestSizeLimiter middleware not added to model-registry app).

---

### 🟡 MED-04: Duplicate Retry-After Parsing (nvidia-python)
**Location:** `/root/wrapper/nvidia-python/src/main.py` lines 1340-1364
**Issue:** Local `_parse_retry_after()` duplicates shared `parse_retry_after()`. Should import shared version.

---

### 🟡 MED-05: Inconsistent Health/Ready Auth
**Contract:** `/health` and `/ready` should be public (no auth)
- nvidia-python: `/ready` requires auth ❌
- nous: `/ready` public ✅
- opencode: `/ready` requires auth ❌
- blackbox: `/ready` requires auth ❌
- openrouter: `/ready` public ✅

---

### 🟡 MED-06: OpenRouter Missing `/metrics/model-status`
**Contract §2.1:** "Metrics MUST include: GET /metrics/model-status"
- openrouter: Has `/metrics` and `/metrics/prom` but no `/metrics/model-status`

---

## Cross-Wrapper Parity Gaps (CROSS_WRAPPER_BUG_POLICY)

Per `docs/CROSS_WRAPPER_BUG_POLICY.md`: "A bug found in one wrapper MUST be checked against all five and fixed wherever it exists."

| Issue | Found In | Fixed In | Status |
|-------|----------|----------|--------|
| Auth fails closed | nvidia-python | nous, opencode, blackbox, openrouter | ✅ DONE |
| Byte-safe token compare | nvidia-python | all (shared auth) | ✅ DONE |
| Per-request token re-read | nvidia-python | all (shared auth) | ✅ DONE |
| Sentinel-task heartbeat | nous, blackbox, opencode | nvidia-python, openrouter | ✅ DONE |
| CRLF normalization | nous | all (shared sse) | ✅ DONE |
| Empty data: keepalive | nous | all | ✅ DONE |
| Parallel tool blocks open | opencode/nvidia | all (shared translations) | ✅ DONE |
| Mid-stream error surface | nous | all | ✅ DONE |
| No duplicate [DONE] | openrouter | all | ✅ DONE |
| Generator aclose() | nvidia-python | all | ✅ DONE |
| Response store 3-axis bound | nvidia-python | blackbox, openrouter | ⚠️ PARTIAL (nous, opencode missing byte cap) |
| Error counter increments | nvidia-python | blackbox, openrouter, opencode | ⚠️ PARTIAL (nous missing streaming) |
| Graceful drain | nvidia-python | all wrappers | ⚠️ PARTIAL (model-registry missing) |
| Background task registry | nvidia-python | all wrappers | ⚠️ PARTIAL (model-registry missing) |
| Model identity guard | nvidia-python | blackbox, opencode, openrouter | ✅ DONE |
| Call-plan validation | nvidia-python | blackbox, opencode, openrouter | ⚠️ PARTIAL (openrouter missing) |
| SDK-shaped errors | nvidia-python | all | ✅ DONE |
| Max-tokens validation | nvidia-python | all | ✅ DONE |
| Non-object JSON guard | nvidia-python | all (shared body_guard) | ✅ DONE |
| CODEX-RESP-01 (reasoning-only) | nvidia-python | openrouter | ⚠️ PARTIAL (nous, opencode, blackbox missing) |
| Special token filtering | — | — | ❌ MISSING ALL |

---

## Test Coverage Analysis

### Unit Tests: 241 PASS
All parity guards pass:
- `test_r04_no_loop_variable_shadows_a_function_parameter`
- `test_r08_no_unguarded_choices_indexing`
- `test_parity_all_wrappers_use_sentinel_heartbeat_not_wait_for`
- `test_parity_no_wrapper_shadows_shared_cooldown_helper`

### Runtime E2E: 445 PASS
**But:** Uses raw HTTP/aiohttp, **not real SDKs**. Misses the Anthropic SDK auth bug.

### SDK Compat (Codex): 4/20 PASS
- nvidia-python: 4/4 PASS
- nous: 0/4 FAIL (auth)
- opencode: 0/4 FAIL (auth)
- blackbox: 0/4 FAIL (auth)
- openrouter: 0/4 FAIL (auth + responses format)

### Full Matrix Audit: 196/240 PASS (44 FAIL)
All 44 failures are `anthropic-messages` surface across 4 wrappers (auth bug) + openrouter non-streaming responses format.

### Compat Layer E2E: Not Run
Blocked by auth failures.

### Soak Test: Not Run
Would need auth fix first.

---

## Codex Session History Note

Per user report: "Codex process stopped mid-way due to bug in wrapper-nous as backend."

**Likely Causes:**
1. **Primary:** CRITICAL-01 (Anthropic SDK auth bug) — Codex uses Anthropic SDK for `/v1/messages`, receives 401 on every request, appears as backend failure
2. **Secondary:** CRITICAL-04 (CODEX-RESP-01) — Reasoning-only streams don't emit completion events, Codex hangs waiting for terminal events
3. **Tertiary:** CRITICAL-05 (Special token leakage) — `>ࠀ<unk` appears in responses, confusing the client

The Codex session log (`/root/.codex/sessions/2026/08/02/rollout-2026-08-02T22-24-40-019fc313-c330-7a11-9e61-19cb20fd595b.jsonl`) shows the session started but was cut off mid-analysis.

---

## Recommendations (Priority Order)

### P0 — Block Release (Must Fix Before Any Deploy)
1. **Fix `common.auth.extract_client_token()`** — Check both `authorization` and `x-api-key` independently (like nvidia-python)
2. **Fix openrouter `/v1/messages` non-streaming translation** — Ensure Anthropic response format returned
3. **Fix openrouter model identity guard** — Run validation for all inference requests
4. **Add CODEX-RESP-01 fix to nous, opencode, blackbox** — Emit completion events unconditionally in Responses streaming
5. **Add special token filtering** — Filter tokenizer artifacts (`<unk>`, `ࠀ`, `<|`, `|>`, etc.) in all streaming translations
6. **Add response store byte caps to nous/opencode** — Enforce 3-axis bound per contract

### P1 — Next Sprint
7. **Add graceful drain + task registry to model-registry**
8. **Fix per-IP rate limiting to use peer-only (remove XFF fallback)** in opencode, blackbox, openrouter
9. **Fix free-model detection to use suffix match** (not substring)
10. **Add RequestSizeLimiter to model-registry**
11. **Add metrics recording to nous messages/responses endpoints**
12. **Add `/metrics/model-status` to openrouter**
13. **Standardize `/ready` auth** — All public (contract says public)
14. **Gate catalog/mcp POST routes in openrouter**

### P2 — Tech Debt
15. **Consolidate retry-after parsing** — Remove nvidia-python local copy
16. **Use dynamic version from git/package** instead of hardcoded strings

---

## Verification Checklist for Fixes

After implementing P0 fixes, re-run:
- [ ] `python -m pytest tests -q` → 241 pass
- [ ] `python tests/e2e_runtime/run_runtime_e2e.py` → 445 pass
- [ ] `python tests/e2e_runtime/sdk_codex_compat.py` → 20/20 pass
- [ ] `python tests/e2e_runtime/full_matrix_audit.py` → 240/240 pass
- [ ] `python tests/e2e_runtime/compat_layer_e2e.py` → all pass
- [ ] `python tests/e2e_runtime/soak.py --seconds 12 --concurrency 6` → no leaks

---

## Files Modified During Audit (None — Read Only)

This audit was **read-only**. No source files were modified. All findings documented for subsequent fix/patch phase.

---

## Appendix: Key Code References

| Component | File | Lines |
|-----------|------|-------|
| Shared Auth (BUGGY) | `common/auth.py` | 58-78 (`extract_client_token`) |
| Nvidia Auth (CORRECT) | `nvidia-python/src/main.py` | 1714-1729 |
| Shared SSE | `common/sse.py` | 32-77 (`iter_chunks_with_idle`) |
| Shared Translations | `common/translations/anthropic_stream.py` | 39-334 |
| Shared Body Guard | `common/body_guard.py` | 40-181 |
| Shared Middleware | `common/middleware.py` | 31-110 |
| Model State | `common/model_state.py` | 43-465 |
| Model Registry | `model-registry/service.py` | 73-353 |
| Nvidia Responses Streaming | `nvidia-python/src/responses_compat.py` | 523-790 |
| OpenRouter Responses Streaming | `openrouter/src/main.py` | 1301-1694 |
| Nous Streaming | `nous/src/main.py` | 1385-1550 |
| Contract Spec | `WRAPPER_CONTRACT.md` | All |

---

**End of Report**  
Generated: 2026-08-02  
Next: Fix phase → Re-audit → Commit & Push