# wrapper-nvidia — Agent Integration Guide

> **Audience:** ILMA agents, subagents, Ferrers/SuperCoding workers, integrators.
> **Snapshot:** 2026-06-30 05:36 WIB (post-audit hardening, systemd-managed, anti-silence watchdog)
>
> ⚠️ **Status: PRODUCTION-READY after audit 2026-06-30** — see
> `AUDIT_REPORT_2026-06-30.md` for full evidence. Three structural bugs were
> fixed: (1) orphan process + dead systemd unit, (2) `server.timeout=300s`
> + `keepAliveTimeout=75s` causing silent stalls, (3) `_save()` race with
> ENOENT on shutdown. Service is now `systemd-managed (PPID=1, single
> canonical unit)`, runs `install.sh` for idempotent deploy, and has a
> guaranteed `ANTI_SILENCE_TIMEOUT_MS=45000` watchdog.

## 1. Cara pakai dalam 30 detik

Wrapper ini listen di `http://127.0.0.1:9100` (LAN `0.0.0.0:9100`).
Pakai OpenAI SDK atau HTTP biasa — drop-in:

```python
from openai import OpenAI
client = OpenAI(
    base_url="http://127.0.0.1:9100/v1",
    api_key="not-needed",                       # BEARER_TOKEN dicek terpisah
)
r = client.chat.completions.create(
    model="meta/llama-3.3-70b-instruct",        # atau apapun yang muncul di /v1/models
    messages=[{"role":"user","content":"halo"}],
    temperature=0.6,
)
print(r.choices[0].message.content)
```

```bash
# Quick health
curl -s http://127.0.0.1:9100/health | jq .
curl -s http://127.0.0.1:9100/v1/models | jq '.data | length'
```

**Catatan penting**: ada internal BEARER_TOKEN (`/root/wrapper/nvidia/.env`). Kalau perlu akses dari scripts:
- Cari di MongoDB SOT lokal, atau
- Pakai service account `nadm`/`nvidia` di `.env`.

---

## 2. Endpoint surface (tested 2026-06-29)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | keypool + rpm + per-key status |
| GET | `/stats` | ringkasan usage |
| GET | `/v1/models` | 121 cached models, OpenAI-shaped |
| GET | `/v1/models/{id}` | detail per-model |
| GET | `/v1/capabilities` | **type, capabilities, endpoints, supported_params per model** |
| GET | `/v1/capabilities/params` | schema params |
| POST | `/v1/chat/completions` | OpenAI chat (primary) |
| POST | `/v1/messages` | **Anthropic Messages API compatible** |
| POST | `/v1/messages/count_tokens` | hitung token Anthropic-style |
| POST | `/v1/embeddings` | OpenAI embed |
| POST | `/v1/images/generations` | OpenAI image gen (via upstream NIM) |
| POST | `/v1/infer` | NVIDIA native async (`/async` upstream) |
| POST | `/v1/ranking` | rerank |
| POST | `/admin/heal-in-flight` | ops: bersihkan request yang macet |
| POST | `/metrics/reset` | ops: reset metric |
| GET | `/metrics/prom` `/metrics/tokens` `/metrics/models` `/metrics/keys` `/metrics/activity` `/metrics/rate-limits` `/metrics/model-status` `/metrics/chart/hourly` `/metrics/chart/daily` `/metrics/models/timeseries` | observability |

---

## 3. Model Census (snapshot 2026-06-29, 121 models)

| Type | Count | Source |
|------|------:|--------|
| `chat` | 92 | llama-3.*, mistral-large-3, qwen3.*, nemotron, deepseek-v4, gemma, phi-4 |
| `chat+code` | 8 | codellama-70b, codegemma, codestral-22b, granite-code, starcoder2 |
| `chat+vision` | 7 | llama-3.2-90b-vision, vila, phi-3-vision, nemotron-vl |
| `embedding` | 10 | nv-embed-v1, nemoretriever, arctic-embed-l, bge-m3 |
| `video` | 2 | cosmos-reason2-8b, ai-synthetic-video-detector |
| `parse` | 2 | nemoretriever-parse, nemotron-parse |

