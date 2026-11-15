# Coarena Wrapper Contract Compliance — 2026-11-15

**Scope:** `coarena/` wrapper upgrade to WRAPPER_CONTRACT v3.2 (2026-10-07)
**Status:** All six contract gates green on isolated mock upstreams; live
client qualification (Claude Code / Codex / OpenCode CLI) is pending and
recorded honestly as not yet performed.
**Frozen-provider policy respected:** `opencode/` and `blackbox/` were never
read, analyzed, edited, tested or probed during this work.

## 1. Summary

The `coarena` metadata existed in `wrappers.json` (port 9107, status
production) while the implementation predated the contract: a plain FastAPI
forwarder with `main:app` entry point, comma-separated key env var, no auth,
no metrics persistence, no required surfaces. This upgrade re-implements the
wrapper on the fleet's shared infrastructure and closes every clause of the
contract that applies to an active wrapper.

## 2. What changed

| Area | Before | After |
|---|---|---|
| Entry point | `uvicorn main:app` (contract §1.1 violation) | `uvicorn src.main:app` with wrapper dir as WorkingDirectory/PYTHONPATH; `wrappers.json`, systemd unit and README aligned |
| Structure | `src/main.py` + requirements only | `__init__.py`, `src/__init__.py`, `src/main.py`, `src/key_pool.py`, `src/metrics.py`, `README.md`, `.env.example`, `requirements.txt` (pinned to `tests/constraints-verified.txt`), `systemd/wrapper-coarena.service` |
| Required surfaces | 5 of 12 | All §2.1 surfaces incl. `/v1/capabilities`, `/api/tags`, `/metrics/model-status`, `/metrics/prom`, `/ready`, `/version`, and shaped `501 not_implemented_error` on `/v1/embeddings` naming nvidia-python/openrouter |
| Upstream dialect | hardcoded forwarder | COMPATIBILITY_LAYER 1/2/3 via `common.compat`; native passthrough for matching dialects (Coarena serves Chat, Responses and Messages natively), shared translators for cross-dialect paths |
| Responses | raw forward | native passthrough with owner-approved Chat adapter fallback (`RESPONSES_UPSTREAM_MODE`), tenant-namespaced bounded response store (§6.3 three-axis) |
| Key pool | round-robin index, no cooldown | `src/key_pool.py`: least-effective-load + RR tie-break, model-scoped cooldown, side-effect-free predicates (B-37), exactly-once in-flight release incl. client disconnect (I4), exhaustion → 429 + Retry-After |
| Cooldown policy | none | shared `common.translations.should_cooldown_key` — model-capacity errors never cool the credential (§2.2.3) |
| Auth | none (open relay) | fail-closed middleware (§5.1), per-request token re-read (B-29), byte-safe compare (B-30), `Authorization: Bearer` + `x-api-key`, exact-match method-gated public paths (B-27), every POST authenticated (B-31) |
| Input handling | bare `body.get` | `common.body_guard.JSONBodyGuard` + 10 MB `RequestSizeLimiter`; `max_tokens`/`max_output_tokens` positive-int cap 1,000,000 on adapter paths; orphan tool message / unknown role shaped 4xx |
| Streaming | raw pipe | `common.sse` sentinel-task heartbeat (B-08), frame-boundary-only comments, upstream terminators preserved verbatim, deterministic generator closure (B-09/R-07), error frames surfaced terminally (B-07/R-03), unparsable frames dropped (B-10) |
| Metrics | none | `src/metrics.py`: JSON snapshot persisted every `METRICS_PERSIST_SEC` (§10, survives SIGKILL), error counter incremented on all failure paths (B-39), Prometheus + per-model/key/activity surfaces |
| Observability | none | `X-Request-ID` + `X-Process-Time` on every response; model outcomes recorded per credential into the shared model registry off the hot path (§10) |
| Lifecycle | none | graceful drain `SHUTDOWN_DRAIN_SEC` (B-34), background-task registry (B-35), one shared aiohttp session (§6.2) |
| Extras | none | authenticated upstream-native `/v1/completions`, `/v1/limits`, `/v1/usage`, `/v1/feedback` (permitted provider extras, §2.1) |
| Registry manifests | none | `model-registry/manifests/providers/coarena.json` (all three surfaces native passthrough) + `manifests/errors/coarena.json` |
| Secrets | real API key committed in `.env.example` | placeholders only; **the exposed key must be rotated on the Coarena account page** |

## 3. Harness and test integration

