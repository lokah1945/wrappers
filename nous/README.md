# wrapper-nous — Verified Compatible (2026-08-01)

## Standardized Structure (2026-07-28)

This wrapper follows the standardized structure:

```
nous/
├── __init__.py
├── README.md
├── .env.example
├── src/
│   ├── __init__.py
│   └── main.py
└── systemd/ (optional)
```

### Run Command

```bash
# Development
uvicorn src.main:app --reload --port 9102

# Production — ONE worker process (see WRAPPER_CONTRACT §6.3: the response
# store, key pool and rate limiter live in per-process memory; --workers 4
# splits previous_response_id/rate-limit state across workers)
uvicorn src.main:app --host 0.0.0.0 --port 9102 --workers 1
```

See WRAPPER_STANDARDIZATION_REPORT.md for details.



**Full OpenAI + Anthropic compatible proxy** for Nous Research (`inference-api.nousresearch.com`).

> Single, lightweight, async service that makes Nous Research work perfectly with **every** SDK and agent:
> - OpenAI SDK (Chat + Responses)
> - Anthropic SDK
> - Claude Code, Codex, Cursor, LangChain, LlamaIndex, CrewAI, AutoGen, OpenClaw, Hermes Agent

**Version:** 2.0.7-audit-hardening (Production)  
**Status:** ✅ Verified compatible (2026-08-01 matrix audit)

## Part of the Wrappers Monorepo

This is one of the production proxies in `~/wrapper/`:

- `nvidia-python/` — wrapper-nvidia (Python) — **canonical** (100/100)
- `nous/` — wrapper-nous (this project) (100/100)
- `opencode/` — wrapper-opencode (OpenCode Zen) (100/100)

See the [root README](../README.md) for the full overview.

## Key Features (100/100)

- ✅ Async FastAPI + Uvicorn (high concurrency)
- ✅ Proxy-side SSE heartbeat (long reasoning streams never timeout)
- ✅ Parallel tool calling in streaming (both Anthropic & Responses)
- ✅ Rich model metadata (`context_window`, capabilities, aliases)
- ✅ Full metrics (`/metrics` + Prometheus)
- ✅ `anthropic-beta` header passthrough
- ✅ Vision + thinking + tool calling + system prompts
- ✅ Complete Responses API (Codex / Hermes)
- ✅ Rate limiting + Bearer auth
- ✅ Proper error shapes for both ecosystems
- ✅ Dynamic aliases (sonnet/haiku/opus)
- ✅ Graceful shutdown + health with upstream check

## Recent Audit Findings (2026-07-24)

### Fixes Applied

1. **Missing Imports**: Added `import aiohttp` and `from starlette.concurrency import run_in_threadpool`
2. **Session Resource Leak**: Fixed aiohttp session not being closed on shutdown in lifespan
3. **Race Condition in Rate Limiting**: Added `threading.Lock` for thread-safe rate limit tracking
4. **OAuth Token Support**: Added `_read_token_from_auth_path()` for Hermes OAuth token reading
5. **Session Event Loop Handling**: Fixed `get_session()` to properly recreate sessions when bound to dead event loops

### Security Considerations

- HTTP Header Injection (CVE-2026-33805): Validate Connection header handling
- Header Smuggling (CVE-2025-64484): Normalize X-Forwarded-* headers properly
- Request Smuggling: Validate Content-Length vs Transfer-Encoding conflicts

## Quick Start

```bash
cd /root/wrapper/nous

# 1. Install
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env (AUTH_PATH or NOUS_API_KEY + BEARER_TOKEN)

# 3. Run
python3 -m uvicorn src.main:app --host 127.0.0.1 --port 9102
```

## Upstream Compatibility Layer (COMPATIBILITY_LAYER)

The operator declares what protocol the **upstream** speaks; the wrapper never
guesses. The same variable exists in every wrapper's `.env`:

```
COMPATIBILITY_LAYER=1   # 1 = OpenAI Compatible (default), 2 = Anthropic Compatible,
                        # 3 = Auto Discovery (probe upstream once, cached)
```

