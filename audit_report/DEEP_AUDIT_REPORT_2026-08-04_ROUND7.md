# Deep Audit Report — 2026-08-04 Round 7 (multi-agent concurrency & cross-tenant integrity)

**Scope**: third full re-audit "from zero" after round 6. Focus per operator
directive: **bug fixes and long-run resilience only — no new features**; verify the
wrappers can serve *many concurrent agents/clients at once* with no race condition,
and that README.md + WRAPPER_CONTRACT.md are fully honoured.

**New instrument**: `tests/e2e_runtime/multiagent_concurrency_e2e.py` — 12
concurrent SDK agents per wrapper (`ThreadPoolExecutor`, real `anthropic`/`openai`
SDKs, dual-header auth like Claude Code) × 3 rounds, each flow carrying a unique
sentinel marker (`MARK<i>-QX7`) through store-chained turns (`previous_response_id`
/ replay) while fault modes (`http429once`, `http500`, `dsml_stream`, slow streams)
run in parallel. Post-storm invariants: `/health` and `/metrics` (recursive
in-flight scan) must report **zero** leaked reservations and must *actually expose*
in-flight telemetry (CONTRACT §10).

**Result**: 1 P0-class defect found & fixed (cross-tenant store-key collision),
1 parity defect fixed (openrouter observability), **all 8 gates green**.

| Gate | Result |
|------|--------|
| `pytest tests -q` | ✅ 298 passed |
| `run_runtime_e2e.py` | ✅ 990 checks, 0 failures |
| `sdk_codex_compat.py` | ✅ clean |
| `compat_layer_e2e.py` | ✅ L2 + L3, all 5 wrappers |
| `full_matrix_audit.py` | ✅ 240/240 |
| `soak.py` | ✅ stable |
| `agent_loop_e2e.py` | ✅ 55 checks, 0 failures |
| **`multiagent_concurrency_e2e.py` (new)** | ✅ 10 checks (2 × 5 wrappers), ~36 flow-runs/wrapper |

---

## Findings & fixes

### P0-R7.1 — Responses-store keys collided across concurrent turns → cross-agent history replay (data corruption)

**Symptom (new harness)**: `[nous] CROSS-TALK: foreign markers ['MARK3-QX7'] in my
replay` — agent A's follow-up turn received agent B's stored conversation.

**Root cause (two converging defects)**:
1. Response ids were minted as `f"resp_{int(time.time()*1000)}"` in nous,
   opencode, blackbox and openrouter. Millisecond timestamps **collide across
   concurrent turns** (12 agents × parallel threads: same-ms mints are routine).
2. Some sites preferred the upstream id (`data.get("id")`, e.g. `chatcmpl-mock`
   on the mock / recycled ids on real gateways) — one constant key shared by
   *every* concurrent caller.

Because the `/v1/responses` store is keyed by `(principal, response_id)` and
auth resolves to a single principal in single-token deployments, one colliding
`response_id` makes two agents overwrite each other's stored turn; the next
`previous_response_id` fetch then replays the **other agent's history** — silent
cross-tenant conversation corruption (worse than an error: agents act on foreign
context). This is exactly the "race condition saat melayani banyak agent" class
the operator asked to eliminate.

**Fix**:
- New shared helper `common/translations/shared.py::new_response_id(prefix="resp")`
  → `resp_<ms>-<secrets.token_hex(6)>` (time-ordered prefix for log greppability,
  48-bit CSPRNG suffix for uniqueness). Exported via
  `common/translations/__init__.py`; **single implementation, no forks** (§7).
- Adopted at **every minting site**: nous (`chat_to_responses` id + stream `rid`),
  opencode (2 sites), blackbox (`chat_to_responses` + 2 stream sites), openrouter
  (`chat_to_responses` + stream `resp_id`). nvidia already mints `_rand('resp')`
  (10-char alnum CSPRNG) — left as is, already unique.
- nous keeps its ImportError fallback shim (cold-path for docs tooling) with the
  same unique-format inline.
- Regression lock: `tests/test_sse_streaming_regressions.py` loader namespace now
  provides the REAL `new_response_id` as `_new_response_id` (3 extracted-
  translator tests previously crashed with `NameError`).

**Proof**: post-fix storm — **zero foreign markers** across ~180 store-chained
flow-runs (12 agents × 3 rounds × 5 wrappers); uniqueness also asserted at the
unit level.

### H2-R7.2 — openrouter observability parity (CONTRACT §10)

- `/metrics` served Prometheus *text* while its four siblings serve the JSON
  summary — dashboards/scripts expecting the shared shape broke. Fix: `/metrics`
  → async JSON `metrics.summary()` + `pool` block; Prometheus exposition moved
  to `/metrics/prom` (no data lost, both available).
- `/health` lacked in-flight reporting. Fix: `in_flight =
  sum(k.in_flight for k in pool.keys)` + `keys_status_detail` — the
  concurrency gate now asserts live in-flight visibility during load and
  drain-to-zero after it.
- Verified openrouter dashboard fetches `/stats` (not `/metrics`) — unaffected.

## Negative sweeps (audited, clean — no change needed)

| Area | Verdict |
|------|---------|
| Key-pool accounting (opencode/blackbox/openrouter): `asyncio.Lock`-guarded acquire+reserve, explicit `expire_block`, decrement on release | ✅ atomic, no double-reserve under storm |
| Rate limiter unbounded growth (5th wrapper class) | ✅ pruned already (nvidia V-08, nous N-14 cyclic-sweep, opencode/blackbox/openrouter sweeps) |
| In-flight leak across fault modes (`http429once`/`http500`/disconnect) | ✅ zero after storm on all 5 wrappers |
| `/v1/models` public, dual-header auth evaluation, fail-closed | ✅ re-verified under parallel load |

## New permanent verification (locks the classes)

| Concurrency check (per wrapper) | What it proves |
|---------------------------------|----------------|
| 12 agents × 3 rounds unique-marker store chains | zero cross-talk; store keys unique per turn (P0-R7.1 lock) |
| Post-storm `/health` + `/metrics` in-flight scan | reservations drain to zero; telemetry actually exposed (§10) |

## Docs maintenance (contract compliance)

- `WRAPPER_CONTRACT.md` → **v3.2**: §11 now lists **8 gates** (298/990/240/L2+L3/
  SDK/55-agent-loop/storm), §12 changelog entry added, footer verification line
  refreshed.
- `README.md` — status block, testing gates, version history (2026-08-04 entry),
  audit summary and footer updated to the same numbers.
