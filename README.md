# wrapper-nvidia

> OpenAI- and Anthropic-compatible transparent proxy for the NVIDIA NIM API.
> Adds multi-key rotation, rate-limit pacing, per-model failover, and
> Prometheus-style metrics on top of NVIDIA NIM.

This repository contains the production **wrapper-nvidia** service only.

- Service: `wrapper-nvidia.service` (systemd)
- Ports: `9100` primary, `9910` additionally bound on the same server (gateway model discovery)
- Provider: NVIDIA NIM (`https://integrate.api.nvidia.com/v1`)
- Models: 120+ free models via the NVIDIA NIM catalog

## Repository layout

```
wrapper-nvidia/
├── install.sh                 # idempotent systemd installer
├── wrapper-nvidia.service     # systemd unit (copied to /etc/systemd/system)
├── .env.example               # canonical config keys
├── .gitignore
├── README.md                  # this file
└── nvidia/                    # wrapper source + assets
    ├── src/
    │   ├── index.js            # server entry, routing, retry/backoff, SSE keepalive
    │   ├── anthropic_compat.js # Anthropic <-> OpenAI translation
    │   ├── responses_compat.js # OpenAI Responses API (Codex / Hermes)
    │   ├── capabilities.js     # model classification
    │   ├── metrics.js          # SQLite-backed telemetry
    │   ├── registry.js         # dynamic NGC-synced model registry
    │   ├── alert_history.js    # alert historian
    │   └── loki_push.js        # optional Loki push
    ├── key_pool.js             # multi-key pool with pacing
    ├── dashboard.html          # ops dashboard
    ├── test/                   # unit + regression + e2e tests
    ├── package.json
    ├── .env.example
    ├── CHANGELOG.md
    └── README.md               # deep-dive docs / runbook
```

## Features

- Transparent proxy: model names pass through exactly as the client sends them.
- Multi-key rotation with even distribution and per-(key, model) rate-limit isolation.
- Internal token-bucket pacing that converts capacity limits into latency instead of 429s.
- OpenAI Chat Completions + Anthropic Messages API translation, plus OpenAI Responses API.
- SSE stream heartbeat so long-running / reasoning streams don't time out.
- Adaptive backpressure (workload-aware queue sizing) and a provider circuit breaker.
- Retry budget cap with jittered exponential backoff; minimal 4-class error taxonomy.
- Prometheus-style metrics on `/metrics/prom`, plus `/health`, `/stats`, `/version`.

## Quick start

### Prerequisites
- Node.js >= 18
- One or more NVIDIA API keys (`nvapi-...`)

### Configure
```bash
cd /root/wrapper/nvidia
cp .env.example .env
# edit .env: set NVIDIA_API_KEY_1, NVIDIA_API_KEY_2, ...
```

### Run (systemd, recommended)
```bash
sudo ./install.sh            # installs unit, enables + starts service, smoke-tests /health
sudo ./install.sh --status   # show service status
```

### Run (manual)
```bash
cd /root/wrapper/nvidia
npm install
npm start
curl http://localhost:9100/health
```

## Endpoints

OpenAI-compatible: `/v1/chat/completions`, `/v1/embeddings`, `/v1/models`,
`/v1/images/generations`, `/v1/ranking`.

Anthropic-compatible: `/v1/messages`, `/v1/messages/count_tokens`.

Management: `/health`, `/stats`, `/metrics/prom`, `/metrics/activity`,
`/metrics/model-status`, `/version`.

See [nvidia/README.md](nvidia/README.md) for full examples and the configuration
reference.

## Configuration

All configuration is via `.env` (auto-reloaded). Key variables:

| Variable | Default | Description |
|---|---|---|
| `NVIDIA_API_KEY_N` | — | NVIDIA API key(s) |
| `LISTEN_PORT` | 9100 | Primary HTTP listen port |
| `ANY_ALSO_PORTS` | 9910 | Extra ports bound on the same server |
| `SOFT_LIMIT_RPM` | 30 | Soft RPM limit per key |
| `HARD_LIMIT_RPM` | 40 | Hard RPM limit per key |
| `REQUEST_TIMEOUT` | 600 | Upstream timeout (seconds) |
| `HEARTBEAT_INTERVAL_MS` | 5000 | Stream heartbeat interval (ms) |
| `DROP_PARAMS` | think | Params to strip proactively |

Secrets (`*.env`, `*.db`, `backups/`, `node_modules/`) are git-ignored and never committed.

## Testing
```bash
cd /root/wrapper/nvidia
npm test                 # unit tests
node test/test.js
```

## License
Internal use only.
