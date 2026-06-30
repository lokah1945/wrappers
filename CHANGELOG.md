# wrapper-nvidia CHANGELOG

## v8.2 — 2026-06-30 (deep audit + hardening) — PRODUCTION-READY
**Auditor**: ILMA v3.29 (audit report: `AUDIT_REPORT_2026-06-30.md`)

### Critical bugs found and resolved
- **#1 Orphan process held port 9100 with no supervision** — wrapper-nvidia.service was `inactive (dead)` since 2026-06-29 14:44, port held by orphan Node.js (PID 235307, PPID=684). Duplicate `nvidia-wrapper.service` also existed.
  - Fix: killed orphan, removed duplicate unit, brought canonical service back under systemd (PPID=1, CGroup `system.slice/wrapper-nvidia.service`).
- **#2 Pathologically long server timeouts** — `server.timeout=300000`, `keepAliveTimeout=75000`. Combined with abort signals caused silent 75s stalls.
  - Fix: env-driven knobs `SERVER_REQUEST_TIMEOUT_MS=60000`, `SERVER_KEEPALIVE_TIMEOUT_MS=10000`, `SERVER_HEADERS_TIMEOUT_MS=15000`. Plus **ANTI_SILENCE_TIMEOUT_MS=45000 watchdog** that 504s any handler that hasn't started writing within 45s.
- **#3 Metrics `_save()` ENOENT race** — periodic 30s save + sync close-save could fire back-to-back, throwing `ENOENT rename metrics.db.tmp -> metrics.db` on shutdown (logged 2026-06-29 14:44:52).
  - Fix: `_saveInFlight` flag coalesces, ENOENT on rename logged as benign (main DB intact), `mkdirSync(recursive)` for parent dir, atomic temp+rename preserved.
- **#4 `pacingMaxWait` hardcoded 60s** — first request after boot could stall up to 60s waiting for key ticket.
  - Fix: configurable via `PACING_MAX_WAIT` env, default 30s, floor 5s.
- **#5 Duplicate systemd unit** — `nvidia-wrapper.service` was confusion-duplicate; removed.
- **#6 Missing `.gitignore`** — no guard to exclude `.env`, `metrics.db`, `*.tmp`. Added.

### New files / scripts
- `install.sh` — idempotent installer. Modes: default (full install + restart + health smoke), `--no-restart` (install without restart), `--status` (read-only).
- `.env.example` — template with new hardening knobs documented.
- `.gitignore` — exclude secrets, runtime data, backups.
- `AUDIT_REPORT_2026-06-30.md` — full audit evidence.

### Files modified
- `src/index.js` — tightened server timeouts, added `guardServer` with anti-silence watchdog, hardened shutdown sequence.
- `src/metrics.js` — `_save()` concurrency-safe + ENOENT-tolerant.
- `src/key_pool.js` — `pacingMaxWait` configurable.
- `wrapper-nvidia.service` (project & system) — hardened with explicit `StartLimitIntervalSec` (in `[Unit]`), `MemoryMax=512M`, all timeout envs.
- `/etc/systemd/system/nvidia-wrapper.service` — REMOVED.
- `README_AGENT.md` — updated snapshot, hardening knobs, anti-stall invariants.

### Verification (11/11 E2E tests passed)
| # | Test | Result |
|---|------|--------|
| 1 | health (sub-ms) | 200 / 1ms |
| 2 | quick chat (OpenAI) | 200 / 0.4-16s |
| 3 | quick chat (Anthropic /v1/messages) | 200 / 0.44s |
| 4 | anthropic count_tokens | 200 / 1ms |
| 5 | embeddings nv-embed-v1 | 200 / 0.58s |
| 6 | malformed JSON fast-fail | 400 / 2ms |
| 7 | unknown model 404 | 404 / 2.48s |
| 8 | 5 concurrent chat completions | 5/5 succeeded |
| 9 | streaming + clean client abort + follow-up | 200 (post) |
| 10 | service still active post-stress | active, NRestarts=0 |
| 11 | TCP RST → service alive | 200 / 1ms |

### Operational notes
- Stdout now flows to **journalctl** (`journalctl -u wrapper-nvidia.service -f`), not `/tmp/wrapper.log` — sysadmins can grep ops events directly.
- All limits are env-tunable. No code change needed to roll back any specific timeout.
- Boot banner includes a one-line summary of hardening env: `Hardening: server.timeout=...ms keepAlive=...ms headers=...ms silenceGuard=...ms`

---

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
