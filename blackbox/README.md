# wrapper-blackbox

## Standardized Structure (2026-07-28)

This wrapper follows the standardized structure:

```
blackbox/
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
uvicorn src.main:app --reload --port 9104

# Production — ONE worker process (see WRAPPER_CONTRACT §6.3: the response
# store, key pool and rate limiter live in per-process memory; --workers 4
# splits previous_response_id/rate-limit state across workers)
uvicorn src.main:app --host 0.0.0.0 --port 9104 --workers 1
```

See WRAPPER_STANDARDIZATION_REPORT.md for details.



**Port:** 9104  
**Upstream:** BLACKBOX AI (`https://api.blackbox.ai`)  
**Type:** OpenAI + Anthropic compatible proxy

## Overview

Production-grade wrapper untuk BLACKBOX AI dengan fitur enterprise:

- ✅ Multi-key rotation (hingga 5 keys dengan load balancing)
- ✅ Circuit breaker (fail-fast saat upstream down)
- ✅ Streaming dengan heartbeat (anti-silence untuk reasoning models)
- ✅ Dynamic aliases (sonnet/haiku/opus → configurable target)
- ✅ Full OpenAI Chat + Responses API + Anthropic Messages support
- ✅ Dashboard monitoring (real-time metrics, key status)
- ✅ .env hot reload (ubah config tanpa restart)

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

### 1. Setup .env

```bash
cd blackbox
cp .env.example .env
# Edit .env dan isi:
# - BLACKBOX_API_KEY_1=sk-xxxxx (wajib)
# - BEARER_TOKEN=your-secure-token (untuk client auth)
```

### 2. Run

```bash
# Development
python -m uvicorn src.main:app --host 127.0.0.1 --port 9104

# Production (systemd)
sudo systemctl start wrapper-blackbox
```

### 3. Test

```bash
# Health check
curl http://127.0.0.1:9104/health

# OpenAI Chat
curl http://127.0.0.1:9104/v1/chat/completions \
  -H "Authorization: Bearer your-secure-token" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "blackboxai/nvidia/nemotron-3-super-120b-a12b:free",
    "messages": [{"role": "user", "content": "Hello"}]
  }'

# Dashboard
open http://127.0.0.1:9104/dashboard
```

## Supported Models

BLACKBOX AI mendukung berbagai model:

- **NVIDIA Nemotron:** `blackboxai/nvidia/nemotron-*`
- **Free Models:** Models dengan suffix `:free` atau `-free`
- **Custom Models:** Model custom yang tersedia di platform

## Dynamic Aliases

Alias `sonnet`, `haiku`, `opus` bisa di-bind ke model tertentu via `.env`:

```env
DYNAMIC_ALIAS_TARGET=blackboxai/nvidia/nemotron-3-super-120b-a12b:free
```

Setelah set, client bisa request model `sonnet` dan wrapper akan route ke model yang di-bind.

## Key Features

### Multi-Key Rotation

Tambahkan multiple keys untuk load balancing:

```env
BLACKBOX_API_KEY_1=sk-aaa
BLACKBOX_API_KEY_2=sk-bbb
BLACKBOX_API_KEY_3=sk-ccc
BLACKBOX_API_KEY_4=sk-ddd
BLACKBOX_API_KEY_5=sk-eee
```

Wrapper akan otomatis rotate keys berdasarkan least-loaded algorithm.

### Circuit Breaker

Saat upstream down, wrapper akan fail-fast dan return 503 tanpa timeout panjang. Circuit akan auto-recover setelah `RECOVERY_TIMEOUT_SEC` (default: 30s).

### Streaming Heartbeat

Untuk reasoning models yang silent selama thinking, wrapper inject heartbeat comments setiap `HEARTBEAT_INTERVAL_MS` (default: 5000ms) untuk mencegah client timeout.

### Free-Only Mode

Restrict wrapper hanya untuk free models:

```env
FREE_ONLY=yes
```

## Configuration

Semua konfigurasi via `.env` (lihat `.env.example` untuk detail lengkap).

### Required Variables

- `BLACKBOX_API_KEY_1` - API key dari BLACKBOX AI
- `BEARER_TOKEN` - Token untuk client authentication

### Optional Variables

