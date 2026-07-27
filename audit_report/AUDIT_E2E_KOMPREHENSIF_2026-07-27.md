# AUDIT E2E KOMPREHENSIF — Proyek Wrapper — 2026-07-27

**Mode:** REPORT-ONLY (tidak ada file project diubah/dieksekusi; tidak ada service di-restart; fix menunggu persetujuan Bos)
**Repo:** `/root/wrapper` @ HEAD `21d0f79` (sinkron `github/main` — di-pull ulang sebelum audit)
**Metode:** 5 audit paralel independen — (1) transparansi, (2) latency statis+live, (3) deep code nous+nvidia-python, (4) deep code opencode+blackbox+common+registry, (5) infra/runtime/tests/git — lalu dikonsolidasi.

**Laporan detail (file:line lengkap) ada di `parts/`:**
- `parts/2026-07-27_part_transparency.md` — 105 titik mutasi request/response, 4 wrapper + common
- `parts/2026-07-27_part_latency.md` — analisa statis hot path + pengukuran live curl direct-vs-wrapper
- `parts/2026-07-27_part_opencode_blackbox_common_registry.md` — 40+ temuan + 13 drift antar-wrapper
- `parts/2026-07-27_part_nous_nvidia.md` — deep audit nous + nvidia-python
- `parts/2026-07-27_part_infra_runtime.md` — 16 temuan infra/runtime/git/docs

---

## RINGKASAN EKSEKUTIF

### 1. Jawaban pertanyaan Bos #1 — "Ada overwrite thinking/reasoning/effort?"

**YA — terbukti, terutama di wrapper-nvidia.** Klaim "mode transparent" TIDAK terpenuhi di kondisi sekarang:

| Temuan | Lokasi | Dampak |
|---|---|---|
| Inject `chat_template_kwargs {enable_thinking: true, thinking: true}` otomatis untuk model deepseek-r1/-v4/`-reasoning` saat client TIDAK minta | nvidia `main.py:730-751` (V1) | Model berpikir padahal tidak diminta → latency & output berubah |
| Inject `reasoning_effort: 'high'/'low'` untuk gpt-oss/kimi/mistral/nemotron saat translasi thinking | nvidia `main.py:697-727` (V2) | Effort client di-overwrite |
| **GLM: client yang eksplisit minta thinking DIPAKSA `thinking: false`** | nvidia `main.py:634 + 712-716` (V3) | Regresi commit `0781634` — kebalikan dari niat komentarnya sendiri |
| Reasoning model DISAJIKAN sebagai jawaban (content) di plain `/v1/chat/completions` | nvidia `main.py:1110-1122` (V15, commit 0781634) | Mutasi respons bahkan di jalur passthrough |
| Thinking block PALSU "[Reasoning not supported…]" disuntik sebagai output model | nvidia `anthropic_compat.py:513-514, 636-660` (V25) | Konten fabricated |
| `max_tokens` client di-overwrite: nous floor ≥1024 + default 4096; nvidia silent-clamp per model + rename `max_completion_tokens` | nous `:812-813, :919`; nvidia `:996-1040, :2068-2073` | Nilai client tidak dihormati |
| Param sampling client DIBUANG diam-diam saat translasi `/v1/messages` (temperature/top_p/top_k/stop_sequences/tool_choice) | opencode `:574-634` (O23); blackbox `:462-526` (B21) | Perilaku model beda dari yang diminta client |
| Riwayat percakapan DIPOTONG diam-diam bila estimasi token > window | nvidia `anthropic_compat.py:171-192` (V19) | Kehilangan konteks tak terlihat |
| nvidia hapus param tanpa syarat dari SEMUA body (`max_output_tokens`, `context_*`, `think`, dll) | nvidia `:673-674, :1104-1107` (V4) | Termasuk plain chat |
| `stream_options {include_usage:true}` di-inject ke semua streaming request | nvidia `:2072-2073` (V7) | Termasuk plain chat |

**Kesimpulan struktural:** tidak ada satupun endpoint yang meneruskan bytes mentah (semua `json.loads`→re-serialize, semua header upstream dibuang, hanya 4–8 header client diteruskan). `/v1/messages` & `/v1/responses` adalah **emulator penuh** (semua upstream hanya punya OpenAI-chat), bukan proxy. `WRAPPER_CONTRACT.md` memang melisensikan translasi ini — tapi V3/V15/V19 bahkan melanggar kontrak internal itu sendiri. Daftar lengkap 105 titik mutasi: `parts/2026-07-27_part_transparency.md`.

