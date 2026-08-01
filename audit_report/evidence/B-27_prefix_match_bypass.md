# Evidence Artifact: B-27 Prefix-Match Public Path Bypass

**Finding:** B-27 — openrouter uses `startswith()` for public paths, widening auth bypass

## Source Code Evidence

### openrouter/src/main.py:538
```python
is_public = any(path.startswith(p) for p in PUBLIC_PATHS)
```

### PUBLIC_PATHS Includes (Line 630-637)
```python
PUBLIC_PATHS_ANY = frozenset({
    '/health', '/ready', '/metrics', '/metrics/prom', '/dashboard', '/stats',
    '/catalog/health', '/catalog/ready', '/catalog/metrics',
    '/mcp/sse', '/mcp/messages', '/mcp',
})

PUBLIC_PATHS_GET = frozenset({'/api/tags', '/v1/models', '/version'})
```

## Bypass Examples

| Request | Matches Prefix | Auth Required? | Actual |
|---|---|---|---|
| `POST /v1/models-internal` | `/v1/models` ✅ | Yes | **No (bypassed)** |
| `POST /metrics-internal` | `/metrics` ✅ | Yes | **No (bypassed)** |
| `POST /health-check` | `/health` ✅ | Yes | **No (bypassed)** |
| `GET /v1/models` | `/v1 models` ✅ | No (correct) | No |
| `POST /v1/models` | `/v1/models` ✅ | **Yes** | **No (method ignored)** |

## nvidia-python Correct Implementation (Line 1651)
```python
is_public = (path in public_paths
             or (method == 'GET' and path == '/v1/models')
             or (method == 'GET' and path.startswith('/v1/models/')))
```

## Key Differences

| Aspect | openrouter (Broken) | nvidia-python (Correct) |
|---|---|---|
| Matching | `startswith()` prefix | Exact match + `startswith` only for `/v1/models/` |
| Method Gating | **Ignored** | **Enforced** (`method == 'GET'`) |
| Future Routes | Auto-bypassed if prefix matches | Protected by default |

## Test Verification
```bash
$ python -m pytest tests/test_sse_streaming_regressions.py::test_b27_public_paths_are_exact_and_method_gated -v
PASSED
```

### Test Implementation
```python
def test_b27_public_paths_are_exact_and_method_gated():
    public_any = frozenset({'/health'})
    public_get = frozenset({'/v1/models'})
    
    assert is_public_path('/health', 'GET', public_any, public_get)
    assert is_public_path('/v1/models', 'GET', public_any, public_get)
    
    # Prefix-matching must NOT leak lookalike route
    assert not is_public_path('/v1/models-internal', 'GET', public_any, public_get)
    
    # Method gating: POST to discovery-only path is not public
    assert not is_public_path('/v1/models', 'POST', public_any, public_get)
```

## Fix Required
Replace prefix matching with exact match + method gating:
```python
PUBLIC_PATHS_ANY = frozenset({...})
PUBLIC_PATHS_GET = frozenset({...})
PUBLIC_GET_PREFIXES = ('/v1/models/',)  # Only specific prefixes

def is_public_path(path, method, public_any, public_get, get_prefixes=()):
    if path in public_any:
        return True
    if method == 'GET':
        if path in public_get:
            return True
        for prefix in get_prefixes:
            if path.startswith(prefix):
                return True
    return False
```