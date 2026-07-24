# wrapper-blackbox

OpenAI- and Anthropic-compatible FastAPI proxy for the BLACKBOX AI public API.

- Upstream base: `https://api.blackbox.ai`
- OpenAI-compatible upstream path: `/chat/completions`
- Local default port: `9104`
- Default policy: `FREE_ONLY=yes`

## Features

- OpenAI Chat Completions: `POST /v1/chat/completions`
- OpenAI Responses API: `POST /v1/responses` translated to chat completions
- Anthropic Messages API: `POST /v1/messages` translated to chat completions
- Anthropic token count: `POST /v1/messages/count_tokens`
- Model discovery: `GET /v1/models`
- Capabilities: `GET /v1/capabilities`
- Multi-key rotation with per-key cooldowns
- Dynamic aliases: `sonnet`, `haiku`, `opus`, `claude-*`
- Strict stream finalization for OpenAI/Responses/Anthropic clients
- `previous_response_id` continuity for tool loops

## Quick Start

```bash
cd /root/wrapper/blackbox
cp .env.example .env
# BLACKBOX_API_KEY_1=sk-...
python -m uvicorn src.main:app --host 0.0.0.0 --port 9104
```

## FREE_ONLY

`FREE_ONLY=yes` is the default. Requests for non-free model ids are rejected unless:

- the id contains `free`, or
- the id is listed in `FREE_MODEL_ALLOWLIST`, or
- `FREE_ONLY=no` is set explicitly.

The wrapper does not silently substitute models. If a client uses an alias (`sonnet`, `haiku`, `opus`, `claude-*`), seed `DYNAMIC_ALIAS_TARGET` to a free concrete model such as:

```bash
DYNAMIC_ALIAS_TARGET=blackboxai/x-ai/grok-code-fast-1:free
```

## Client Examples

### OpenAI SDK

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:9104/v1", api_key="local")
print(client.chat.completions.create(
    model="blackboxai/x-ai/grok-code-fast-1:free",
    messages=[{"role":"user","content":"hello"}],
).choices[0].message.content)
```

### Claude Code / Anthropic SDK

```bash
export ANTHROPIC_BASE_URL=http://localhost:9104/v1
export ANTHROPIC_API_KEY=local
```

## Contract

This wrapper follows `../WRAPPER_CONTRACT.md` exactly: same runtime surfaces, multi-key retry semantics, stream closure semantics, tool/result semantics, and SDK-shaped errors as the other wrappers in this monorepo.
