# Deep Audit Report — 2026-08-03 Round 5 (DSML double-scrub, stop_reason parity, index lifecycle)

**Scope**: end-to-end continuation of the round-4 audit after all gates were green.
Round 5 targeted the remaining replication gap between "gates green" and "agents work":
MiniMax DSML tool-call markup handling across every surface of all 5 proxies, plus an
`audit from zero` pass that re-exercised the class of every round-5 fix across all
wrappers (CONTRACT §8 parity).

**Result**: 8 defect classes found & fixed (3 P0-Critical, 4 P1-High, 1 P2-Medium),
**all 6 official gates green** with new permanent assertions locking every fix.

| Gate | Result |
|------|--------|
| `pytest tests -q` | ✅ 298 passed |
| `run_runtime_e2e.py` | ✅ 990 checks, 0 failures |
| `sdk_codex_compat.py` | ✅ all wrappers × modes parse cleanly (OpenAI SDK) |
| `compat_layer_e2e.py` | ✅ COMPATIBILITY_LAYER=2 & =3 across 5 wrappers |
| `full_matrix_audit.py` | ✅ 240/240 |
| `soak.py` | ✅ 8581 requests, 0 failures, RSS Δ3MB |

---

## Findings & fixes

### P0-D5.1 — DSML "double-scrub": tool call silently deleted on `/v1/messages` (nvidia, openrouter)
**Symptom**: runtime e2e `dsml_stream` — `DSML tool call not recovered as tool_use block (got [])`.
**Root cause**: unlike nous/opencode/blackbox (whose translators receive the RAW parsed
chunk and scrub internally), nvidia and openrouter route upstream bytes through the
SHARED `PassthroughBlockRewriter` FIRST (which suppressed MiniMax
`<|DSML|tool_calls>…` markup as part of the R5 P0-4 work) and only then hand the
re-serialised frames to the Anthropic translator. The translator's recovery logic
(`parse_dsml_from_text`) never saw the markup → tool call silently lost, agent turn
ends with plain text — exactly the "agent stops mid-process / tool never runs" class.
**Fix**:
- `common/sanitize_tokens.py`: `PassthroughSSE(dsml_suppress: bool = True)` and
  `PassthroughBlockRewriter(..., dsml_suppress: bool = True)`; with `False` the DSML
  filter is not installed and markup bytes pass through for the recovering translator.
- `nvidia`: `_new_block_rewriter(dsml_suppress=…)`; `proxy_openai.stream_wrapper` uses
  `dsml_suppress=(metric_path != '/v1/messages')` (chat/responses keep suppression).
- `openrouter`: `_proxy_request(..., dsml_suppress=True)`; the
  `/v1/messages → chat/completions` call-site passes `dsml_suppress=False`.
**Proof**: runtime e2e 990/0 incl. recovery assertions; unit tests
`TestR5DsmlSuppressFlag` (3) + `TestR5TranslatorRecovery` (4).

### P0-D5.2 — fragmented DSML opener leaked as visible text (nvidia streaming translator)
**Root cause**: nvidia `anthropic_compat.stream_openai_to_anthropic` detected DSML with
per-chunk `chunk.find('<|DSML|tool_calls>')`. An opener split across two chunks
(`<|DS` + `ML|tool_calls>…`) was never matched and the fragment leaked into the text
channel; the legacy inline `process_dsml` machine only handled chunk-aligned markup.
**Fix**: the translator now feeds content through the shared cross-chunk
`DsmlMarkupFilter` (suppression + collection), flushes it in the terminal drain
(shared order: DSML flush → token-filter), and re-emits every complete collected
markup segment as real `tool_use` blocks before `message_delta`. The legacy state
machine stays as the degraded mode when `common/` is unavailable.
**Proof**: `test_nvidia_stream_recovers_tool_use` (mid-tag split, stop_reason);
runtime e2e dsml_stream.

### P0-D5.3 — stop_reason `end_turn` on DSML tool turns (agent closes turn, never runs tool)
**Symptom**: new runtime assertion caught `[nous]/[opencode]/[blackbox] /v1/messages
mode=dsml_stream: stop_reason must be tool_use (got 'end_turn')` plus the same on the
non-stream shapes.
**Root cause**: every B-06 finish_reason mapping honoured upstream `finish: 'stop' →
end_turn` even while recovered DSML `tool_use` blocks were present. MiniMax reports
`finish: 'stop'` for tool turns (it does not know its markup is a tool protocol), so
agents closed the turn and never executed the recovered tool.
**Fix (8 variants, CONTRACT §8)**: upgrade `end_turn → tool_use` ONLY when DSML tools
were recovered (real `tool_calls` with `finish: 'stop'` still map to `end_turn` — B-06):
- `common/translations/shared.py::openai_to_anthropic_response` (non-stream)
- `common/translations/anthropic_stream.py` finish branch (stream)
- `nous` local forks (non-stream `openai_to_anthropic`, stream finish branch — return
  value of `_drain_dsml_terminal` was discarded)
- `opencode`, `blackbox` local non-stream forks
- `openrouter` inline translator (stream + non-stream), `nvidia` (already any-tool_use).
**Proof**: new e2e assertions (stream + non-stream stop_reason), unit
`TestR5DsmlStopReasonUpgrade` (6 tests incl. B-06 guard).

