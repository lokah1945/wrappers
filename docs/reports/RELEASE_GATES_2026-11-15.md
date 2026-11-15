# Release Gates Report — Audit Final Wrappers

Tanggal: 15 November 2026. HEAD: `496c280` (audit/final-audit-20261115). Baseline pembanding: [`WRAPPERS_FINAL_AUDIT_BASELINE.md`](../WRAPPERS_FINAL_AUDIT_BASELINE.md) pada `66ec1c1`. Lingkungan: Linux x86_64, Python 3.12.16, dependency = `tests/constraints-verified.txt` (OpenAI SDK 2.54.0, Anthropic SDK 0.125.0). Scope provider aktif: nvidia-python, nous, openrouter, coarena; registry 9200 dan dashboard 9106 sebagai layanan pendukung; `blackbox/` + `opencode/` OUT_OF_SCOPE_FROZEN (tidak dibaca/diuji/dipatch).

## Ringkasan gate

| Gate | Isi | Hasil | Bukti |
|---|---|---|---|
| G0 | HEAD/scope/instruksi; frozen excluded sebelum akses | **PASS** | `common/wrapper_scope.py` → 4 provider aktif; conftest audit-hook memblokir akses frozen; seluruh suite melaporkan skip frozen dengan jujur |
| G1 | Matriks sumber upstream dan capability aktual | **PASS (dengan batas)** | Matriks upstream di `docs/audits/WRAPPERS_FINAL_AUDIT_2026-11-15.md` §2; sumber resmi diperiksa 15 Nov 2026 (dokumentasi, bukan live) |
| G2 | Contract/auth/model/isolation/error/continuation regression seluruh provider aktif | **PASS** | Unit suite 1366 pass / 0 fail / 105 skip (skip = frozen + integration-marker); runtime E2E 356/0 |
| G3 | Native defaults: tiga client API, opaque fields/SSE, tools/reasoning, unsupported-route policy | **PASS** | `active_production_acceptance.py` 260/260 (65 × 4 provider; sebelumnya 190/5 dengan Coarena tidak tercakup dan 4 failure NVIDIA) |
| G4 | Compatibility layer 1/2/3, discovery ambiguity, adapter negatif | **PASS** | `compat_layer_e2e.py` exit 0 × 4 provider (layer 2 + auto layer 3); `full_matrix_audit.py` 192/192; precedence tetap: `RESPONSES_UPSTREAM_MODE` dan `NATIVE_MESSAGES_MODE` terdokumentasi di `COMPATIBILITY_LAYER.md` |
| G5 | Hot swap hidup: add/remove/replace/zero-key/state-retention/atomic/PID | **PASS** | `hot_swap_acceptance.py` 40/40 (10 skenario × 4 provider, PID tetap, bound reload ≤ 5 dtk); diagnostik pool/parser/watcher/loop 24/24; `WRAPPER_SKIP_DOTENV` TIDAK dipakai pada gate ini |
| G6 | Dependency resolver aktual, clean install, startup/readiness | **PASS** | Semua 6 service resolve individual + gabungan terhadap `tests/constraints-verified.txt` (konflik Coarena uvicorn/pydantic/python-dotenv diperbaiki); dry-run install bersih `ResolutionImpossible` hilang |
| G7 | Resource/performance baseline vs hasil; soak; recovery | **PASS (batas diakui)** | Soak 30 dtk × 4 provider: 0 failure, p95 stabil; key-churn 300 generation × 4 provider bounded (RSS 0–564KB, thread/FD stabil) — `docs/reports/RESOURCE_PROFILE_2026-11-15.md` |
| G8 | OpenAI SDK Chat/Responses + Anthropic SDK Messages, stream/non-stream, multi-turn tools | **PASS** | Dalam acceptance 260/260 (SDK asli, versi tercatat) + `sdk_codex_compat.py` semua wrapper × mode parse clean + full matrix 192/192 |
| G9 | Live upstream credential sah | **BLOCKED/UNVERIFIED** | Tidak ada credential sah pada sesi ini; sesuai instruksi master prompt §2.7 gate ditandai, bukan dipalsukan. Kualifikasi live historis di `wrappers.json` tetap berlaku untuk model/client yang tercatat |
| G10 | Release manifest, rollback drill, staging revision | **PARTIAL** | Manifest gate + bukti lokal lengkap; deploy/rollback drill ke host produksi di luar otorisasi sesi (AGENTS.md: tidak merge/deploy produksi tanpa otorisasi; push branch audit + PR dilakukan sesuai instruksi pemilik) |

