# wrapper-nvidia

> OpenAI- & Anthropic-compatible proxy for NVIDIA NIM API with multi-key rotation, rate-limit pacing, per-model failover, and Prometheus metrics.

## Quick Start

### Prerequisites

- Node.js >= 18
- One or more NVIDIA API keys (`nvapi-...`)

### Setup

```bash
cd /root/wrapper/nvidia
cp .env.example .env   # or edit the existing .env
```

At minimum, `.env` must have at least one API key:

```ini
NVIDIA_API_KEY_1=nvapi-xxxx...
NVIDIA_API_KEY_2=nvapi-yyyy...
LISTEN_PORT=9100
```

### Run

```bash
npm start
```

Or use `--watch` for development (auto-restart on file changes):

```bash
npm run dev
```

### Verify

```bash
# Health check
curl http://localhost:9100/health

# List models
curl http://localhost:9100/v1/models | jq '.data | length'

# Chat completion
curl http://localhost:9100/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"meta/llama-3.1-8b-instruct","messages":[{"role":"user","content":"Hello"}],"max_tokens":100}'

# Prometheus metrics
curl http://localhost:9100/metrics/prom
```

## Endpoints

### OpenAI-Compatible

| Endpoint | Method | Description |
|---|---|---|
| `/v1/chat/completions` | POST | Chat completions (sync + streaming) |
| `/v1/embeddings` | POST | Text embeddings |
| `/v1/models` | GET | List all available models |
| `/v1/models/:id` | GET | Get model details |
| `/v1/complete` | POST | Legacy completions (converted to chat) |
| `/v1/engines` | GET | Legacy models alias |
| `/v1/engines/:id` | GET | Legacy model detail alias |
| `/v1/images/generations` | POST | Image generation |
| `/v1/ranking` | POST | Reranking |

### Anthropic-Compatible

| Endpoint | Method | Description |
|---|---|---|
| `/v1/messages` | POST | Anthropic Messages API (translated to/from OpenAI) |
| `/v1/messages/count_tokens` | POST | Estimate input token count |

### Ollama-Compatible

| Endpoint | Method | Description |
|---|---|---|
| `/api/tags` | GET | List models (Ollama format) |
| `/api/show` | GET | Model info stub |
| `/api/version` | GET | Version info |

### Management & Monitoring

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Health status JSON |
| `/stats` | GET | Full stats (keys, metrics, models) |
| `/metrics/prom` | GET | Prometheus metrics (text) |
| `/metrics` | GET | Dashboard metrics (JSON) |
| `/metrics/tokens` | GET | Token usage stats |
| `/metrics/models` | GET | Per-model stats + blocked models |
| `/metrics/keys` | GET | Per-key stats |
| `/metrics/activity` | GET | Recent request log |
| `/metrics/rate-limits` | GET | Rate limit events + summary |
| `/metrics/model-status` | GET | Model availability status |
| `/metrics/reset` | POST | Reset all metric counters |
| `/metrics/chart/hourly` | GET | Hourly chart data |
| `/metrics/chart/daily` | GET | Daily chart data |
| `/admin/heal-in-flight` | POST | Reset stuck in-flight counters |
| `/v1/capabilities` | GET | Model capability classification |
| `/v1/capabilities/params` | GET | Supported parameters per capability |
| `/version` | GET | Version string |
| `/dashboard.html` | GET | Web dashboard (if present) |

## How to Use

### curl

```bash
# Sync chat
curl http://localhost:9100/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "meta/llama-3.1-8b-instruct",
    "messages": [{"role": "user", "content": "What is 1+1?"}],
    "max_tokens": 100
  }'

# Streaming chat
curl -N http://localhost:9100/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "meta/llama-3.1-8b-instruct",
    "messages": [{"role": "user", "content": "Count from 1 to 5"}],
    "stream": true,
    "max_tokens": 200
  }'

# Anthropic format
curl http://localhost:9100/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "meta/llama-3.1-8b-instruct",
    "messages": [{"role": "user", "content": "Hi, what can you do?"}],
    "max_tokens": 200
  }'

# Embeddings
curl http://localhost:9100/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nvidia/nv-embed-v1",
    "input": "Hello world",
    "input_type": "query"
  }'

# Image generation
curl http://localhost:9100/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{
    "model": "black-forest-labs/flux.1-schnell",
    "prompt": "a cat in a spacesuit"
  }'
```

### Python

```python
import requests

BASE = "http://localhost:9100"

# Chat
r = requests.post(f"{BASE}/v1/chat/completions", json={
    "model": "meta/llama-3.1-8b-instruct",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 100
})
print(r.json()["choices"][0]["message"]["content"])

# Streaming
r = requests.post(f"{BASE}/v1/chat/completions", json={
    "model": "meta/llama-3.1-8b-instruct",
    "messages": [{"role": "user", "content": "Count to 5"}],
    "stream": True,
    "max_tokens": 200
}, stream=True)
for line in r.iter_lines():
    if line and line.startswith(b"data: "):
        data = line[6:]
        if data.strip() == b"[DONE]":
            break
        import json
        chunk = json.loads(data)
        content = chunk["choices"][0]["delta"].get("content", "")
        if content:
            print(content, end="", flush=True)
```

### Node.js

```javascript
const http = require('http');

function post(path, body) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const req = http.request({
      hostname: 'localhost', port: 9100, path,
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) }
    }, res => {
      let body = '';
      res.on('data', c => body += c);
      res.on('end', () => resolve(JSON.parse(body)));
    });
    req.on('error', reject);
    req.write(data);
    req.end();
  });
}

async function main() {
  const res = await post('/v1/chat/completions', {
    model: 'meta/llama-3.1-8b-instruct',
    messages: [{ role: 'user', content: 'Hello' }],
    max_tokens: 100
  });
  console.log(res.choices[0].message.content);
}
main();
```

### OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:9100/v1",
    api_key="ignored-by-wrapper"  # wrapper manages its own keys
)

r = client.chat.completions.create(
    model="meta/llama-3.1-8b-instruct",
    messages=[{"role": "user", "content": "Hello"}]
)
print(r.choices[0].message.content)
```

### Anthropic SDK

```python
from anthropic import Anthropic

client = Anthropic(
    base_url="http://localhost:9100",
    api_key="ignored-by-wrapper"
)

r = client.messages.create(
    model="meta/llama-3.1-8b-instruct",
    max_tokens=100,
    messages=[{"role": "user", "content": "Hello"}]
)
print(r.content[0].text)
```

## Configuration

All config via `.env` file (auto-reloaded every `KEYS_RELOAD_SECONDS`):

| Variable | Default | Description |
|---|---|---|
| `NVIDIA_API_KEY_N` | — | NVIDIA API key(s). Supports `_1` through `_N` |
| `LISTEN_PORT` | 9101 | HTTP listen port |
| `LISTEN_HOST` | 0.0.0.0 | Bind address |
| `SOFT_LIMIT_RPM` | 30 | Soft RPM limit per key (pacing target) |
| `HARD_LIMIT_RPM` | 40 | Hard RPM limit per key |
| `QUEUE_LIMIT` | 1.0 | Queue admission rate (req/s per key) |
| `MAX_QUEUE_SIZE` | 100 | Max queued requests before 503 |
| `MAX_CONNECTIONS` | 200 | Upstream connection pool size |
| `NVIDIA_BASE_URL` | https://integrate.api.nvidia.com | LLM base URL |
| `NVIDIA_GENAI_URL` | https://ai.api.nvidia.com | GenAI base URL |
| `NVIDIA_NVCF_URL` | https://api.nvcf.nvidia.com | NVCF base URL |
| `BEARER_TOKEN` | — | Optional auth token for clients |
| `DROP_PARAMS` | think | Comma-separated params to strip proactively |
| `REQUEST_TIMEOUT` | 600 | Upstream request timeout (seconds) |
| `KEYS_RELOAD_SECONDS` | 60 | Interval for `.env` re-read |
| `METRICS_DB` | `metrics.db` | SQLite database path |
| `MODEL_REFRESH_SEC` | 600 | Model list refresh interval |
| `VERIFY_ON_BOOT` | true | Run model verification on startup |
| `QUIET_RETRIED_429` | 3 | Max retries per request |
| `MODEL_GRACE_FAILS` | 2 | Consecutive timeouts before marking model unavailable |

## Architecture

```
Client ──HTTP──► wrapper-nvidia (Node.js)
                     │
          ┌──────────┴──────────┐
          │     key_pool.js      │  ← multi-key rotation, rate-limit classification
          │  anthropic_compat.js │  ← Anthropic↔OpenAI schema translation
          │    capabilities.js   │  ← per-model type/context classification
          │      metrics.js      │  ← SQLite-backed telemetry
          └──────────┬──────────┘
                     │
          ┌──────────┴──────────┐
          │    undici Agent      │  ← HTTP/2 connection pool
          └──────────┬──────────┘
                     ▼
    https://integrate.api.nvidia.com/v1
```

### Key features

- **Multi-key rotation**: transparent failover across API keys. One 429 → next key, never a failed request.
- **Per-model failover**: model-level 429s block only that model on that key, not the entire key.
- **Rate-limit classification**: corroboration-based logic distinguishes model-level vs key-level 429s.
- **Internal pacing**: token-bucket admission queue turns capacity limits into latency, never 429s to caller.
- **Model verification**: periodic probes detect degraded and unavailable models; grace count prevents false positives from transient timeouts.
- **Anthropic translation**: full request/response translation between Anthropic Messages API and OpenAI Chat Completions format.
- **Prometheus metrics**: `/metrics/prom` for integration with monitoring stacks.
- **Live `.env` reload**: keys and limits re-read from `.env` every `KEYS_RELOAD_SECONDS` without restart.

## Development

### Tests

```bash
# Unit tests
npm test

# E2E (requires running server)
python3 test_e2e.py
node test_translation_e2e.js

# Rate limit tests
node test_rate_limit_rotation.js
```

### Project layout

```
├── src/
│   ├── index.js              # Server entry, routes, handlers
│   ├── anthropic_compat.js   # Anthropic↔OpenAI translation
│   ├── capabilities.js       # Model classification
│   ├── metrics.js            # SQLite-backed metrics
│   ├── alert_history.js      # Alert-grade historian
│   └── loki_push.js          # Optional Loki push
├── key_pool.js               # Multi-key pool with pacing
├── test/
│   └── test.js               # Unit tests
├── test_e2e.py               # Python E2E tests
├── test_translation_e2e.js   # JS E2E tests
├── test_rate_limit_rotation.js # Rate limit tests
├── package.json
├── .env                      # Configuration (gitignored)
├── dashboard.html            # Web dashboard
└── CHANGELOG.md
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `503 - All API keys exhausted` | All keys rate-limited or blocked | Wait for cooldown or add more keys |
| Model returns 404 | Model ID incorrect or unavailable | Check `GET /v1/models` for available IDs |
| High latency | Pacing active under high load | Increase `MAX_CONNECTIONS` or `SOFT_LIMIT_RPM` |
| `clientIp is not defined` | Missing function in code (fixed in v4.6.0) | Update to latest version |
| Auth fails with `Unauthorized` | `BEARER_TOKEN` mismatch | Check `.env` and client Authorization header |
