# Performance & Reliability Audit Plan and Implementation Notes

Date: 2026-07-24

## Goal

The wrappers should behave like near-zero-overhead provider adapters: agents should experience the same semantics and nearly the same responsiveness as direct upstream access, while gaining multi-key resilience, protocol compatibility, stream finalization, and observability.

## Source-of-Truth Ports

Active sequential ports are now:

| Wrapper | Port |
|---|---:|
| nvidia-python | 9101 |
| nous | 9102 |
| opencode | 9103 |
| blackbox | 9104 |

The following files are expected to agree with that table:

- `wrappers.json`
- `README.md`
- `install.sh`
- `*/.env.example`
- `*/systemd/*.service`
- `*/wrapper-*.service`

## Performance Design Principles

1. **Reuse one aiohttp session per wrapper process**
   - No per-request `ClientSession` on hot paths.
   - Connection pools use env-configurable limits.
   - DNS caching is enabled.

2. **Do not buffer streams**
   - Successful upstream streams are returned as `StreamingResponse` immediately.
   - Key reservations are released in finalizers.

3. **Do not perform upstream checks in `/health`**
   - `/health` is process/pool health and must be fast.
   - `/ready` performs optional upstream readiness checks.

4. **Retry only credential/provider failures**
   - Retry: `401`, `402`, `403`, `408`, `409`, `429`, `5xx`.
   - Do not retry malformed client input.
   - Do not cool down a key for model-capacity/deployment errors that are not key-level failures.

5. **Keep stream lifecycle SDK-complete**
   - OpenAI Chat: terminal `data: [DONE]`.
   - Responses: `response.completed` before `data: [DONE]`.
   - Anthropic: `message_delta` + `message_stop`.

## Implemented Improvements

### Port and metadata consistency

- Updated active ports to `9101-9104` across docs/config/install metadata.
- Blackbox now runs on `9104` to match the sequential no-gap layout.

### Connection pooling

- `nous`, `opencode`, and `blackbox` now use env-configurable `aiohttp.TCPConnector` settings:
  - `MAX_CONNECTIONS`
  - `MAX_CONNECTIONS_PER_HOST`
  - `CONNECT_TIMEOUT_SEC`
  - `REQUEST_TIMEOUT_SEC`
  - `STREAM_REQUEST_TIMEOUT_SEC`
- NVIDIA already had connection pooling; the audit preserved it.

### Health/readiness split

- Added `/ready` to wrappers.
- `nous /health` is now fast and no longer performs upstream probing.
- `/ready` can perform upstream readiness checks using the same multi-key retry semantics.

### Concurrency regression tests

Added `tests/test_concurrency_contracts.py` covering:

- concurrent acquire/release no leak for OpenCode key pool
- concurrent acquire/release no leak for Blackbox key pool
- concurrent acquire/release no leak for NVIDIA key pool reservation
- concurrent acquire/release no leak for Nous key pool
- response store bounding

### Performance tooling

Added:

- `tests/perf/bench_proxy_latency.py`
- `tests/perf/load_agent_sim.py`

These scripts measure direct-vs-wrapper latency, streaming TTFT, mixed surface load, and agent-like traffic behavior.

## Recommended Benchmark Commands

Blackbox example:

```bash
python tests/perf/bench_proxy_latency.py \
  --wrapper-base http://127.0.0.1:9104/v1 \
  --direct-base https://api.blackbox.ai \
  --api-key "$BLACKBOX_API_KEY_1" \
  --model blackboxai/nvidia/nemotron-3-super-120b-a12b:free \
  --requests 20 \
  --concurrency 5
```

Streaming:

```bash
python tests/perf/bench_proxy_latency.py \
  --wrapper-base http://127.0.0.1:9104/v1 \
  --direct-base https://api.blackbox.ai \
  --api-key "$BLACKBOX_API_KEY_1" \
  --model blackboxai/nvidia/nemotron-3-super-120b-a12b:free \
  --requests 20 \
  --concurrency 5 \
  --stream
```

Mixed agent simulation:

```bash
python tests/perf/load_agent_sim.py \
  --base-url http://127.0.0.1:9104/v1 \
  --api-key "$BLACKBOX_API_KEY_1" \
  --model blackboxai/nvidia/nemotron-3-super-120b-a12b:free \
  --requests 100 \
  --concurrency 10 \
  --stream-ratio 0.5
```

## Acceptance Criteria

For local/mock or stable upstream conditions:

- no unhandled exceptions
- no key in-flight leaks
- no stream finalization leaks
- no response deltas after `response.completed`
- no client-visible retriable/key-level error while another key succeeds
- `/health` responds without upstream dependency
- `/ready` reflects upstream/pool readiness
- fatal/bug lint clean
- dependency audit clean

## Latest Validation

```text
compileall: pass
pytest: pass
transparency runner: pass
ruff F/E9/B: pass
bandit high severity: no findings
pip-audit: no known vulnerabilities
install.sh syntax: pass
```

## NVIDIA Claude Code Kimi Guardrail

A production incident was reviewed where Claude Code reported the selected NVIDIA
model `moonshotai/kimi-k2.6` as unavailable even though the model exists and the
API key is valid. The wrapper now avoids two false-negative patterns:

1. Background model-verification transient failures are no longer hard-blocking
   explicit client-selected models. Only retired/404/410 models are blocked by
   default. Strict behavior can be restored with `STRICT_BLOCK_UNAVAILABLE_MODELS=true`.
2. `moonshotai/kimi-k2.6` output tokens are capped to the NVIDIA Build example
   limit of `16384`, and Claude/Anthropic `thinking` does not inject
   `reasoning_effort` for this model by default.

Config overrides:

```text
MODEL_MAX_TOKENS_CAPS=moonshotai/kimi-k2.6:16384
DISABLE_REASONING_INJECTION_PATTERNS=moonshotai/kimi-k2.6,kimi-k2.6
STRICT_BLOCK_UNAVAILABLE_MODELS=false
```
