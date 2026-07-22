# wrapper-opencode

Production proxy for **[OpenCode Zen](https://opencode.ai/docs/zen/)** — multi-protocol AI gateway.

**Score: 100/100** · Version `1.0.1-opencode-zen-py`

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
- `POST /v1/responses`  (native passthrough for GPT*; chat-translate otherwise)
- `POST /v1/messages`   (native passthrough for Claude*/Qwen*; chat-translate otherwise)
- `POST /v1/messages/count_tokens`
- `GET  /v1/models`
- `GET  /health`, `/metrics`, `/metrics/prom`

## Quick start

```bash
cd opencode
pip install -r requirements.txt
cp .env.example .env   # OPENCODE_API_KEY_1 from https://opencode.ai/auth
python -m uvicorn src.main:app --host 0.0.0.0 --port 9107
```

### Claude Code
```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:9107
export ANTHROPIC_API_KEY=wrapper-local-key   # = BEARER_TOKEN if set
```

### Codex / Hermes / OpenAI SDK
```bash
export OPENAI_BASE_URL=http://127.0.0.1:9107/v1
export OPENAI_API_KEY=wrapper-local-key
```

## Production features
- Multi-key rotation + load shedding (`INFLIGHT_SOFT_CAP=100`)
- Shared aiohttp session (no per-request leak)
- Streaming + heartbeat
- `name:null` tools filter (Codex/Hermes)
- Claude Code aliases (`sonnet`/`opus`/`haiku`)
- `.env` hot reload, metrics, bearer auth
