# Audit Reports Index

## Reports

| Date | Report | Description |
|------|--------|-------------|
| 2026-08-01 | [DEEP_AUDIT_REPORT_2026-08-01.md](DEEP_AUDIT_REPORT_2026-08-01.md) | Previous audit (pre-fix baseline) |
| 2026-08-02 | [DEEP_AUDIT_REPORT_2026-08-02.md](DEEP_AUDIT_REPORT_2026-08-02.md) | Comprehensive audit — 5 Critical, 6 High, 6 Medium findings |
| 2026-08-03 | [DEEP_AUDIT_REPORT_2026-08-03.md](DEEP_AUDIT_REPORT_2026-08-03.md) | End-to-end live reproduction audit. Official gates all green (241/445/240/20), yet 8 live bug classes found via adversarial harness: 4 Critical (fabricated success on mid-stream disconnect, auth precedence, function-name-in-args-delta, special token `<unk>` leakage), 5 High |
| 2026-08-03 R5 | [DEEP_AUDIT_REPORT_2026-08-03_ROUND5.md](DEEP_AUDIT_REPORT_2026-08-03_ROUND5.md) | Round 5: MiniMax DSML tool-markup class — double-scrub deleting tool calls on `/v1/messages` (nvidia/openrouter), fragmented opener leak, `end_turn` on recovered tool turns (8 variants fixed), incomplete-markup leak fork, env-gated suppression, block-index reuse. All 6 gates green: 298 unit / 990 runtime / 240 matrix / SDK / L2+L3 / soak. |
| 2026-08-04 R6 | [DEEP_AUDIT_REPORT_2026-08-04_ROUND6.md](DEEP_AUDIT_REPORT_2026-08-04_ROUND6.md) | Round 6: instrument = REAL `anthropic`/`openai` SDK agent loops (55 checks × 5 wrappers: tool_use round trips, DSML recovery through the SDK, streamed/non-streamed store replay, SDK-internal 429 retry, tenant isolation, shaped errors). Found & fixed P0 openrouter `/v1/responses` store replay losing the assistant tool_calls turn (incl. streamed turns never stored) — the Codex "dies on turn 2" class. All 7 gates green. |
| 2026-08-04 R7 | [DEEP_AUDIT_REPORT_2026-08-04_ROUND7.md](DEEP_AUDIT_REPORT_2026-08-04_ROUND7.md) | Round 7: instrument = multi-agent CONCURRENCY storm (12 concurrent SDK agents × 5 wrappers, unique per-agent markers). Found & fixed **P0 cross-tenant store-key collision** (`resp_<ms>`/upstream-id reuse → agents replayed each other's history): shared `new_response_id()` mints `resp_<ms>-<12hex>` everywhere; openrouter §10 parity (`/metrics` JSON + `/metrics/prom`, `/health` in-flight). Contract → v3.2 (8 gates). |
| 2026-08-04 R8 | [DEEP_AUDIT_REPORT_2026-08-04_ROUND8.md](DEEP_AUDIT_REPORT_2026-08-04_ROUND8.md) | **Current — round 8 (final).** Post-green re-audit of shared mutable state the gates cannot see: (1) **store dict-aliasing** — 4 of 5 wrappers stored/returned LIVE message dicts by reference (latent cross-request corruption; nous N-19 deep-copy pattern now applied both directions in all wrappers); (2) **nvidia `.env` key hot-reload silently dead** — watchdog-thread callback used `asyncio.get_event_loop()` (RuntimeError) → captured running loop at init instead; (3) axis-bound tests added for nous + openrouter stores. **All 8 gates green: 300 unit / 990 runtime / 240 matrix / SDK / L2+L3 / 55 agent-loop / 10 concurrency / soak.** |

## Summary of Findings (2026-08-03)

| Severity | Count | Key Issues |
|----------|-------|------------|
| 🔴 Critical | 4 | Fabricated success on clean-EOF mid-stream (all 5 wrappers — incl. truncated tool-call JSON delivered as complete), auth `Authorization` masking valid `x-api-key` (4 wrappers), function NAME emitted as arguments delta (4 wrappers + common/compat), special token leakage on every surface (`<unk>`, `<s>`, `<\|im_start\|>`, U+0800) |
| 🟠 High | 5 | `input_image` dropped on Responses (4 wrappers), thinking blocks missing `signature` (strict parse fails), Responses usage missing `*_details` objects (strict parse fails), nvidia synthesized fake assistant text, README `--workers 4` vs in-memory stores |
| 🟡 Medium | 6 | nous store no byte cap, no heal_in_flight in 3 wrappers, openrouter `/metrics/model-status` missing, XFF fallback rate limit, registry drain/size-limit missing, openrouter mcp>=2 boot crash |
| ⚪ Low | 4 | `/ready` auth inconsistency, openrouter missing max_tokens validation, nvidia duplicate retry-after parser, tool-block close on reasoning interleave |

## Test Status (2026-08-04 R8 run — contract v3.2, all 8 gates)

| Test Suite | Result |
|------------|--------|
| Unit + regressions (300) | ✅ 300 pass |
| Runtime E2E (990) | ✅ 990 pass |
| SDK Compat (openai Codex parser) | ✅ clean |
| COMPATIBILITY_LAYER (L2 + L3) | ✅ all 5 wrappers |
| Full Matrix (240) | ✅ 240 pass |
| Real-SDK agent loop (55) | ✅ 55 pass |
| Multi-agent concurrency storm (10) | ✅ zero cross-talk, zero leaked in-flight |
| Soak | ✅ 0 failures |
| Adversarial probes | ✅ folded into the harnesses above (modes + agent scenarios) |

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
