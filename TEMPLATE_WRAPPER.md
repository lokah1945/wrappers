# Adding a New Wrapper — Template Guide

This repo is designed so that adding a new LLM provider wrapper takes
**~100 lines of code**, not ~2000. The shared infrastructure lives in
`common/base_wrapper.py` and `common/translations/`.

## Quick Start (5 steps)

### 1. Create the wrapper directory

```
myprovider/
├── __init__.py
├── src/
│   ├── __init__.py
│   └── main.py          # ~100 lines — your wrapper
├── .env.example
├── requirements.txt
├── README.md
├── pyproject.toml       # optional
├── dashboard.html       # copy from common/dashboard_template.html
└── systemd/
    └── wrapper-myprovider.service
```

### 2. Write `src/main.py` (~100 lines)

```python
#!/usr/bin/env python3
"""wrapper-myprovider: MyProvider LLM proxy.

Inherits all shared infrastructure from BaseWrapper:
  - Multi-key rotation + per-key RPM + 429 cooldown (anti rate-limit)
  - Transparent header forwarding (only swap Authorization)
  - Retry-After header parsing
  - Multi-key retry loop on 429/5xx (anti 4xx/5xx)
  - Auth: Authorization: Bearer AND x-api-key
  - Dashboard + metrics PUBLIC (no token required)
  - /health, /ready, /version, /metrics, /metrics/prom, /dashboard, /api/tags
  - .env hot-reload
"""

import os
from common.base_wrapper import BaseWrapper, WrapperConfig


class MyProviderWrapper(BaseWrapper):
    CONFIG = WrapperConfig(
        provider_name="myprovider",
        provider_env_prefix="MYPROVIDER",
        upstream_base_url_env="MYPROVIDER_BASE_URL",
        upstream_default_url="https://api.myprovider.com/v1",
        listen_port=9107,
        key_env_pattern=r'^MYPROVIDER_API_KEY(_\d+)?$',
    )

    def __init__(self):
        super().__init__()
        # Register provider-specific routes
        self.app.post("/v1/chat/completions")(self.chat_completions)
        self.app.post("/v1/messages")(self.messages)
        self.app.post("/v1/responses")(self.responses)
        # /v1/models, /api/tags, /health, /ready, /dashboard, /metrics
        # are already registered by BaseWrapper.

    async def chat_completions(self, request):
        """OpenAI Chat Completions — transparent proxy to upstream."""
        body = await self.parse_json_body(request)
        if not body:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                status_code=400,
            )
        return await self.proxy_request_with_pool(
            method="POST",
            path="chat/completions",
            body=body,
            request=request,
            stream=bool(body.get("stream")),
        )

    async def messages(self, request):
        """Anthropic Messages API → translate to Chat Completions, proxy, translate back."""
        from common.translations import AnthropicStreamState
        from fastapi.responses import JSONResponse, StreamingResponse
        body = await self.parse_json_body(request)
        if not body:
            return JSONResponse(
                {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                status_code=400,
            )
        # Translate Anthropic → OpenAI (use shared translator or write your own)
        # ... anthropic_to_openai(body) ...
        # Then proxy_request_with_pool and translate response back.
        # See opencode/blackbox for full example.

    async def responses(self, request):
        """OpenAI Responses API → translate to Chat Completions."""
        # See openrouter/src/main.py for full responses_to_chat + chat_to_responses example.
        pass


wrapper = MyProviderWrapper()
app = wrapper.app


if __name__ == "__main__":
    wrapper.run()
```

### 3. Create `.env.example`

```bash
# MyProvider API keys (one per line, supports _1, _2, ... for rotation)
MYPROVIDER_API_KEY_1=sk-xxx
MYPROVIDER_API_KEY_2=sk-yyy

# Client authentication (set DISABLE_AUTH=1 to disable)
BEARER_TOKEN=your-secure-token-here

# Upstream URL
MYPROVIDER_BASE_URL=https://api.myprovider.com/v1

# Per-key RPM limits (anti rate-limit)
MYPROVIDER_SOFT_LIMIT_RPM=30
MYPROVIDER_HARD_LIMIT_RPM=40

# Per-IP rate limit (0 disables — use per-key limits only for multi-agent)
RATE_LIMIT_RPM=600
```

### 4. Create `systemd/wrapper-myprovider.service`

