# Baseline Audit Menyeluruh Wrappers

Tanggal: 15 November 2026. Repository: <https://github.com/lokah1945/wrappers>. HEAD yang diuji: [`66ec1c188face2ace6cb3ee6e2f2d1ae3d44ebd4`](https://github.com/lokah1945/wrappers/commit/66ec1c188face2ace6cb3ee6e2f2d1ae3d44ebd4).

Dokumen ini mendasari [`MASTER_PROMPT_WRAPPERS_FINAL_AUDIT.md`](MASTER_PROMPT_WRAPPERS_FINAL_AUDIT.md). Audit persiapan membaca sumber terbaru, membandingkan dokumen resmi upstream, dan menjalankan test terisolasi. Tidak ada perbaikan kode runtime, inferensi bercredential ke upstream, deployment, atau klaim kualifikasi live yang dilakukan.

## Kesimpulan

Konsep dasar provider aktif sama: menerima client APIs, memeriksa autentikasi/policy, memilih credential, memanggil upstream, meneruskan native response atau adapter yang diizinkan, mencatat outcome, dan menutup resource. Kode aktual belum sepenuhnya terseragamkan. Ada duplikasi yang layak diekstrak serta perbedaan operasional yang tidak dapat dijelaskan hanya oleh kebutuhan upstream.

Hot swap add/remove key tanpa restart belum dapat dinyatakan benar pada seluruh provider. Adanya `load_from_env`, import watchdog, atau dokumentasi “hot reload” tidak cukup. Diagnostik menemukan stale key setelah baris file dihapus, state reset saat pool dibangun ulang, tidak adanya jalur watcher runtime pada OpenRouter/Coarena, dan masalah callback/atomic replace pada watcher lainnya.

Compatibility regression dan matriks adapter lulus pada scope yang diuji. Gate native production justru menemukan kegagalan dan coverage gap. Selain itu, dependency Coarena bertentangan dengan release constraints. Karena itu, fleet belum layak diberi kesimpulan production ready dari evidence persiapan ini.

## Scope dan metode

`AGENTS.md` dan `wrappers.json` diperiksa lebih dahulu. Selector menghasilkan empat provider inference aktif: NVIDIA, Nous, OpenRouter, Coarena. Model-registry dan unified dashboard diperiksa sebagai layanan pendukung. Implementasi `blackbox/` dan `opencode/` yang frozen tidak dibaca atau diuji; hasil skip dilaporkan.

Sumber diambil pada SHA tetap melalui GitHub, mencakup shared modules, provider aktif, tests, deployment helpers, dan dokumen terkait. Snapshot lokal hanya menyertakan sumber yang diperlukan untuk audit; bukan checkout production. Kekurangan empat file pada setup test awal diselesaikan dengan mengambil file dari SHA yang sama, kemudian test yang terdampak dijalankan ulang. Kegagalan setup tersebut tidak diklasifikasikan sebagai bug repository.

Test menggunakan fake keys, temporary state, mock upstream loopback, port test khusus dan credential/state isolation bawaan repository. Pool/parser/handler tertentu dieksekusi dari AST untuk membatasi side effects; bukti itu dibedakan dari test service/socket/SDK. Tidak ada service production yang disentuh.

## Hasil pengujian aktual

