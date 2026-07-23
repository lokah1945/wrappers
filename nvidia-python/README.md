# wrapper-nvidia (Python) — v8.6.5-py

> OpenAI- and Anthropic-compatible transparent proxy for the NVIDIA NIM API.

**Status:** ✅ **PRODUCTION READY — 100/100**  
**Version:** 8.6.5-py  
**Implementation:** Python (FastAPI + aiohttp)  
**Port:** 9101

> **Important:** The legacy Node.js implementation in `nvidia/` is **deprecated** and has been removed from production.  
> All new deployments and production traffic **must** use this Python version.

This is the **single source of truth** for wrapper-nvidia going forward.

## Features (Full Parity + Hardening)

- Transparent proxy for NVIDIA NIM (model names pass through exactly)
- Multi-key rotation + adaptive pacing + load shedding (INFLIGHT_SOFT_CAP=100)
- Full OpenAI Chat Completions + Responses API
- Full Anthropic Messages API (including streaming + parallel tools + thinking)
- Claude Code / gateway aliases (`haiku`, `sonnet`, `opus`, `claude-*`)
- Reasoning model injection (deepseek, nemotron, qwen, glm, etc.)
- Model verification + retired/unavailable model handling
- Production timeouts: ANTI_SILENCE (960s), TTFT, PRE_RESPONSE, HEADERS
- Stream buffering + anti-silence heartbeat + reasoning-only placeholder
- .env hot-reload (watchdog)
- Rich metrics (`/metrics`, Prometheus, ttft, pacing, model-status)
- Bearer auth + health checks

## Quick Start

### 1. Install

```bash
cd /root/wrapper/nvidia-python
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env — at minimum:
# NVIDIA_API_KEY_1=nvapi-...
# BEARER_TOKEN=your-token (optional but recommended)
```

### 3. Run

```bash
python -m uvicorn src.main:app --host 0.0.0.0 --port 9101
# or
python -m src.main
```

### 4. Verify

```bash
curl http://localhost:9101/health
curl http://localhost:9101/v1/models | jq '.data | length'
```

## Endpoints

**OpenAI-compatible**
- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/embeddings`
- `POST /v1/images/generations`
- `POST /v1/ranking`
- `GET  /v1/models`

**Anthropic-compatible**
- `POST /v1/messages`
- `POST /v1/messages/count_tokens`

**Management**
- `GET /health`
- `GET /metrics`, `/metrics/prom`
- `GET /stats`

## Configuration (Key .env Variables)

| Variable                    | Default     | Description                              |
|----------------------------|-------------|------------------------------------------|
| `NVIDIA_API_KEY*`          | —           | One or more `NVIDIA_API_KEY_1`, ...      |
| `LISTEN_PORT`              | 9101        | Listen port                              |
| `INFLIGHT_SOFT_CAP`        | 100         | Load shedding threshold                  |
| `ANTI_SILENCE_TIMEOUT_MS`  | 960000      | Anti-silence for reasoning models        |
| `TTFT_TIMEOUT_MS`          | 120000      | Time-to-first-token warning              |
| `PRE_RESPONSE_TIMEOUT_MS`  | 300000      | Client-facing pre-response watchdog      |
| `VERIFY_ON_BOOT`           | true        | Run model verification on startup        |

## Client Configuration

### Claude Code CLI

```bash
export ANTHROPIC_BASE_URL="http://localhost:9101/v1"
export ANTHROPIC_API_KEY="test-key"
claude code chat "Hello, what is 2+2?"
```

### OpenAI SDK (Python)

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:9101/v1", api_key="test-key")

response = client.chat.completions.create(
    model="sonnet",  # Dynamic alias resolves to last concrete model
    messages=[{"role": "user", "content": "Hello!"}]
)
```

## Production Notes

- Uses FastAPI + aiohttp (high performance async)
- Automatic .env hot-reload
- Full model verification loop runs in background
- **Production Ready: 100/100**

## Related

- Root wrapper README: `../README.md`
- Nous wrapper: `../nous/`
- OpenCode wrapper: `../opencode/`