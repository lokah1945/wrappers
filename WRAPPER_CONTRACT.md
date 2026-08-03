# Wrapper Monorepo Contract

**Version:** 3.2 (2026-08-04)
**Supersedes:** 3.1 (2026-08-01)
**Status:** Normative. This document is the specification every wrapper is held to.

This monorepo contains provider-specific wrappers that must behave as **one coherent product**. Upstreams differ (NVIDIA NIM, Nous Research, OpenCode Zen, BLACKBOX AI, OpenRouter), but the client-facing contract is intentionally identical across all wrappers.

> **How to read this document.** Requirements use RFC-2119 keywords: **MUST**, **MUST NOT**, **SHOULD**, **MAY**. Every clause here was verified against the code at the version stamped above; where a wrapper legitimately deviates, the deviation is stated explicitly rather than hidden.

**Changes from v2.0 are summarised in [§12](#12-changelog).**

---

## 1. Standardized structure

All wrappers follow an identical layout:

```
<wrapper>/
├── __init__.py              # Package marker
├── README.md                # Wrapper-specific documentation
├── .env.example             # Configuration template
├── dashboard.html           # Monitoring dashboard
├── requirements.txt         # Pinned runtime dependencies
├── src/
│   ├── __init__.py
│   ├── main.py              # FastAPI application (entry point)
│   ├── key_pool.py          # Credential pool  (nvidia/opencode/blackbox/openrouter)
│   └── metrics.py           # Metrics collector (nvidia/opencode/blackbox/openrouter)
└── systemd/
    └── wrapper-<name>.service
```

Two documented deviations:

- **`nous`** keeps its key pool and metrics inline in `src/main.py` rather than in separate modules.
- **`nvidia-python`** carries additional provider modules (`anthropic_compat.py`, `responses_compat.py`, `capabilities.py`, `registry.py`, `alert_history.py`, `loki_push.py`) because NIM has the richest surface area.

Both are accepted; behaviour MUST still satisfy every clause below.

### 1.1 Run command (authoritative)

The wrapper directory is both the working directory and the `PYTHONPATH` root:

```bash
# systemd (production) — see <wrapper>/systemd/*.service
WorkingDirectory=/root/wrapper/<wrapper>
Environment=PYTHONPATH=/root/wrapper/<wrapper>
ExecStart=/usr/bin/python3 -m uvicorn src.main:app --host 127.0.0.1 --port <PORT>

# local development
cd <wrapper> && uvicorn src.main:app --reload --port <PORT>
```

`model-registry` is a plain service, not a wrapper: `python3 model-registry/service.py`.

> **Correction to v2.0.** v2.0 documented `uvicorn wrapper.src.main:app` (dotted package prefix). That is **not** how any wrapper is actually launched — every systemd unit and `wrappers.json` uses `src.main:app` with the wrapper directory on `PYTHONPATH`. `wrappers.json` is the machine-readable source of truth.

### 1.2 Path reference pattern

From inside `<wrapper>/src/main.py`:

```python
Path(__file__).resolve().parents[1]   # -> <wrapper>/      (dashboard.html, .env, *.db)
Path(__file__).resolve().parents[2]   # -> repo root       (the `common/` package)
```

Both wrapper dir and repo root are inserted on `sys.path` at import so `common/` resolves under either launch mode.

---

## 2. Non-negotiable runtime contract

### 2.1 Required surfaces

Every wrapper **MUST** expose:

| Surface | Endpoint |
|---|---|
| OpenAI Chat Completions | `POST /v1/chat/completions` |
| OpenAI Responses | `POST /v1/responses` |
| Anthropic Messages | `POST /v1/messages` |
| Anthropic token counting | `POST /v1/messages/count_tokens` |
| Model discovery | `GET /v1/models`, `GET /api/tags` |
| Capabilities | `GET /v1/capabilities` |
| Health / readiness | `GET /health`, `GET /ready` |
| Metrics | `GET /metrics`, `GET /metrics/prom`, `GET /metrics/model-status` |
| Dashboard | `GET /dashboard` |
| Version | `GET /version` |

`POST /v1/embeddings` **MUST** exist on every wrapper. Where the upstream has no embeddings API (`nous`, `opencode`, `blackbox`) it **MUST** return a shaped `501 not_implemented_error` naming a wrapper that does support it — never a bare 404. `nvidia-python` and `openrouter` proxy embeddings for real.

Provider-specific extras (nvidia's `/v1/ranking`, `/v1/images/*`, `/v1/engines`, `/events`; openrouter's `/catalog/*`, `/openrouter/keys/*`, `/mcp/*`) are permitted **provided** they never alter the semantics of the shared surfaces.

### 2.2 Behavioural invariants

1. **A single failed credential is never a whole-wrapper failure.**
2. **All-key retry before client error.** For retriable statuses (`401`, `402`, `403`, `408`, `409`, `429`, `5xx`) every available credential **MUST** be tried before returning an error.
3. **Per-key cooldown.** A failing key is cooled down and skipped; other keys keep serving. A cooldown decision **MUST** come from the shared `common.translations.should_cooldown_key` — a model-capacity error **MUST NOT** cool the credential.
4. **Exactly-once in-flight accounting.** A key is reserved exactly once on selection and released exactly once on non-stream completion, stream completion, stream exception, upstream EOF, **or client disconnect**.
5. **Terminally complete streams.** OpenAI ends with exactly one `data: [DONE]`; Anthropic ends with `message_delta` + `message_stop`; Responses ends with `response.completed` **or** `response.failed`, then `data: [DONE]`.
6. **No unstructured tool leakage.** Clients receive structured tool calls, never raw DSML or provider-specific markup.
7. **Conversation continuity.** `previous_response_id` retains enough assistant `tool_calls` context that the next tool result is never orphaned. The store **MUST** be tenant-namespaced and bounded (§6.3).
8. **Transparent model choice.** Wrappers do not silently substitute client-selected models. Aliases (`sonnet`, `haiku`, `opus`, `claude-*`) are dynamic/operator-bound, not hardcoded provider choices.
9. **SDK-shaped errors.** OpenAI surfaces return OpenAI-shaped errors; Anthropic surfaces return Anthropic-shaped errors.
10. **Provider specifics stay behind the adapter boundary.**

---

## 3. Streaming contract (normative)

This section is new in v3.0. Every clause below corresponds to a defect found in live testing; the tag in brackets is the finding ID, and each is enforced by a regression test.

### 3.1 Parsing upstream SSE

- **`data:` with or without a space MUST both parse.** The space is optional per the SSE spec. `[B-02]`
- **A bare `data:` (empty value) is a keep-alive, NOT end-of-stream.** Only a literal `[DONE]` terminates. `[B-01]`
- **CRLF (`\r\n`) framing MUST be normalised** before splitting, or a CRLF upstream buffers until EOF instead of streaming. `[N-08]`
- **`id:`, `retry:` and comment (`:`) lines MUST be tolerated** and ignored.
- **A frame that fails to parse as JSON MUST be logged and dropped.** It **MUST NOT** be synthesised into assistant content — doing so renders protocol text to the user as model output. `[B-10]`
- **`"choices": []` is legal.** Indexing `choices[0]` without a non-empty check raises `IndexError`, which escapes as HTTP 500 mid-stream. `[R-08]`
- **A trailing partial frame with no blank line MUST still be flushed.**

### 3.2 Emitting Anthropic SSE

- **Exactly one `message_start` and exactly one `message_stop`.** Nothing may follow `message_stop`.
- **Every `content_block_start` MUST be matched by a `content_block_stop` on the same index.** An index **MUST NOT** be reused after close, and no `content_block_delta` may reference a closed or unopened index.
- **Parallel tool blocks MUST stay open concurrently.** OpenAI interleaves argument fragments across all active tool indices; closing tool #1 when tool #2 opens orphans tool #1's later fragments and the agent's tool call silently never executes. `[R-02]`
- **A `tool_use` block MUST carry a non-empty `name`.** Phantom unnamed blocks indicate the tool map was cleared prematurely. `[R-02]`
- **Reassembled `input_json_delta` fragments MUST form valid JSON per tool.**
- **`stop_reason` MUST map strictly from `finish_reason`** (`stop`→`end_turn`, `length`→`max_tokens`, `tool_calls`→`tool_use`, `content_filter`→`refusal`). It **MUST NOT** be forced to `tool_use` merely because a tool appeared earlier — that makes Claude Code wait forever for a tool result and masks `max_tokens` truncation. `[B-06]`
  Inferring `tool_use` is permitted **only** in the no-`finish_reason` path (`force_done`) when a tool block was still open.

### 3.3 Error transparency

- **An upstream failure MUST NOT be presented as a successful turn.** A mid-stream `{"error": ...}` frame has no `"choices"` key and was historically dropped, closing the stream with a fabricated `end_turn` — the client persisted a truncated answer and could not retry. It **MUST** surface as an Anthropic `error` event / Responses `response.failed`. `[B-07, R-03]`
- **Transport errors MUST NOT be injected as model text.** No `[upstream stream error: …]` inside a `text_delta` or `output_text.delta`. `[B-13]`
- **Content arriving after `finish_reason` MUST be dropped** (protocol requirement) **and counted**, so truncation is observable rather than silent. `[B-05]`

### 3.4 Heartbeats and liveness

- Wrappers **MUST** emit `: heartbeat` comments during upstream idle gaps so reasoning models do not trip client/LB idle timeouts.
- Idle detection **MUST** use the sentinel-task pattern in `common.sse.iter_chunks_with_idle`. `asyncio.wait_for` on the upstream iterator is **forbidden**: it cancels the pending read and cannot distinguish an idle upstream from a dead one, so a failed stream is heartbeated forever while the client hangs. `[B-08]`
- A heartbeat **MUST** only be injected at a clean line boundary, never mid-frame.
- A real upstream error **MUST** terminate the stream visibly, not be masked as idle.

### 3.5 Terminators and cleanup

- **`data: [DONE]` MUST be emitted at most once.** Appending it unconditionally produces the corrupt frame `[DONE]data: [DONE]` when the upstream already sent one without its trailing blank line. `[R-06]`
- **Async generators MUST NOT yield after `GeneratorExit`/`CancelledError`.** Re-raise and clean up in `finally`.
- **Wrapped upstream generators MUST be closed deterministically** with `await gen.aclose()`. Merely breaking out of `async for` leaves the generator suspended, so its `finally` — which releases the response and the pool key — never runs, and the connection is never returned to the connector. `[B-09, R-07]`

### 3.6 Response translation

- **Non-streaming replies MUST be translated to the surface's shape.** Returning a raw OpenAI `chat.completion` body on `/v1/messages` or `/v1/responses` makes the reply unparseable to Claude Code and Codex. `[R-05]`
- A wrapper **MUST NOT** forward frames belonging to a foreign protocol onto a surface. `[B-12]`

---

## 4. Input handling

- **A malformed or non-object JSON body MUST yield a shaped `4xx`, never a `5xx`.** A valid-but-non-object body (`[1,2,3]`, `"str"`, `42`) reaches `body.get(...)`, and `list.get` raises `AttributeError` → HTTP 500. A 500 tells the SDK the *server* is broken and many SDKs then retry, amplifying load. Enforced centrally by `common.body_guard.JSONBodyGuard`, registered on all five wrappers. `[R-01]`
- `max_tokens` **MUST** be a positive integer and is capped at `1_000_000`.
- Request size is capped by `common.middleware.RequestSizeLimiter` (default 10 MB, `MAX_REQUEST_BYTES`). All five wrappers **MUST** use the same default.
- Unknown `role` values and orphan `tool` messages are repaired or rejected with a shaped 400.

---

## 5. Authentication

Two implementation shapes exist and both are acceptable, provided behaviour is identical:

| Wrapper | Mechanism |
|---|---|
| `nvidia-python`, `openrouter` | HTTP middleware (protected by default; new routes are covered automatically) |
| `nous`, `opencode`, `blackbox` | Per-route `_auth_check(request)` delegating to `common.auth` |

Middleware is **preferred** for new wrappers because a forgotten decorator cannot silently expose a route.

Requirements:

1. **Fail closed.** With no `BEARER_TOKEN` configured, inference endpoints **MUST** return `503`, not serve openly. Opt out only via `REQUIRE_AUTH=false`. `[B-28]`
2. **Re-read the token per request** so rotation and revocation take effect without a restart. `[B-29]`
3. **Byte-safe comparison.** `hmac.compare_digest` on two `str` raises `TypeError` on non-ASCII input; both operands **MUST** be encoded, yielding a clean 401. `[B-30]`
4. **Accept both `Authorization: Bearer` and `x-api-key`**, evaluated independently.
5. **Public paths MUST be exact-matched and method-gated.** Prefix matching lets `/v1/models` match `/v1/models-internal`. `[B-27]`
6. **Every POST surface is authenticated and rate-limited**, including `/v1/embeddings` and the catch-all. `[B-31]`
7. **Privileged management APIs MUST NOT share the inference bypass.** openrouter's `/openrouter/keys/*` (create/rotate/delete provisioning keys) requires `OPENROUTER_MANAGEMENT_TOKEN`. `[B-26]`
8. `OPTIONS` preflight passes without auth.

---

## 6. Resource management

### 6.1 Credential pool

- Selection is least-effective-load with round-robin tie-break.
- `record()` (telemetry) and `increment_in_flight()` (accounting) are **separate** calls. Folding them together lets any unpaired path permanently inflate `in_flight`, skewing selection away from healthy keys. `[B-36]`
- `is_blocked()` / `is_hard_blocked()` **MUST** be side-effect-free. Block expiry is an explicit `expire_block()` swept under the lock in `acquire()` — a predicate that clears state can be called by a metrics scrape outside the lock. `[B-37]`
- Pool locks **MUST** be `asyncio.Lock` in async contexts.
- Exhaustion returns **`429`** (never `503`) so SDKs auto-retry.
- Load shedding is **off** by default.

### 6.2 Connections

- One shared `aiohttp.ClientSession` per wrapper; never one per request.
- Streaming responses use `total=None` with a `sock_read` idle timeout, so long generations survive while a dead upstream is still detected.

### 6.3 Response store

The `previous_response_id` store **MUST** be bounded on **all three** axes — entry count, total bytes, and TTL. Count alone is insufficient: 200 multi-MB histories is still unbounded memory. `[B-33]`

| Variable | Default |
|---|---|
| `RESPONSES_STORE_MAX_ENTRIES` | `200` |
| `RESPONSES_STORE_MAX_BYTES` | `33554432` (32 MB) |
| `RESPONSES_STORE_TTL_SEC` | `3600` |

Keys **MUST** be namespaced by auth principal.

### 6.4 Lifecycle

- **Graceful shutdown MUST drain in-flight requests** (`SHUTDOWN_DRAIN_SEC`, default 30) before closing the session, or every deploy severs active streams. `[B-34]`
- **Fire-and-forget tasks MUST be retained in a registry.** asyncio holds only a weak reference, so an unreferenced task can be garbage-collected mid-flight. `[B-35]`
- Background tasks are awaited (bounded) during shutdown.

---

## 7. Shared modules — the parity mechanism

Cross-wrapper drift is the single largest historical source of bugs in this repo: the same defect fixed in one wrapper and not in its four siblings. Shared behaviour therefore lives in `common/` and **MUST NOT** be reimplemented locally.

| Module | Responsibility |
|---|---|
| `common/auth.py` | Fail-closed auth decision, token extraction, byte-safe compare, public-path test |
| `common/sse.py` | Sentinel-task idle iterator (`IDLE`, `iter_chunks_with_idle`), CRLF normalisation |
| `common/body_guard.py` | ASGI guard rejecting non-object JSON bodies with a shaped 400 |
| `common/translations/anthropic_stream.py` | `AnthropicStreamState` — OpenAI SSE → Anthropic SSE |
| `common/translations/shared.py` | Protocol conversion, error normalisation, retry/cooldown policy, DSML parsing, header forwarding |
| `common/middleware.py` | `RequestSizeLimiter`, header sanitisation |
| `common/model/` | Model registry client, call plans, identity guard, validation |
| `common/model_state.py` | Account-scoped model state persistence |
| `common/base_wrapper.py` | Reference base class for new wrappers |

**Shadowing a shared helper with a local definition of the same name is forbidden.** A local `_should_cooldown_key` silently overrode the shared import in three wrappers and let cooldown policy diverge. If the shared version is inadequate, improve it — do not fork it. `[B-21]`

`nous` keeps a dict-based `AnthropicStreamState` variant for its `stream_with_heartbeat` serializer. This is an accepted deviation; it **MUST** satisfy §3 identically, and it is covered by the same tests.

---

## 8. Cross-wrapper parity policy

Per [`docs/CROSS_WRAPPER_BUG_POLICY.md`](docs/CROSS_WRAPPER_BUG_POLICY.md), **a bug found in one wrapper MUST be checked against all five and fixed wherever it exists.** This is not advisory: in the 2026-08-01 audit, **six of eight runtime findings existed in more than one wrapper**, and two of them were found only by an automated guard after manual review had missed them.

Four parity guards run in CI and fail the build on regression:

| Guard | Prevents |
|---|---|
| `test_r04_no_loop_variable_shadows_a_function_parameter` | A loop variable overwriting a parameter (leaked SSE frames as model text) |
| `test_r08_no_unguarded_choices_indexing` | `choices[0]` without a non-empty check |
| `test_parity_all_wrappers_use_sentinel_heartbeat_not_wait_for` | Reintroducing `asyncio.wait_for` heartbeats |
| `test_parity_no_wrapper_shadows_shared_cooldown_helper` | Shadowing a shared helper |

---

## 9. Configuration

### 9.1 Ports and entry points

`wrappers.json` is the machine-readable source of truth.

| Wrapper | Port | Entry point | Env prefix | Upstream |
|---|---|---|---|---|
| `nvidia-python` | 9101 | `src.main:app` | `NVIDIA_` | NVIDIA NIM |
| `nous` | 9102 | `src.main:app` | `NOUS_` | Nous Research |
| `opencode` | 9103 | `src.main:app` | `OPENCODE_` | OpenCode Zen |
| `blackbox` | 9104 | `src.main:app` | `BLACKBOX_` | BLACKBOX AI |
| `openrouter` | 9106 | `src.main:app` | `OPENROUTER_` | OpenRouter |
| `model-registry` | 9200 | `service:app` | `MODEL_REGISTRY_` | internal |

Port 9105 is intentionally unused.

### 9.2 Shared environment variables

| Variable | Default | Meaning |
|---|---|---|
| `BEARER_TOKEN` | *(none)* | Client auth token. Unset ⇒ 503 unless `REQUIRE_AUTH=false`. |
| `REQUIRE_AUTH` | `true` | Fail closed when no token is configured. |
| `DISABLE_AUTH` | *(unset)* | Explicit open mode (LAN only). |
| `LISTEN_HOST` / `LISTEN_PORT` | `127.0.0.1` / per-wrapper | Bind address. |
| `<PREFIX>_API_KEY_1..N` | *(none)* | Upstream credential pool. |
| `<PREFIX>_BASE_URL` | provider default | Upstream base URL. |
| `<PREFIX>_SOFT_LIMIT_RPM` / `SOFT_LIMIT_RPM` | `30` | Per-key soft RPM (pacing). |
| `<PREFIX>_HARD_LIMIT_RPM` / `HARD_LIMIT_RPM` | `40`–`60` | Per-key hard RPM. |
| `RATE_LIMIT_RPM` | `600` | Per-IP limit; `0` disables. |
| `RATE_LIMIT_COOLDOWN_SEC` | `65` | Cooldown after a 429. |
| `KEY_COOLDOWN_MAX_SEC` | `300` | Cooldown ceiling. |
| `HEARTBEAT_INTERVAL_MS` | `5000` | Idle heartbeat interval. |
| `STREAM_SOCK_READ_TIMEOUT_SEC` | `300` | Upstream read-idle timeout. |
| `CONNECT_TIMEOUT_SEC` | `30` | Upstream connect timeout. |
| `REQUEST_TIMEOUT_SEC` | `600` | Non-streaming total timeout. |
| `MAX_CONNECTIONS` | `200` | Connector limit. |
| `MAX_REQUEST_BYTES` | `10485760` | Request size cap (10 MB, all wrappers). |
| `SHUTDOWN_DRAIN_SEC` | `30` | In-flight drain window. |
| `RESPONSES_STORE_MAX_ENTRIES` | `200` | Response-store entry cap. |
| `RESPONSES_STORE_MAX_BYTES` | `33554432` | Response-store byte cap. |
| `RESPONSES_STORE_TTL_SEC` | `3600` | Response-store TTL. |
| `METRICS_PERSIST_SEC` | `60` | Metrics snapshot interval. |
| `FREE_ONLY` / `FREE_MODEL_ALLOWLIST` | `no` | Restrict to free models. |
| `DYNAMIC_ALIAS_TARGET` | *(none)* | Operator-bound alias target. |
| `COMPATIBILITY_LAYER` | `1` | **Upstream dialect, declared by the operator** — `1` OpenAI Compatible (default, current behaviour), `2` Anthropic Compatible (upstream speaks the Messages API; surfaces are translated to/from Anthropic, `/v1/messages` passes through verbatim), `3` Auto Discovery (probe upstream once per base URL, cache with `COMPATIBILITY_PROBE_TTL_SEC`, fall back to `1`). Invalid values fail fast in `validate_config()`. See `docs/COMPATIBILITY_LAYER.md`. |
| `COMPATIBILITY_PROBE_TTL_SEC` | `300` | Cache TTL for auto-discovery probes (layer 3). |
| `LOAD_SHEDDING_ENABLED` | `false` | Shed load above `INFLIGHT_SOFT_CAP`. |
| `OPENROUTER_MANAGEMENT_TOKEN` | falls back to `BEARER_TOKEN` | Provisioning-API credential (openrouter). |

---

## 10. Observability

- **Metrics MUST persist across restarts** (JSON snapshot, or SQLite for nvidia) and **MUST** be written periodically, not only on graceful shutdown, so counters survive SIGKILL/OOM.
- **The error counter MUST actually increment.** Streaming requests and error paths were historically uncounted, leaving `error_rate` permanently ~0 and the dashboard reporting false health. `[B-39]`
- Every response carries `X-Request-ID` and `X-Process-Time`.
- `/health` reports key availability, in-flight counts and version; `/metrics/prom` exposes Prometheus format.
- Model outcomes are recorded per credential into the shared model registry by all five wrappers (`nvidia-python` calls `MODEL_REGISTRY_CLIENT.schedule_observation` directly; the others go through a `_record_model_result` helper). Persistence **MUST** be off the request hot path — never awaited before responding.

---

## 11. Testing and verification

A wrapper is **not** contract-compliant until it passes all eight gates.

```bash
python -m pip install -r tests/requirements.txt

# 1. Unit + parity + regression suite (incl. AI Gateway translation matrix)
python -m pytest tests -q                        # 308 tests

# 2. Live agent-traffic E2E — boots each wrapper as a real server against a
#    mock upstream and drives it as Claude Code / Codex / OpenAI SDK would.
python tests/e2e_runtime/run_runtime_e2e.py      # 990 checks, exits non-zero on failure

# 3. SDK compatibility — every wrapper's /v1/responses output (streaming +
#    non-streaming) must parse with the official openai SDK (Codex's parser).
python tests/e2e_runtime/sdk_codex_compat.py     # 5 wrappers × 4 modes

# 4. COMPATIBILITY_LAYER E2E — layer=2 (Anthropic upstream) across all 5
#    wrappers × 3 surfaces + layer=3 auto-discovery against both mocks.
python tests/e2e_runtime/compat_layer_e2e.py

# 5. Full matrix audit — every wrapper × upstream dialect × surface ×
#    parameter, driven with the real anthropic + openai SDKs.
python tests/e2e_runtime/full_matrix_audit.py    # 240 checks

# 6. Sustained load — leak, pool-starvation and degradation detection
python tests/e2e_runtime/soak.py --seconds 12 --concurrency 6

# 7. Agent-loop E2E — full tool-use round trips with the REAL anthropic +
#    openai SDKs: tool_use → tool_result → next turn, DSML recovery through
#    the SDK, streamed + non-streamed /v1/responses replay, SDK-internal
#    transient-429 retry, tenant isolation, shaped error surfaces.
python tests/e2e_runtime/agent_loop_e2e.py       # 55 checks (11 × 5 wrappers)

# 8. Multi-agent CONCURRENCY E2E — 12 concurrent SDK agents per wrapper
#    mixing all surfaces and fault modes; asserts zero cross-talk (unique
#    per-agent markers), event integrity under load, and zero leaked
#    in-flight reservations after the storm (CONTRACT §2.2.4).
python tests/e2e_runtime/multiagent_concurrency_e2e.py
```

The streaming E2E drives **22 streaming modes** against 3 surfaces on all 5 wrappers: `normal`, `nospace`, `keepalive`, `crlf`, `tools`, `reasoning`, `reasoning_only`, `nofinish`, `noterminator`, `midstream_error`, `usage_after`, `empty`, `unicode`, `slow`, `bigchunk`, `bytesplit`, `comments`, `dupfinish`, `nullcontent`, `emptychoices`, `toolnoid`, `longtool`. The mock upstream additionally provides non-stream modes (`echo`, `error`, `http429once`, `abrupt`, `http500`, `http429`) for parameter/error/retry probes.

**Unit tests alone are insufficient.** Every one of the eight runtime findings (R-01…R-08) passed a green 110-test unit suite; they were only reachable by running the servers and speaking to them over a socket.

---

## 12. Changelog

### v3.1 → v3.2 (2026-08-04)

Three deep-audit rounds (2026-08-03…04) hardened the five wrappers without adding features; every clause was re-verified end-to-end.

- **§2.2.4 / §3.3 stream integrity:** premature upstream EOF now always surfaces an error frame (never a clean-looking truncated stream); upstream DSML tool-call markup is suppressed at the SSE layer and recovered tool calls are emitted as first-class `tool_calls`/`tool_use` blocks with correct `stop_reason` (`tool_use` upgrade) and lifecycle block indices on all 5 wrappers.
- **§4:** special-token sanitisation (`<unk>`, `<|im_start|>`, U+0800 fragments) holds partial fragments across chunk boundaries so a token split mid-chunk cannot leak raw to the client; valid-JSON-but-semantically-broken bodies get shaped 4xx instead of upstream echoes.
- **§5:** stale `Authorization` headers from outer gateways can no longer mask `x-api-key` auth; both headers are evaluated independently (dual-header Claude Code / Hermes style).
- **§6.1 / §6.3:** mid-stream fault accounting is exactly-once per reservation; store bounds enforced on all three axes with tenant-namespaced keys.
- **§6.3 / cross-tenant corruption fix (P0):** Responses-store keys are now minted by a single shared helper `new_response_id()` (`common/translations/shared.py`, `resp_<ms>-<12hex>`). Millisecond-timestamp and upstream-`chatcmpl-*` reuse collided under concurrent turns — two tenants shared one store key and replayed each other's history. All minting sites in the 4 OpenAI-store wrappers use the shared helper (§7 — no forking).
- **§6.3 openrouter:** `/v1/responses` previous-response replay now persists assistant turns (including `tool_calls`) for both streamed and non-streamed turns, so multi-turn tool loops survive `previous_response_id` chaining.
- **§10:** openrouter `/metrics` now serves the JSON summary like its four siblings (Prometheus exposition moved to `/metrics/prom`); `/health` reports live in-flight reservations on every wrapper.
- **§11:** expanded from six to **eight gates** — added the real-SDK agent-loop E2E (55 checks: tool_use round trips, DSML recovery, replay, retry, tenant isolation) and the multi-agent concurrency storm (12 concurrent SDK agents × 5 wrappers asserting zero cross-talk and zero leaked in-flight reservations).

### v3.0 → v3.1 (2026-08-01)

- **§9.2:** added `COMPATIBILITY_LAYER` (operator-declared upstream dialect: `1` OpenAI, `2` Anthropic, `3` auto-discovery) and `COMPATIBILITY_PROBE_TTL_SEC`. See [`docs/COMPATIBILITY_LAYER.md`](docs/COMPATIBILITY_LAYER.md).
- **§11:** expanded to six gates — the previous three plus the SDK-compatibility gate (official openai SDK parses every wrapper's Responses output), the COMPATIBILITY_LAYER E2E, and the full matrix audit (240 checks).
- **§4 / §10 enforcement:** contract §4 (positive-int/capped `max_tokens`, role/tool-message validation on every surface) and §10 (`X-Request-ID` on every response) are now enforced on all five wrappers and locked by regression guards.

### v2.0 → v3.0

### Corrected — v2.0 statements that did not match the code

| v2.0 claim | Reality |
|---|---|
| Run command `uvicorn wrapper.src.main:app` | Every unit uses `src.main:app` with the wrapper dir on `PYTHONPATH`. |
| "4 wrapper implementations" | There are **5**; `openrouter` (9106) was missing entirely. |
| "Audit Score: 100/100 — Enterprise Grade" across six categories | Not substantiated. Later audits found 37 issues including an unauthenticated key-deletion API and provably broken parallel tool calls. Replaced with verifiable test gates (§11). |
| "Enterprise features: ✅ All 5 implemented" per wrapper | Graceful drain was absent from nvidia and openrouter until 2026-08-01; a background-task registry was absent from openrouter. |
| Port table omitted 9106 | Added. |
| References to root-level audit files | Moved to `docs/`; links updated. |

### Added — new normative sections

- **§3 Streaming contract** — the largest addition. Parsing, block lifecycle, parallel tool calls, error transparency, heartbeats, terminators, generator cleanup. Each clause traces to a defect found in live testing.
- **§4 Input handling** — non-object JSON bodies must not 500.
- **§5 Authentication** — fail-closed, per-request token read, byte-safe compare, exact public-path matching, management-API separation.
- **§6 Resource management** — pool accounting, side-effect-free predicates, three-axis store bounds, drain, task registry.
- **§7 Shared modules** — the parity mechanism, and the prohibition on shadowing shared helpers.
- **§8 Cross-wrapper parity policy** — elevated from advisory to enforced, with four CI guards.
- **§10 Observability** — metrics must persist and error counters must actually increment.
- **§11 Testing** — three concrete gates replacing the previous four ad-hoc shell snippets.

### Documented deviations

`nous` inlines its key pool, metrics and a dict-based `AnthropicStreamState` in `src/main.py`. Accepted; behaviour must still satisfy every clause and is covered by the same tests.

---

## 13. References

- [`README.md`](README.md) — repository entry point
- [`wrappers.json`](wrappers.json) — machine-readable deployment config
- [`docs/CROSS_WRAPPER_BUG_POLICY.md`](docs/CROSS_WRAPPER_BUG_POLICY.md) — parity policy
- [`docs/TEMPLATE_WRAPPER.md`](docs/TEMPLATE_WRAPPER.md) — new-wrapper skeleton
- [`docs/DOCUMENTATION_INDEX.md`](docs/DOCUMENTATION_INDEX.md) — full documentation map
- [`docs/audits/RUNTIME_AUDIT_2026-08-01.md`](docs/audits/RUNTIME_AUDIT_2026-08-01.md) — runtime verification
- [`docs/COMPATIBILITY_LAYER.md`](docs/COMPATIBILITY_LAYER.md) — operator-declared upstream dialect
- [`docs/audits/FULL_MATRIX_AUDIT_2026-08-01.md`](docs/audits/FULL_MATRIX_AUDIT_2026-08-01.md) — full matrix audit (240 checks)
- [`docs/audits/TRANSLATION_LAYER_AUDIT_2026-08-01.md`](docs/audits/TRANSLATION_LAYER_AUDIT_2026-08-01.md) — AI Gateway translation-layer audit
- [`docs/audits/CODEX_RESP02_SDK_COMPAT_AUDIT_2026-08-01.md`](docs/audits/CODEX_RESP02_SDK_COMPAT_AUDIT_2026-08-01.md) — SDK-compat (Codex) audit
- [`docs/reports/PRODUCTION_READINESS_2026-08-01.md`](docs/reports/PRODUCTION_READINESS_2026-08-01.md) — readiness scorecard
- [`productions/PRODUCTION_RUNBOOK.md`](productions/PRODUCTION_RUNBOOK.md) — operational runbook

---

**Version:** 3.2 · **Last updated:** 2026-08-04
**Verification at this version:** 308 unit/regression tests · 990 live runtime E2E checks (5 wrappers × 3 surfaces × 22 modes) · official-SDK compat (openai + anthropic) · 240 full-matrix checks · 55 real-SDK agent-loop checks · multi-agent concurrency storm (12 agents × 5 wrappers, zero cross-talk) · soak; **8/8 gates green, 0 failures**
