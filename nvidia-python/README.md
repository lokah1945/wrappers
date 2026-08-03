# wrapper-nvidia (Python) — v8.6.5-py

## Standardized Structure (2026-07-28)

This wrapper follows the standardized structure:

```
nvidia-python/
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
uvicorn src.main:app --reload --port 9101

# Production — ONE worker process (see WRAPPER_CONTRACT §6.3: the response
# store, key pool and rate limiter live in per-process memory; --workers 4
# splits previous_response_id/rate-limit state across workers)
uvicorn src.main:app --host 0.0.0.0 --port 9101 --workers 1
```

See WRAPPER_STANDARDIZATION_REPORT.md for details.



> OpenAI- and Anthropic-compatible transparent proxy for the NVIDIA NIM API.

**Status:** ✅ **Verified compatible** (2026-08-01 matrix audit: 240/240 checks)  
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

## Recent Audit Findings (2026-07-24)

### Fixes Applied

1. **Auth Error Format Consistency**: Fixed inconsistent error format in auth middleware - all paths now return `{'error': {'message': ..., 'type': ...}}` format for OpenAI SDK compatibility

2. **Redundant Public Path Check**: Removed duplicate `/metrics/prom` check (was listed twice in public_paths and is_public conditions)

### Security Considerations

- HTTP Header Injection (CVE-2026-33805): Validate Connection header handling
- Header Smuggling (CVE-2025-64484): Normalize X-Forwarded-* headers properly
- Request Smuggling: Validate Content-Length vs Transfer-Encoding conflicts

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
python -m uvicorn src.main:app --host 127.0.0.1 --port 9101
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