### 2. Jawaban pertanyaan Bos #2 — "Bug latency wrapper vs curl"

**ROOT CAUSE DITEMUKAN & TERUKUR** (detail: `parts/2026-07-27_part_latency.md`):

| Rank | Penyebab | Bukti |
|---|---|---|
| **#1 (HIGH confidence)** | **nvidia key-pool pacing sleep**: `QUEUE_LIMIT=4` → `_admit_interval=0.25s` (`key_pool.py:446-595`). Setiap burst request > jumlah key siap di-delay per "gelombang" 250 ms | Terukur: 12-concurrent +0.3s, 24-concurrent +0.7s (pola tangga 250ms persis); direct curl FLAT ~0.2s |
| #2 (MED) | Verify-loop background (600s, ~100 model × 6 akun probe nyata) memakan RPM key pool yang sama → pacing #1 aktif lebih cepat | 664 sweep di log |
| #3 (MED) | Tulis SQLite (model-state/metrics) di-`await` di hot path SEBELUM respons dikirim — semua 4 wrapper | ~1–10 ms/request di window non-throttle |
| #4 (kondisional) | Auto-inject thinking (V1/F6) menambah latency sisi model untuk model reasoning | Kode sendiri mendokumentasikan kasus GLM 4–5s |
| #5 (LOW) | opencode/blackbox paksa `Accept-Encoding: identity` → respons besar tanpa kompresi | Hanya respons besar |

**Penting:** single request terisolasi TIDAK ada penalti wrapper (TTFB streaming paritas; `/v1/models` malah lebih cepat via wrapper karena cache). Perlambatan yang Bos rasakan adalah **efek concurrency/pacing** — agent selalu mengirim burst, dan pacing nvidia menahannya; curl tunggal tidak.

Infrastruktur dasarnya sehat: session aiohttp shared + pooled, streaming chunk-by-chunk tanpa buffering, `X-Accel-Buffering: no`. Bukan masalah arsitektur, tapi 2–3 keputusan konfigurasi/hot-path.

### 3. Temuan CRITICAL/HIGH lain (butuh keputusan Bos)

| ID | Sev | Temuan |
|---|---|---|
| OC-1/BB-1/DR-3 | **CRITICAL** | `Mutex` hand-rolled di key_pool (opencode, blackbox, nvidia) TIDAK cancellation-safe → satu client disconnect saat antri lock bisa **menggantung semua request selamanya** |
| OC-3/BB-4 | HIGH | `/dashboard` TANPA auth dan MENYISIPKAN plaintext `BEARER_TOKEN` di HTML |
| BB-3 | HIGH | blackbox live dengan `BEARER_TOKEN` KOSONG = semua endpoint tanpa auth (hanya dilindungi bind loopback) |
| INF-F1 | HIGH | **model-registry menolak 100% telemetry sejak start** (2.504 × 503): unit systemd-nya satu-satunya tanpa `EnvironmentFile=` → admin token tak terbaca → `providers_loaded: []` |
| INF-F2 | HIGH | **nvidia-python masih running kode stale `92893e7`** — fix NameError `68c1d47` ada di disk tapi BELUM live (proses start 11:30, commit 12:05, tidak pernah restart) |
| DR-1 | HIGH | Heartbeat BUG-CODEX2: fix `asyncio.wait_for` hanya ada di nous; opencode/blackbox masih pola lama (policy sendiri bilang "needs fix") — generator translasi malah tanpa heartbeat sama sekali |
| DR-2 | MED | Circuit breaker MATI di 3 dari 4 wrapper (`record_failure/success` tidak pernah dipanggil) |
| OC-2/BB-2 | HIGH | Leak koneksi streaming saat call-plan rejection (tanpa `resp.release()`) |
| CM-1 | HIGH | `AliasResolver.resolve` bug scope-skip — binding registry tak pernah ketemu |
| MR-1 | HIGH | Registry DB path drift: DB 2,5 MB orphan di root repo; service pakai path kosong |
| V-02 | HIGH | nvidia `key_pool.py:928-933`: `km.split('/')` ValueError karena model ID mengandung `/` → per-key model limit TAK PERNAH expired → key di-skip selamanya untuk model itu setelah satu 429 (terkonfirmasi via eksekusi pola) |
| V-03 | HIGH | nvidia `_acquire_slot`: ghost ticket menumpuk di `_waiting` saat client cancel → backpressure `max_queue_size=500` akhirnya menolak SEMUA traffic + starvasi rank pacing |
| N-01 | HIGH | nous `post_nous`: network error tidak dibungkus → leak in-flight slot key permanen + 500 mentah ke client |
| N-02 | HIGH | nous `_RESPONSE_STORE` unbounded (sibling bug yang SUDAH difix di nvidia `responses_compat._bounded_store`) — memory leak percakapan |
| V-04 | HIGH | **Loki 401 retry storm SEDANG BERLANGSUNG** di `nvidia_py.log` — batch tak dibatasi, auth Loki gagal terus |
| V-05 | HIGH | nvidia `_stream_proxy` tanpa `resp.release()` (fix-nya sudah ada di `stream_wrapper` — sibling instance terlewat) |

