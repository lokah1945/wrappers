# Evidence Directory Index

**Location:** `/root/wrapper/audit_report/evidence/`  
**Generated:** 2026-08-01  
**Purpose:** Raw proof artifacts for every audit finding

---

## File Inventory

| File | Finding | Type | Description |
|---|---|---|---|
| `B-01_empty_data_terminator.md` | B-01 | Code + Test | blackbox/opencode treat empty `data:` as terminator |
| `B-02_space_required_parsing.md` | B-02 | Code + Executable Proof | openrouter discards `data:{...}` (no space) |
| `B-03_parallel_tool_calls.md` | B-03 | Code + Executable Proof | openrouter tool-call translation loses args |
| `B-06_stop_reason_mapping.md` | B-06 | Code + Test Matrix | stop_reason forced to tool_use masking max_tokens |
| `B-10_nous_frame_leak.md` | B-10 | Code + Terminal Reproduction | nous synthesizes frames as assistant text |
| `B-26_openrouter_mgmt_api.md` | B-26 | Code + Attack Vector | Unauthenticated key provisioning API |
| `B-27_prefix_match_bypass.md` | B-27 | Code + Bypass Table | Prefix-match auth bypass |
| `B-28_fail_open.md` | B-28 | Code + Scenarios | 3 wrappers fail open when token unset |
| `RUNTIME_FINDINGS_R01_R08.md` | R-01..R-08 | Summary + Verification | 8 runtime bugs from E2E harness |

---

## Evidence Categories

### 1. Source Code Evidence
Exact file:line references with quoted source showing the bug

### 2. Executable Proofs
Runnable scripts (`/tmp/prove*.py`) that reproduce the bug byte-for-byte

### 3. Test Verification
pytest commands showing the regression test that catches the bug

### 3. Impact Analysis
Agent/SDK behavior when bug triggered (Claude Code hangs, empty responses, etc.)

---

## Cross-Reference to Main Reports

| Main Report | Evidence Files Referenced |
|---|---|
| `03_STREAMING_CORRECTNESS.md` | B-01, B-02, B-03, B-06, B-10 |
| `04_SECURITY_AUTH.md` | B-26, B-27, B-28 |
| `07_RUNTIME_FINDINGS.md` | R-01 through R-08 |
| `09_AGENT_COMPATIBILITY_PROOFS.md` | B-06, B-10 |

---

## Verification Commands

```bash
# Run all evidence-backed regression tests
python -m pytest tests/test_sse_streaming_regressions.py -v

# Run live E2E (proves runtime findings)
python tests/e2e_runtime/run_runtime_e2e.py

# Run soak (proves stability)
python tests/e2e_runtime/soak.py --seconds 12 --concurrency 6
```

---

## Evidence Standards

Every finding in the audit reports must have:
1. ✅ Exact file:line reference
2. ✅ Quoted source code showing the bug
3. ✅ Executable reproduction OR test that catches it
4. ✅ Impact analysis (what agent/SDK breaks)
4. ✅ Reference implementation for fix
5. ✅ Test verification command