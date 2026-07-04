# wrapper-nvidia CHANGELOG

## v8.1 — 2026-06-25 (incident fix)
- **Incident**: 403 Forbidden on `/v1/chat/completions` after ILMA upgrade/patch
  - `.env` truncated to placeholder values (`nvapi-...`) during patch
  - Backup `.env` also truncated (taken after corruption)
  - Running `nvidia-wrapper.service` held old keys in memory
  - **Fix**: restore valid `.env`, restart systemd service to reload keys
  - **Verification**: direct NVIDIA API 200 OK, wrapper `/v1/chat/completions` 200 OK, streaming 200 OK

# wrapper-nvidia CHANGELOG

## v8.0 — 2026-06-25 (production hardening)
- **Bug fixes v7→v8**:
  - Pool init race on uvicorn bind (9100 already used by old process)
  - Refresh script using `reload-or-restart` got stuck on D-Bus (47s+); now uses `try-restart` (5.6s)
  - Structured JSON logger had `asctime=null` (parent Formatter not invoked); now uses `record.created` ISO-8601
  - `wrapper-alert.sh` newline lost in patch — restored
- **New ops scripts**:
  - `/root/wrapper/nvidia/wrapper-refresh.sh` (item B) — idempotent env reload + restart
  - `/root/wrapper/nvidia/wrapper-alert.sh` (item E) — no-Prometheus health probe
  - `/root/wrapper/nvidia/prometheus-alerts.yaml` (item E) — 5 alert rules
- **New endpoints**:
  - `GET /metrics/prom` (item D) — Prometheus 0.0.4 exposition format
- **Runtime changes**:
  - `WRAPPER_JSON_LOG=1` enabled in `/etc/systemd/system/nvidia-wrapper.service`
  - JSONL sink at `metrics_data/wrapper-events.jsonl` (append-only, agent-visible analytics)
- **Capabilities retained**:
  - 5-key round-robin fan-out with per-key RPM budget
  - Auto-retry on 429 across pool (constraint: 1 retry per request)
  - Model-level RPM ledger (in-memory) for 429 micro-cooldowns
  - OpenAI-compatible `/v1/chat/completions`, `/v1/models`

## v4.4 — pre-v8 baseline
- KeyPool with 5 NVIDIA NIM keys
- Per-model RPM tracking
- Outage-aware 1-retry policy
- 121 NVIDIA models cached
- 54 known-unavailable model IDs hidden from /v1/models listing
