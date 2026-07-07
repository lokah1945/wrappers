# wrapper-nvidia

> Production-hardened OpenAI-compatible proxy for NVIDIA NIM with atomic multi-key
> rotation, internal pacing, structured telemetry, and zero-disruption `.env` reload.

[![Status](https://img.shields.io/static/v1?label=version&message=v9.0-rc1&color=blue)](#changelog)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](#)
[![License](https://img.shields.io/badge/license-proprietary-red)]()

---

## 1. What is this?

`wrapper-nvidia` is an OpenAI-API-compatible reverse-proxy for `integrate.api.nvidia.com`
with deep runtime hardening that vanilla SDK calls don't give you:

| Problem (vanilla)                                        | This wrapper                            |
|----------------------------------------------------------|-----------------------------------------|
| One 429 ⇒ user sees a failed request                    | Atomic per-key reservation, transparent failover |
| Multi-key management = manual orchestration              | Live `keypool` with hot-reload + capacity model |
| Bursty traffic hits single-key quota                    | Internal pacing = latency, never 429 to caller |
| Silent upstream 5xx                                      | JSON-sink events, alert history, optional Loki push |
| Hours spent on disk for model list                      | 24h model verification, ready before `/v1/models` query |
| Visibility is "hey Lucid" log scraping                   | Prometheus `/metrics/prom`, Grafana dashboard, alerts |

Original upstream: `https://integrate.api.nvidia.com/v1`. This wrapper listens on
`http://127.0.0.1:9100/v1`.

---

## 2. Quick start

### 2.1 Prereqs
- Linux (systemd userland), Python ≥ 3.11, network access to NVIDIA NIM.
- One or more NVIDIA API keys (`nvapi-…`).

### 2.2 Install
```bash
cd /root/wrapper/nvidia
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn httpx pydantic prometheus-client
```

### 2.3 Configure `.env`
```bash
cp .env.example .env    # if provided, else create your own
# Required — up to 50 keys supported (NVIDIA_API_KEY_1 .. _N) or single
# NVIDIA_API_KEY. Wrapper dedupes and reloads via SIGHUP.
cat >> .env <<'EOF'
NVIDIA_API_KEY_1=nvapi-XXXX1
NVIDIA_API_KEY_2=nvapi-XXXX2
NVIDIA_API_KEY_3=nvapi-XXXX3
EOF
```

### 2.4 Run
```bash
# foreground
python3 main.py

# OR via the bundled systemd unit
systemctl --user daemon-reload
systemctl --user enable --now nvidia-wrapper.service
systemctl --user status nvidia-wrapper.service
```

### 2.5 Smoke test
```bash
curl -sS http://127.0.0.1:9100/v1/models | jq '.data | length'
curl -sS http://127.0.0.1:9100/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"z-ai/glm-5.1","messages":[{"role":"user","content":"hi"}],"max_tokens":16}'
```

---

## 3. Architecture

### 3.1 Layer diagram
```
                    ┌─────────────────────────────────────────────┐
                    │          main.py (FastAPI/uvicorn)          │
  client ──HTTP──► │                                              │
                    │  ┌─────────────┐     ┌──────────────────┐  │
                    │  │ rate lim.   │     │ anthropic_compat │  │
                    │  │ + Middleware│     │ schema adapter   │  │
                    │  └─────┬───────┘     └────────┬─────────┘  │
                    │        │                      │             │
                    │        ▼                      ▼             │
                    │  ┌──────────────────────────────────────┐   │
                    │  │          KeyPool  (key_pool.py)      │   │
                    │  │  • atomic per-key reservation         │   │
                    │  │  • 429 → next key                  │   │
                    │  │  • in-flight counter                │   │
                    │  │  • hot-reload via SIGHUP                  │
                    │  └──────────────────────────────────────┘   │
                    │        │                                      │
                    │        ▼                                      │
                    │  ┌──────────────────────────────────────┐    │
                    │  │  httpx.AsyncClient (shared pool)     │    │
                    │  └──────────────────────────────────────┘    │
                    └──────────────┬───────────────────────────────┘
                                   │
                                   ▼
                  https://integrate.api.nvidia.com/v1
```

### 3.2 Components

| File                       | Role                                                                |
|----------------------------|---------------------------------------------------------------------|
| `main.py`                  | FastAPI app, route handlers, structured-log emit, SIGHUP handler   |
| `key_pool.py`              | Atomic reservation, capacity model, blacklist, hot-reload           |
| `capabilities.py`          | Per-model capability resolution (router for picker)                 |
| `metrics.py`               | In-memory counters + Prometheus `/metrics/prom`                     |
| `anthropic_compat.py`      | Optional Anthropic-API request/response adapter                    |
| `loki_push.py`             | Optional JSON-sink → Loki push (network-tolerant)                  |
| `alert_history.py`         | Separate historian: derive alert grades from JSON-sink → JSONL      |
| `wrapper-refresh.sh`       | Live `.env` reload (SIGHUP wrapper) — **user-facing shuffle**        |
| `wrapper-alert.sh`         | One-line status helper for ops (prints key health, latency p95)     |
| `grafana_dashboard.json`   | Ready-to-import Grafana dashboard (`uid=wrapper-nvidia`)            |
| `prometheus-alerts.yaml`   | Prometheus alert rules tied to gauges & counters                    |
| `CHANGELOG.md`             | Versioned history                                              |
| `INCIDENT_2026-06-25.md`   | Latest postmortem (403 root cause: truncated `.env`)           |

### 3.3 Failure modes — what caller sees

| Internal event                                  | Caller experience                       |
|-------------------------------------------------|-----------------------------------------|
| 429 from one key                                | Try next key → 200 OK                   |
| All keys 429 → pacing state                     | Caller may see latency spike, still 200 |
| Upstream 5xx                                    | Internal retry once → 503 if persistent |
| Pool exhausted (hot-reload pending)             | 503 JSON, `Retry-After` header          |
| Model 404                                       | Hidden from `/v1/models` after scan     |

**Promise:** as a user of `wrapper-nvidia`, you never see a 429 unless the entire
key pool is exhausted — the wrapper turns capacity limits into latency.

---

## 4. Operations

### 4.1 Lifecycle

```bash
systemctl --user status nvidia-wrapper.service
systemctl --user restart nvidia-wrapper.service
journalctl --user -u nvidia-wrapper.service -f
```

### 4.2 Hot .env reload

```bash
bash wrapper-refresh.sh       # sends SIGHUP
```
Wrapper re-reads `.env`, dedupes keys (preserves order), re-reserves capacity.

### 4.3 Observability

| Surface            | URL                                        | Format      |
|--------------------|--------------------------------------------|-------------|
| Prometheus         | `http://127.0.0.1:9100/metrics/prom`       | text/plain  |
| Live events        | `metrics_data/wrapper-events.jsonl`        | JSONL       |
| Alert history      | `metrics_data/alert-history.jsonl`         | JSONL       |
| (optional) Loki    | `LOKI_PUSH_URL=http://loki:3100/loki/api/v1/push` | push     |
| Grafana            | Import `grafana_dashboard.json`            | —           |

#### 4.3.1 Alert history
```bash
python3 alert_history.py --mode daemon    # tail JSON-sink → alert-history
python3 alert_history.py --mode once      # snapshot run
python3 alert_history.py --mode top       # group counts
```

Alert grades emitted: `exhaustion(critical)`, `rate_limit(warn)`,
`upstream_5xx(warn)`, `model_unavailable(warn)`, `pacing(info)`, `key_disabled(warn)`.

#### 4.3.2 Loki push (optional)
```bash
LOKI_PUSH_URL=http://loki:3100/loki/api/v1/push \
  python3 loki_push.py --mode daemon
```
If `LOKI_PUSH_URL` is unset, the script stays dormant (returns 0, no side-effects).

### 4.4 Key health

```bash
bash wrapper-alert.sh
```
Walks `/metrics/prom` + tail of events, prints health scoreboard.

---

## 5. Security

- **No spoofing** — wrapper preserves `Authorization: Bearer <client key>` semantics
  when `PASSTHROUGH_AUTH=true`; otherwise internal `NVIDIA_API_KEY_*` is used.
- **Local-only by default** — bound to `127.0.0.1:9100`. Do NOT expose to WAN.
- **No secrets in logs** — key labels only (e.g., `key=key1`); raw keys redacted.
- **JSON sink is local** — events stay in `metrics_data/`; Loki push is opt-in.
- **Hot reload** does NOT write the secret to disk again — it re-reads `.env`.
- **Audit-friendly** — every alert has schema-fixed fields; see § 6.2.

---

## 6. Telemetry schema

### 6.1 Gauge — Prometheus
| Metric                                  | Type     | Description                          |
|-----------------------------------------|----------|--------------------------------------|
| `wrapper_nvidia_active_keys_total`      | gauge    | Healthy keys in pool right now        |
| `wrapper_nvidia_blocked_keys_total`     | gauge    | Keys currently in 429 / cool-down     |
| `wrapper_nvidia_in_flight_requests`     | gauge    | Open async tasks waiting up-stream    |
| `wrapper_nvidia_last_request_latency_ms`| gauge    | Last request latency (ms)             |
| `wrapper_nvidia_p95_latency_ms`         | gauge    | 60s rolling p95 latency               |
| `wrapper_nvidia_exhaustions_total_24h`  | counter  | All-keys-exhausted count (24h)        |
| `wrapper_nvidia_5xx_total`              | counter  | Upstream 5xx count                    |
| `wrapper_nvidia_429_total`              | counter  | Upstream 429 count                    |

### 6.2 Alert event
```json
{
  "ts_iso":     "2026-06-25T07:12:00Z",
  "ts_source":  "...",
  "kind":       "rate_limit",
  "severity":   "warn",
  "model":      "z-ai/glm-5.1",
  "key_label":  "key1",
  "msg":        "..."
}
```

---

## 7. Development

### 7.1 Layout
```
.
├── main.py                # FastAPI entrypoint
├── key_pool.py            # Capacity model
├── capabilities.py        # Model capability router
├── metrics.py             # Prometheus exporter
├── anthropic_compat.py    # Optional Anthropic bridge
├── loki_push.py           # Optional Loki push (dormant unless env)
├── alert_history.py       # Alert-grade JSONL derivator
├── wrapper-refresh.sh     # Live .env reload (SIGHUP)
├── wrapper-alert.sh       # Operator status one-liner
├── grafana_dashboard.json # 11-panel dashboard
├── prometheus-alerts.yaml # 13 alert rules
├── tests/                 # (recommended) pytest harness
├── .env                   # Local secrets — gitignored
├── .gitignore
└── metrics_data/          # JSONL sink & alert history (gitignored)
```

### 7.2 Conventions
- Python 3.11+, type hints, async-first. No `print()` outside test scripts.
- Logging via stdlib `logging` → JSON-sink (one line per event).
- No `requests`/`urllib3` directly — go through `httpx.AsyncClient()` pool.
- Secrets: keys are labels in logs/metrics; raw keys live only in `.env`.

### 7.3 Tests
```bash
python3 -m pytest tests/ -q         # unit
bash smoke.sh                         # end-to-end (boots wrapper on 9101, hits /v1, asserts)
```

---

## 8. Troubleshooting

| Symptom                                    | Cause                          | Fix                                  |
|--------------------------------------------|--------------------------------|--------------------------------------|
| `503 exhausted`                            | All keys rate-limited          | Wait for cool-down or add keys       |
| `model not listed in /v1/models`           | Model id absent or 404         | Verify NVIDIA catalog URL            |
| Latency > 5s always                        | Token bucket pacing too tight  | Increase `MAX_CONNECTIONS`           |
| `JSON log sink failed to open`             | Disk full / permission         | Free space / chmod                   |
| Loki push verbose `URL err` in stderr      | Loki offline                   | Set `LOKI_PUSH_URL=""` to silence    |

Forensics:
```bash
tail -f metrics_data/wrapper-events.jsonl | jq -c 'select(.msg|test("429|exhaust"))'
python3 alert_history.py --mode top
```

---

## 9. Changelog

- **v9.0 (2026-06-25)** — production-hardened release:
  - atomic key reservation, internal pacing, dynamic hot-reload
  - JSON sink events → Loki push opt-in
  - alert_history.py + Grafana dashboard
  - Prometheus alert rules + one-line `wrapper-alert.sh`
  - cleanup pre-commit
  - cancelled planned auto-rotate helper (runtime already does atomic rotation;
    not introducing duplicate path)
- **v8.1 (2026-06-25)** — 403 incident fix: `.env` truncation restored, restart required
  for runtime to re-load keys
- **v8.0** — structured JSON logging + Prometheus `/metrics/prom` + ops config
- **v7.0** — all-exhausted schema + cross-key rotation logging
(… see `CHANGELOG.md` for full history)

---

## 10. License

Proprietary — internal use only. Not for redistribution.