| Pemeriksaan | Hasil | Batas klaim |
|---|---|---|
| Selected pytest: 16 file | 421 passed, 38 skipped, 1 warning; exit 0 | Bukan full suite; skips berkaitan freeze/mixed-provider scope |
| `compat_layer_e2e.py` | Exit 0; layer 2 dan auto layer 3 berjalan pada empat provider aktif | Fixture memakai Responses Chat adapter secara eksplisit |
| `full_matrix_audit.py` | 192/192 passed; 0 failed/blocked; exit 0 | SDK sungguhan terhadap mock; layer 1 sengaja memakai Messages translation dan Responses Chat adapter |
| `active_production_acceptance.py` | 190 passed, 5 failed; exit 1 | Scope harness hanya NVIDIA, Nous, OpenRouter; Coarena tidak tercakup |
| Native acceptance ulang khusus NVIDIA | 61 passed, 4 failed; exit 1 | Empat failure streaming berulang; timeout all-keys-402 tidak terulang |
| Hot-swap diagnostics | 14 observasi pool/parser/handler/source | Menemukan gap; bukan bukti hot swap service hidup telah lulus |
| Dependency consistency | 3 direct-pin conflicts pada Coarena | Terbukti secara specifier; clean release install belum lulus |
| Duplikasi source | 28 kelompok fungsi AST Coarena/OpenRouter cocok setelah normalisasi nama provider | Kandidat extraction, bukan jaminan fungsi interchangeable atau penghematan RAM |
| Profil resource/soak lengkap | NOT RUN | Tidak ada persentase penghematan CPU/RAM atau klaim leak-free |
| Upstream live/client CLI/deploy | NOT RUN | Native upstream availability dan production rollout tetap UNVERIFIED |

OpenAI SDK `2.54.0` dan Anthropic SDK `0.125.0` digunakan pada suite SDK mock. Environment test bukan clean environment release constraints; rincian package dan evidence ada dalam [`artifacts/fleet_validation_summary.json`](artifacts/fleet_validation_summary.json).

### Lima kegagalan gate native

| Check NVIDIA | Hasil awal |
|---|---|
| Layer 1/context-recovery/stream1 | AssertionError tanpa detail tambahan |
| Layer 1/context-input-overflow/stream1 | AssertionError tanpa detail tambahan |
| Layer 1/context-rotation/stream1 | AssertionError tanpa detail tambahan |
| Layer 1/native/stream1 | Request ditambah `stream_options.include_usage=true`; oracle native menganggap perubahan di luar provider metadata |
| Layer 1/all-keys-402/stream0 | TimeoutError pada request client gate |

Ini adalah failure gate yang benar-benar teramati. Root cause seluruhnya belum ditetapkan. Perubahan `include_usage` dapat ditelusuri ke `nvidia-python/src/main.py:4247–4257`; penilaiannya harus menyelaraskan policy native, telemetry, dan oracle test, bukan menghapus assertion agar hijau. Pengulangan khusus NVIDIA menghasilkan empat failure streaming yang sama; timeout exhaustion tidak terulang, sehingga harus dicatat sebagai kegagalan intermiten yang masih perlu diselidiki. Detail kedua run ada dalam validation summary.

Coverage native gate harus diperbaiki: daftar provider pada `active_production_acceptance.py:32–36` hanya berisi tiga provider. Filter metadata setelah dictionary tidak otomatis menambahkan Coarena yang tidak ada dalam dictionary tersebut.

## Apakah COMPATIBILITY_LAYER sudah dipatuhi?

Keempat provider aktif memakai helper compatibility/validasi dan memiliki jalur konversi yang lolos fixture lokal. NVIDIA menerapkan compatibility melalui Server/routes, sedangkan Nous/OpenRouter/Coarena mempunyai native Messages override selain dialek umum. Native Responses diatur melalui `RESPONSES_UPSTREAM_MODE`.

Kesimpulan yang tepat adalah **compatibility adapter teruji sebagian, full native production compliance belum lulus**, bukan sekadar “semua wrapper sudah sama”.

Temuan penting:

1. `COMPATIBILITY_LAYER` adalah scalar OpenAI/Anthropic/discovery, bukan peta capability per route. Upstream yang mendukung beberapa API perlu native route policy yang explicit.
2. `NATIVE_MESSAGES_MODE` dapat mengubah pilihan Messages secara independen. Precedence terhadap setting operator dan capability profile harus terdokumentasi serta diuji.
3. Helper `common/compat.py:85–90` menerima 405/501 sebagai syarat Responses fallback tanpa memeriksa payload. Perlu test pembeda missing route versus fitur yang tidak diimplementasikan pada route yang tersedia.
4. Fixture matrix memaksa beberapa adapter legacy. Hasil 192/192 tidak membuktikan default native route bebas regresi.
5. Client API harus ditentukan route, bukan nama model. Chat, Responses, dan Messages tetap wire API berbeda walaupun model/vendor sama.

