# Audit Reports Index

## Reports

| Date | Report | Description |
|------|--------|-------------|
| 2026-08-01 | [DEEP_AUDIT_REPORT_2026-08-01.md](DEEP_AUDIT_REPORT_2026-08-01.md) | Previous audit (pre-fix baseline) |
| 2026-08-02 | [DEEP_AUDIT_REPORT_2026-08-02.md](DEEP_AUDIT_REPORT_2026-08-02.md) | Comprehensive audit — 5 Critical, 6 High, 6 Medium findings |
| 2026-08-03 | [DEEP_AUDIT_REPORT_2026-08-03.md](DEEP_AUDIT_REPORT_2026-08-03.md) | End-to-end live reproduction audit. Official gates all green (241/445/240/20), yet 8 live bug classes found via adversarial harness: 4 Critical (fabricated success on mid-stream disconnect, auth precedence, function-name-in-args-delta, special token `<unk>` leakage), 5 High |
| 2026-08-03 R5 | [DEEP_AUDIT_REPORT_2026-08-03_ROUND5.md](DEEP_AUDIT_REPORT_2026-08-03_ROUND5.md) | **Current — round 5 (final).** Post-green re-audit: MiniMax DSML tool-markup class — double-scrub deleting tool calls on `/v1/messages` (nvidia/openrouter), fragmented opener leak, `end_turn` on recovered tool turns (8 variants fixed), incomplete-markup leak fork, env-gated suppression, block-index reuse. **Audit-from-zero sweep done. All 6 gates green: 298 unit / 990 runtime / 240 matrix / SDK / L2+L3 / soak.** |

## Summary of Findings (2026-08-03)

| Severity | Count | Key Issues |
|----------|-------|------------|
| 🔴 Critical | 4 | Fabricated success on clean-EOF mid-stream (all 5 wrappers — incl. truncated tool-call JSON delivered as complete), auth `Authorization` masking valid `x-api-key` (4 wrappers), function NAME emitted as arguments delta (4 wrappers + common/compat), special token leakage on every surface (`<unk>`, `<s>`, `<\|im_start\|>`, U+0800) |
| 🟠 High | 5 | `input_image` dropped on Responses (4 wrappers), thinking blocks missing `signature` (strict parse fails), Responses usage missing `*_details` objects (strict parse fails), nvidia synthesized fake assistant text, README `--workers 4` vs in-memory stores |
| 🟡 Medium | 6 | nous store no byte cap, no heal_in_flight in 3 wrappers, openrouter `/metrics/model-status` missing, XFF fallback rate limit, registry drain/size-limit missing, openrouter mcp>=2 boot crash |
| ⚪ Low | 4 | `/ready` auth inconsistency, openrouter missing max_tokens validation, nvidia duplicate retry-after parser, tool-block close on reasoning interleave |

## Test Status (2026-08-03 run)

| Test Suite | Result |
|------------|--------|
| Unit (241) | ✅ 241 pass |
| Runtime E2E (445) | ✅ 445 pass |
| SDK Compat (20) | ✅ 20 pass |
| Full Matrix (240, incl. L2) | ✅ 240 pass |
| Adversarial probes (45, new) | ❌ **33 fail** — all failures map to P0/P1 findings |

## Coverage gaps identified (why bugs survived all gates)

- Mock upstream HAS `abrupt` mode (mid-stream disconnect) but it is in NO harness mode list — 0 executions ever
- No dual-header auth probe (`Authorization` + `x-api-key` as the Anthropic SDK sends)
- No special-token modes in any mock
- Gates use lenient SDK parsing only; strict `model_validate_json` never exercised
- No vision content probe on the Responses surface

## Next Steps (priority)

1. P0-1: teach stream finalizers `saw_terminal`; EOF without terminal → error/`response.failed` (+ add `abrupt`/`abort_tool` mock modes to gates)
2. P0-2: shared auth evaluates BOTH headers as candidates (nvidia pattern)
3. P0-3: remove `delta: fn['name']` from 5 locations (+ delta-accumulation assertion)
4. P0-4: `common/sanitize_tokens.py` filter at all translation points (+ tail-buffer for cross-chunk fragments)
5. P1-1..P1-5: input_image forwarding, thinking `signature`, usage details objects, remove nvidia synthetic text, workers=1 contract
6. Re-run all gates incl. new adversarial probes, then commit & push
