# wrapper-openrouter — v1.0.0

> OpenAI- and Anthropic-compatible transparent proxy for the OpenRouter API.

**Status:** ✅ **Verified compatible** (2026-08-01 matrix audit: 240/240 checks)
**Version:** 1.0.0-audit-hardening
**Implementation:** Python (FastAPI + aiohttp)
**Port:** 9106

---

## Features

- **Transparent proxy** for OpenRouter (model names pass through exactly)
- **Multi-key rotation** — load-balance across multiple API keys
- **Adaptive pacing + load shedding** (INFLIGHT_SOFT_CAP=100)
- **Full streaming** with anti-silence heartbeat
- **OpenAI Chat Completions** — `POST /v1/chat/completions`
- **OpenAI Responses API** — `POST /v1/responses`
- **Anthropic Messages API** — `POST /v1/messages` (translated to OpenAI)
- **Model listing** — `GET /v1/models` from upstream
- **FREE_ONLY mode** — restrict to free models only (`:free` suffix)
- **MCP Catalog Integration** — query NVIDIA NIM + multi-provider catalog
- **Rich metrics** — `/metrics` (Prometheus), `/stats`
- **Bearer auth** + rate limiting
- **Health checks** — `/health`, `/ready`

---

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

## Quick Start

### 1. Install

```bash
cd /root/wrapper/openrouter
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env — at minimum:
#   OPENROUTER_API_KEY_1=sk-or-v1-...
#   BEARER_TOKEN=your-token
```

### 3. Run

```bash
uvicorn src.main:app --host 0.0.0.0 --port 9106
# or
python -m uvicorn src.main:app --host 127.0.0.1 --port 9106
```

### 4. Verify

```bash
curl http://localhost:9106/health
curl http://localhost:9106/v1/models | jq '.data | length'
curl http://localhost:9106/catalog/health
```

---

## Endpoints

### OpenAI-compatible
| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/chat/completions` | Chat completions (streaming + non-streaming) |
| POST | `/v1/responses` | OpenAI Responses API |
| POST | `/v1/embeddings` | Embeddings |
| POST | `/v1/images/generations` | Image generation |
| GET | `/v1/models` | List all models |
| GET | `/v1/models/:model` | Single model detail |

### Anthropic-compatible
| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/messages` | Anthropic Messages API |
| POST | `/v1/messages/count_tokens` | Token counting |

### Catalog (NVIDIA NIM + Multi-Provider)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/catalog/health` | Catalog liveness |
| GET | `/catalog/stats` | Catalog statistics |
| GET | `/catalog/providers` | Supported providers |
| GET | `/catalog/models` | NIM model search |
| GET | `/catalog/model?id=publisher/slug` | Single NIM model detail |
| GET | `/catalog/provider-models` | Multi-provider model search |

### MCP (Model Context Protocol)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/mcp/sse` | SSE transport |
| POST | `/mcp/messages` | JSON-RPC messages |

### Management
| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/ready` | Readiness probe |
| GET | `/metrics` | Prometheus metrics |
| GET | `/stats` | Request + key pool stats |
| GET | `/dashboard` | HTML dashboard |
| GET | `/version` | Version info |

---

## Client Configuration

### OpenAI SDK (Python)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:9106/v1",
    api_key="your-bearer-token",
)

response = client.chat.completions.create(
    model="openai/gpt-4o",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(response.choices[0].message.content)
```

### Anthropic SDK (Python)

```python
from anthropic import Anthropic

client = Anthropic(
    base_url="http://localhost:9106",
    api_key="your-bearer-token",
)

message = client.messages.create(
    model="anthropic/claude-sonnet-4",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello!"}],
)
print(message.content)
```

### Claude Code CLI

```bash
export ANTHROPIC_BASE_URL="http://localhost:9106"
export ANTHROPIC_API_KEY="your-bearer-token"
claude code chat "Hello, what is 2+2?"
```

### MCP Client (Claude Desktop)

```json
{
  "mcpServers": {
    "openrouter-catalog": {
      "url": "http://localhost:9106/mcp/sse"
    }
  }
}
```

---

## Configuration

### Key Environment Variables

| Variable | Default | Description |
|---------------------------|---------|------------------------------------------|
| `OPENROUTER_API_KEY_1` | — | Primary API key |
| `OPENROUTER_API_KEY_2+` | — | Additional keys for rotation |
| `BEARER_TOKEN` | — | Client auth token |
| `LISTEN_PORT` | 9106 | Listen port |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Upstream base URL |
| `FREE_ONLY` | no | Restrict to free models only |
| `CATALOG_DB` | — | Path to shared catalog SQLite DB |
| `HARD_LIMIT_RPM` | 60 | Per-key rate limit |
| `RATE_LIMIT_RPM` | 120 | Per-IP rate limit |
| `DISABLE_AUTH` | — | Disable bearer auth (pre-auth mode) |
| `INFLIGHT_SOFT_CAP` | 100 | Load shedding threshold |

---

## Related

- [NVIDIA NIM Model Fetcher (Catalog)](https://github.com/lokah1945/nvidia-nim_model_fetcher)
- [Wrappers Monorepo](https://github.com/lokah1945/wrappers)
- [OpenRouter API Docs](https://openrouter.ai/docs)
