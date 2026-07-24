# Wrappers

Production-grade API proxies for Claude Code, OpenAI SDK, Anthropic SDK, and OpenClaw.

This monorepo contains hardened, SDK-compatible transparent proxies that add multi-key rotation, pacing, metrics, streaming reliability, and full OpenAI + Anthropic compatibility.

## Current Status (2026-07-24)

| Wrapper            | Status          | Score   | Port   | Use Case |
|--------------------|-----------------|---------|--------|----------|
| **nvidia-python**  | ✅ Production   | **100/100** | 9101   | NVIDIA NIM API proxy |
| **nous**           | ✅ Production   | **100/100** | 9102   | Nous Research inference API |
| **opencode**       | ✅ Production   | **100/100** | 9103   | OpenCode Zen gateway |
| **blackbox**       | ✅ Production   | **100/100** | 9104   | BLACKBOX AI API proxy |

## Recent Audit Findings (2026-07-24)

### Security & Bug Fixes

**Critical Issues Fixed:**
- Missing imports in `nous/wrapper_nous.py` (aiohttp, run_in_threadpool)
- Session resource leak - aiohttp sessions now properly closed on shutdown
- Race condition in rate limiting (added threading.Lock)
- Inconsistent auth error format in nvidia-python (now consistent across all paths)
- Removed hardcoded DEFAULT_MODEL injection (transparent model selection)

**Security Considerations:**
- HTTP Header Injection (CVE-2026-33805): Validate Connection header handling
- Header Smuggling (CVE-2025-64484): Normalize X-Forwarded-* headers
- Request Smuggling: Validate Content-Length vs Transfer-Encoding conflicts

See individual READMEs for detailed audit findings.

## Repository Layout

```
wrapper/
├── README.md                    # This file
├── .env.example                 # Environment configuration template
├── install.sh                   # Installation script
├── artifacts/                   # Archived reports and backups
│   └── backup-nvidia-nodejs.tar.gz
│
├── nvidia-python/               # ← NVIDIA NIM proxy (Python)
│   ├── src/main.py             # FastAPI entry point
│   ├── src/key_pool.py         # API key rotation
│   ├── src/anthropic_compat.py # A↔O translation
│   ├── src/responses_compat.py # Responses API streaming
│   ├── .env.example            # NVIDIA-specific config
│   └── README.md
│
├── nous/                        # Nous Research proxy (Python)
│   ├── wrapper_nous.py         # Main FastAPI application
│   ├── .env.example            # Nous-specific config
│   ├── model_catalog_template.json  # Codex model metadata template
│   └── README.md
│
├── opencode/                    # OpenCode Zen proxy
    ├── src/main.py             # FastAPI entry point
    ├── .env.example            # OpenCode-specific config
    └── README.md
│
└── blackbox/                    # BLACKBOX AI proxy
    ├── src/main.py             # FastAPI entry point
    ├── .env.example            # BLACKBOX-specific config
    └── README.md
```

## Quick Start

### 1. NVIDIA NIM Proxy (Port 9101)

```bash
cd nvidia-python
pip install -r requirements.txt
cp .env.example .env   # add your NVIDIA_API_KEY_*
python -m uvicorn src.main:app --port 9101
```

### 2. Nous Research Proxy (Port 9102)

```bash
cd nous
pip install -r requirements.txt
python -m uvicorn wrapper_nous:app --port 9102
```

### 3. OpenCode Zen Proxy (Port 9103)

```bash
cd opencode
pip install -r requirements.txt
cp .env.example .env   # add your API keys
python -m uvicorn src.main:app --port 9103
```


### 4. BLACKBOX AI Proxy (Port 9104)

```bash
cd blackbox
pip install -r requirements.txt
cp .env.example .env   # add BLACKBOX_API_KEY_*
python -m uvicorn src.main:app --port 9104
```

## Client Configuration

### Claude Code CLI

```bash
export ANTHROPIC_BASE_URL="http://localhost:9101/v1"  # or 9102/9103/9104
export ANTHROPIC_API_KEY="test-key"  # or your actual key

# Test
claude code chat "Hello, what is 2+2?"
```

### OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:9101/v1",
    api_key="test-key"
)

response = client.chat.completions.create(
    model="sonnet",  # or "haiku", "opus", or concrete model id
    messages=[{"role": "user", "content": "Hello!"}]
)
```

### OpenAI Responses API (Codex)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:9101/v1",
    api_key="test-key"
)

response = client.responses.create(
    model="sonnet",
    input="Hello!"
)
```

## Model catalog and availability safety

All four wrappers now separate provider catalog data from credential/account availability. A public `/models` entry is not treated as proof that every account can invoke the model, and an account-scoped 404 is never converted into global retirement. Catalog data is stored in an ignored per-wrapper SQLite `model-state.db` with stale-while-revalidate behavior; account-scoped status and event history are stored with a credential fingerprint, never the raw API key.

Classification and operational details are documented in [MODEL_AVAILABILITY.md](MODEL_AVAILABILITY.md). Each wrapper exposes `/metrics/model-status`. `MODEL_CATALOG_TTL_SEC` controls catalog freshness and defaults to six hours.

## Features

- ✅ **Dynamic Aliases**: sonnet/haiku/opus resolve to last concrete model called
- ✅ **Streaming SSE**: Full event sequences with heartbeat
- ✅ **Tool Calls**: OpenAI function_calling + Anthropic tool_use formats
- ✅ **Multi-turn**: previous_response_id support
- ✅ **FREE_ONLY Mode**: Filter for free models only
- ✅ **Error Normalization**: SDK-compatible error messages
- ✅ **Rate Limiting**: Per-key and per-IP limits
- ✅ **Metrics**: Prometheus + JSON metrics endpoints

## Production Readiness

All wrappers achieved **100/100** production readiness score:

| Feature | nvidia-python | nous | opencode | blackbox |
|---------|--------------|------|----------|----------|
| Claude Code alias | ✅ | ✅ | ✅ | ✅ |
| Streaming SSE | ✅ | ✅ | ✅ | ✅ |
| Tool calls | ✅ | ✅ | ✅ | ✅ |
| Multi-turn | ✅ | ✅ | ✅ | ✅ |
| FREE_ONLY | N/A | ✅ | ✅ | ✅ (default yes) |
| Error format | ✅ | ✅ | ✅ | ✅ |

## License

Internal use only.