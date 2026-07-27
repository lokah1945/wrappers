# ILMA Wrapper Audit Report Index

**Last audit:** 2026-07-28 (Round 3 - Post-restructure)
**Repo HEAD:** `0ffe52c` (github/main, up-to-date)
**Status:** ✅ PRODUCTION-READY (all critical fixes verified, 1 minor test issue)

## Reports — E2E Audit 2026-07-27

| File | Date | Scope | Key findings |
|------|------|-------|--------------|
| **`AUDIT_E2E_KOMPREHENSIF_2026-07-27.md`** | 2026-07-27 14:45 | **LAPORAN INDUK konsolidasi 5 audit paralel** — baca ini dulu | Jawaban 2 pertanyaan Bos: (1) YA ada overwrite thinking/reasoning/effort — nvidia inject `chat_template_kwargs`/`reasoning_effort`, GLM client intent di-override; (2) root cause latency = nvidia key-pool pacing `QUEUE_LIMIT=4` (0.25s/gelombang, terukur +0.3–0.7s saat concurrent) + SQLite write di hot path. Rekomendasi patch Paket A–D menunggu persetujuan. |
| `parts/2026-07-27_part_transparency.md` | 2026-07-27 13:40 | **Audit transparansi E2E semua 4 wrapper + common** — semua mutasi request/response/header/SSE, per file:line | ❌ TIDAK ADA wrapper yang transparan byte-level. CRITICAL: nvidia inject `chat_template_kwargs`/`reasoning_effort` (V1-V2); GLM thinking client DI-OVERRIDE ke false (V3, regresi 0781634); reasoning disajikan sbg content di plain chat (V15); max_tokens di-floor/clamp/default di semua wrapper (N19/N20/V8); param sampling dibuang saat translasi (O23/B21); riwayat dipotong diam-diam (V19); konten fabricated (V25/O22/B20) |
| `parts/2026-07-27_part_latency.md` | 2026-07-27 13:45 | **Audit latency wrapper-vs-curl** — statis hot path + pengukuran live (non-stream, streaming TTFB, concurrency, /v1/models) | Root cause TERUKUR: nvidia `key_pool.py` pacing `QUEUE_LIMIT=4` → tangga 250ms saat concurrent (F1); verify-loop makan RPM pool live (F7); SQLite write awaited di hot path 4 wrapper (F3); single request PARITAS (tidak ada penalti wrapper) |
| `parts/2026-07-27_part_opencode_blackbox_common_registry.md` | 2026-07-27 13:20 | **Deep code audit opencode + blackbox + common + model-registry** + tabel drift antar-wrapper | CRITICAL: Mutex cancellation deadlock (OC-1/BB-1, 3 wrapper); HIGH: `/dashboard` bocorkan BEARER_TOKEN tanpa auth (OC-3/BB-4); blackbox BEARER_TOKEN kosong = tanpa auth (BB-3); heartbeat BUG-CODEX2 belum di-port ke opencode/blackbox (DR-1); circuit breaker mati di 3 wrapper (DR-2); registry DB path drift (MR-1); 13 drift antar-wrapper |
| `parts/2026-07-27_part_infra_runtime.md` | 2026-07-27 13:42 | **Audit infra/runtime/tests/git/docs** — systemd, port, health, log, gitignore, secret scan, install.sh | HIGH: model-registry tolak 2.504 write internal (503, unit tanpa `EnvironmentFile=`) → `providers_loaded=[]`; nvidia-python running stale tanpa fix-nya sendiri; blackbox `.env` 0.0.0.0:9108 bahaya run manual; `runtime/*.commit` git-tracked = tak andal; tidak ada secret nyata di tree; README "100/100" stale |
| `parts/2026-07-27_part_nous_nvidia.md` | 2026-07-27 14:44 | **Deep code audit nous + nvidia-python (bit-level)** — semua file src dibaca penuh; verifikasi fix f5bfeea/68c1d47/21d0f79 | ✅ Ketiga fix terverifikasi PRESENT & complete. CRITICAL: V-01 Mutex nvidia key_pool deadlock on cancel. HIGH: V-02 `split('/')` ValueError → per-key model limit tak pernah expired (key di-skip selamanya utk model setelah 1×429); V-03 ghost ticket `_waiting` → backpressure shed semua traffic; N-01 network error post_nous leak in-flight slot + 500; N-02 `_RESPONSE_STORE` nous unbounded (sibling bug yg sudah difix di nvidia); V-04 Loki 401 retry storm SEDANG TERJADI di log; V-05 `resp.release()` missing di `_stream_proxy`; N-03/V-06 dashboard token leak + nvidia default bind 0.0.0.0 |
| `AUDIT_COMPREHENSIVE_2026-07-27T11-50.md` | 2026-07-27 11:50 | **Full audit: git/services/ports/health/config/creds + nous-hang + nvidia-500 verification + next-agent playbook** | ✅ nous Codex hang FIXED (f5bfeea verified); ✅ nvidia 500 FIXED (ILMA local patch); ⚠️ blackbox port mismatch 9108≠9104; ⚠️ legacy 9100 in codex config; ⚠️ `.deployed_commit` stale |
| `AUDIT_COMPREHENSIVE_2026-07-27.md` | 2026-07-27 05:12 | Code-level + client-compat audit | B1 memory leak, B2 registry dead, B3 nvidia circuit_open (historical) |
| `AUDIT_REBUILD_2026-07-27T04-37.md` | 2026-07-27 04:37 | Runtime probe + stale-version | Runtime stale, model-registry empty |
| `AUDIT_REBUILD_2026-07-27.md` | 2026-07-27 | Rebuild audit round 1 | Initial findings |
| `ILMA_AUDIT_2026-07-27_WRAPPER_PROD_READINESS.md` | 2026-07-27 | Prod-readiness | Gate evaluation |
| `AUDIT_WRAPPER_GLM_LATENCY_2026-07-27.md` | 2026-07-27 05:36 | GLM thinking latency | opt_out_default_thinking fix |