**TIDAK ada model image-gen** (flux/sdxl) di upstream NIM ini → image harus dari xAI Grok (Nous).

---

## 4. Recommended routing rules untuk agent

### 4.1 Selection priority
```
1. Free-only models dulu (semua NVIDIA = free tier via nvapi-*)
2. Pilih berdasarkan capability type:
   - chat+code → mistral-large-3-675b (best quality) ili meta/llama-3.3-70b
   - chat+vision → meta/llama-3.2-90b-vision-instruct, microsoft/phi-3.5-moe
   - long-context (>128k) → meta/llama-3.1-70b-instruct (131k ctx)
   - safety check → nvidia/llama-3.1-nemoguard-8b-content-safety
3. Pakai fallback cascade (lihat §6)
```

### 4.2 Fallback chain example (sot cascade)
```python
CASCADE = [
    "meta/llama-3.3-70b-instruct",             # primary
    "mistralai/mistral-large-3-675b-instruct-2512",
    "qwen/qwen3.5-397b-a17b",
    "nvidia/nemotron-3-super-120b-a12b",
    "deepseek-ai/deepseek-v4-pro",
]
```

---

## 5. Conformance & parameters

Wrapper supports **OpenAI + Anthropic wire format**:

| Format | Endpoint | Notes |
|--------|----------|-------|
| OpenAI | `/v1/chat/completions` | full param set, plus NVIDIA extras (`top_k`, `repetition_penalty`, `guided_json`, `guided_regex`, dll) |
| Anthropic | `/v1/messages` | `/v1/messages/count_tokens` count_token utility |

**Defaults**: temperature=1, top_p=1, max_tokens=1024. Override via payload.
**Context window terpanjang**: 131072 token (Llama-3.1 family).

---

## 6. Reliability features (built-in)

| Feature | Value |
|---------|-------|
| Active key pool | 5 NVIDIA API keys (`NVIDIA_API_KEY_1..5`) |
| Hot reload `.env` | `KEYS_RELOAD_SECONDS=60` |
| Model list fresh | `MODEL_REFRESH_SEC=600` (10 min) |
| Soft RPM | 30 req/min/key |
| Hard RPM | 40 req/min/key |
| Queue limit | `QUEUE_LIMIT=4` (cap concurrency, turunkan latency 64%) |
| Load shedding | `LOAD_SHEDDING_ENABLED=true`, `INFLIGHT_SOFT_CAP=50` |
| Restart policy | systemd `Restart=on-failure`, `StartLimitBurst=5` |
| Metrics DB | `metrics.db` (SQLite, 30-day prune) |
| Logs | structured JSON (`WRAPPER_JSON_LOG=true`) → journalctl |
| Persistence | `BEARER_TOKEN` di `.env`, `MAX_CONNECTIONS=1000`, `REQUEST_TIMEOUT_SEC=60` |

---

## 7. Operational runbook

### 7.1 Verify health
```bash
systemctl show nvidia-wrapper.service --property=ActiveEnterTimestamp,MainPID,SubState
curl -s http://127.0.0.1:9100/health | jq '{status, keys: .keys|length, rpm: .rpm}'
```

### 7.2 Trigger config hot-reload (no restart)
- Edit `.env`, simpan. `KEYS_RELOAD_SECONDS=60` akan reload otomatis.

### 7.3 Force model list refresh
- Restart service (systemd) → `await pool.refreshModels(true)` di main() rebuilds cache.
- Atau tunggu ≤10 menit — `setInterval(refresh, MODEL_REFRESH_SEC*1000)`.

### 7.4 Incident recovery
- `POST /admin/heal-in-flight` clears stuck requests.
- `POST /metrics/reset` zero outs counters.
- Cek `journalctl -u nvidia-wrapper.service --since '10 min ago' | jq .` untuk structured logs.

---

## 8. Common pitfalls