## Hot swap API key: hasil per provider

| Provider | Jalur aktual baseline | Gap yang dibuktikan | Status |
|---|---|---|---|
| NVIDIA | Watchdog `on_modified` → dotenv → callback → async `sync_keys` | Deleted assignment tetap di process env; callback di thread tanpa loop tidak menjalankan sync; moved event tidak diproses; `sync_keys([])` mempertahankan pool lama | PARTIAL, belum memenuhi add/remove tanpa restart |
| Nous | Watchdog `on_modified` → custom dotenv → `KEY_POOL.load_from_env` | Deleted assignment tetap ada; moved event tidak memicu reload; rebuild membuat object baru dan menghapus in-flight/cooldown/counters retained key | PARTIAL, state safety belum benar |
| OpenRouter | dotenv dibaca saat import; pool di-load saat lifespan startup | Dalam main tidak ada watcher/scheduler reload pool setelah startup; direct rebuild juga mereset state | Runtime hot swap belum terbukti/terhubung |
| Coarena | dotenv dibaca saat import; pool di-load saat lifespan startup | Dalam main tidak ada watcher/scheduler reload pool setelah startup; direct rebuild juga mereset state | Runtime hot swap belum terbukti/terhubung |

Evidence rinci: [`artifacts/fleet_snapshot_diagnostics.json`](artifacts/fleet_snapshot_diagnostics.json); reproducer: [`artifacts/verify_fleet_audit.py`](artifacts/verify_fleet_audit.py).

### Apa yang diuji dan apa yang belum

- Pool refresh langsung menambahkan fake key kedua pada semua provider. NVIDIA `sync_keys(nonempty)` mempertahankan object/in-flight/cooldown retained key. Nous/OpenRouter/Coarena membuat object baru: in-flight 2 menjadi 0, cooldown hilang, counter 7 menjadi 0.
- Penghapusan satu assignment dari file, diikuti overwrite reload parser dan actual pool load, masih menghasilkan key lama dalam process env dan pool pada keempat provider. Pada OpenRouter/Coarena refresh ini sengaja dipanggil eksplisit untuk menunjukkan bug parser/reconciliation; itu bukan klaim bahwa runtime sudah memiliki watcher.
- Watcher handler NVIDIA/Nous dieksekusi dengan watchdog modified/moved event. Modified memanggil parse/sync stub; atomic rename/replace event tidak memanggil keduanya. Registrasi observer di-stub, sehingga hasil ini bukan test kernel/filesystem notification menyeluruh.
- Callback NVIDIA yang sebenarnya dieksekusi di background thread tanpa loop asyncio. Tidak terjadi sync; ini cocok dengan konteks thread watchdog biasa. Loop service perlu dicapture saat startup dan dipakai secara thread-safe.
- Tidak ada mock test yang mengubah credential production atau meneruskan key asli ke upstream. Test add/remove service hidup dengan perubahan file dan PID tetap masih menjadi pekerjaan wajib pelaksana.

Mengedit `EnvironmentFile` systemd atau menjalankan `export` di shell lain tidak otomatis memperbarui environment proses yang sudah berjalan. Solusi harus mendefinisikan sumber konfigurasi authoritative dan rekonsiliasi key milik sumber itu. Jangan menghapus semua environment variable sebagai jalan pintas.

## Kesamaan kode dan peluang efisiensi

Ukuran `main.py` snapshot: NVIDIA 5.403 baris, Nous 4.138, OpenRouter 3.745, Coarena 2.226. Tidak ditemukan subclass `BaseWrapper` top-level dalam empat main yang diperiksa. Mereka memang memakai sejumlah shared modules, tetapi dokumentasi “BaseWrapper menangani seluruh pondasi” tidak membuktikan runtime telah menggunakannya.

