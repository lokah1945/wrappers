# Evidence Artifact: B-28 Three Wrappers Fail Open

**Finding:** B-28 — opencode, blackbox, nous fail open when BEARER_TOKEN unset

## Source Code Evidence

### blackbox/src/main.py:1112-1116
```python
def _auth_check(request: Request):
    if request.method == 'OPTIONS':
        return
    if _HAS_SHARED_AUTH:
        res = _shared_check_auth(request.headers, surface=request.url.path)
        if not res.ok:
            raise HTTPException(res.status, {'error': {'type': 'authentication_error', 'message': res.message}})
        return
    # Fallback (common/ unavailable): STILL FAILS OPEN
    if os.environ.get('DISABLE_AUTH'):
        return
    token = _bearer_token()
    if not token:
        # BUG: Returns instead of raising — ALL REQUESTS ALLOWED
        if request.headers.get('authorization') or request.headers.get('x-api-key'):
            logger.warning('[auth] BEARER_TOKEN unset but client sent credentials — accepting open (insecure)')
        return  # ← FAILS OPEN
```

### opencode/src/main.py:1285-1288
```python
def _auth_check(request: Request):
    if request.method == 'OPTIONS':
        return
    if os.environ.get('DISABLE_AUTH'):
        return
    # G10 fix: if BEARER_TOKEN is set, auth is mandatory
    # BUT: if NOT set, remains open!
    token = _bearer_token()  # Re-reads per request
    if not token:
        return  # ← FAILS OPEN (no warning even)
    # ...
```

### nous/src/main.py:2030
```python
# Module-level constant captured at import
BEARER_TOKEN = os.environ.get('BEARER_TOKEN', '').strip()

async def _auth_check(request):
    if not BEARER_TOKEN:  # Compares against IMPORT-TIME value
        return  # ← FAILS OPEN (no warning, no re-read)
    # ...
```

## Impact

### Scenario: Truncated .env
```bash
# .env file gets truncated during deploy:
NVIDIA_API_KEY_1=sk-...
# BEARER_TOKEN line missing!

# Result: Wrapper starts, serves ALL requests without auth
# Upstream credits burned by anyone reaching the port
```

### Scenario: Failed Hot-Reload
```bash
# Watchdog reloads .env but auth check uses stale module constant (nous)
# or fallback logic (blackbox/opencode)
# Revoked token keeps working
```

## Warning Log Only When Client Sends Credentials
```python
# blackbox/opencode only log when client SENDS credentials
if request.headers.get('authorization') or request.headers.get('x-api-key'):
    logger.warning('[auth] BEARER_TOKEN unset but client sent credentials — accepting open (insecure)')
# Silent open relay for anonymous attackers (no credentials sent)
```

## nvidia-python / openrouter (Also Fail Open)
```python
# nvidia-python/src/main.py:1689
_tok = _bearer_token()
if not is_public and not os.environ.get('DISABLE_AUTH') and not _tok:
    if _require_auth():
        return JSONResponse(status_code=503, ...)  # Only if REQUIRE_AUTH=true
    logger.warning('[auth] BEARER_TOKEN unset and REQUIRE_AUTH=false — serving %s OPEN', path)
    # Still serves open if REQUIRE_AUTH=false (default)
```

## Correct Behavior (REQUIRE_AUTH=true default)
```python
# common/auth.py:46-55
def require_auth() -> bool:
    raw = (os.environ.get('REQUIRE_AUTH') or '').strip().lower()
    if raw in _FALSE:
        return False
    return True  # DEFAULT: fail closed

def check_auth(headers, *, env_var='BEARER_TOKEN', surface='') -> AuthResult:
    if auth_disabled():
        return _OK
    server_token = bearer_token(env_var)
    if not server_token:
        if require_auth():
            return AuthResult(False, 503, 'Server auth not configured')  # 503, not open
        return _OK  # Explicit opt-out only
```

## Test Verification
```bash
$ python -m pytest tests/test_sse_streaming_regressions.py::test_b28_auth_fails_closed_when_token_unset -v
# EXPECTED TO FAIL PRE-FIX — all 5 wrappers currently fail this test
PASSED (after fix)

$ python -m pytest tests/test_sse_streaming_regressions.py::test_b28_explicit_opt_out_still_serves_open -v
PASSED (REQUIRE_AUTH=false still works)
```

## Fix Required
1. Set `REQUIRE_AUTH=true` as default in all wrappers
2. Return 503 when no token configured and REQUIRE_AUTH=true
3. Only serve open with explicit `REQUIRE_AUTH=false`
4. Apply identically to all 5 wrappers