1. **404 pada model yang baru ditambah NIM** → cache belum refresh. Tunggu 10 menit atau restart service.
2. **429 setiap lintas soft cap** → cascade ke key berikutnya secara otomatis (atomic reservation). Kalau semuanya 429 → turun ke model berikutnya di Cascade §4.2.
3. **.env truncated** (per INS-2026-06-25) → patch jangan rewrite `.env` utuh; pakai patch snippet. Selalu backup `.env` ke `backups/` sebelum edit.
4. **Image generation returns 404** → benar, NVIDIA NIM ini tidak expose image gen. Pakai Nous Grok via xAI.

---

## 9. Integrasi yang sudah dipakai

| Layer | Pemakaian |
|-------|-----------|
| ILMA/workflow | `ilma_model_router_data/PROVIDER_INTELLIGENCE_MASTER.json` source = `nvidia/*` |
| Subagent | `ilma_claudecode_agent.py` Tier 1 (NVIDIA NIM) priority |
| SOT routing | `ilma_sot_dispatcher.py` pilih `nvidia/*` dulu |
| Coding agent | `provider_kernel.NVIDIA` direct → `_9100/v1/chat/completions` |
| Image router | falls through ke xAI/Nous (NIM bukan image provider) |
| Hermes gateway | openai-compat base_url ditambah |

---

## 10. Quick reference

```
BASE:        http://127.0.0.1:9100/v1
SERVICE:     wrapper-nvidia.service (systemd, enabled, PPID=1)
LISTEN:      0.0.0.0:9100
KEYS:        5 (auto-rotate, transparent, hot-reload 60s)
MODELS:      121 cached
REFRESH:     keys 60s, models 600s
PROCESS:     /usr/bin/node /root/wrapper/nvidia/src/index.js
PROJECT:     /root/wrapper/nvidia
INSTALL:     ./install.sh [--status | --no-restart]
GIT:         git@github.com:lokah1945/wrapper-nvidia.git
```

**Hardening knobs (env vars, all optional)**:

| Var | Default | Description |
|-----|---------|-------------|
| `REQUEST_TIMEOUT_SEC` | 60 | Per-upstream call hard timeout |
| `SERVER_REQUEST_TIMEOUT_MS` | 60000 | Node `server.timeout` |
| `SERVER_KEEPALIVE_TIMEOUT_MS` | 10000 | Node keep-alive socket timeout |
| `SERVER_HEADERS_TIMEOUT_MS` | 15000 | Node headers parse timeout |
| `ANTI_SILENCE_TIMEOUT_MS` | 45000 | Watchdog: 504 if no response body started |
| `PACING_MAX_WAIT` | 30 | Max seconds `acquireSlot` waits for a key ticket |

---

## 11. Anti-stall invariants (guaranteed post-audit-2026-06-30)

1. **No silent hang > 45 s** — `ANTI_SILENCE_TIMEOUT_MS` watchdog fires 504
   if `res.writeHead()` hasn't been called.
2. **No zombie sockets after client abort** — `req.clientAbortSignal` propagates
   to upstream undici fetch via `AbortSignal.any([...])`.
3. **No double-release of keys** — every `release*()` path uses a one-shot
   guard (`streamReleased`, `ctStreamReleased`).
4. **No DB corruption on shutdown** — `metrics._save()` is coalesced with
   `_saveInFlight` flag; ENOENT on rename is logged but treated benign.
5. **systemd owns the lifecycle** — wrapper is always restarted by systemd
   within 5 s of any unexpected exit; orphan processes (PPID ≠ 1) on port
   9100 are killed automatically by `install.sh` before restart.
6. **Single canonical service unit** — duplicate `nvidia-wrapper.service` was
   removed; only `wrapper-nvidia.service` references the binary.

---

**Maintainer**: ILMA v3.29 (SOT + ClaudeCode-style parallel coding + SuperCoding)
**Last audited**: 2026-06-30 05:36 WIB (full report: `AUDIT_REPORT_2026-06-30.md`)
**Last verified**: 2026-06-30 05:36 WIB
