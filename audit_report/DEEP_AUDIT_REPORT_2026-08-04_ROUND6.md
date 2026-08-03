# Deep Audit Report — 2026-08-04 Round 6 (real-SDK agent loops & store replay)

**Scope**: second full re-audit "from zero" after round 5. Round 6 changed the audit
instrument itself: instead of SSE-level byte assertions, every wrapper was driven with
the **real `anthropic` (0.120.2) and `openai` (2.52) SDKs** the way Claude Code /
Codex / openclaw / hermes / opencode / openhands use them — typed event parsing,
block-accumulation strictness, tool-use round trips, store replays, SDK-internal
retries, tenant isolation.

**Instrument**: new harness `tests/e2e_runtime/agent_loop_e2e.py`
(boots mock upstream + all 5 wrapper servers; 11 agent scenarios × 5 wrappers = 55
checks). The `anthropic` client is configured exactly like Claude Code
(`Authorization: Bearer` **and** `x-api-key`, so the P0-2 dual-header contract is
exercised on every call).

**Result**: 1 P0-class defect found & fixed (openrouter `/v1/responses` store replay),
**all 7 gates green**.

| Gate | Result |
|------|--------|
| `pytest tests -q` | ✅ 298 passed |
| `run_runtime_e2e.py` | ✅ 990 checks, 0 failures |
| `sdk_codex_compat.py` | ✅ clean |
| `compat_layer_e2e.py` | ✅ L2 + L3, all 5 wrappers |
| `full_matrix_audit.py` | ✅ 240/240 |
| `soak.py` | ✅ stable |
| **`agent_loop_e2e.py` (new)** | ✅ 55 checks, 0 failures |

---

## Finding & fix

### P0-R6.1 — openrouter `/v1/responses`: replayed history lost the assistant turn (Codex dies on turn 2)
**Symptom (new harness)**: `responses.replay: replayed history lost assistant
tool_calls: want {'call_a','call_b'}, got set()`.
**Root cause**: every openrouter store write for `/v1/responses` persisted only the
REQUEST messages (`_store_response(principal, rid, chat_body.get('messages', []))`) —
never the assistant reply. A Codex follow-up turn (`previous_response_id` +
`function_call_output` items) replayed a history whose assistant `tool_calls` turn was
missing; the wrapper mapped the outputs to `role:tool` entries that no longer matched
any assistant tool_call → upstream rejects the orphaned tool message with 400
("No tool call found for function call output") → **agent loop dies mid-run**. This is
exactly the user-visible "proses berhenti di tengah jalan" class, and it violated the
§8 parity (nous/opencode/blackbox/nvidia all persist `request + assistant reply`).
**Fix (3 sub-defects)**:
1. Added `_assistant_message_from_chat` helper (same semantics as the other wrappers).
2. All three non-stream store sites now persist `messages + [assistant reply]
   (incl. tool_calls)`.
3. Streamed `/v1/responses` turns were **never stored at all** — the translator gained
   an optional `store_ctx=(principal, request_messages)`; on a successful
   `response.completed` it persists the full turn (stream failure/disconnect paths
   never store partials).

**Proof**:
- `agent_loop_e2e check_responses_replay` (non-stream turn 1): replayed history
  carries assistant tool_calls AND role:tool entries with matching ids — echo-verified
  upstream — on all 5 wrappers.
- `agent_loop_e2e check_responses_replay_streamed_turn` (streamed turn 1): same
  guarantees after a `response.completed` stream.

### Harness-hardening (not wrapper defects)
- `check_error_surface` now asserts *shaped* SDK errors (typed exception with
  actionable message) instead of demanding upstream text survival — multi-key pools
  legitimately collapse a retried upstream 500 into their own exhaustion error
  (CONTRACT §5); what agents need is a parseable error, never a hang/parse failure.

## New permanent verification (locks the classes)

| Agent scenario (per wrapper) | What it proves |
|------------------------------|----------------|
| `/v1/messages` stream tools → tool_result echo turn | strict SDK block lifecycle; `stop_reason=tool_use`; upstream receives `role:tool` per tool_use id (no orphan) |
| `/v1/messages` stream DSML | MiniMax markup surfaces through the SDK as an **executable** `tool_use` (name+JSON input), never as text |
| `/v1/messages` non-stream tools | strict `Message` pydantic parse, stop_reason |
| `/v1/messages` stream slow | wrapper heartbeats keep strict SDK iteration alive (no read timeout) |
| `/v1/chat/completions` tools loop | args are parseable JSON; role:tool echo turn |
| `/v1/chat/completions` stream tools | P0-3 class: tool name never inside accumulated arguments delta |
| `/v1/responses` replay (non-stream + streamed turn) | full-turn store & orphan-free replay (CONTRACT §6.3/§8) |
| `http429once` with `max_retries=3` | transient 429 recovered INSIDE the SDK (retriable status + shaped body) |
| tenant isolation replay | another principal's token cannot replay history (§6.3 runtime proof) |
| `http500` error surfaces | shaped typed exception on both SDKs |

## Audit-from-zero notes (round 6 sweeps, no further defects)

- Pool acquire/release balance re-verified via soak (8581 requests, 0 failures) and
  the heavier agent-loop traffic.
- Parameter passthrough (temperature/top_p/tools/system) already locked by the `echo`
  mode in `run_runtime_e2e.py`.
- `SPECIAL_TOKEN_FILTER*` coupling re-checked after the R5 fix; DSML suppression stays
  active with the cosmetics switch off (unit tests `TestR5DsmlEnvIndependence`).
- Cross-wrapper store-replay parity now uniform: all 5 wrappers persist
  `request messages + assistant reply (incl. tool_calls)` for stream and non-stream
  `/v1/responses` turns.

## Accepted, documented non-contract gaps (unchanged from R5)

- COMPATIBILITY_LAYER=2 Anthropic-upstream passthrough: DSML suppressed, not recovered.
- Chat surface DSML = strip-only by design parity.