### P1-D5.4 — incomplete DSML markup leaked in non-stream bodies (fork of shared bug)
**Root cause**: nvidia `anthropic_compat.openai_to_anthropic` has its own inline DSML
segment parser (a §7 fork); on an unterminated trailing segment it appended the RAW
markup to the visible text (`'…don't leak partial markup'` comment contradicted by code)
— the same defect fixed in the shared `parse_dsml_from_text` earlier in R5.
**Fix**: drop unterminated trailing segments (shared semantics).
**Proof**: `test_nvidia_nonstream_drops_incomplete_markup`.

### P1-D5.5 — DSML suppression gated behind `SPECIAL_TOKEN_FILTER=0`
**Root cause**: `scrub_chat_chunk_inplace` / `scrub_openai_response_inplace`
early-returned when the cosmetic token filter env was off, which silently also disabled
DSML markup suppression. DSML leakage is a protocol failure (client JSON parse), not
visual noise, and must not be conditional on the cosmetics switch.
**Fix**: gate only token filtering on the env; DSML suppression is always active.
**Proof**: `TestR5DsmlEnvIndependence` (2 tests).

### P1-D5.6 — content_block index reused after close (openrouter streaming translator)
**Symptom**: runtime e2e — `[openrouter] /v1/messages mode=dsml_stream: content_block
index 0 reused after close`.
**Root cause**: the natural-EOF close of an open TEXT block was the only block-close
site that did not advance `block_index`; the subsequent DSML drain opened the recovered
tool block on the just-closed index — breaking SDK block bookkeeping.
**Fix**: `block_index += 1` after the close (parity with every other close site).
**Proof**: runtime e2e 990/0 (index-reuse assertion active on every surface).

### P2-D5.7 — brittle fixed char-window source-grep test (R-05 class)
`test_b06_openrouter_non_streaming_content_filter_maps_to_refusal` used a 2400-char
window that silently moved off the mapped code when the function grew (R5 DSML
recovery). Replaced with a boundary-based window (next top-level `def`), matching the
R-05 repair pattern.

### Previously fixed earlier in round 5 (verified by this round's full gate run)
- nvidia metrics accounting (§10): streamed success turns recorded; terminal upstream
  errors recorded on stream & non-stream paths; transport exhaustion → 502 (nous
  parity) instead of a fabricated 429; `_record_local_reject` for early returns.
- nvidia catch-all route (§4): shaped 400 for non-object JSON / non-string model.
- `DsmlMarkupFilter` (shared, `common/sanitize_tokens.py`): cross-chunk, fullwidth-aware
  DSML suppressor+collector with 512KB fail-safe; `strip_dsml_markup` one-shot.
- Shared `parse_dsml_from_text`: unterminated trailing segments dropped (was appended).
- `PassthroughSSE`/`PassthroughBlockRewriter`: DSML suppression on all three dialect
  shapes; mock mode `dsml_stream` (mid-tag split); 3 non-stream shapes with recovery
  assertions.
- nous ResponsesStreamState: DSML suppression + done-flush (suppress-only — parity).
- opencode/blackbox/openrouter responses stream translators + `chat_to_responses`:
  suppression (parity decision: responses surface = suppress only).
- openrouter `_translate_openai_stream_to_anthropic`: drain + tool re-emit on
  natural-EOF path.

---

## Audit-from-zero sweep (second pass)

After all fixes, every round-5 class was re-exercised repo-wide:

1. Re-searched ALL `parse_dsml_from_text` forks / call sites (5 wrappers + common):
   every copy now drops unterminated segments; every consumer of recovered tools
   upgrades stop_reason.
2. Verified double-suppression ordering on every `/v1/messages` variant
   (stream/non-stream × 5 wrappers): markup reaches the recovering translator on
   nvidia/openrouter; scrub-internal recovery on nous/opencode/blackbox.
3. Verified chat & responses surfaces suppress but never crash on empty deltas
   (fully-markup frame → empty delta is skipped downstream; harmless).
4. Index lifecycle check across translators (openrouter increment-at-close convention
   vs shared/nvidia increment-at-open convention).
5. Env-knob coupling audit (`SPECIAL_TOKEN_FILTER*`) → D5.5.
6. Full compile of every `.py` in the repo — clean.

## Known, documented non-contract gaps (accepted)

- COMPATIBILITY_LAYER=2 Anthropic-upstream passthrough: DSML markup is suppressed
  (never leaked) but not recovered as `tool_use` (no client-visible garbage; tool
  signal may be lost on an upstream that translates MiniMax → Anthropic format).
  Recovery would require a stateful text-block rewrite of the Anthropic dialect —
  recorded as future work, not a contract violation (§2.2.5/§3.3 unaffected).
- Chat surface: DSML = strip-only (no tool recovery), by design parity with the rest
  of the chat surface (no tool-call reconstruction there at all).
- `SPECIAL_TOKEN_FILTER_GENERIC` / `EXTRA` unchanged semantics.

## Evidence artefacts

- Runtime logs: `/tmp/rt-*.log` during the 990-check green run.
- Unit evidence: `tests/test_audit_2026_08_03.py` (57 tests incl. 13 new R5 tests),
  `tests/test_sse_streaming_regressions.py` boundary fix.
- Harness evidence: `tests/e2e_runtime/run_runtime_e2e.py` asserts DSML recovery +
  `stop_reason=tool_use` on stream and non-stream anthropic shapes (990 checks).
- Gate logs quoted above (298 / 990 / 240 / SDK / L2+L3 / soak).
