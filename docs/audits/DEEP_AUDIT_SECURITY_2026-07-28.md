# Deep Security Audit Report - 2026-07-28

**Audit Date:** 2026-07-28  
**Auditor:** Deep audit from scratch (round 2)  
**Scope:** Cross-tenant data isolation, resource leaks, race conditions

---

## Critical Security Vulnerabilities Found

### 🔴 BUG-SEC-RESPONSE-STORE (CRITICAL) — Cross-Tenant Data Leak

**Location:** 
- `nvidia-python/src/responses_compat.py:57`
- `nous/wrapper_nous.py:1385`
- `blackbox/src/main.py:1385`

**Component:** Response store (`_RESPONSE_STORE`) for `/v1/responses` API

**Severity:** CRITICAL - Cross-tenant data isolation violation

**Description:**
The response store uses only the response ID as the key, without any tenant/principal namespacing. This means any client can read another client's conversation history by providing a `previous_response_id` that belongs to another user.

**Attack Vector:**
1. User A makes a request, gets response id "resp_abc123"
2. User A's conversation (including sensitive data, API keys in tool calls, etc.) is stored
3. User B sends request with `previous_response_id: "resp_abc123"`
4. User B can READ User A's entire conversation history
5. This violates data isolation between tenants

**Affected Wrappers:**
- ❌ nvidia-python: `_RESPONSE_STORE[resp_id] = messages` (NO namespacing)
- ❌ nous: `_RESPONSE_STORE[rid] = messages` (NO namespacing)
- ❌ blackbox: `_RESPONSE_STORE[rid] = messages` (NO namespacing)
- ✅ opencode: `store_key = f"{principal}\x00{key}"` (CORRECT - namespaced by auth principal)

**Evidence:**

**nvidia-python (VULNERABLE):**
```python
# responses_compat.py:57
_RESPONSE_STORE[resp_id] = messages  # No principal namespacing!

# responses_compat.py:692
if prev and prev in _RESPONSE_STORE:
    stored = _RESPONSE_STORE[prev]  # Any client can read any response!
```

**nous (VULNERABLE):**
```python
# wrapper_nous.py:1385
_RESPONSE_STORE[rid] = messages  # No principal namespacing!
```

**blackbox (VULNERABLE):**
```python
# main.py:1385
_RESPONSE_STORE[rid] = messages  # No principal namespacing!
```

**opencode (CORRECT):**
```python
# main.py:847
store_key = f"{principal}\x00{key}"  # Namespaced by auth principal!
```

**Fix Required:**
All three vulnerable wrappers must implement principal-based namespacing like opencode:
1. Extract principal from request (Bearer token, API key, or client IP as fallback)
2. Namespace the store key: `f"{principal}\x00{response_id}"`
3. Apply namespacing on both store and retrieve operations

**Impact:**
- **Data Leak:** Sensitive conversation data can leak between tenants
- **Compliance Violation:** Violates data isolation requirements for multi-tenant systems
- **Security Risk:** API keys, tokens, and other secrets in tool calls can be stolen
- **Trust Issue:** Breaks fundamental security assumption of tenant isolation

---

## Summary

**Critical Bugs Found:** 1  
**Wrappers Affected:** 3 out of 4 (75%)  
**Fix Complexity:** Medium (requires extracting principal from request context)

**Next Steps:**
1. Implement principal extraction in all affected wrappers
2. Namespace response store keys by principal
3. Add tests to verify cross-tenant isolation
4. Audit other shared stores for similar issues

---

## Audit Methodology

1. **Code Review:** Line-by-line review of response store implementation
2. **Cross-Wrapper Comparison:** Compared implementation across all 4 wrappers
3. **Security Analysis:** Identified tenant isolation violations
4. **Attack Vector Analysis:** Documented concrete attack scenarios

---

**Audit completed:** 2026-07-28  
**Status:** CRITICAL - Immediate fix required
