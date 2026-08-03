# Deep Audit Report — 2026-08-04 Round 10 (id-uniqueness closure: tool_use ids)

**Scope**: sixth full re-audit "from zero" (R9 committed, all 8 gates green).
Continued the R9.3 sweep one layer deeper: after `resp_*`/`msg_*`/`chatcmpl-*`
came every `toolu_*` mint. A colliding **tool_use id** is worse than a
duplicate message id: Anthropic pairing is by id, so two turns in a stored &
replayed history carrying the same fallback id makes `tool_result`
ambiguous — upstream rejects it (400) or pairs it with the wrong call and the
agent loop derails mid-run.

**Result**: 14 fallback mints uniquified, 2 regression tests added,
**all 8 gates green (310 unit now)**.

| Gate | Result |
|------|--------|
| `pytest tests -q` | ✅ 310 passed |
| `run_runtime_e2e.py` | ✅ 990 checks, 0 failures |
| `sdk_codex_compat.py` | ✅ clean |
| `compat_layer_e2e.py` | ✅ L2 + L3 |
| `full_matrix_audit.py` | ✅ 240/240 |
| `soak.py` | ✅ stable |
| `agent_loop_e2e.py` | ✅ 55 checks |
| `multiagent_concurrency_e2e.py` | ✅ 10 checks |

---

## Findings & fixes

### R10.1 — ms-timestamp `toolu_*` fallbacks collided under concurrency (14 sites)

Fallback tool_use ids minted as `f"toolu_{int(time.time()*1000)}"` (and DSML
variants with a 10k-modulo `hash(name)` suffix) collided whenever two turns
minted a fallback id inside the same millisecond — and because assistant
`tool_calls` are persisted in the Responses store (R6) and replayed via
`previous_response_id`, the collision survives in history, not just in one
stream. Python's `hash()` is also per-process randomized, so the DSML suffix
added zero cross-process uniqueness.

Sites (all now carry `-{secrets.token_hex(3)}`, keeping `toolu_*` prefix and
the existing length budget — Anthropic ids allow 64 chars of `[A-Za-z0-9_-]`):

| File | Sites |
|------|-------|
| `common/translations/shared.py` | DSML parser + 2 Anthropic⇄OpenAI bridges |
| `common/translations/anthropic_stream.py` | stream tool-call + DSML drain fallbacks |
| `nvidia-python/src/anthropic_compat.py` | DSML parser, stream, non-stream |
| `nvidia-python/src/main.py` | inlined stream state + DSML drain |
| `nous/src/main.py` | inlined stream state + DSML drain + non-stream |
| `opencode/src/main.py` | non-stream chat→anthropic |
| `blackbox/src/main.py` | non-stream chat→anthropic |
| `openrouter/src/main.py` | stream block-index fallbacks + DSML block |

Note: index-scoped fallbacks (`toolu_{self.index}`) were ALSO suffixed — the
index is unique within one message but NOT across turns of a stored history.

**Regression lock**: `test_dsml_recovered_tool_use_ids_unique` (50 parses →
50 distinct ids) and `test_stream_tool_call_fallback_ids_unique` (50 streams
minting a missing-id tool call mid-turn → 50 distinct ids).

## Negative sweeps (clean this round)

| Area | Verdict |
|------|---------|
| `common/translations/anthropic_stream.py` full read (block lifecycle, R-02 parallel tool blocks, B-06 stop_reason mapping, R-03 error frame, DSML drain, force_done heuristics) | ✅ hardened, no new finding |
| `common/body_guard.py` full read (JSON-null hole, semantic contract, replay subtleties) | ✅ complete |
| `parse_dsml_from_text` (incomplete-markup suppression, R5 hold) | ✅ correct |
| `repair_orphan_tool_messages` (orphan role:tool → user text) | ✅ correct pairing logic |
| `common/model/central_client.py` observation queue | ✅ bounded `asyncio.Queue(maxsize=…)` + QueueFull→dropped-counter + circuit breaker + worker auto-restart |
| `common/model_state.py` async façades | ✅ every SQLite path goes through `asyncio.to_thread` |
| `nvidia/src/metrics.py` | ✅ aiosqlite end-to-end, no loop-blocking |
| nous OAuth token cache (`_read_token_from_auth_path`) | ✅ mtime-cached read only; no refresh race |
| `except BaseException` / swallowed `CancelledError` sweep | ✅ all cleanup paths re-raise properly |

**Verification at commit: 8/8 gates green — 310 unit · 990 runtime E2E · 240
matrix · SDK-compat · L2+L3 · 55 agent-loop · 10 concurrency · soak.**
