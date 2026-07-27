# wrapper-vercel

## Standardized Structure (2026-07-28)

This wrapper follows the standardized structure:

```
vercel/
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
uvicorn vercel.src.main:app --reload --port 9105

# Production
uvicorn vercel.src.main:app --host 0.0.0.0 --port 9105 --workers 4
```

See WRAPPER_STANDARDIZATION_REPORT.md for details.



**Port:** 9105  
**Upstream:** Vercel AI Gateway (`https://ai-gateway.vercel.sh/v1`)  
**Type:** OpenAI + Anthropic compatible proxy

## Overview

Production-grade wrapper untuk Vercel AI Gateway dengan fitur enterprise:

- ✅ Multi-key rotation (hingga 4+ keys dengan load balancing)
- ✅ Circuit breaker (fail-fast saat upstream down)
- ✅ Streaming dengan heartbeat (anti-silence untuk reasoning models)
- ✅ Dynamic aliases (sonnet/haiku/opus → configurable target)
- ✅ Full OpenAI Chat + Responses API + Anthropic Messages support
- ✅ Dashboard monitoring (real-time metrics, key status)
- ✅ .env hot reload (ubah config tanpa restart)

## Quick Start

### 1. Setup .env

```bash
cd vercel
cp .env.example .env
# Edit .env dan isi:
# - VERCEL_API_KEY_1=vck_xxxxx (wajib)
# - BEARER_TOKEN=your-secure-token (untuk client auth)
```

### 2. Run

```bash
# Development
python -m vercel.wrapper_vercel

# Production (systemd)
sudo systemctl start wrapper-vercel
```

### 3. Test

```bash
# Health check
curl http://127.0.0.1:9105/health

# OpenAI Chat
curl http://127.0.0.1:9105/v1/chat/completions \
  -H "Authorization: Bearer your-secure-token" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "anthropic/claude-3.5-sonnet",
    "messages": [{"role": "user", "content": "Hello"}]
  }'

# Dashboard
open http://127.0.0.1:9105/dashboard
```

## Supported Models

Vercel AI Gateway mendukung routing ke berbagai provider:

- **Anthropic:** `anthropic/claude-*`
- **OpenAI:** `openai/gpt-*`
- **Google:** `google/gemini-*`
- **Meta:** `meta/llama-*`
- **DeepSeek:** `deepseek/deepseek-*`
- **Mistral:** `mistralai/mistral-*`

## Dynamic Aliases

Alias `sonnet`, `haiku`, `opus` bisa di-bind ke model tertentu via `.env`:

```env
DYNAMIC_ALIAS_TARGET=anthropic/claude-3.5-sonnet
```

Setelah set, client bisa request model `sonnet` dan wrapper akan route ke `anthropic/claude-3.5-sonnet`.

## Key Features

### Multi-Key Rotation

Tambahkan multiple keys untuk load balancing:

```env
VERCEL_API_KEY_1=vck_aaa
VERCEL_API_KEY_2=vck_bbb
VERCEL_API_KEY_3=vck_ccc
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

## Dashboard

Akses dashboard di `http://127.0.0.1:9105/dashboard` untuk monitoring real-time:

- Request metrics (RPS, latency, error rate)
- Key status (available, blocked, in-flight)
- Model availability
- Circuit breaker state

## Logs

Log file: `/root/wrapper/vercel/vercel.log` (configurable via `LOG_FILE`)

## Troubleshooting

### 503 Service Unavailable
- Check `VERCEL_API_KEY_1` di `.env`
- Verify key masih valid di Vercel dashboard
- Check circuit breaker status di `/health`

### 429 Rate Limited
- Tambahkan lebih banyak keys (`VERCEL_API_KEY_2`, `VERCEL_API_KEY_3`, dll)
- Reduce `RATE_LIMIT_RPM` jika perlu

### Stream Timeout
- Increase `STREAM_REQUEST_TIMEOUT_SEC` (default: 900s)
- Check upstream connectivity

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check + key status |
| `/v1/models` | GET | List available models |
| `/v1/chat/completions` | POST | OpenAI Chat API |
| `/v1/responses` | POST | OpenAI Responses API |
| `/v1/messages` | POST | Anthropic Messages API |
| `/dashboard` | GET | Monitoring dashboard |
| `/metrics` | GET | Prometheus metrics |

## Architecture

```
Client (Claude Code/Codex/OpenAI SDK)
    ↓
wrapper-vercel (9105)
    ↓
Vercel AI Gateway
    ↓
Backend Provider (Anthropic/OpenAI/Google/etc)
```

## License

Internal use only.
