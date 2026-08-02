# Deep Audit Report — Wrapper Monorepo
**Date:** 2026-08-02  
**Auditor:** Automated Deep Audit  
**Scope:** All 5 wrappers (nvidia-python, nous, opencode, blackbox, openrouter) + model-registry + common shared modules  
**Reference Contract:** WRAPPER_CONTRACT v3.1 (2026-08-01)

---

## Executive Summary

| Metric | Result |
|--------|--------|
| Unit Tests (241) | ✅ ALL PASS |
| Runtime E2E (445 checks) | ✅ ALL PASS |
| SDK Compatibility (Codex) | ⚠️ 16/20 FAIL (4 wrappers) |
| Full Matrix Audit (240 checks) | ⚠️ 44 FAIL (all anthropic-messages surface) |
| Compat Layer E2E | Not run (blocked by above) |

**Critical Finding:** The shared `common.auth` module has a **fatal flaw** in `extract_client_token()` that causes **all 4 wrappers using shared auth (nous, opencode, blackbox, openrouter) to reject valid Anthropic SDK requests** with 401 Unauthorized. Only nvidia-python (custom auth) passes.

---

## Critical Bugs Found

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

## High Severity Bugs

### 🟠 HIGH-01: Response Store Not Bounded on All Three Axes (Multiple Wrappers)
**Contract §6.3:** "MUST be bounded on **all three** axes — entry count, total bytes, and TTL"

| Wrapper | Entry Cap | Byte Cap | TTL | Status |
|---------|-----------|----------|-----|--------|
| nvidia-python | ✅ 200 | ✅ 32MB | ✅ 3600s | PASS |
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

### 🟠 HIGH-03: Graceful Shutdown Drain Missing in Some Wrappers
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

## Medium Severity Bugs

### 🟡 MED-01: Shadowed Shared Cooldown Helper (Historical, Now Fixed)
**Contract §7:** "Shadowing a shared helper with a local definition of the same name is forbidden"

**Status:** FIXED in current code — all wrappers now import `should_cooldown_key` from `common.translations`. Previous local `_should_cooldown_key` definitions removed.

**Verified by test:** `test_parity_no_wrapper_shadows_shared_cooldown_helper` PASSES

---

### 🟡 MED-02: Retry-After Parsing Inconsistency
**Issue:** Some wrappers use local `_retry_after_seconds()` delegating to shared `parse_retry_after`, but the delegation signatures differ.

| Wrapper | Uses Shared | Local Wrapper | Status |
|---------|-------------|---------------|--------|
| nvidia-python | ✅ `_parse_retry_after` (local, handles RFC1123 date) | Local | OK |
| nous | ✅ `_retry_after_seconds` → shared | Delegates | OK |
| opencode | ✅ `_retry_after_seconds` → shared | Delegates | OK |
| blackbox | ✅ `_retry_after_seconds` → shared | Delegates | OK |
| openrouter | ✅ `_parse_retry_after` (shared) | Direct | OK |

**Note:** All now use shared implementation, but nvidia-python has local copy for historical reasons. Should consolidate.

---

### 🟡 MED-03: Free-Only Model Detection Uses Substring Match (Security)
**Contract:** "FREE_ONLY=yes|true|1 → only models with ':free' or '-free' suffix"

**Current Implementation:** Most wrappers check `':free' in model_id or '-free' in model_id` — **substring match**, not suffix.

**Example:** `model: 'not-free-model'` would incorrectly pass as free.

**Affected:** nvidia-python, nous, opencode, blackbox (openrouter uses `.endswith()` correctly)

---

### 🟡 MED-04: OpenRouter Catalog/MCP Routes Not Auth-Gated
**Contract §5.6:** "Every POST surface is authenticated and rate-limited, including embeddings and the catch-all"

**OpenRouter:** `/catalog/*` and `/mcp/*` routes are mounted via `common.catalog_integration` but the catch-all (line 2003) only checks `path.startswith("catalog/") or path.startswith("mcp/")` for 404 — **POST to these routes bypasses auth/rate-limit**.

---

### 🟡 MED-05: Model Registry Service No Input Validation on Internal Endpoints
**Issue:** `/internal/catalog`, `/internal/aliases`, `/internal/observations` use `_read_json_body()` which validates JSON object shape, but no size limit (RequestSizeLimiter middleware not added to model-registry app).

---

## Low Severity / Technical Debt

### 🔵 LOW-01: Duplicate Retry-After Parsing (nvidia-python)
**Location:** `/root/wrapper/nvidia-python/src/main.py` lines 1340-1364
**Issue:** Local `_parse_retry_after()` duplicates shared `parse_retry_after()`. Should import shared version.

### 🔵 LOW-02: Inconsistent Health/Ready Auth
**Contract:** `/health` and `/ready` should be public (no auth)
- nvidia-python: `/ready` requires auth ❌
- nous: `/ready` public ✅
- opencode: `/ready` requires auth ❌
- blackbox: `/ready` requires auth ❌
- openrouter: `/ready` public ✅

### 🔵 LOW-03: Missing `/v1/capabilities` in Some Wrappers
**Contract §2.1:** "Capabilities MUST exist: GET /v1/capabilities"
- nvidia-python: ✅
- nous: ✅
- opencode: ✅
- blackbox: ✅
- openrouter: ✅ (added in current version)
- model-registry: N/A

### 🔵 LOW-04: OpenRouter Missing `/metrics/model-status`
**Contract §2.1:** "Metrics MUST include: GET /metrics/model-status"
- openrouter: Has `/metrics` and `/metrics/prom` but no `/metrics/model-status`

### 🔵 LOW-05: Hardcoded Version Strings
Multiple wrappers have hardcoded `VERSION = 'x.y.z'` instead of reading from package metadata or git tag.

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
| Call-plan validation | nvidia-python | blackbox, opencode, openrouter | ✅ DONE |
| SDK-shaped errors | nvidia-python | all | ✅ DONE |
| Max-tokens validation | nvidia-python | all | ✅ DONE |
| Non-object JSON guard | nvidia-python | all (shared body_guard) | ✅ DONE |

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

**Likely Cause:** The Anthropic SDK auth bug (CRITICAL-01) would cause Codex (which uses Anthropic SDK for `/v1/messages`) to receive 401 on every request, appearing as a backend failure. Codex would retry and eventually give up.

---

## Recommendations (Priority Order)

### P0 — Block Release
1. **Fix `common.auth.extract_client_token()`** — Check both `authorization` and `x-api-key` independently (like nvidia-python)
2. **Fix openrouter `/v1/messages` non-streaming translation** — Ensure Anthropic response format returned
3. **Add byte cap to nous/opencode response stores** — Enforce 3-axis bound per contract

### P1 — Next Sprint
4. **Add graceful drain + task registry to model-registry**
5. **Fix per-IP rate limiting to use peer-only (remove XFF fallback)** in opencode, blackbox, openrouter
6. **Fix free-model detection to use suffix match** (not substring)
7. **Add RequestSizeLimiter to model-registry**

### P2 — Tech Debt
8. **Consolidate retry-after parsing** — Remove nvidia-python local copy
9. **Standardize `/ready` auth** — All public or all auth'd (contract says public)
10. **Add `/metrics/model-status` to openrouter**
11. **Gate catalog/mcp POST routes in openrouter**
12. **Use dynamic version from git/package** instead of hardcoded strings

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
| Contract Spec | `WRAPPER_CONTRACT.md` | All |

---

**End of Report**  
Generated: 2026-08-02  
Next: Fix phase → Re-audit → Commit & Push