**Verifikasi fix terbaru (parts/nous_nvidia §0):** `f5bfeea` (Codex hang), `68c1d47` & `21d0f79` (sanitize NameError) — ketiganya **PRESENT & complete** di HEAD. Risiko residual adalah sibling-pattern di atas, bukan regresi fix-nya.

### 4. Status runtime vs repo

| Service | Port | Running commit | Verdict |
|---|---|---|---|
| nous | 9102 | `21d0f79` | ✅ current |
| opencode | 9103 | `21d0f79` | ✅ current |
| blackbox | 9104 | `21d0f79` | ✅ current (`.env` drift 9108/0.0.0.0 tetap bahaya untuk run manual) |
| nvidia-python | 9101 | `92893e7` | ❌ STALE — fix sendiri belum live |
| model-registry | 9200 | `1ad8845` | ❌ STALE + telemetry mati (INF-F1) |

`runtime/*.commit` git-tracked → selalu ter-clobber oleh git pull; satu-satunya sinyal andal adalah `/health.git_commit`. `.deployed_commit` juga stale. Git hygiene: tidak ada secret nyata di tree; `metrics-snapshot.json` (2 file) perlu masuk `.gitignore`.

---

## REKOMENDASI PATCH (MENUNGGU PERSETUJUAN — belum ada yang diterapkan)

**Paket A — Latency (keluhan utama):**
1. nvidia `key_pool.py`: hilangkan/naikkan drastis pacing `QUEUE_LIMIT` (mis. 100+) atau gate `admit_ok` di belakang `rpm_ok` yang sudah ada
2. Pindahkan semua `await` tulis SQLite/metrics ke background task (fire-and-forget) di 4 wrapper
3. Pisahkan verify-loop dari kuota RPM key pool live / turunkan cadence

**Paket B — Transparansi (keluhan utama):**
4. nvidia: hapus `apply_default_reasoning` auto-inject (V1); perbaiki GLM override (V3) agar hormati niat client; matikan/opsi-kan `ensure_nonempty_content` reasoning-as-content (V15); hentikan hapus-param tanpa syarat (V4), rename `max_completion_tokens` (V6), inject `stream_options` (V7), silent clamp (V8)
5. nous: hapus floor `max_tokens` ≥1024 & default 4096 (N19/N20); hapus delete-param chat (N3); perluas whitelist param translasi (N21)
6. opencode/blackbox: teruskan temperature/top_p/top_k/stop_sequences/tool_choice di translasi `/v1/messages` (O23/B21)
7. nvidia: hapus/opsi-kan silent history truncation (V19) — kembalikan 400 ke client, jangan potong diam-diam

**Paket C — Stabilitas & keamanan:**
8. Ganti `Mutex` → `asyncio.Lock` di 3 key_pool (CRITICAL)
9. Auth `/dashboard` + stop embed token; isi blackbox `BEARER_TOKEN`
10. Port heartbeat `asyncio.wait_for` ke opencode/blackbox/nvidia (DR-1); wire circuit breaker (DR-2)
11. Fix leak `resp.release()` di call-plan rejection (OC-2/BB-2); pindah cek call-plan SEBELUM call upstream
12. Tambah `EnvironmentFile=` di unit model-registry (INF-F1) + restart registry & nvidia-python (INF-F2) — butuh restart window

**Paket D — Hygiene:**
13. `.gitignore`: + `metrics-snapshot.json`, + `runtime/`; perbaiki blackbox `.env` (127.0.0.1:9104); hapus `.deployed_commit` atau otomasi; update README "100/100" yang stale; hapus profile Codex port 9100 mati; fix `JSONDecodeError` → 400 (nous/opencode)

Instruksikan paket mana yang disetujui, akan saya kerjakan berurutan dengan verifikasi per langkah.