Perbandingan AST Coarena/OpenRouter menemukan 28 kelompok fungsi berukuran minimal 10 baris dengan isi sama setelah normalisasi string nama provider. Kandidat meliputi session acquisition, background task management, usage parsing, Responses/Anthropic conversion, key selection/failure handling, dan metrics persistence/aggregation. Detail fungsi/lokasi tersimpan dalam JSON diagnostik.

Rekomendasi: extract satu tanggung jawab setelah behavioral parity terbukti; mulai dari key/config reconciliation, ownership transport/lifecycle, converter identik, dan telemetry primitives. Pertahankan policy upstream dalam adapter. Jangan menyalin ulang file main besar untuk provider berikutnya atau memaksakan inheritance sebelum base class siap.

Jumlah baris bukan ukuran biaya runtime. Shared code tetap memiliki object/cache/session sendiri di tiap proses. Audit pelaksana harus mengukur RSS/USS/PSS, CPU/loop lag, tasks/threads/FDs, connections, cache/history/cardinality, disk I/O, proxy latency/TTFT, serta growth saat key churn dan load/soak. Tidak ada klaim penghematan RAM yang dibuat pada baseline ini.

## Dependency dan release blocker

| Coarena requirement | Release constraint | Hasil |
|---|---|---|
| `uvicorn[standard]==0.27.0` | `uvicorn==0.54.0` | Tidak memiliki versi yang memenuhi keduanya |
| `pydantic==2.5.0` | `pydantic==2.13.5` | Tidak memiliki versi yang memenuhi keduanya |
| `python-dotenv==1.0.0` | `python-dotenv==1.2.4` | Tidak memiliki versi yang memenuhi keduanya |

NVIDIA/Nous/OpenRouter mengacu ke shared runtime requirements, sedangkan Coarena membawa pin sendiri. Installer memproses selected requirements dengan release constraints, sehingga keberhasilan test memakai paket yang sudah terpasang tidak membuktikan install rilis aktual berhasil.

Selected test installer lolos dengan requirements fixture sintetis. Perlu gate tambahan terhadap dependency aktual, termasuk include rekursif, di environment bersih. Evidence konflik: [`artifacts/fleet_dependency_conflicts.json`](artifacts/fleet_dependency_conflicts.json).

## Perbandingan upstream resmi

| Provider/deployment | Yang dikonfirmasi dari sumber publik | Implikasi audit |
|---|---|---|
| NVIDIA hosted | Referensi LLM hosted menyebut `integrate.api.nvidia.com` dan Chat Completions [U1] | Jangan mengekstrapolasi route self-hosted ke hosted tanpa bukti |
| NVIDIA NIM container | Dokumentasi versi tertentu mencantumkan Chat, Responses, Messages/count_tokens; backend/model/version menentukan fitur [U2] | Peta deployment capability dan konfigurasi native perlu tepat |
| Nous | OpenAPI publik yang berhasil diambil mencantumkan `/chat/completions` dan `/completions` dengan base `/v1` [U3] | Code default native Messages/Responses memerlukan evidence runtime/dokumen tambahan; omission bukan bukti pasti unsupported |
| OpenRouter | Endpoint Messages dan Responses didokumentasikan secara terpisah [U4][U5] | Pertahankan wire API yang cocok; jangan memaksa Messages/Responses melewati Chat |
| Coarena | Dua SDK base dan route native didokumentasikan; reference juga mencatat gateway substitution/account behavior [U6] | Native boundary tetap diperlukan; exact-model fidelity/account policy harus diverifikasi terpisah |

Dokumentasi upstream tidak mengatur hot swap file service lokal; bagian itu adalah tanggung jawab runtime wrappers. Metadata `supports` atau lifecycle `production` juga bukan bukti kualifikasi seluruh fitur, model, dan client.

## Prioritas untuk agent pelaksana