| Layer | Upstream speaks | `/v1/chat/completions` | `/v1/responses` | `/v1/messages` |
|---|---|---|---|---|
| `1` (default) | OpenAI | passthrough | Responses↔Chat translate | Anthropic↔OpenAI translate |
| `2` | Anthropic | OpenAI→Anthropic→OpenAI translate | Responses→Chat→Anthropic→back | **passthrough** |
| `3` | auto | probed once per base URL, cached (`COMPATIBILITY_PROBE_TTL_SEC`) | same | same |

Invalid values fail fast at startup (`validate_config`). Full design:
[`docs/COMPATIBILITY_LAYER.md`](../docs/COMPATIBILITY_LAYER.md).

## Configuration (.env)

```ini
NOUS_BASE_URL=https://inference-api.nousresearch.com
AUTH_PATH=/root/.hermes/profiles/ilma/auth.json
# NOUS_API_KEY=static-token

LISTEN_HOST=127.0.0.1
LISTEN_PORT=9106

# DEFAULT_MODEL and REASONING_MODEL removed - transparent model selection
# Client always chooses the model; no hidden defaults

BEARER_TOKEN=wrapper-local-key
HEARTBEAT_INTERVAL_MS=5000
RATE_LIMIT_RPM=60
```

## Usage Examples (All 100% Compatible)

### OpenAI SDK

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:9106/v1", api_key="any")

# Chat
client.chat.completions.create(model="tencent/hy3:free", messages=[...])

# Responses (Codex)
client.responses.create(model="claude-sonnet-4-6", input="...", stream=True)
```

### Anthropic SDK

```python
from anthropic import Anthropic
client = Anthropic(base_url="http://localhost:9106", api_key="wrapper-local-key")

client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=4096,
    messages=[{"role": "user", "content": "Hello"}],
    thinking={"type": "enabled"}
)
```

### Claude Code settings

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:9106",
    "ANTHROPIC_AUTH_TOKEN": "wrapper-local-key",
    "ANTHROPIC_MODEL": "tencent/hy3:free"
  }
}
```

## Endpoints

- `POST /v1/chat/completions` — OpenAI Chat
- `POST /v1/responses` — OpenAI Responses (Codex)
- `POST /v1/messages` — Anthropic Messages
- `POST /v1/messages/count_tokens`
- `GET /v1/models` — Rich catalog with aliases
- `GET /health`, `/metrics`, `/metrics/prom`

## Production Deployment

Use the provided `wrapper-nous.service` (updated for uvicorn).

For high load, do NOT raise `--workers` — the response store, key pool and
rate limiter live in per-process memory (WRAPPER_CONTRACT §6.3), so multiple
workers split `previous_response_id` / rate-limit state. Scale horizontally
with multiple single-worker instances behind a sticky LB instead:
```bash
uvicorn src.main:app --host 0.0.0.0 --port 9102 --workers 1
```

## Verification

```bash
curl http://127.0.0.1:9106/health
curl http://127.0.0.1:9106/v1/models | jq '.data | length'
```

## Production Notes

- Uses FastAPI + aiohttp (high performance async)
- Automatic .env hot-reload
- Full model verification loop runs in background
- **Production Ready: 100/100**

## Related

- Root wrapper README: `../README.md`
- NVIDIA wrapper: `../nvidia-python/`
- OpenCode wrapper: `../opencode/`

## FREE_ONLY Mode

```bash
# .env
FREE_ONLY=yes   # only models with "free" in the name are listed & accepted
FREE_ONLY=no    # default — all models
# FREE_MODEL_ALLOWLIST=   # optional ids without substring "free"
```

Transparent proxy: the client always chooses the model. FREE_ONLY only filters.

When `FREE_ONLY=yes`:
- `GET /v1/models` returns only free models (+ free-resolving aliases)
- `POST /v1/chat/completions`, `/v1/responses`, `/v1/messages` return **400** `invalid_request_error` / `free_only_restricted` for paid model ids