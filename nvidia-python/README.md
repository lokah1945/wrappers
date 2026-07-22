# wrapper-nvidia (Python) — v8.6.5-py

> OpenAI- and Anthropic-compatible transparent proxy for the NVIDIA NIM API.

**Status:** ✅ **PRODUCTION READY — 100/100**  
**Version:** 8.6.5-py  
**Implementation:** Python (FastAPI + aiohttp)  
**Canonical source:** This directory (`nvidia-python`)

> **Important:** The legacy Node.js implementation in `nvidia/` is **deprecated** and will be removed.  
> All new deployments and production traffic **must** use `nvidia-python/`.

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
- Legacy catch-all + Ollama compatibility (`/api/tags`, `/api/show`, etc.)
- Bearer auth + health + dashboard

See `AUDIT_REPORT_2026-07-23.md` for the complete 100/100 end-to-end audit against the Node.js reference.

## Quick Start

### 1. Install

```bash
cd /home/user/wrappers/nvidia-python
pip install -r requirements.txt
# or for development
pip install -e ".[dev]"
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
- `GET /dashboard`
- `GET /api/tags` (Ollama compat)

Full list and legacy routes in `src/main.py`.

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

All values match the audited Node.js production configuration.

## Migration from Legacy Node.js (`nvidia/`)

1. Stop the old Node service.
2. Point all clients to the new port / URL (default 9101).
3. Use the same `.env` keys (format is compatible).
4. The Python version is **bit-for-bit behaviorally equivalent** (or better) for all documented production paths.

See `MIGRATION.md` for detailed steps.

## Testing

```bash
cd /home/user/wrappers/nvidia-python
python -m pytest tests/ -q
```

118+ tests covering core paths, translators, verification, streaming, etc.

## Production Notes

- Uses FastAPI + aiohttp (high performance async).
- Automatic .env hot-reload.
- Full model verification loop runs in background.
- Matches or exceeds the 2026-07-22 Node.js audit in every observable behavior.

**Current production score:** 100/100 (see `AUDIT_REPORT_2026-07-23.md`).

## Related

- Root wrapper README: `../README.md`
- Legacy Node reference (read-only): `../nvidia/`
- Nous wrapper (sibling): `../nous/`

---

**wrapper-nvidia (Python) is now the canonical implementation.**  
All traffic should migrate to `nvidia-python/`.