| Prioritas | Pekerjaan | Acceptance criteria |
|---|---|---|
| P0/P1 | Selidiki dan perbaiki native gate failures dan dependency conflict | Native default test dan clean release resolution lulus dengan policy yang konsisten |
| P1 | Shared config/key reconciliation dan lifecycle watcher | Add/remove/replace/zero-key benar tanpa restart; state retained dan retired key tidak dipilih lagi |
| P1 | Lengkapi native gate scope, negative fallback dan compatibility precedence | Semua provider eligible diuji; native dan adapter gates tidak tertukar |
| P1 | Audit auth/isolation/model identity/continuation/resources pada seluruh provider | Contract matrix berbukti dan tidak ada blocker tersembunyi |
| P1/P2 | Extract duplikasi yang semantik sama | Contract tidak berubah, adapter khusus upstream jelas, implementation lama tidak tertinggal |
| P1/P2 | Profil resource dan soak sebelum/sesudah | Pengukuran reproducible, growth/batas terukur, tidak ada klaim hemat tanpa angka |
| P2 | Koreksi template/README/runbook/CI | Dokumentasi mencerminkan code path, env precedence, hot swap, native support, dan qualification aktual |

Tugas utama tetap dijalankan melalui master prompt: audit, perbaikan, refactoring terukur, testing, dokumentasi, dan handoff. Jangan menyatakan semua provider identik atau production ready hanya karena shared module tersedia atau satu suite hijau.

## Sumber

- [U1 — NVIDIA hosted LLM APIs](https://docs.api.nvidia.com/nim/reference/llm-apis).
- [U2 — NVIDIA NIM 2.1.5 API reference](https://docs.nvidia.com/nim/large-language-models/2.1.5/api-reference.html).
- [U3 — Nous public OpenAPI](https://portal.nousresearch.com/api/openapi), berhasil diambil tanpa credential; spec title Nous Research Inference API, version 1.0.0.
- [U4 — OpenRouter Anthropic Messages](https://openrouter.ai/docs/api/api-reference/anthropic-messages/create-a-message).
- [U5 — OpenRouter Responses](https://openrouter.ai/docs/api/api-reference/responses/create-a-response).
- [U6 — Coarena developer reference](https://coarena.ai/developers).
- Semua sumber publik di atas diperiksa 15 November 2026. Dukungan akun/model/deployment dapat berubah.
- [Repository instructions](https://github.com/lokah1945/wrappers/blob/66ec1c188face2ace6cb3ee6e2f2d1ae3d44ebd4/AGENTS.md), [lifecycle metadata](https://github.com/lokah1945/wrappers/blob/66ec1c188face2ace6cb3ee6e2f2d1ae3d44ebd4/wrappers.json), dan [compatibility helper](https://github.com/lokah1945/wrappers/blob/66ec1c188face2ace6cb3ee6e2f2d1ae3d44ebd4/common/compat.py).
- [NVIDIA watcher/callback](https://github.com/lokah1945/wrappers/blob/66ec1c188face2ace6cb3ee6e2f2d1ae3d44ebd4/nvidia-python/src/main.py#L693), [NVIDIA sync_keys](https://github.com/lokah1945/wrappers/blob/66ec1c188face2ace6cb3ee6e2f2d1ae3d44ebd4/nvidia-python/src/key_pool.py#L681), [Nous loader/watcher](https://github.com/lokah1945/wrappers/blob/66ec1c188face2ace6cb3ee6e2f2d1ae3d44ebd4/nous/src/main.py#L412).
- [OpenRouter lifecycle](https://github.com/lokah1945/wrappers/blob/66ec1c188face2ace6cb3ee6e2f2d1ae3d44ebd4/openrouter/src/main.py#L702), [Coarena lifecycle](https://github.com/lokah1945/wrappers/blob/66ec1c188face2ace6cb3ee6e2f2d1ae3d44ebd4/coarena/src/main.py#L400), [Coarena requirements](https://github.com/lokah1945/wrappers/blob/66ec1c188face2ace6cb3ee6e2f2d1ae3d44ebd4/coarena/requirements.txt).