```ini
[Unit]
Description=wrapper-myprovider: MyProvider LLM proxy (port 9107)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/wrapper/myprovider
Environment=PYTHONPATH=/root/wrapper/myprovider
EnvironmentFile=-/root/wrapper/myprovider/.env
Environment=LOG_FILE=/root/wrapper/myprovider/myprovider.log
ExecStart=/usr/bin/python3 -m uvicorn src.main:app --host 127.0.0.1 --port 9107
Restart=always
RestartSec=3
StandardOutput=append:/root/wrapper/myprovider/myprovider.log
StandardError=append:/root/wrapper/myprovider/myprovider.log

[Install]
WantedBy=default.target
```

### 5. Register in `install.sh` and `wrappers.json`

**install.sh** — add to WRAPPERS array:
```bash
"myprovider|myprovider|wrapper-myprovider.service|http://127.0.0.1:9107/health"
```

**wrappers.json** — add entry:
```json
"myprovider": {
  "port": 9107,
  "module": "src.main",
  "entry_point": "src.main:app",
  "upstream": "MyProvider",
  "status": "production",
  "structure": "standardized",
  "run_command": "uvicorn src.main:app --host 127.0.0.1 --port 9107",
  "env_prefix": "MYPROVIDER",
  "supports": ["openai_chat", "anthropic_messages"]
}
```

Done! Your wrapper now has:
- ✅ Multi-key rotation + 429 cooldown (anti rate-limit)
- ✅ Transparent header forwarding (only Authorization swapped)
- ✅ Multi-key retry loop (anti 4xx/5xx — client never sees 5xx for recoverable errors)
- ✅ Auth: Authorization: Bearer AND x-api-key (universal client compat)
- ✅ Dashboard + metrics PUBLIC (no token required)
- ✅ /health, /ready, /version, /metrics, /metrics/prom, /dashboard, /api/tags
- ✅ Retry-After header parsing (correct cooldown duration)
- ✅ .env hot-reload (new keys take effect without restart)
- ✅ Streaming heartbeat (anti-silence for long generations)

## What BaseWrapper Handles (so you don't have to)

| Feature | Implementation |
|---------|---------------|
| KeyPool | `common/base_wrapper.py:KeyPool` — per-key RPM, model-scoped blocks |
| Multi-key retry loop | `proxy_request_with_pool()` — retries 429/5xx/network across all keys |
| Retry-After parsing | `common/translations/shared.py:parse_retry_after` — int + RFC date |
| Transparent headers | `common/translations/shared.py:build_forward_headers` — broad allowlist |
| Auth (Bearer + x-api-key) | `_middleware()` — dual auth, OPTIONS exempt |
| Per-IP rate limit | `RateLimiter` — 0 disables, 600 default |
| Dashboard (public) | `/dashboard` route — serves dashboard.html, no auth |
| Metrics (public) | `/metrics`, `/metrics/prom` — no auth |
| /api/tags (Ollama) | BaseWrapper stub — override to return model list |
| /health, /ready | BaseWrapper — returns key pool status |
| .env hot-reload | Wire in subclass (see nvidia-python pattern) |
| Streaming heartbeat | `proxy_request_with_pool()` stream path — synth [DONE] on EOF |

## When to Override

- **`/v1/models`**: override to fetch from upstream catalog.
- **`/api/tags`**: override to return Ollama-format model list.
- **`/v1/chat/completions`**: usually no override needed (transparent proxy).
- **`/v1/messages`**: override if upstream is OpenAI-only (translate Anthropic→OpenAI).
- **`/v1/responses`**: override if upstream is OpenAI-only (translate Responses→Chat).

## Reference Implementations

- **nvidia-python** — most complete; has embeddings, rerank, images, /api/tags.
- **opencode/blackbox** — clean OpenAI-compat wrappers with Anthropic translation.
- **openrouter** — has Responses API translation + MCP catalog integration.
- **nous** — has OAuth token + static key pool, Responses + Anthropic translation.

All 5 wrappers now share `common/translations/` for:
- `parse_retry_after()` — HTTP Retry-After header parsing
- `build_forward_headers()` — transparent client header forwarding
- `is_retriable_status()` — 429/5xx/408/409 classification
- `should_cooldown_key()` — per-key cooldown heuristic
- `normalize_upstream_error()` — standardize error envelopes
- `AnthropicStreamState` — Anthropic SSE event lifecycle
- `parse_dsml_from_text()` — MiniMax DSML tool markup parser
- `repair_orphan_tool_messages()` — fix tool/tool_result without preceding tool_call