## Perbaikan P0/P1 yang mengubah hasil gate

| Temuan baseline | Perbaikan | Bukti delta |
|---|---|---|
| 4 failure streaming gate native NVIDIA (`include_usage` diinjeksi di luar metadata provider) | Injeksi menjadi opt-in `NVIDIA_STREAM_USAGE_TELEMETRY=1`; default native murni; oracle TIDAK dilonggarkan | acceptance NVIDIA 61/65 → 65/65; 2 test unit diubah mengikuti policy baru (`test_plain_chat_stream_usage_telemetry_is_opt_in`) |
| Coarena tidak tercakup acceptance (daftar provider 32–36) | Coarena ditambahkan (port 19407, base `/v1`) | acceptance Coarena 65/65; fleet 260/260 |
| 3 konflik dependency Coarena (uvicorn 0.27.0/pydantic 2.5.0/python-dotenv 1.0.0 vs verified pins) | `coarena/requirements.txt` → `-r ../requirements-runtime.txt` | pip resolver: semua service RESOLVES (gabungan sekalipun) |
| Hot swap PARTIAL/terputus pada 4 provider | `common/env_reload.py` (ownership reconcile, modified/created/moved/deleted, debounce, polling fallback, loop capture thread-safe, LKG korup, zero-key valid) + `sync_keys` state-preserving × 4 pool | 15 temuan FAIL baseline → 24/24 PASS post-fix; live 40/40 |
| NVIDIA callback loop dari thread watchdog gagal (Python 3.10+) | Penjadwalan via loop yang dicapture di `start()` | Diagnostik E_thread_safe_loop_scheduling PASS × 4 |
| `sync_keys([])` NVIDIA mempertahankan pool lama | Zero-key generation valid | Diagnostik B_zero_key_generation PASS × 4; live remove-last-key PASS × 4 |
| Nous rebuild destruktif dari thread observer tanpa lock | sync_keys state-preserving di service loop | live state-retained PASS; unit 13/13 |
| Nous retry exclusion pakai label ordinal | Exclusion berbasis api_key | unit test `test_nous_pool_sync_keys_state_retention` |
| Coarena `/v1/models` tanpa metadata konteks fleet | Enrichment `discovery_limits` (aditif, field native utuh) | acceptance `context-catalog-cache` Coarena PASS |
| `validate_config` NVIDIA menolak `NVIDIA_API_KEY` tanpa `_1` | Menerima pola pool `NVIDIA_API_KEY(_N)?` | boot hot-swap NVIDIA PASS |

## Kegagalan intermiten

Timeout `all-keys-402/stream0` NVIDIA dari run persiapan TIDAK terulang pada 3 run penuh sesi ini (2 × acceptance penuh + 1 ulang khusus). Ditetapkan sebagai kegagalan intermiten lingkungan run persiapan; dipantau, tidak dibuka sebagai blocker.

## Exclusions dan batas yang jujur

- 105 skip unit = provider frozen + marker `integration` (butuh live upstream) — bukan kegagalan.
- Soak 30 detik = smoke-soak; klaim leak-free jangka panjang tidak dibuat.
- Profil 1.000 key sintetis tidak dijalankan; key-churn 300 generation menjadi bukti bounded state.
- G9 live dan G10 deploy drill tetap UNVERIFIED/BLOCKED sesuai otorisasi sesi.

## Versi tercatat

Python 3.12.16; fastapi 0.142.2; uvicorn 0.54.0; aiohttp 3.14.4; pydantic 2.13.5; python-dotenv 1.2.4; watchdog 6.0.0; starlette 1.7.0; mcp 1.30.0; openai 2.54.0; anthropic 0.125.0; pytest 8.4.2; pytest-asyncio 0.26.0 (semua dari `tests/constraints-verified.txt`).
