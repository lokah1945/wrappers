# wrapper-nvidia

> OpenAI- & Anthropic-compatible transparent proxy for NVIDIA NIM API with multi-key rotation, rate-limit pacing, and per-model failover.

**Version:** 8.6.2  
**Branch:** audit-2026-07-19  
**Status:** ✅ Production Ready

## Quick Start

### Prerequisites

- Node.js >= 18
- One or more NVIDIA API keys (`nvapi-...`)

### Setup

```bash
cd /root/wrapper/nvidia
cp .env.example .env
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

### Verify

```bash
# Health check
curl http://localhost:9100/health

# List models
curl http://localhost:9100/v1/models | jq '.data | length'

# Chat completion (OpenAI format)
curl http://localhost:9100/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"meta/llama-3.1-8b-instruct","messages":[{"role":"user","content":"Hello"}],"max_tokens":100}'

# Chat completion (Anthropic format)
curl http://localhost:9100/v1/messages \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"meta/llama-3.1-8b-instruct","messages":[{"role":"user","content":"Hello"}],"max_tokens":100}'
```

## Endpoints

### OpenAI-Compatible

| Endpoint | Method | Description |
|---|---|---|
| `/v1/chat/completions` | POST | Chat completions (sync + streaming) |
| `/v1/responses` | POST | OpenAI Responses API (Codex, wire_api="responses") |
| `/v1/embeddings` | POST | Text embeddings |
| `/v1/models` | GET | List all available models |
| `/v1/images/generations` | POST | Image generation |
| `/v1/ranking` | POST | Reranking |

### Anthropic-Compatible

| Endpoint | Method | Description |
|---|---|---|
| `/v1/messages` | POST | Anthropic Messages API |
| `/v1/messages/count_tokens` | POST | Estimate input token count |

### Management & Monitoring

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Health status JSON |
| `/stats` | GET | Full stats (keys, metrics, models) |
| `/metrics/prom` | GET | Prometheus metrics |
| `/metrics/activity` | GET | Recent request log |
| `/metrics/model-status` | GET | Model availability status |
| `/version` | GET | Version string |

## Usage Examples

### curl

```bash
# Streaming (OpenAI format)
curl -N http://localhost:9100/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "meta/llama-3.1-8b-instruct",
    "messages": [{"role": "user", "content": "Count from 1 to 5"}],
    "stream": true,
    "max_tokens": 200
  }'

# Streaming (Anthropic format)
curl -N http://localhost:9100/v1/messages \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "meta/llama-3.1-8b-instruct",
    "messages": [{"role": "user", "content": "Count from 1 to 5"}],
    "stream": true,
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
```

### Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:9100/v1",
    api_key="ignored-by-wrapper"
)

r = client.chat.completions.create(
    model="meta/llama-3.1-8b-instruct",
    messages=[{"role": "user", "content": "Hello"}]
)
print(r.choices[0].message.content)
```

### Python (Anthropic SDK)

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

## Transparent Proxy Mode

The wrapper operates as a **transparent proxy** — model names pass through exactly as the client sends them. No hardcoded mapping, no silent model swapping.

- **Client sends** `meta/llama-3.1-8b-instruct` → **NVIDIA NIM receives** `meta/llama-3.1-8b-instruct`
- **Client sends** `nvidia/nemotron-3-ultra-550b-a55b` → **NVIDIA NIM receives** `nvidia/nemotron-3-ultra-550b-a55b`
- **Client sends** non-existent model → **404 returned transparently**

The wrapper handles:
- API key load balancing (even distribution across keys)
- Rate limit detection and key rotation
- Anthropic ↔ OpenAI format translation
- Stream heartbeat for long-running requests
- Metrics logging

## Configuration

All config via `.env` file (auto-reloaded):

| Variable | Default | Description |
|---|---|---|
| `NVIDIA_API_KEY_N` | — | NVIDIA API key(s) |
| `LISTEN_PORT` | 9100 | HTTP listen port |
| `SOFT_LIMIT_RPM` | 30 | Soft RPM limit per key |
| `HARD_LIMIT_RPM` | 40 | Hard RPM limit per key |
| `QUEUE_LIMIT` | 1.0 | Queue admission rate (req/s per key) |
| `MAX_QUEUE_SIZE` | 100 | Max queued requests before 503 |
| `REQUEST_TIMEOUT` | 600 | Upstream timeout (seconds) |
| `HEARTBEAT_INTERVAL_MS` | 5000 | Stream heartbeat interval (ms) |
| `DROP_PARAMS` | think | Params to strip proactively |

## Architecture

```
Client ──HTTP──► wrapper-nvidia (Node.js)
                     │
          ┌──────────┴──────────┐
          │     key_pool.js      │  ← multi-key rotation, rate-limit pacing
          │  anthropic_compat.js │  ← Anthropic↔OpenAI translation
          │    capabilities.js   │  ← model classification
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

- **Transparent proxy**: model names pass through exactly as client sends them
- **Multi-key rotation**: transparent failover across API keys
- **Per-model failover**: model-level 429s block only that model on that key
- **Internal pacing**: token-bucket admission queue turns capacity limits into latency
- **Anthropic translation**: full request/response translation for Claude Code compatibility
- **Stream heartbeat**: periodic ping during long-running streams
- **Unique message IDs**: each response has a unique ID for event tracking
- **Accurate token counts**: input_tokens reported in message_start events

## Project Layout

```
├── src/
│   ├── index.js              # Server entry, routes, handlers
│   ├── anthropic_compat.js   # Anthropic↔OpenAI translation
│   ├── capabilities.js       # Model classification
│   ├── metrics.js            # SQLite-backed metrics
│   ├── alert_history.js      # Alert historian
│   └── loki_push.js          # Optional Loki push
├── key_pool.js               # Multi-key pool with pacing
├── test/
│   └── test.js               # Unit tests
├── .env.example              # Config template
├── package.json
├── CHANGELOG.md
├── AUDIT_REPORT_2026-07-06.md
└── README.md
```

## Testing

```bash
# Unit tests
npm test

# E2E tests (requires running server)
curl http://localhost:9100/health
```

### Test Results (2026-07-06)

| Category | Result |
|----------|--------|
| Unit tests | ✅ 4/4 pass |
| E2E tests | ✅ 25/25 pass |
| Model tests | ✅ 38/40 pass (0 failures, 2 timeouts) |
| Load balancing | ✅ ±0.5% even across 5 keys |

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for version history.

## License

Internal use only.
