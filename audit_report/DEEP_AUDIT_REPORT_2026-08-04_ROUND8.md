# Deep Audit Report — 2026-08-04 Round 8 (post-green re-audit: concurrency & long-run resilience)

**Scope**: fourth full re-audit "from zero" (round 7 committed all 8 gates green).
Operator directive: bug fixes + long-run hardening of EXISTING logic only — **no
new features**; prove the wrappers serve many concurrent agents with no race
condition; README.md + WRAPPER_CONTRACT.md honoured in full.

**Method**: fresh static sweeps over the shared mutable state paths the gates
cannot see (store identity/aliasing, cross-thread callbacks, counter pairing),
plus full gate re-runs after every change.

**Result**: 2 real defects found & fixed, 3 regression tests added,
**all 8 gates green (300 unit now)**.

| Gate | Result |
|------|--------|
| `pytest tests -q` | ✅ 300 passed (+2 R8 regression tests) |
| `run_runtime_e2e.py` | ✅ 990 checks, 0 failures |
| `sdk_codex_compat.py` | ✅ clean |
| `compat_layer_e2e.py` | ✅ L2 + L3, all 5 wrappers |
| `full_matrix_audit.py` | ✅ 240/240 |
| `soak.py --seconds 12 --concurrency 6` | ✅ stable, RSS delta 2 MB |
| `agent_loop_e2e.py` | ✅ 55 checks, 0 failures |
| `multiagent_concurrency_e2e.py` | ✅ 10 checks, 0 failures |
| `run_runtime_e2e.py --wrapper nvidia-python` (post-watchdog-fix) | ✅ 198 checks |

---

## Findings & fixes

### R8.1 (High, latent corruption) — Response stores shared LIVE message dicts with the request pipeline (4 of 5 wrappers)

**Class**: shared-mutable-state across requests — a race-condition amplifier.
nous' N-19 fix (deep copy on BOTH store write and replay read) existed only in
nous; opencode, blackbox, openrouter and nvidia still:
- stored the caller's `messages` list **by reference** — any later in-place
  mutation of those dicts (normalisation, sanitisation, content coercion)
  would silently rewrite the stored history, and
- returned stored dicts **by reference / shallow `list()` copy** on replay —
  two concurrent turns replaying the same `previous_response_id` shared the
  SAME nested dicts between pipelines, so one turn's in-place edit could
  corrupt the other's upstream body AND poison the store for every later hop.

**Audit evidence**: `_store_response`/`_load_response` (opencode 952/1027),
`_store_response`/`_get_stored_conversation` (blackbox 1859/1903, openrouter
2210/2243), `_bounded_store`/`_load_stored` (nvidia responses_compat 126/152).
Today the pipelines are read-mostly (verified: all `m['content'] = ...` sites
operate on freshly-built or response-side dicts), which is exactly why the
gates never caught it — but the alias meant ONE future in-place edit (or one
unusual request shape) corrupts cross-agent replay state permanently. Under
the R6/R7 multi-agent storm this is a data-integrity time bomb, and §8 parity
mandates identical store semantics across all 5 wrappers.

**Fix**: nous' exact N-19 pattern applied to all four lagging wrappers —
`copy.deepcopy` (with the same `(TypeError, ValueError, RecursionError)`
fallback to `list()`) on BOTH the write and the read path, 8 sites total.
No behaviour change for well-formed traffic; aliasing vector closed.

**Proof**: new `TestStoreDeepCopyIsolation` —
`test_all_wrappers_store_isolated_both_directions` (5 subtests): mutate the
caller's dicts after the store write → stored history pristine; mutate the
REPLAYED copy → stored history pristine for a concurrent second replay.

### R8.2 (robustness bug) — nvidia `.env` key hot-reload silently never worked

`Server.init()` registers `_sync_pool_from_env` into `_ENV_RELOAD_CALLBACKS`,
which the watchdog **observer thread** invokes on `.env` modification. The
callback scheduled the async pool sync via `asyncio.get_event_loop()` — which
raises `RuntimeError` on any modern Python when called from a thread without a
running loop (the exception was caught and logged, so key rotation hot-reload
degraded to a silent no-op → operators believing new keys were live while the
pool kept the old ones).

**Fix**: capture `asyncio.get_running_loop()` inside `init()` (we are provably
on the loop thread there) and hand it to `run_coroutine_threadsafe`. The
cross-thread marshalling itself (pool lock inside the loop thread) was already
correct. Verified: `run_runtime_e2e.py --wrapper nvidia-python` still boots
and passes 198 checks.

### R8.3 (parity lock) — store axis-bound tests for nous + openrouter

nvidia/opencode/blackbox had explicit entry-count/byte-axis store tests; nous
and openrouter only had them implicitly. Added
`test_nous_openrouter_store_axes_bounded` (entry-count axis + "freshest entry
survives" invariant, including nous' async `_STORE_LOCK` path and openrouter's
OrderedDict LRU).

## Negative sweeps (audited this round, clean — no change needed)

| Area | Verdict |
|------|---------|
| Key-pool lock discipline: nvidia manual acquire/release is try/finally everywhere; opencode/blackbox/openrouter use `async with`; acquire+increment atomic | ✅ |
| Key-pool `load_from_env` from watchdog threads: atomic REBIND of the key list (no in-place list mutation) — no torn iteration | ✅ |
| Store funcs in all 5 wrappers are synchronous (no `await` between read/write) → atomic on the event loop | ✅ |
| `msg_*`-id ms-timestamp mints: opaque per-response ids, never store keys (`resp_*` store keys were uniquified in R7) | ✅ benign, documented |
| `common/sse.iter_chunks_with_idle`: retained-task pattern, pending chunk task cancelled in `finally` | ✅ |
| `common/compat.probe_upstream_compatibility`: double-checked cache under module lock | ✅ |
| Rate limiters (common RateLimiter + 5 in-wrapper stores): lock-guarded + periodic sweep, bounded | ✅ |
| nvidia `_compat2_proxy` in-flight accounting: every increment has a matching decrement on 429/error/exception/non-stream/stream-finally paths | ✅ |
| Metrics counters: nous `threading.Lock`-guarded + persisted; others direct int increments on the loop | ✅ |
| Request-size middleware: chunked counting + BaseException sentinel cannot be swallowed by endpoint `except Exception` | ✅ |
| Docs: README/contract updated to 8 gates + v3.2 everywhere; stale v3.1/298 references swept | ✅ |

## Docs maintenance

- Test count 298 → **300** propagated to contract §11, contract footer, README
  status/testing/version-history/audit/footer blocks.
- INDEX.md: round-8 row added as Current.
