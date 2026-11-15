# wrapper-coarena

OpenAI + Anthropic compatible transparent proxy for the **Coarena Model API**
(<https://coarena.ai> — Coarena by Coasty Systems). Follows the monorepo
standardized wrapper layout and `WRAPPER_CONTRACT.md` v3.2.

- **Port:** 9107
- **Entry point:** `src.main:app` (WorkingDirectory/PYTHONPATH = this directory)
- **Env prefix:** `COARENA_`
- **Upstream:** `https://coarena.ai/api/v1` (one key serves both dialects)

## Run

```bash
# production (systemd) — see systemd/wrapper-coarena.service
ExecStart=/usr/bin/python3 -m uvicorn src.main:app --host 127.0.0.1 --port 9107

# local development
cd coarena && uvicorn src.main:app --reload --port 9107
```

Configuration comes from `.env` (see `.env.example`). `BEARER_TOKEN` is
required for client auth and the wrapper **fails closed** (503) when it is
unset; both `Authorization: Bearer` and `x-api-key` are accepted.

## Surfaces

| Surface | Endpoint | Behaviour |
|---|---|---|
| OpenAI Chat Completions | `POST /v1/chat/completions` | Native passthrough; translated when `COMPATIBILITY_LAYER=2` |
| OpenAI Responses | `POST /v1/responses` | Native passthrough; owner-approved Chat adapter only for a definitively unsupported route (`RESPONSES_UPSTREAM_MODE`) |
| Anthropic Messages | `POST /v1/messages` | Native passthrough (upstream speaks Messages natively); cross-dialect translation with `NATIVE_MESSAGES_MODE=translate` |
| Anthropic token counting | `POST /v1/messages/count_tokens` | Native passthrough |
| Legacy completions | `POST /v1/completions` | Native passthrough (Coarena extra) |
| Embeddings | `POST /v1/embeddings` | Shaped `501 not_implemented_error` — upstream has no embeddings API; use `nvidia-python` (9101) or `openrouter` (9103) |
| Model discovery | `GET /v1/models`, `GET /api/tags`, `GET /v1/models/{id}` | Native envelope, TTL-cached; no key needed upstream |
| Capabilities | `GET /v1/capabilities` | Capability discovery (contract §2.1) |
| Health / readiness | `GET /health`, `GET /ready` | Key availability, in-flight, version, git provenance |
| Metrics | `GET /metrics`, `GET /metrics/prom`, `GET /metrics/model-status` | JSON + Prometheus + per-model registry states; JSON snapshot persisted every `METRICS_PERSIST_SEC` |
| Dashboard | `GET /dashboard` | `410` — unified dashboard only (port 9106) |
| Version | `GET /version` | App/upstream/git provenance |
| Coarena extras | `GET /v1/limits`, `GET /v1/usage`, `POST /v1/feedback` | Authenticated upstream-native passthrough |

## Behavioural guarantees (contract)

- **Key pool** (`src/key_pool.py`): least-effective-load rotation with
  round-robin tie-break, per-key RPM pacing (defaults soft 25 / hard 30 —
  upstream documents 30 req/min per key), model-scoped cooldown blocks,
  side-effect-free predicates (B-37), exactly-once in-flight release on
  success / stream end / exception / upstream EOF / client disconnect (I4),
  exhaustion returns **429** with `Retry-After`.
- **All-key retry** on retriable statuses (401/402/403/408/409/429/5xx) before
  any client error; cooldown decisions come from the shared
  `common.translations.should_cooldown_key` — model-capacity errors never cool
  the credential.
- **Streaming** (`common.sse` sentinel-task heartbeat): `: heartbeat` comments
  only at complete frame boundaries, upstream native framing and terminators
  forwarded verbatim (no synthesized `[DONE]` on native paths), unparsable
  frames logged and dropped (B-10), mid-stream error frames surface as
  Anthropic `error` / Responses `response.failed` events (B-07/R-03),
  generators closed deterministically (B-09/R-07).
- **Input handling**: `common.body_guard.JSONBodyGuard` + 10 MB
  `RequestSizeLimiter`; `max_tokens`/`max_output_tokens` must be positive
  integers capped at 1,000,000 on adapter paths; unknown roles and orphan tool
  messages rejected with shaped 4xx on cross-dialect adapters.
- **Auth**: fail-closed (§5), token re-read per request (B-29), byte-safe
  compare (B-30), exact-match public paths (B-27), every POST authenticated
  (B-31).
- **Resources**: one shared `aiohttp` session (§6.2), graceful drain
  (`SHUTDOWN_DRAIN_SEC`, B-34), background-task registry (B-35), tenant-
  namespaced bounded response store (entry count / bytes / TTL, B-33).
- **Observability**: metrics persist across restarts and the error counter
  increments on every failure path (B-39); every response carries
  `X-Request-ID` + `X-Process-Time`; model outcomes recorded per credential
  into the shared model registry off the hot path (§10).
- **Transparent model choice**: the requested model is never substituted;
  aliases are operator-bound only (`DYNAMIC_ALIAS_TARGET`).

## Client configuration

```bash
# OpenAI SDK / Codex / LangChain (through this wrapper)
export OPENAI_BASE_URL=http://127.0.0.1:9107/v1
export OPENAI_API_KEY=$BEARER_TOKEN          # the wrapper token, not the upstream key

# Anthropic SDK / Claude Code (through this wrapper)
export ANTHROPIC_BASE_URL=http://127.0.0.1:9107
export ANTHROPIC_AUTH_TOKEN=$BEARER_TOKEN
```

The upstream Coarena credential never leaves the wrapper; clients authenticate
with the local `BEARER_TOKEN` only.

## Upstream notes

- Free tier: $5 of model use per day, funded by Coarena; 30 requests/minute,
  2 concurrent per key (documented limits the pool respects by default).
- `GET /api/v1/models` needs no upstream key; the wrapper caches it for
  `MODEL_CATALOG_TTL_SEC` (default 300 s) and feeds the shared model registry.
- Model ids (2026-11 snapshot): `claude-fable-5`, `claude-fable-5-1`,
  `claude-fable-5-2`, `claude-haiku-5-5`, `claude-opus-5-5`,
  `claude-sonnet-5-5`, `deepseek-v4-1-flash-1017`, `gemini-3-8-flash`,
  `gemini-4-argon`, `gemini-4-carbon`, `glm-5-3-flash`, `gpt-6-1-sol`,
  `gpt-6-1-terra`, `gpt-6-astra`, `gpt-6-luna`, `grok-4-7`, `hy4`, `kimi-k3`,
  `kimi-k3-1-pro`, `mimo-v2-6-pro`, `minimax-m3-1-flash`, `qwen3-8-2-4t-a95b`.
- Docs: <https://coarena.ai/docs> · machine-readable spec:
  <https://coarena.ai/api/openapi.json>

## Security note

A real API key was historically committed inside `.env.example`. The template
now ships placeholders only; **rotate the exposed key on the Coarena account
page** and keep real credentials exclusively in the local (chmod 600) `.env`.