- `tests/test_coarena_contract.py` (new): surfaces inventory, fail-closed auth,
  dual-header auth + byte-safe compare, public-path method gating, shaped 501
  embeddings, R-01 non-object bodies, §4 max_tokens validation, B-39 error
  counter, B-33 three-axis store bounds, §3.5 native terminator preservation,
  documented upstream RPM limits, contract entry points in the systemd unit,
  no-real-credential guard on `.env.example`, fail-fast config validation.
- `tests/e2e_runtime/run_runtime_e2e.py`, `sdk_codex_compat.py`,
  `compat_layer_e2e.py`, `full_matrix_audit.py`: coarena registered
  (ports 19107/19207, `COARENA_` prefix, `/v1` base style).
- `tests/test_wrapper_scope.py`, `tests/test_production_gates.py`: scope
  tuples updated for the coarena production entry (metadata-driven counts).
- `tests/test_context_recovery.py`, `tests/test_native_request_headers.py`:
  coarena transport branches added (mirror openrouter fixture); two
  pre-existing fixture drifts fixed honestly (nvidia `_stream_proxy` stub
  signature; nvidia streaming `stream_options.include_usage` expectation).
- `openrouter/src/main.py` + `coarena/src/main.py`: wrapped upstream content
  iterator is now closed deterministically in `stream_gen` (§3.5; fixes the
  pre-existing GC-timing flake in
  `test_streaming_response_cleanup.py::test_actual_openrouter_native_disconnect…`).

## 4. Gate evidence (2026-11-15, this working tree)

| Gate | Command | Result |
|---|---|---|
| 1 | `python -m pytest tests -q` | **1353 passed, 105 skipped, 0 failed** |
| 2 | `python tests/e2e_runtime/run_runtime_e2e.py` | **356 checks, 0 failures** (4 active wrappers × 3 surfaces × 22 stream modes + discovery/error/disconnect/concurrency probes) |
| 3 | `python tests/e2e_runtime/sdk_codex_compat.py` | **all wrappers × 4 modes parsed by the official openai SDK**, incl. coarena |
| 4 | `python tests/e2e_runtime/compat_layer_e2e.py` | **layer 2 + layer 3 verified across 4 production wrappers**, incl. coarena |
| 5 | `python tests/e2e_runtime/full_matrix_audit.py` | **192/192 PASS, 0 FAIL, 0 BLOCKED** (real anthropic + openai SDKs) |
| 6 | `python tests/e2e_runtime/soak.py --seconds 12 --concurrency 6` | **0 failures**, p50 18 ms / p95 30 ms, RSS +4 MB, 0 server-log issues |

Gate counts differ from the 2026-08-01 historical snapshot because the
frozen pending providers are excluded from scope by `wrappers.json` — the
active fleet is nvidia-python, nous, openrouter and coarena.

## 5. Limitations (stated, not hidden)

- No live Coarena request was made from this audit; all gates run against the
  repository's isolated mock upstreams with fake credentials. Live client
  qualification (Claude Code / Codex / OpenCode CLI loops on a real model)
  must be recorded before treating coarena as production-verified in the
  sense of the other three wrappers.
- Coarena's catalog publishes plain model ids without per-model parameter
  metadata, so `/v1/capabilities` reports the transport-facts shape with
  honest provider-sourced entries; deeper per-model capability profiles stay
  `unknown` until runtime observations fill the shared registry.
- Historical committed credentials: `.env.example` previously contained a
  real `coa_…` key. It ships placeholders now, but that key must be rotated
  and the git history treated as exposed.

## 6. Files touched

- New: `coarena/__init__.py`, `coarena/src/__init__.py`, `coarena/src/main.py`,
  `coarena/src/key_pool.py`, `coarena/src/metrics.py`, `coarena/README.md`,
  `coarena/systemd/wrapper-coarena.service`,
  `model-registry/manifests/providers/coarena.json`,
  `model-registry/manifests/errors/coarena.json`,
  `tests/test_coarena_contract.py`
- Modified: `coarena/.env.example`, `coarena/requirements.txt`,
  `wrappers.json`, `README.md`, `tests/e2e_runtime/run_runtime_e2e.py`,
  `tests/e2e_runtime/sdk_codex_compat.py`,
  `tests/e2e_runtime/compat_layer_e2e.py`,
  `tests/e2e_runtime/full_matrix_audit.py`, `tests/test_wrapper_scope.py`,
  `tests/test_production_gates.py`, `tests/test_context_recovery.py`,
  `tests/test_native_request_headers.py`,
  `tests/test_active_native_passthrough.py`, `openrouter/src/main.py`
- Removed: `coarena/systemd/coarena.service` (non-contract unit name)
