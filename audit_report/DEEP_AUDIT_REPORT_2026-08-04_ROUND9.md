# Deep Audit Report — 2026-08-04 Round 9 (unit-6 registry races + global id-uniqueness hardening)

**Scope**: fifth full re-audit "from zero" (R8 committed, all 8 gates green).
Directive unchanged: bug fixes + long-run resilience of existing logic only —
**no new features**; keep the whole monorepo safe for many concurrent
agents/clients (Claude Code, Codex, openclaw, hermes, opencode, openhands, …).

**New coverage this round**: (a) the sixth monorepo unit, **model-registry
service (:9200)**, which earlier rounds had not audited line-by-line; (b) a
global sweep of every remaining non-unique id mint (the R7 class) across all
translation layers, not just Responses store keys.

**Result**: 3 defect classes found & fixed, 8 regression tests added,
**all 8 gates green (308 unit now)** · registry-service tests 7/7.

| Gate | Result |
|------|--------|
| `pytest tests -q` | ✅ 308 passed |
| `run_runtime_e2e.py` | ✅ 990 checks, 0 failures |
| `sdk_codex_compat.py` | ✅ clean |
| `compat_layer_e2e.py` | ✅ L2 + L3, all 5 wrappers |
| `full_matrix_audit.py` | ✅ 240/240 |
| `soak.py` | ✅ stable |
| `agent_loop_e2e.py` | ✅ 55 checks, 0 failures |
| `multiagent_concurrency_e2e.py` | ✅ 10 checks, 0 failures |

---

## Findings & fixes

### R9.1 (race) — model-registry `/health` iterated `registries` while ingests mutated it (MR-2 class)

`model-registry/service.py /health` did `"providers_loaded":
sorted(central.registries)` — a plain-dict **iteration** that is NOT guarded,
while `/internal/catalog` creates provider entries in the **threadpool**
(under `CentralRegistry._guard`). Concurrent health polls (systemd/monitor
polls constantly!) during a catalog ingest → `RuntimeError: dictionary
changed size during iteration` → **500 on the monitoring endpoint**. This is
the exact class the MR-2 comment fixed for `list_models`; the `/health` call
site was missed.

**Fix**: `CentralRegistry.providers()` — guarded snapshot; `/health` now uses
it. Regression lock: thread-hammer test (8 providers created concurrently
while 400 guarded snapshots are taken) + source-lock test (no direct
iteration in the health body).

### R9.2 (parity) — model-registry alias ingest ran sync SQLite on the event loop, outside the guard

`/internal/aliases` performed `LocalModelRegistry.bind_alias` →
`profile_store.save_alias` (**SQLite write**) directly in the async handler
(event loop), while `/internal/catalog` and `/internal/observations` had the
MR-2 `asyncio.to_thread` treatment. Result under concurrent agents: SQLite
I/O stalls ALL in-flight requests of that process, and the binding loop raced
concurrent catalog writes to the same registry.

**Fix**: `CentralRegistry.bind_aliases(provider, bindings)` — guarded binding
loop; handler validates shapes (still shaped 400s), then
`await asyncio.to_thread(central.bind_aliases, …)`. Behaviour proof:
existing + new alias ingest/resolve tests pass (7/7 service tests).

### R9.3 (hardening, R7-class) — every remaining non-unique id mint uniquified (all translation layers)

The R7 store-key fix uniquified `resp_*` ids; the same ms-timestamp collision
window remained on every other id channel — manifesting as **duplicate
message/item ids across concurrent turns** (agents that cache/dedupe by
message id can merge or drop turns):

- **nvidia non-stream Anthropic path**: caller passed
  `request_id=f"msg_{int(time.time())}"` → translator produced
  **`msg_msg_<epoch_seconds>`** (double prefix **and** second-granularity
  non-unique). Fixed at both ends: translator composes via
  `_compose_msg_id()` (single prefix, CSPRNG fallback) and the caller now
  lets the translator mint.
- `common/translations/shared.py`: fallbacks in
  `anthropic_to_openai_response` (`msg_…`) and
  `stream_anthropic_to_openai` (`chatcmpl-…`) gained `-{secrets.token_hex(4)}`.
- `common/translations/anthropic_stream.py`: `AnthropicStreamState.msg_id`.
- nvidia inlined `AnthropicStreamState.msg_id` (main.py).
- nous inlined stream state (`msg-…`) and the Responses/Anthropic id
  fallbacks in opencode / blackbox / openrouter — all now route through a
  `_new_msg_id()` helper (or inline secrets mint in nous), marked R9.
- Regression lock: `TestR9UniqueMessageIds` — 50 sequential mints must be 50
  distinct ids for every minting path (shared fallbacks, stream states,
  nvidia compat incl. the `msg_msg_` regression), loader namespace for the
  isolated openrouter translator teaches `_new_msg_id` (same pattern as R7's
  `_new_response_id`).

## Negative sweeps (audited this round, clean)

| Area | Verdict |
|------|---------|
| DSML fork in nvidia `anthropic_compat` | ✅ imports the SHARED parser (R5 fix held) |
| nvidia `_recent_429` window list | ✅ pruned on every read path |
| nvidia `_sse_clients` add/discard pairing | ✅ finally-paired |
| model-registry `resolve()`/`call_plan()`/`status_map()` | ✅ single `.get()` ops / per-call SQLite connections — atomic |
| nous/opencode/blackbox pool `load_from_env` hot rebind from watchdog thread | ✅ atomic list rebind, no in-place mutation |
| `common/logging_utils.py`, `common/body_guard.py` | ✅ read-through, no findings |
| Legacy `resp_{int…}` mint sites | ✅ none remain anywhere |

## Docs maintenance

Unit count 300 → **308** propagated to contract §11/footer, README status/
testing/version-history/audit/footer blocks, and the INDEX status table.
