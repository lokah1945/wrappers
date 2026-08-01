# Evidence Artifact: B-26 openrouter Management API Unauthenticated

**Finding:** B-26 — openrouter `/openrouter/keys/*` provisioning API completely unauthenticated

## Source Code Evidence

### openrouter/src/main.py:539 (Auth Middleware)
```python
@app.middleware('http')
async def auth_middleware(request: Request, call_next):
    # ...
    is_public = (path in public_paths
                 or path.startswith('/metrics/')
                 or path.startswith('/stats')
                 or (method == 'GET' and path == '/v1/models')
                 # ...
                 or (method == 'GET' and path.startswith('/catalog/'))
                 or (method == 'GET' and path.startswith('/mcp/')))
    
    # BUG: /openrouter/ NOT excluded from public paths!
    # Falls through to auth check ONLY if not public
```

### Exposed Routes (All Proxy to OpenRouter Provisioning API)

| Route | Line | Capability | Risk |
|---|---|---|---|
| `POST /openrouter/keys/list` | 1912 | Enumerate all keys | Credential enumeration |
| `POST /openrouter/keys/create` | 1921 | **Mint new keys with arbitrary spend limits** | **Financial loss — unlimited key creation** |
| `GET /openrouter/keys/{hash}` | 1933 | Read key details | Credential exposure |
| `PATCH /openrouter/keys/{hash}` | 1939 | Modify/disable keys | Service disruption |
| `DELETE /openrouter/keys/{hash}` | 1946 | **Permanently delete keys** | **Destruction of production credentials** |
| `POST /openrouter/keys/rotate` | 1952 | Rotate credentials | Credential theft |
| `GET /openrouter/keys/usage` | 1981 | Read billing/usage | Financial data exposure |

### Route Implementation Example (Line 1921)
```python
@app.post("/openrouter/keys/create")
async def openrouter_create_key(request: Request):
    """Create a new OpenRouter API key."""
    body = await request.json() if request.headers.get("content-length") else {}
    name = body.get("name", f"wrapper-created-{int(time.time())}")
    limit = body.get("limit")
    payload = {"name": name}
    if limit is not None:
        payload["limit"] = limit
    return await _mgmt_request("POST", json_body=payload)
```

### CORS Configuration (Line 595) — Enables CSRF
```python
app.add_middleware(CORSMiddleware,
    allow_origin_regex=r'https?://(127\.0\.0\.1|localhost|\[::1\])(:[0-9]+)?$',
    allow_credentials=True,  # CSRF possible from browser on localhost
)
```

## Attack Vector

### CSRF from Localhost Browser
```html
<!-- Any website on localhost can execute: -->
<form action="http://localhost:9106/openrouter/keys/create" method="POST">
    <input name="name" value="stolen-key">
    <input name="limit" value="1000000">  <!-- $1M spend limit -->
</form>
<script>document.forms[0].submit()</script>
```

### Direct API Access
```bash
# Anyone reaching port 9106 can:
curl -X POST http://localhost:9106/openrouter/keys/create \
  -H "Content-Type: application/json" \
  -d '{"name": "backdoor", "limit": 1000000}'

curl -X DELETE http://localhost:9106/openrouter/keys/{hash}  # Delete production keys
```

## No Sibling Wrapper Exposes Comparable API

| Wrapper | Management API | Auth Required |
|---|---|---|
| nvidia-python | ❌ None | N/A |
| nous | ❌ None | N/A |
| opencode | ❌ None | N/A |
| blackbox | ❌ None | N/A |
| **openrouter** | ✅ **7 provisioning routes** | **❌ NONE** |

## Test Verification
```bash
$ python -m pytest tests/test_sse_streaming_regressions.py::test_b26_openrouter_management_routes_are_not_public -v
PASSED
```

## Fix Required
1. Remove `/openrouter/` from public path bypass
2. Require dedicated `MANAGEMENT_TOKEN` (separate from inference `BEARER_TOKEN`)
3. Bind management routes to loopback only (`127.0.0.1`)
4. Add explicit auth check for all `/openrouter/*` routes