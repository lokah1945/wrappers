# wrapper-opencode

Production-grade API proxy for OpenCode (specialized wrapper, modeled after wrapper-nvidia).

## Features
- Full OpenAI Chat + Responses API compatibility
- Anthropic Messages compatibility (basic)
- Multi-key rotation + pacing + load shedding (`INFLIGHT_SOFT_CAP=100`)
- Streaming with anti-silence heartbeat
- .env hot reload support
- Rich metrics + Prometheus
- Bearer token auth

## Quick Start

```bash
cd opencode
pip install -r requirements.txt
cp .env.example .env   # add your OPENCODE_API_KEY_*
python -m uvicorn src.main:app --port 9107
```

## Environment Variables
See `.env.example`. Main keys:
- `OPENCODE_API_KEY_1`, `OPENCODE_API_KEY_2`, ...
- `OPENCODE_BASE_URL`
- `LISTEN_PORT=9107`
- Production timeouts and `INFLIGHT_SOFT_CAP`

## Endpoints
- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/messages`
- `GET /v1/models`
- `GET /health`
- `GET /metrics`
- `GET /metrics/prom`

## Notes
- Designed to be used as custom model provider for Claude Code, Codex, OpenClaw, etc.
- OpenCode upstream is expected to be OpenAI-compatible.
- Part of the wrappers monorepo.

Version: 1.0.0-opencode-py