## Reported-Bug Status (Bos 2026-07-27)

| Bug | Status | Evidence |
|-----|--------|----------|
| **wrapper-nous hang in Codex** (process stops before final response) | ✅ FIXED | f5bfeea (BUG-CODEX1+2). Log post-fix: 0 errors. Reproduced `/v1/responses` stream → HTTP 200, 4.8s, complete. |
| **wrapper-nvidia 500 in Claude Code** | ✅ FIXED (local, uncommitted) | `NameError: sanitize_header_value` → patched `nvidia-python/src/main.py`. HTTP 500 gone. Remaining 400/404 = upstream NVIDIA model availability. |

## Current Service/Port Map (VERIFY BEFORE USE)

| Service | Port | Auth | Client |
|---------|------|------|--------|
| wrapper-nous | **9102** | `Bearer wrapper-local-key` | Codex CLI (`/v1/responses`) |
| wrapper-nvidia-python | **9101** | `Bearer wrapper-local-key` | Claude Code/Codex nvidia calls |
| wrapper-opencode | **9103** | `Bearer wrapper-local-key` | OpenCode |
| wrapper-blackbox | **9104** | `Bearer wrapper-local-key` | BLACKBOX AI |
| wrapper-model-registry | **9200** | admin token | model intelligence |
| ~~wrapper-nvidia (legacy 9100)~~ | 9100 | ❌ REMOVED | do NOT use |

## Critical Findings (this audit)

1. **F1 MED** — blackbox `.env LISTEN_PORT=9108` but service runs on **9104** (CLI override). 9108 closed. Misleading, not fatal.
2. **F2 LOW** — `/root/.codex/config.openrouter-nemotron.toml:14` → `base_url=http://127.0.0.1:9100/v1` (legacy, removed). Will fail to connect.
3. **F3 LOW** — `.deployed_commit=62307eb` stale vs HEAD `92893e7`.
4. **F4 LOW** — Uncommitted: `nvidia-python/src/main.py` (ILMA 500 fix) + 2 runtime pointers. Commit before any `git checkout`/`reset`.
5. **F5 INFO** — Bos memory "wrapper-nvidia:9100" OBSOLETE → actual is `wrapper-nvidia-python:9101`.

## Action Items for Next Agent

- [ ] **nous hang recurs?** → check `nous/wrapper_nous.log` for `store_conversation`/`stream_with_heartbeat` traceback. Fix already in f5bfeea.
- [ ] **nvidia 500 recurs?** → ILMA fix uncommitted (§8 in AUDIT_COMPREHENSIVE_2026-07-27T11-50.md). Re-apply + restart `wrapper-nvidia-python.service`.
- [ ] **Commit nvidia fix** (Bos approval): `git add nvidia-python/src/main.py && git commit && git push github main`. PULL FROM github ONLY.
- [ ] **Fix blackbox port**: edit `blackbox/.env LISTEN_PORT=9104` (or service to 9108) for consistency.
- [ ] **Fix legacy 9100**: update `/root/.codex/config.openrouter-nemotron.toml` to 9101/9102.
- [ ] **Sync `.deployed_commit`**: `echo $(git rev-parse HEAD) > .deployed_commit`.

## Client-Compat Status (verified 11:50)

| Client | Endpoint | Status |
|--------|----------|--------|
| Codex CLI | `/v1/responses` (9102) | ✅ HTTP 200, complete stream |
| Claude Code | `/v1/messages` (9101) | ✅ 500 fixed, flows to upstream |
| Hermes Agent | `/v1/chat/completions` | ✅ |
| OpenClaw | all | ✅ |

## Deep-Dive Audit (2026-07-27)

### New Comprehensive Audit Report Added

| File | Description |
|------|-------------|
| `COMPREHENSIVE_AUDIT_2026-07-27_DEEP_DIVE.md` | Full deep-dive analysis with 28 findings (3 critical, 12 high, 15 medium, 8 low) |
| `findings.json` | Machine-readable JSON of all findings |

### Critical Findings from Deep-Dive

1. **CRIT-1: Mutex Cancellation Deadlock** - opencode/blackbox key_pool.py can hang entirely under load
2. **CRIT-2: Dashboard Token Exposure** - `/dashboard` leaks BEARER_TOKEN without auth
3. **CRIT-3: Empty Bearer Token** - blackbox runs unauthenticated

### High Severity Findings

4. **HIGH-1: AliasResolver Scope-Skip Bug** - Alias resolution silently fails
5. **HIGH-2: Streaming Response Leak** - Connections leak on call-plan rejection
6. **HIGH-3: NVIDIA GLM Client Intent Override** - thinking parameter incorrectly inverted
7. **HIGH-4: Circuit Breaker Never Opens** - record_success/failure never called
8. **HIGH-5: Model Registry DB Path Drift** - Service uses empty DB instead of root DB
9. **HIGH-6: Rate Limiter Spoofable** - Uses X-Forwarded-For, allows bypass
10. **HIGH-7: SQLite Write on Hot Path** - 1-10ms TTFB added to every request
11. **HIGH-8: Verify Loop RPM Consumption** - Verification competes with live traffic
12. **HIGH-9-13: Cross-Wrapper Drift** - Inconsistent implementations across wrappers

- [AUDIT COMPREHENSIVE 2026-07-28 ROUND3](AUDIT_COMPREHENSIVE_2026-07-28_ROUND3.md) — Latest audit after restructure, 1 minor test issue found
