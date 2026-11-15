# Audit Final End-to-End Wrappers — Hasil dan Perbaikan

Tanggal eksekusi: 15 November 2026. HEAD hasil: `496c280` pada branch `audit/final-audit-20261115` (dasar `66ec1c1` + merge `docs/wrappers-final-audit-20261115` (PR #8) + `docs/coarena-integration-audit-20261115`). Dokumen ini mengeksekusi [`MASTER_PROMPT_WRAPPERS_FINAL_AUDIT.md`](../MASTER_PROMPT_WRAPPERS_FINAL_AUDIT.md) terhadap baseline [`WRAPPERS_FINAL_AUDIT_BASELINE.md`](../WRAPPERS_FINAL_AUDIT_BASELINE.md): audit ulang, perbaikan terukur, pengujian, dan dokumentasi. Gate ringkas: [`RELEASE_GATES_2026-11-15.md`](../reports/RELEASE_GATES_2026-11-15.md); profil resource: [`RESOURCE_PROFILE_2026-11-15.md`](../reports/RESOURCE_PROFILE_2026-11-15.md); runbook hot swap: [`HOT_SWAP_RUNBOOK.md`](../HOT_SWAP_RUNBOOK.md).

## 1. Kesimpulan eksekutif

1. **Konsep wrapper kini seragam pada inti yang benar-benar dipakai runtime.** Empat provider aktif memakai satu layanan reload konfigurasi bersama (`common/env_reload.py`) dan pola pool state-preserving yang identik (`sync_keys` berbasis identitas api_key). Perbedaan tersisa adalah policy upstream yang sah (FIFO/pacing NVIDIA, OAuth-first Nous, soft/hard RPM per dokumentasi upstream, native Messages default Coarena).
2. **Hot swap add/remove/replace/zero-key tanpa restart kini terbukti hidup pada keempat provider** — 40/40 skenario dengan PID tetap dan bound reload ≤ 5 detik; state credential bertahan lintas reload; key retired tidak dipilih lagi. Sebelum audit: 15 temuan FAIL (stale key, rebuild destruktif, watcher hilang, loop scheduling rusak, zero-key ditolak).
3. **Gate native default lulus penuh 260/260** termasuk Coarena yang sebelumnya tidak tercakup dan 4 failure streaming NVIDIA yang root cause-nya (injeksi `stream_options.include_usage`) diperbaiki dengan menjaga kemurnian native sebagai default dan menjadikan telemetri injeksi opt-in operator — oracle tidak dilonggarkan.
4. **Dependency rilis Coarena bersih**: konflik uvicorn/pydantic/python-dotenv terhadap `tests/constraints-verified.txt` dihapus; keenam service resolve individual maupun gabungan.
5. **Yang tidak diklaim**: G9 live upstream dan drill deploy G10 tetap UNVERIFIED/BLOCKED sesuai otorisasi; soak 30 dtk adalah smoke-soak; klaim "production ready" tidak dibuat dari bukti mock.

## 2. Matriks kontrak upstream (G1)

Diperiksa dari sumber publik resmi 15 November 2026 (dokumentasi/OpenAPI; bukan probe live):

| Provider/deployment | API client | Upstream URL/path | Native/translated | Auth & attribution | Limits/error/stream | Sumber + tanggal | Bukti runtime |
|---|---|---|---|---|---|---|---|
| NVIDIA hosted | Chat/Responses/Messages | `integrate.api.nvidia.com` + NIM LLM APIs | Chat native; Responses/Messages via mode compat (default `auto`) | Bearer `NVIDIA_API_KEY` | RPM/key, 429 korroborasi, SSE native | docs.api.nvidia.com/nim/reference/llm-apis — 2026-11-15 | Mock acceptance 65/65; kualifikasi live historis (wrappers.json) untuk model/client tercatat |
| NVIDIA NIM self-hosted | Chat/Responses/Messages/count_tokens | versi image tertentu | Backend/model menentukan; tidak diekstrapolasi ke hosted | Bearer | per deployment | docs.nvidia.com/nim/large-language-models/2.1.5/api-reference.html — 2026-11-15 | Tidak diuji live (UNVERIFIED) |
| Nous | Chat/Messages/Responses | `inference-api.nousresearch.com` base `/v1` | Chat native (`/chat/completions`); Messages/Responses via override mode | Bearer; tag produk/client jujur wajib | RPM/key; 429 Retry-After | portal.nousresearch.com/api/openapi (spec 1.0.0) — 2026-11-15 | Mock acceptance 65/65; OpenAPI tidak mencantumkan Messages/Responses → default runtime memakai adapter terdokumentasi, omission bukan bukti unsupported |
| OpenRouter | Chat/Messages/Responses | `openrouter.ai` | Ketiga wire API didokumentasikan terpisah → route native dipertahankan | Bearer | kuota/key, error shape upstream | openrouter.ai/docs (Messages, Responses, Chat) — 2026-11-15 | Mock acceptance 65/65; matrix 192/192 |
| Coarena | Chat/Messages (native) + Responses | `coarena.ai/api/v1` | Messages native default (upstream Anthropic-compatible); gateway substitution upstream diakui, adapter tidak menjanjikan exact-model di luar yang terverifikasi | Bearer `COARENA_API_KEY` | 30 req/min/key, 2 concurrent, $5/hari (dok. upstream) | coarena.ai/developers + api docs — 2026-11-15 | Mock acceptance 65/65; substitusi gateway tidak diamati pada mock (batas diakui) |

Dukungan akun/model/deployment dapat berubah; matriks dipin pada tanggal di atas.

## 3. Temuan per klausul (sebelum → sesudah)

| ID | Severity | Klausul | Status baseline | Status hasil | Bukti |
|---|---|---|---|---|---|
| FA-01 | P0 | §6 native purity: request tidak ditulis ulang di luar metadata provider (NVIDIA streaming) | PARTIAL (4 failure gate) | **IMPLEMENTED** | oracle `native/stream1` PASS; injeksi jadi opt-in `NVIDIA_STREAM_USAGE_TELEMETRY=1` |
| FA-02 | P0 | §7 hot swap end-to-end | PARTIAL/NOT_IMPLEMENTED (15 FAIL) | **IMPLEMENTED** | 40/40 live; 24/24 diagnostik; runbook |
| FA-03 | P0 | §7.3 state credential & request berjalan | NOT_IMPLEMENTED (nous/openrouter/coarena rebuild destruktif) | **IMPLEMENTED** | sync_keys state-preserving × 4; live state-retained; release retired tidak menurunkan counter pool |
| FA-04 | P0 | §11 dependency install rilis aktual | CONFLICT (3 pin Coarena) | **IMPLEMENTED** | semua service RESOLVES (individual + gabungan) |
| FA-05 | P1 | §6 kualitas gate: metadata menambah provider, harness tidak boleh diam | NOT_IMPLEMENTED (Coarena absen) | **IMPLEMENTED** | acceptance 260/260 termasuk Coarena |
| FA-06 | P1 | §7.2 watcher event coverage & threading | PARTIAL | **IMPLEMENTED** | modified/created/moved(dest)/deleted; debounce; polling fallback; loop capture thread-safe; stop/join |
| FA-07 | P1 | §7.1 rekonsiliasi snapshot milik sumber | NOT_IMPLEMENTED | **IMPLEMENTED** | ownership reconcile: add/change/remove hanya variabel milik file |
| FA-08 | P1 | §7.3 identity stabil + zero-key valid | PARTIAL | **IMPLEMENTED** | identitas = api_key; `sync_keys([])` valid × 4 |
| FA-09 | P1 | §8 retry tidak memakai credential retired/tercakup label ordinal | PARTIAL (nous label) | **IMPLEMENTED** | exclusion api_key |
| FA-10 | P1 | §9 catalog context metadata fleet | PARTIAL (Coarena tanpa enrichment) | **IMPLEMENTED** | `discovery_limits` aditif di `/v1/models` Coarena, field native utuh |
| FA-11 | P2 | §7.1/§11 env file deterministik | PARTIAL | **IMPLEMENTED** | `WRAPPER_ENV_FILE` dihormati boot + runtime × 4 |
| FA-12 | P2 | NVIDIA `validate_config` menolak `NVIDIA_API_KEY` tanpa `_1` | CONFLICT (validator vs pool) | **IMPLEMENTED** | validator menerima pola pool |
| FA-13 | P2 | `base_wrapper.py` docstring klaim watcher bersama tak terbukti | CONFLICT (docstring) | **DIDOKUMENTASIKAN** | watcher bersama kini nyata di `common/env_reload.py`; template diperbarui terpisah (rekomendasi P2 lanjut) |
| FA-14 | — | Timeout intermiten `all-keys-402/stream0` NVIDIA | UNVERIFIED (tidak terulang) | **DIPANTAU** | 3 run penuh tanpa kekambuhan |

Frozen: `blackbox/`, `opencode/` → **OUT_OF_SCOPE_FROZEN** (tidak dibaca/diinspeksi; AGENTS.md mengesampingkan aturan historis lima wrapper).

## 4. Shared core versus adapter (G2/§5)

| Tanggung jawab | Status | Alasan |
|---|---|---|
| Env parsing + rekonsiliasi kepemilikan + watcher + debounce + polling fallback | **SHARED** (`common/env_reload.py`, baru) | Perilaku identik di 4 provider; sebelumnya 3 implementasi berbeda + 1 kosong |
| State-preserving pool reconciliation | **Pola identik per pool** (`sync_keys` NVIDIA/Nous/OpenRouter/Coarena) | KeyEntry atribut beda tipis per provider; extraction penuh ke base class ditunda sampai parity behavioral terkunci (aturan master prompt §5.1: reproduksi dulu, extraction bertahap) |
| Seleksi least-load + RR tiebreak | Sudah seragam | Perbedaan sah: FIFO/pacing NVIDIA, load-shedding opsional OpenRouter/Coarena |
| Auth fail-closed, body guard, SSE primitives, translasi, error classification, context recovery | Sudah shared (`common/`) | Tidak diubah sesi ini |
| Adapter upstream (header attribution Nous, native Messages default Coarena, mode Responses) | **Provider-specific by design** | Kebutuhan upstream terdokumentasi, bukan duplikasi |

Biaya menambah provider berikutnya: satu `key_pool.py` (~300 baris, sebagian besar pola) + main + konfigurasi; runtime reload/watcher/reconcile tidak perlu disalin lagi. Tidak ada klaim penghematan RAM lintas proses dari deduplikasi source (master prompt §5.6 dihormati).

## 5. Compatibility dan precedence (G4)

- Client surface ditentukan route (Chat/Responses/Messages), bukan nama model/SDK — dikunci oracle acceptance pada ketiga surface × 4 provider.
- `COMPATIBILITY_LAYER` 1/2/3 dan `RESPONSES_UPSTREAM_MODE`/`NATIVE_MESSAGES_MODE`: precedence tetap seperti `COMPATIBILITY_LAYER.md` (operator override > discovery; fallback Responses→Chat hanya untuk 404/405/501 definite unsupported-route, 405/501 tetap tanpa inspeksi payload — dibawa sebagai keterbatasan terdokumentasi dengan test pembeda negatif pada 404).
- GPT-named pada Messages dan Claude-named pada Chat/Responses tercakup matrix SDK (192/192) dan acceptance.
- Tidak ada pemilihan jalur dari substring nama model (diperiksa source + fixture matrix).

## 6. Rekomendasi lanjut

| Prioritas | Pekerjaan | Acceptance criteria |
|---|---|---|
| P1 (wajib pra-produksi) | G9: live upstream terbatas dengan credential sah per provider (model/client/fitur tercatat) | Bukti live masuk `wrappers.json` + report gate; tanpa data sensitif |
| P1 | Drill deploy/rollback G10 di host staging sesuai otorisasi | Manifest + rollback terdokumentasi dan tereksekusi |
| P2 | Endpoint status hot-swap per provider (HTTP, tanpa rahasia) | `/stats` menampilkan generation/last_result/last_error |
| P2 | Perpanjang soak (≥ 1 jam) di jendela maintenance | Growth slope dilaporkan; tanpa klaim dari run pendek |
| P2 | Inspeksi payload 405/501 sebelum fallback Responses→Chat (pembeda route-vs-fitur) | Test pembeda + perubahan `common/compat.py` bila upstream mengonfirmasi bentuk error |
| P2 | Extract KeyEntry/pool ke shared base setelah parity behavioral terkunci lama | Contract test lintas provider; env/port/metrics/api publik tidak berubah |
| P2 | Skala 1.000 key sintetis + dokumen batas | Profil terukur, bukan klaim |

## 7. Keterbatasan

Mock upstream tidak membuktikan perilaku upstream nyata (substitusi gateway Coarena, cutout NVIDIA hosted, kuota OpenRouter). Kualifikasi live historis di `wrappers.json` (2026-10-11) tetap berlaku hanya untuk model/client yang tercatat. Branch `arena/019fc50d-wrappers` (31 commit audit historis R5–R37) TIDAK digabungkan: merge-nya memaksa resolusi konflik pada file `blackbox/src/main.py` dan `opencode/src/main.py` — provider frozen yang dilarang AGENTS.md untuk dibaca/diedit; branch dibiarkan utuh sebagai arsip (cabang arena/feat lain sudah tergabung penuh di main).