- `BLACKBOX_API_KEY_2` hingga `BLACKBOX_API_KEY_5` - Additional keys untuk rotation
- `DYNAMIC_ALIAS_TARGET` - Model target untuk aliases
- `FREE_ONLY` - Restrict ke free models only
- `FREE_MODEL_ALLOWLIST` - Additional free models tanpa substring "free"

## Dashboard

Akses dashboard di `http://127.0.0.1:9104/dashboard` untuk monitoring real-time:

- Request metrics (RPS, latency, error rate)
- Key status (available, blocked, in-flight)
- Model availability
- Circuit breaker state

## Logs

Log file: `/root/wrapper/blackbox/blackbox.log` (configurable via `LOG_FILE`)

## Troubleshooting

### 503 Service Unavailable
- Check `BLACKBOX_API_KEY_1` di `.env`
- Verify key masih valid di BLACKBOX dashboard
- Check circuit breaker status di `/health`

### 429 Rate Limited
- Tambahkan lebih banyak keys (`BLACKBOX_API_KEY_2`, dll)
- Reduce `RATE_LIMIT_RPM` jika perlu
- Check upstream rate limits

### Stream Timeout
- Increase `STREAM_REQUEST_TIMEOUT_SEC` (default: 900s)
- Check upstream connectivity
- Verify model supports streaming

### Authentication Error
- Verify `BEARER_TOKEN` match antara client dan wrapper
- Check token di `.env` tidak expired
- Restart wrapper setelah update `.env`

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check + key status |
| `/ready` | GET | Readiness check |
| `/v1/models` | GET | List available models |
| `/v1/chat/completions` | POST | OpenAI Chat API |
| `/v1/responses` | POST | OpenAI Responses API |
| `/v1/messages` | POST | Anthropic Messages API |
| `/v1/messages/count_tokens` | POST | Token counting |
| `/dashboard` | GET | Monitoring dashboard |
| `/metrics` | GET | Prometheus metrics |
| `/metrics/prom` | GET | Prometheus format |
| `/metrics/model-status` | GET | Model availability |

## Architecture

```
Client (Claude Code/Codex/OpenAI SDK)
    ↓
wrapper-blackbox (9104)
    ↓
BLACKBOX AI API
    ↓
Backend Models (NVIDIA, Custom, etc)
```

## Performance Tuning

### Connection Pooling

```env
MAX_CONNECTIONS=200
MAX_CONNECTIONS_PER_HOST=100
```

### Rate Limiting

```env
RATE_LIMIT_RPM=120
BLACKBOX_HARD_LIMIT_RPM=60
```

### Timeouts

```env
CONNECT_TIMEOUT_SEC=30
REQUEST_TIMEOUT_SEC=600
STREAM_REQUEST_TIMEOUT_SEC=900
```

## Security

### Authentication

Semua endpoints (kecuali `/health` dan `/dashboard`) require `Authorization: Bearer <token>` header.

### CORS

Restricted to localhost only:
```
allow_origin_regex=r'https?://(127\.0\.0\.1|localhost|\[::1\])(:[0-9]+)?$'
```

### Header Sanitization

Semua forwarded headers di-sanitize untuk mencegah injection attacks.

### Cross-Tenant Isolation

Response store namespaced by principal (SHA-256 hash of token/IP).

## Monitoring

### Prometheus Metrics

```bash
curl http://127.0.0.1:9104/metrics/prom
```

### Health Check

```bash
curl http://127.0.0.1:9104/health | jq
```

Response:
```json
{
  "status": "ok",
  "keys": 5,
  "available": 4,
  "live_keys": [...],
  "metrics": {...},
  "circuit_breaker": {...}
}
```

## Deployment

### Systemd Service

```ini
[Unit]
Description=Wrapper Blackbox AI
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/wrapper/blackbox
Environment="PATH=/root/wrapper/blackbox/venv/bin"
ExecStart=/root/wrapper/blackbox/venv/bin/python -m uvicorn src.main:app --host 127.0.0.1 --port 9104
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Docker

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY blackbox/ ./blackbox/
COPY common/ ./common/
RUN pip install -r blackbox/requirements.txt

EXPOSE 9104
CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "127.0.0.1", "--port", "9104"]
```

## License

Internal use only.
