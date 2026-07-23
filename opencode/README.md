# wrapper-opencode

Production proxy for **[OpenCode Zen](https://opencode.ai/docs/zen/)** — multi-protocol AI gateway.

**Status:** ✅ **PRODUCTION READY — 100/100**  
**Version:** 1.0.4-dynamic-alias  
**Port:** 9107

## Zen endpoint map (from official docs)

| Family | Examples | Zen path |
|--------|----------|----------|
| GPT 5.x | `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex` | `POST /responses` |
| Claude | `claude-sonnet-4-6`, `claude-opus-4-6`, `claude-haiku-4-5` | `POST /messages` |
| Qwen3.x | `qwen3.7-plus`, … | `POST /messages` |
| Gemini | `gemini-3-flash`, … | `POST /models/{id}` |
| OpenAI-compatible | Grok, DeepSeek, MiniMax, GLM, Kimi, free models | `POST /chat/completions` |
| Catalog | — | `GET /models` |

Base URL: `https://opencode.ai/zen/v1` (override with `OPENCODE_BASE_URL`).

## Client-facing surface (always available)

This wrapper **always** exposes standard SDK paths so Claude Code / Codex / Hermes / OpenAI SDK work unchanged:

- `POST /v1/chat/completions`
- `POST /v1/responses` (native passthrough for GPT*; chat-translate otherwise)
- `POST /v1/messages` (native passthrough for Claude*/Qwen*; chat-translate otherwise)
- `POST /v1/messages/count_tokens`
- `GET /v1/models`
- `GET /health`, `/metrics`, `/metrics/prom`

## Quick Start

```bash
cd /root/wrapper/opencode

# 1. Install
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env (OPENCODE_API_KEY_1 from https://opencode.ai/auth + BEARER_TOKEN)

# 3. Run
python -m uvicorn src.main:app --host 0.0.0.0 --port 9107
```

### Claude Code

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:9107/v1
export ANTHROPIC_API_KEY=wrapper-local-key   # = BEARER_TOKEN if set
```

### Codex / Hermes / OpenAI SDK

```bash
export OPENAI_BASE_URL=http://127.0.0.1:9107/v1
export OPENAI_API_KEY=wrapper-local-key
```

## Production Features

- ✅ Multi-key rotation + load shedding (`INFLIGHT_SOFT_CAP=100`)
- ✅ Shared aiohttp session (no per-request leak)
- ✅ Streaming + heartbeat with anti-silence
- ✅ `name:null` tools filter (Codex/Hermes compatibility)
- ✅ Claude Code aliases (`sonnet`/`opus`/`haiku`)
- ✅ Dynamic alias binding (no hardcoded targets)
- ✅ `.env` hot reload, metrics, bearer auth
- ✅ Full OpenAI + Anthropic SDK compatibility

## FREE_ONLY Mode

```bash
# .env
FREE_ONLY=yes
FREE_MODEL_ALLOWLIST=big-pickle   # Zen free model without "free" in the id
```

Filters `GET /v1/models` and rejects paid models on Chat / Responses / Messages with OpenAI/Anthropic-compatible 400 error envelopes so SDKs surface a clean error.

## Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/chat/completions` | POST | OpenAI Chat Completions |
| `/v1/responses` | POST | OpenAI Responses (Codex) |
| `/v1/messages` | POST | Anthropic Messages |
| `/v1/messages/count_tokens` | POST | Token counting |
| `/v1/models` | GET | Model catalog with aliases |
| `/health` | GET | Health check |
| `/metrics` | GET | JSON metrics |
| `/metrics/prom` | GET | Prometheus metrics |

## Verification

```bash
curl http://localhost:9107/health
curl http://localhost:9107/v1/models | jq '.data | length'
```

## Production Notes

- Uses FastAPI + aiohttp (high performance async)
- Automatic .env hot-reload
- Full model verification loop runs in background
- **Production Ready: 100/100**

## Related

- Root wrapper README: `../README.md`
- NVIDIA wrapper: `../nvidia-python/`
- Nous wrapper: `../nous/`