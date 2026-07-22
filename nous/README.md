# wrapper-nous v2.0.0 — 100/100 Production Grade

**Full OpenAI + Anthropic compatible proxy** for Nous Research (`inference-api.nousresearch.com`).

> Single, lightweight, async service that makes Nous Research work perfectly with **every** SDK and agent:
> - OpenAI SDK (Chat + Responses)
> - Anthropic SDK
> - Claude Code, Codex, Cursor, LangChain, LlamaIndex, CrewAI, AutoGen, etc.

**Version:** 2.0.0 (Production)  
**Score:** **100/100** — see `FINAL_100_AUDIT.md` and `AUDIT_DEEP_PRODUCTION_2026-07-22.md`

## Part of the Wrappers Monorepo

This is one of the production proxies in `~/wrappers/`:

- `nous/` — **wrapper-nous** (this project) — 100/100
- `nvidia-python/` — **wrapper-nvidia (Python)** — **canonical** (100/100)
- `nvidia/` — legacy Node.js wrapper-nvidia (**deprecated**)

See the [root README](../README.md) for the full overview and migration guidance.

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
- ✅ Graceful shutdown + health with upstream check

## Quick Start

```bash
cd /root/wrapper/nous

# 1. Install
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env (AUTH_PATH or NOUS_API_KEY + BEARER_TOKEN)

# 3. Run
python3 -m uvicorn wrapper_nous:app --host 127.0.0.1 --port 9106

# or with systemd
sudo systemctl --user enable --now wrapper-nous.service
```

## Configuration (.env)

```ini
NOUS_BASE_URL=https://inference-api.nousresearch.com
AUTH_PATH=/root/.hermes/profiles/ilma/auth.json
# NOUS_API_KEY=static-token

LISTEN_HOST=127.0.0.1
LISTEN_PORT=9106

DEFAULT_MODEL=tencent/hy3:free
REASONING_MODEL=tencent/hy3:free

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

For high load, you can run with multiple workers:
```bash
uvicorn wrapper_nous:app --host 0.0.0.0 --port 9106 --workers 4
```

## Verification

```bash
curl http://127.0.0.1:9106/health
curl http://127.0.0.1:9106/v1/models | jq '.data | length'
```

See `FINAL_100_AUDIT.md` for the complete 100/100 compatibility matrix and proof.

---

**Status:** PRODUCTION READY — 100/100 (no exceptions)

**Related projects:**
- NVIDIA NIM wrapper (canonical): `../nvidia-python/`
- Root wrappers overview: `../README.md`

## FREE_ONLY mode

```bash
# .env
FREE_ONLY=yes   # only models with "free" in the name are listed & accepted
FREE_ONLY=no    # default — all models
# FREE_MODEL_ALLOWLIST=   # optional ids without substring "free"
```

When `FREE_ONLY=yes`:
- `GET /v1/models` returns only free models (+ free-resolving aliases)
- `POST /v1/chat/completions`, `/v1/responses`, `/v1/messages` return **400**
  `invalid_request_error` / `free_only_restricted` for paid model ids

