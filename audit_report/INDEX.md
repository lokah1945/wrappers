# Audit Reports Index

## Reports

| Date | Report | Description |
|------|--------|-------------|
| 2026-08-01 | [DEEP_AUDIT_REPORT_2026-08-01.md](DEEP_AUDIT_REPORT_2026-08-01.md) | Previous audit (pre-fix baseline) |
| 2026-08-02 | [DEEP_AUDIT_REPORT_2026-08-02.md](DEEP_AUDIT_REPORT_2026-08-02.md) | **Current comprehensive audit** — 5 Critical, 6 High, 6 Medium findings |

## Summary of Findings (2026-08-02)

| Severity | Count | Key Issues |
|----------|-------|------------|
| 🔴 Critical | 5 | Shared auth breaks Anthropic SDK (4 wrappers), OpenRouter responses format, OpenRouter model guard, Codex stops mid-process (CODEX-RESP-01), Special token leakage (`>ࠀ<unk`) |
| 🟠 High | 6 | Response store byte caps missing (nous, opencode), metrics error counter (nous), model-registry shutdown/task registry, rate limit XFF bypass, nous streaming metrics |
| 🟡 Medium | 6 | Free-model substring match, OpenRouter catalog/mcp auth, duplicate retry-after, health/ready auth inconsistency, missing endpoints |

## Test Status

| Test Suite | Before Fix | After Fix (Target) |
|------------|------------|---------------------|
| Unit (241) | ✅ 241 pass | 241 pass |
| Runtime E2E (445) | ✅ 445 pass | 445 pass |
| SDK Compat (20) | ⚠️ 4 pass | 20 pass |
| Full Matrix (240) | ⚠️ 196 pass | 240 pass |
| Compat Layer E2E | Blocked | All pass |
| Soak | Not run | No leaks |

## Next Steps

1. Fix CRITICAL-01: `common/auth.py` `extract_client_token()` 
2. Fix CRITICAL-02: OpenRouter `/v1/messages` non-streaming translation
3. Fix CRITICAL-03: OpenRouter model identity guard
4. Fix CRITICAL-04: CODEX-RESP-01 in nous, opencode, blackbox
5. Fix CRITICAL-05: Special token filtering in all streaming translations
6. Fix HIGH-01: Response store byte caps (nous, opencode)
7. Fix HIGH-02: Nous metrics error counter for streaming
8. Fix HIGH-03/04: Model-registry graceful drain + task registry
9. Fix HIGH-05: Rate limit peer-only (remove XFF fallback)
10. Re-run all test gates
11. Commit & push