# ILMA HARD AUDIT — Wrapper Production Readiness (Enterprise-Grade?)

**Date:** 2026-07-27
**Auditor:** ILMA (independent re-audit, HARD READ-ONLY)
**Target:** `/root/wrapper` @ `1ad8845` (github/main, pulled 2026-07-27)
**Method:** Code read + pytest + live curl probes against running services (ports 9101-9104, 9200)
**Principle:** Do NOT trust commit messages / docstrings claiming "100/100 enterprise". Verify independently.

---

## VERDICT

**PRODUCTION READY: PARTIAL — "Enterprise-grade hardening present, but NOT yet 100/100."**

The codebase has genuinely strong production scaffolding (circuit breaker, size limiter, sanitization, graceful shutdown, structured logging, key pool, rate limiting, retry). However, the self-declared "100/100 enterprise" claim is **false** — there is a failing test, inconsistent security fixes, and weak dependency pinning. Recommend **1 remediation pass** before declaring true production-grade.

**Scorecard (my independent rubric):**

| Dimension | Status | Notes |
|-----------|--------|-------|
| Correctness / Tests | 🟡 PARTIAL | 75 passed / **1 FAILED** (76 total). Failing test contradicts "100/100". |
| Auth / Access Control | 🟢 GOOD | Chat endpoints 401 without token (verified). `/v1/models` public on 3/4 (acceptable, catalog-only). |
| Security (injection) | 🟡 MINOR GAP | Header injection mitigated in `common/middleware` but **NOT fully in `wrapper_nous.py` local copy** (no control-char strip). |
| Resilience (circuit breaker) | 🟢 GOOD | State machine fixed (BUG-ECB1). Logic sound. |
| Input validation | 🟢 GOOD | Oversized → 413 (verified). Invalid model → 400 (verified). |
| Observability | 🟢 GOOD | JSON logging, metrics endpoints, health/ready. |
| Dependency mgmt | 🔴 WEAK | All `>=` unpinned → supply-chain drift risk. Enterprise needs `==` or hash pinning. |
| Graceful shutdown | 🟢 GOOD | SIGTERM/SIGINT handler present. |
| Code hygiene / dedup | 🟡 MINOR | Duplicated `_sanitize_header_value` (nous local vs common). Docstring still says "100/100". |

**Overall: ~85/100** (not 100/100 as claimed).

---

## FINDINGS (evidence-backed)

### 🔴 F-1 — Failing test: `test_central_client.py::test_disabled_client_does_not_enqueue_or_open_connections`
- **Evidence:** `pytest` → `1 failed, 75 passed`. Assertion `client.enabled is False` fails because `ModelRegistryClient("")` still resolves `enabled=True` via `MODEL_REGISTRY_URL` env fallback (line 24, `central_client.py`).
- **Impact:** In prod, `MODEL_REGISTRY_URL` is always set, so the client is enabled anyway — low runtime risk. BUT it proves the "100/100" claim is fabricated; a red test = not 100/100.
- **Fix:** `enabled` should be `bool(base_url)` only, ignore env when explicit `""` passed; OR test should `monkeypatch.delenv("MODEL_REGISTRY_URL")`.

### 🟡 F-2 — Inconsistent SEC2 header sanitization (duplicated + weaker in nous)
- **Evidence:** `common/middleware.py:88-103` `sanitize_header_value` strips CR/LF **AND** control chars `0x00-0x1f/0x7f`. But `wrapper_nous.py:49-53` has a *local* `_sanitize_header_value` that strips only `\r`/`\n` — control chars survive. Commit `3b3dc4a "two security/correctness bugs in sanitize module"` did NOT touch the nous local copy.
- **Impact:** If nous forwards any attacker-controlled header value through its local sanitizer, control-char injection (log injection / smuggling) is possible. Live probe showed no leakage via curl malformed-header (status 000 = curl rejected), but the code-path gap remains.
- **Fix:** Delete local `_sanitize_header_value` in `wrapper_nous.py`; import from `common.middleware`.

### 🔴 F-3 — Unpinned dependencies (supply-chain risk)
- **Evidence:** All 4 `requirements.txt` use `>=` with no upper bound (`fastapi>=0.115`, `aiohttp>=3.10`, etc.). No `requirements.lock` / hash pinning.
- **Impact:** A future `pip install` can pull breaking major versions. Enterprise CI must pin exact versions + hashes.
- **Fix:** Generate `requirements.lock` via `pip freeze` / `pip-compile --generate-hashes`.

### 🟡 F-4 — "/v1/models" public without auth on 3/4 wrappers
- **Evidence:** Live probe: `9102` (nous), `9101` (nvidia), `9104` (blackbox) → `200` no-token; `9103` (opencode) → `401`. Chat endpoints correctly 401 (verified `P1b`).
- **Impact:** Model catalog is non-sensitive (no keys), so this is acceptable. But inconsistent policy across wrappers = minor hygiene issue.
- **Fix:** Either protect all `/v1/models` or document "catalog is public by design" uniformly.

### 🟡 F-5 — Stale "100/100" claims in code/docstrings
- **Evidence:** `wrapper_nous.py:6` docstring: "Achieves 100/100 production readiness". Commit `f3c97be` title: "true 100/100 enterprise".
- **Impact:** Misleads operators; contradicts F-1 (red test).
- **Fix:** Update docstrings/commits to reflect real status after remediation.

### 🟢 F-6 (POSITIVE) — Resilience & input validation verified working
- Circuit breaker: state machine correctly transitions OPEN→HALF_OPEN (`before_request` mutates `_state`, line 109-117).
- Oversized request → `413` (verified `P3`, 13MB rejected).
- Invalid model → `400` (verified `P2`).
- Auth gate on chat → `401` no-token (verified `P1b`).
- Graceful shutdown handler + JSON logging present.

---

## LIVE PROBE RESULTS (raw)
```
[P1]  no-token /v1/models (nous)   -> 200   (public catalog)
[P1b] no-token /v1/chat/completions -> 401   ✅ auth gate works
[P2]  invalid model                 -> 400   ✅
[P3]  13MB request                  -> 413   ✅ size limiter works
[P4]  CRLF header (malformed)       -> 000   (curl rejected; no server leak)
[P5]  nvidia /health                -> 200
[P6]  blackbox /health              -> 200
[P7]  registry /health              -> 200
[P-aux] nvidia /v1/models no-token  -> 200   (public)
[P-aux] opencode /v1/models no-token-> 401   ✅
[P-aux] blackbox /v1/models no-token-> 200   (public)
```

## TEST RESULTS
```
75 passed, 1 failed (76 total) in 6.31s
FAILED: tests/test_central_client.py::test_disabled_client_does_not_enqueue_or_open_connections
```

---

## RECOMMENDATION
**Not yet "true" enterprise 100/100.** Ship-blocking for the *claim*, not for runtime safety. Do this 1-pass remediation:
1. Fix F-1 (test or `enabled` logic) → green suite.
2. Fix F-2 (dedupe sanitizer in nous).
3. Fix F-3 (pin deps with hashes).
4. Fix F-5 (update docstrings/commits to honest status).
5. Decide F-4 policy (uniform protect or document public).

After these, re-run `pytest` → should be 76 passed, 0 failed → then "enterprise-grade" is honest.

**Runtime safety today: ✅ SAFE to keep in production** (auth, size limits, circuit breaker all verified). The gap is *maturity/claim accuracy*, not live danger.
