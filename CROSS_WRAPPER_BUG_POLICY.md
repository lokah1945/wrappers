# Cross-Wrapper Bug Checking Policy

## Principle
When a bug is found in ONE wrapper, ALL wrappers must be checked and fixed because they share similar architecture (different upstream specs, same design pattern).

## Why
All 4 wrappers (nvidia-python, nous, opencode, blackbox) follow the same architectural pattern:
- FastAPI + async streaming
- Key pool rotation
- Circuit breaker
- Heartbeat mechanism
- Stream finalization
- Error normalization
- Auth middleware

If a bug exists in one wrapper's implementation of these shared patterns, it likely exists in all wrappers.

## Examples

### Example 1: Heartbeat Bug (BUG-CODEX2)
**Found in:** wrapper-nous
**Root cause:** `stream_with_heartbeat` only fired heartbeats when upstream was sending data
**Fix:** Use `asyncio.wait_for` with timeout to fire heartbeats even when upstream is idle

**Cross-wrapper check required:**
- ✅ nvidia-python: Already uses `asyncio.wait_for` pattern (or needs fix)
- ⚠️ opencode: Uses same `stream_with_heartbeat` pattern (needs fix)
- ⚠️ blackbox: Uses same `stream_with_heartbeat` pattern (needs fix)

### Example 2: Stream Finalization Bug (BUG-CODEX1)
**Found in:** wrapper-nous
**Root cause:** `store_conversation` called after stream could block generator finalization
**Fix:** Wrap in try/finally with error handling

**Cross-wrapper check required:**
- ✅ nvidia-python: Similar pattern in Responses API (needs check)
- ⚠️ opencode: Similar pattern in Responses API (needs fix)
- ⚠️ blackbox: Similar pattern in Responses API (needs fix)

### Example 3: Thinking Injection Bug (RC-1)
**Found in:** wrapper-nvidia
**Root cause:** GLM forced `thinking: True` causing 4-5s latency
**Fix:** Changed to `thinking: False` with `opt_out_default_thinking: True`

**Cross-wrapper check required:**
- ⚠️ nous: Check if similar reasoning injection exists
- ⚠️ opencode: Check if similar reasoning injection exists
- ⚠️ blackbox: Check if similar reasoning injection exists

## Procedure

When a bug is found:

1. **Document the bug** in the wrapper where it was found
2. **Identify the pattern** (e.g., heartbeat, stream finalization, error handling)
3. **Check all other wrappers** for the same pattern
4. **Fix all affected wrappers**, not just the one where the bug was found
5. **Run cross-wrapper test suite** to verify all wrappers pass
6. **Update this document** with the new example

## Test Matrix

The SDK compatibility test suite (`tests/test_sdk_compatibility_simulation.py`) tests:

```
4 wrappers × 2 SDKs × 3 endpoints × 2 thinking modes × 2 streaming × 3 tool configs = 288+ combinations
```

All combinations must pass before declaring production-ready.

## Enforcement

- **Code review:** All PRs must include cross-wrapper bug check
- **CI/CD:** Run cross-wrapper test suite on every commit
- **Incident response:** When a bug is found, check all wrappers within 24h
- **Documentation:** Update this document with new examples

## Contact

If you find a bug that affects multiple wrappers, report it immediately and fix all affected wrappers before merging.
