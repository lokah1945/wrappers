# MASTER PROMPT — Final Audit End-to-End Wrappers, Efisiensi, Compatibility, dan Hot Swap

Gunakan seluruh dokumen ini sebagai instruksi agent pelaksana. Tugas pelaksana mencakup audit aktual, reproduksi, perbaikan, refactoring terukur, pengujian, dan dokumentasi sampai hasilnya siap direview untuk production. Penyusunan prompt ini sendiri bukan bukti bahwa seluruh perbaikan atau deployment sudah dilakukan.

## 1. Misi dan prinsip yang tidak boleh berubah

Repository: <https://github.com/lokah1945/wrappers>.

Pastikan seluruh wrapper aktif mengikuti satu konsep dan kontrak perilaku yang konsisten. Perbedaan implementasi harus dapat dijelaskan oleh kebutuhan upstream, deployment, atau bukti pengukuran; bukan karena masing-masing provider menyalin lalu memelihara ulang seluruh runtime.

Tujuan akhirnya adalah wrapper yang efektif, efisien, mudah ditambah provider, dan layak dioperasikan. Pertahankan:

- Native passthrough ketika API masuk cocok dengan API upstream; translasi hanya ketika diperlukan dan diizinkan policy.
- Model pilihan pengguna, explicit aliases, dan tidak ada substitusi model/provider secara diam-diam.
- Autentikasi lokal, isolation, retry credential yang tepat, bounded resources, cleanup, dan error yang jujur.
- Penambahan, penghapusan, serta penggantian API key tanpa restart service dan tanpa merusak request yang sedang berjalan.
- Satu sumber kontrak umum dengan adapter upstream yang kecil dan beralasan, didukung pengujian bersama.

Jangan menyamakan “konsep seragam” dengan seluruh provider harus identik. Jangan menyamakan kode lebih pendek dengan RAM lebih kecil. Buktikan setiap perubahan melalui contract tests dan pengukuran yang dapat direproduksi.

## 2. Baseline, scope, dan keselamatan kerja

Audit persiapan mengacu pada HEAD `66ec1c188face2ace6cb3ee6e2f2d1ae3d44ebd4`, 15 November 2026. Pada HEAD ini NVIDIA, Nous, OpenRouter, dan Coarena eligible sebagai provider aktif; model-registry adalah dependency, dashboard adalah control service terpisah. Pembaruan Coarena pada HEAD ini menggantikan banyak temuan snapshot sebelumnya.

Baca `docs/WRAPPERS_FINAL_AUDIT_BASELINE.md` dan evidence terkait jika tersedia. Lalu lakukan hal berikut sebelum memeriksa implementasi:

1. Baca `AGENTS.md`, `wrappers.json`, HEAD/branch/worktree terbaru. Gunakan `common/wrapper_scope.py` untuk memilih scope.
2. Provider pending/frozen tidak boleh dibaca, diimpor, diuji, diprobe, dipatch, atau dioperasikan. Pada baseline: `blackbox/` dan `opencode/`. OpenCode sebagai aplikasi client tetap boleh diuji melalui backend aktif.
3. Jangan mengikuti daftar lama “semua lima wrapper” atau menghapus freeze demi membuat audit terlihat lengkap. Tambahkan status `OUT_OF_SCOPE_FROZEN` tanpa menginspeksi implementasinya.
4. Pertahankan pekerjaan worktree dan perubahan agent lain. Gunakan branch audit/perbaikan terpisah. Jangan menghidupkan service di port production untuk test.
5. Test menggunakan credential palsu, file sementara, database/cache/log terisolasi, dan proses milik test sendiri. Jangan membaca `.env`, OAuth, credential, atau log production pada tahap mock.
6. Pembacaan sumber, edit, test, refactoring terbatas, branch, commit terfokus, dan draft PR termasuk dalam tugas ketika prompt ini dieksekusi dengan akses yang sesuai. Jangan merge atau deploy production di luar otorisasi sesi yang berlaku.
7. Tidak ada alasan meminta input ulang untuk keputusan implementasi rutin. Jika akses live belum tersedia, selesaikan pekerjaan lokal dan tandai gate live `BLOCKED/UNVERIFIED`.

## 3. Audit harus menghasilkan temuan yang dapat dibuktikan

Gunakan status per temuan/klausul: `IMPLEMENTED`, `PARTIAL`, `NOT_IMPLEMENTED`, `CONFLICT`, `UNVERIFIED`, dan `OUT_OF_SCOPE_FROZEN`. Untuk hasil test gunakan `PASS`, `FAIL`, `BLOCKED`, atau `NOT_RUN`.

Setiap temuan memuat ID, severity, HEAD, file/fungsi, jalur pemicu, expected versus actual, dampak, sumber upstream jika relevan, reproduksi, rekomendasi, acceptance criteria, dan hasil setelah perbaikan. Jangan menulis temuan hanya dari judul commit, docstring, README, atau hasil historis.

Audit wajib meliputi:

- Kontrak, arsitektur runtime, compatibility, translasi, auth, key/config lifecycle, concurrency, stream ownership, retry/error classification.
- Model identity, aliases, catalog/registry, context limits/recovery, continuation, tool calls, principal/account boundaries.
- Session/connector/task/thread/process lifecycle, cache/history/metrics persistence, dependency installation, systemd/supervisor, readiness, dashboard, CI, release/rollback.
- Source duplication dan runtime resource cost, beserta biaya menambah provider berikutnya.

Baca kode yang benar-benar dipanggil. Modul bernama `BaseWrapper` atau fungsi `load_from_env()` tidak otomatis dipakai oleh seluruh provider, dan keberadaannya tidak membuktikan fitur runtime bekerja.

## 4. Bandingkan dengan dokumentasi setiap upstream

Buat matriks dengan kolom berikut:

| Provider/deployment | API client | Upstream URL/path | Native/translated/unsupported/unknown | Model/feature/version | Auth dan attribution | Limits/error/stream | Source + tanggal | Bukti runtime |
|---|---|---|---|---|---|---|---|---|

Gunakan dokumentasi resmi terbaru, OpenAPI, dan runtime evidence yang sah. Entry point sumber awal:

- NVIDIA hosted: <https://docs.api.nvidia.com/nim/reference/llm-apis>.
- NVIDIA self-hosted NIM: dokumentasi versi/image yang benar, misalnya <https://docs.nvidia.com/nim/large-language-models/2.1.5/api-reference.html>.
- Nous: <https://portal.nousresearch.com/api-docs>, spesifikasi <https://portal.nousresearch.com/api/openapi>, dan dokumentasi resmi Hermes/Nous terkait endpoint yang digunakan.
- OpenRouter: <https://openrouter.ai/docs/api/api-reference/anthropic-messages/create-a-message>, <https://openrouter.ai/docs/api/api-reference/responses/create-a-response>, beserta Chat, auth, errors, limits, dan routing docs.
- Coarena: <https://coarena.ai/docs>, <https://coarena.ai/developers>, <https://coarena.ai/api/openapi.json>.

Jangan menganggap semua deployment dari vendor sama. Dukungan Messages pada NIM container tertentu tidak otomatis membuktikan hosted `integrate.api.nvidia.com` mendukungnya. API gateway dapat menerima wire API native walaupun gateway sendiri menerjemahkan di belakangnya; audit wrapper menilai boundary yang dihadapinya.

Ketika spesifikasi publik tidak mencantumkan route yang digunakan kode, tandai dokumentasi dan runtime verification secara terpisah. Omission dalam spec bukan bukti mutlak route tidak tersedia. Jangan menggunakan blog pihak ketiga untuk menyatakan suatu capability resmi.

Catat perbedaan upstream yang memang sah: attribution Nous, routing/account behavior gateway, NIM deployment/model metadata, model-specific reasoning/tool support, status/error shape, credential type, biaya, quota, dan retention. Jangan memasukkan detail upstream tersebut ke seluruh shared core melalui conditional provider yang terus bertambah.

## 5. Petakan shared core versus adapter

Telusuri setiap wrapper dari route sampai transport dan cleanup. Buat tabel untuk auth/body guards, config reload, credential pool, retry, native HTTP/SSE, translators, context/continuation, registry/catalog, telemetry/persistence, dan lifecycle.

Untuk setiap bagian, putuskan: sudah shared, duplikasi tanpa alasan, mirip tetapi policy berbeda, atau benar-benar khusus upstream. Catat alasan dan kontraknya.

Gunakan AST/token/symbol comparison sebagai kandidat duplikasi, lalu lakukan review semantik. Audit persiapan menemukan 28 kelompok fungsi Coarena/OpenRouter yang AST-nya sama setelah normalisasi nama provider, dengan minimum 10 baris fungsi. Itu indikator untuk investigasi, bukan izin menghapus fungsi tanpa membandingkan perilaku.

Evaluasi reuse melalui composition/helper atau perbaikan `BaseWrapper`; jangan memaksa semua provider mewarisi base class lama yang belum memenuhi kontrak native/lifecycle aktual. Kandidat pertama yang masuk akal adalah config/key reconciliation, auth, transport ownership, error helpers, converter yang benar-benar sama, dan telemetry primitives.

Aturan refactoring:

1. Reproduksi perilaku dan tambahkan behavioral contract test sebelum extraction.
2. Pindahkan satu tanggung jawab pada satu waktu, dengan adapter hook yang kecil dan explicit provider policy.
3. Pertahankan env names, routes, metrics, persistence, ports, serta API publik yang sudah dipakai, atau sediakan migrasi kompatibel yang terdokumentasi.
4. Hilangkan implementasi lama setelah jalur baru digunakan dan teruji; jangan meninggalkan dua runtime aktif yang melakukan pekerjaan sama.
5. Tunjukkan dependency graph dan bukti biaya menambah provider berkurang. Jangan menciptakan plugin framework, event bus, distributed coordinator, atau service baru tanpa kebutuhan nyata.
6. Kode di file bersama tetap dimuat ulang di setiap proses. Jangan mengklaim deduplikasi source otomatis menghemat RAM lintas service.

## 6. Final audit COMPATIBILITY_LAYER

Periksa seluruh provider aktif terhadap `COMPATIBILITY_LAYER=1/2/3`, nilai unset/invalid, startup discovery, cache, config override, serta `RESPONSES_UPSTREAM_MODE` dan `NATIVE_MESSAGES_MODE` bila tersedia.

Definisi audit:

- Client surface ditentukan route: Chat Completions, Responses, atau Messages; bukan nama model atau nama SDK saja.
- Layer 1/2 adalah konfigurasi dialek legacy yang harus memiliki arti dan precedence yang terdokumentasi; layer 3 adalah discovery yang memiliki batas kepastian.
- Provider multi-API harus bisa memakai route native yang sesuai. Satu scalar hasil discovery tidak cukup untuk menyatakan seluruh capability set upstream.
- `ProtocolProfile`, `ModelProfile.protocol_for()`, capability metadata, dan call plan yang sudah ada harus dievaluasi sebelum menambah selector baru.

Buat matriks API client × upstream Chat-only / Messages-only / multi-API / ambiguous / unsupported, untuk stream dan non-stream. Tambahkan GPT-named model pada Messages dan Claude-named model pada Chat/Responses. Pastikan tidak ada pemilihan jalur dari substring nama model.

Tetapkan precedence yang jelas antara native capability, operator setting, dan fallback policy. Jika setting lama benar-benar berarti force dialect, dokumentasikan pilihan itu dan tangani kombinasi yang bertentangan secara eksplisit. Jangan mengganti arti 1/2/3 secara diam-diam atau mengiklankan native preservation sambil menerjemahkan tanpa diketahui operator.

### Native policy

Native Chat → Chat, Responses → Responses, Messages → Messages harus mempertahankan unknown fields, tool types, null, multimodal/reasoning blocks, version/beta metadata, dan SSE event data. Jangan mengarahkan Responses ke Chat hanya karena keduanya OpenAI-compatible.

Translasi lintas wire API hanya pada adapter yang diizinkan. Responses → Chat tetap pengecualian untuk route native yang benar-benar unsupported, bukan jalan keluar generic error. Uji bahwa model/account/history 404, auth/quota, invalid parameter, timeout, 5xx, malformed successful SSE, dan feature-level 501 tidak disalahartikan sebagai route tidak ada. Pada baseline helper bersama menerima 405/501 tanpa memeriksa payload; audit semantik sebelum mempertahankan perilaku tersebut.

Discovery harus bounded, tidak memakai credential production secara diam-diam, tidak menghabiskan inference quota, dan tidak menyimpulkan capability dari 401/403 ambigu. Pisahkan configured setting, effective dialect, capability known/unknown, dan verified model/client features pada observability.

### Kualitas gate

Audit harness, bukan hanya output hijau. Sebagian fixture baseline menetapkan `RESPONSES_UPSTREAM_MODE=chat` dan/atau `NATIVE_MESSAGES_MODE=translate`; fixture itu menguji adapter legacy, bukan native production defaults. Native acceptance harus terpisah, menjalankan konfigurasi default rilis, dan meng-cover semua provider eligible. Gagalkan gate jika metadata menambah provider tetapi harness diam-diam melewatinya.

## 7. Hot swap API key — kontrak wajib

Hot swap berarti administrator mengubah sumber konfigurasi yang didukung saat service hidup; key baru dipakai dan key terhapus tidak mendapat request baru, dengan PID/service tetap hidup. Memanggil fungsi pool secara manual atau mengubah `os.environ` di test bukan bukti hot swap file end-to-end.

Audit implementasi saat ini, kemudian buat perilaku yang konsisten pada seluruh provider aktif:

### 7.1 Sumber konfigurasi dan rekonsiliasi

- Tetapkan sumber key yang authoritative: file runtime, OS env awal, secret provider, atau control API yang benar-benar sudah ada. Dokumentasikan precedence dan ownership.
- `load_dotenv(override=True)` tidak menghapus variable yang barisnya hilang dari file. Lakukan reconciliation snapshot untuk key yang memang dimiliki sumber tersebut; jangan membersihkan seluruh `os.environ` atau menghapus variable milik sumber lain.
- Systemd `EnvironmentFile` dibaca saat startup; mengeditnya tidak otomatis mengubah environment proses yang sudah berjalan. Aplikasi harus memiliki jalur reload sendiri. `export` di shell lain juga tidak mengubah proses service.
- Dukung naming yang resmi dipakai, deduplikasi nilai, urutan/identity yang stabil, compatibility alias env yang masih perlu, serta penambahan/penghapusan tanpa restart.
- Parse/validate snapshot utuh lalu commit atomik di event loop. Jangan menerapkan setengah file atau mengosongkan auth akibat transient write.
- File invalid harus mempertahankan last-known-good dengan status reload gagal yang terlihat. File valid yang sengaja berisi nol key harus menghasilkan nol key eligible, bukan diam-diam mempertahankan key lama.

### 7.2 Watcher dan threading

- Tangani write biasa, create, atomic rename/replace, delete, dan perubahan inode yang relevan. Periksa destination path pada moved event; jangan hanya `on_modified` atau substring `.env`.
- Watch direktori/file dengan path deterministik; jangan mengambil `.env` lain dari working directory/home secara tidak terduga.
- Debounce burst event dan gunakan parsing/polling bounded. Ketiadaan dependency watcher tidak boleh mematikan hot swap tanpa status yang jelas; gunakan fallback yang sederhana bila sesuai.
- Capture event loop service saat startup dan schedule dari thread dengan API thread-safe yang benar. Jangan mencari loop asyncio dari thread watchdog lalu berharap loop service ditemukan.
- Mutasi pool harus terserialisasi dengan acquire/release. Watcher tidak boleh membuat ulang pool di thread lain saat request aktif.
- Watcher/task harus dimiliki lifecycle, dihentikan, dan di-join/await saat shutdown; tidak bocor setelah restart atau test.

### 7.3 State credential dan request yang sedang berjalan

- Credential yang tidak berubah mempertahankan cooldown, RPM window, account scope, observations, counters, dan reservation yang masih berjalan.
- Gunakan identity stabil berdasarkan credential, bukan label ordinal yang berubah saat urutan file diubah. Jangan menampilkan credential mentah pada log/metrics.
- Key yang dihapus segera retired dari acquisition baru setelah generation baru diterapkan. Request yang sudah memegangnya boleh selesai sesuai kebijakan drain yang terdokumentasi; release tetap tepat sekali pada object/generation yang benar.
- Retry request tidak boleh memakai kembali credential yang sudah retired. Jangan memutus stream sehat hanya karena konfigurasi key berubah.
- Penghapusan key terakhir adalah skenario normal: service tetap hidup, readiness/inference mencerminkan tidak ada credential, discovery lokal tetap mengikuti kontrak, dan tidak ada stale key yang dipakai.
- Reordering/duplikasi/re-add key tidak boleh mereset quota/cooldown tanpa alasan. Tombstone dan retirement state harus bounded agar ribuan pergantian key tidak menambah RAM terus-menerus.
- Native continuation yang terikat akun harus mempertahankan affinity dan principal isolation saat pool berubah; jangan menyeberangkan riwayat ke akun lain.

### 7.4 Acceptance test hot swap

Jalankan test terhadap service hidup dengan file sementara dan mock upstream yang mencatat credential fingerprint aman. Jangan menonaktifkan watcher dengan `WRAPPER_SKIP_DOTENV=true` pada test ini; override path konfigurasi dan seluruh state tetap terisolasi.

| Skenario | Bukti yang wajib |
|---|---|
| 1 → 2 → banyak key | Jumlah eligible dan distribusi request berubah, PID tetap |
| Hapus satu key | Setelah generation baru aktif, tidak ada acquisition/upstream call baru dengan key lama |
| Ganti nilai/reorder/duplikasi | Identity/state tidak rusak, tidak ada key dihitung ganda |
| Hapus key terakhir → tambah kembali | Empty state valid, tidak memakai stale key, pulih tanpa restart |
| Cooldown/RPM saat reload | State retained untuk credential yang sama |
| Stream aktif saat key dihapus | Stream menyelesaikan/menutup sesuai policy; reservation tidak bocor |
| Retry bersamaan reload | Retired key tidak dipilih lagi; model tetap |
| Write biasa dan atomic replace | Keduanya diterapkan dalam bound reload yang terdokumentasi |
| Invalid/partial file lalu perbaikan | Last-known-good dan error generation jelas; recovery otomatis |
| Multiworker jika didukung | Semua worker menerapkan generation; batas account/key tidak terlipat diam-diam |
| Shutdown/restart berulang | Tidak ada watcher/thread/task/FD yang tertinggal |

Tentukan dan ukur bound reload yang praktis, misalnya paling lambat lima detik pada filesystem lokal yang didukung. Dokumentasikan pengecualian deployment; jangan menjanjikan angka tanpa test.

## 8. Transport, pool/retry, auth, dan streaming

Seragamkan kontrak umum dengan mempertahankan perbedaan policy yang sah:

- Shared session/connector per runtime yang sesuai, connect/read/total timeout jelas, bounded connection pool, cancellation-safe cleanup.
- Least-load credential selection, eligible attempted set, exactly-once reserve/release, cooldown berbasis error classification, bounded retry/deadline.
- Credential failure berbeda dari model capacity, bad request, account restriction, missing scope, dan daily quota. Pergantian key pada akun sama tidak menambah kuota akun.
- Tidak ada replay setelah output SSE diterima client, synthetic success terminal saat upstream gagal, buffer tak terbatas, atau heartbeat di tengah frame/UTF-8/CRLF.
- Pertahankan native errors/terminators; local errors memakai shape API client. Jangan memperbaiki native schema dengan whitelist translator lama.
- Framing HTTP harus benar setelah decompress/re-serialize; hapus stale Content-Length, Content-Encoding dan hop-by-hop/Connection-nominated headers, jangan membocorkan cookie/credential.
- Fail-closed auth, token reload/revocation sesuai kontrak, dual credential headers yang benar, OPTIONS dan public GET paths exact/method-gated, body-size/object guards.
- Pisahkan inference access dari keys/account/history export/limit mutation/management. Jangan membuka privileged upstream surface lewat catch-all atau metrics/debug route.
- Graceful drain, background task ownership, metrics flush, DB/session close, watcher shutdown, serta startup config validation harus lengkap.

Uji upstream lambat, non-JSON/HTML, wrong content type, HTTP 200 error body, retryable failure, stream putus, read-idle, send failure, serta cancellation sebelum generator dimulai.

## 9. Exact model, catalog, context, dan continuation

Audit seluruh provider untuk explicit model ID, alias binding, no silent fallback, actual model identity, gateway substitution, dan dropped/emulated parameters. Coarena pada dokumen upstream dapat melakukan substitusi; adapter harus mendeteksi/menolak pelanggaran yang teramati dan menampilkan batas jaminannya. Jangan mengarang opsi upstream untuk menonaktifkannya.

Pisahkan provider identity gateway dari backend vendor yang secara resmi dipilih gateway. Provider/model fallback dari wrapper tidak boleh muncul tanpa policy/otorisasi yang sesuai; jangan menebak identitas hanya dari nama response.

Registry/catalog harus mempertahankan provenance, age, native metadata, per-model capabilities, unknown/stale/account-scoped states, dan alias rules. Model tidak boleh globally dead karena satu key gagal.

Native `previous_response_id` tetap native. Verifikasi ownership client, account affinity saat key rotation/hot swap, retry, expiry, full explicit history, dan tool-call/result multi-turn. ID yang sulit ditebak bukan pengganti authorization saat beberapa pengguna wrapper berbagi akun upstream.

Local store yang diperlukan harus bounded menurut entries, bytes, TTL, dan principal. Jangan menggandakan penyimpanan percakapan tanpa kebutuhan. Context recovery hanya memakai evidence limit yang benar, tidak menghapus history secara diam-diam, mengganti model, atau menjanjikan context dari tebakan.

## 10. Efisiensi CPU, RAM, disk, I/O, dan jaringan

Buat baseline sebelum refactoring pada kondisi dan dependency yang dicatat. Ukur per proses dan total fleet; sampling tunggal tidak cukup untuk menyatakan stabil atau leak-free.

| Dimensi | Pengukuran minimum |
|---|---|
| Memory | RSS, USS/PSS bila OS mendukung, peak, idle/warm/load/cooldown; hindari menjumlahkan RSS shared pages sebagai memori unik |
| CPU/event loop | Utilisasi, loop lag, blocking disk/JSON/lock/regex di hot path, task/thread counts |
| I/O/network | Session/connection/FD counts, outbound calls, catalog polling, registry writes, metric flush dan retry amplification |
| State | Catalog/cache/history/response store/metrics cardinality, queued observations/events, disk/SQLite/WAL growth |
| Latency/throughput | p50/p95/p99 proxy overhead, TTFT, completion latency, throughput, errors pada offered load yang sama |
| Scaling | 1/10/100/1.000 synthetic keys atau batas relevan, model metadata besar, long stream, slow consumer, concurrent clients |

Pisahkan overhead wrapper dari latency model/upstream. Gunakan mock yang dapat mengontrol waktu; rekam warm-up, durasi, concurrency, payload, sample interval, environment dan raw evidence. Ulangi run secukupnya untuk menilai noise; pilih ambang regresi berdasarkan baseline serta kebutuhan, bukan angka rekaan.

Cari parsing JSON berulang, deepcopy seluruh history/catalog per-request, response buffering, pemindaian semua key terlalu sering, sync file persistence di event loop, unbounded per-key/model labels, task tanpa backpressure, dan background probe/polling ganda. Jangan meniadakan audit/telemetry yang penting demi benchmark bagus; gunakan batching/cache/write-behind bounded jika terbukti perlu.

Tambahkan soak dengan warm-up, steady load, key churn, error burst, disconnect, lalu cooldown. Laporkan memory/FD/task return-to-baseline dan growth slope. Soak beberapa detik hanya smoke; pilih durasi yang bisa membuktikan failure mode yang diaudit dan nyatakan keterbatasannya.

Pertahankan process isolation provider yang sudah ada kecuali ada bukti biaya dan tradeoff yang membenarkan perubahan. Menyatukan seluruh provider ke satu proses, menambahkan Redis, atau shared cache service bukan keputusan default.

## 11. Dependencies, instalasi, deploy, dan upgrade

Resolver harus dijalankan untuk dependency aktual seluruh service yang dipilih, termasuk include requirements rekursif dan release constraints, pada environment bersih. Test installer dengan requirements sintetis tidak membuktikan dependency rilis dapat di-install.

Pada baseline, Coarena mem-pin uvicorn, pydantic, dan python-dotenv ke versi yang bertentangan dengan `tests/constraints-verified.txt`. Audit ulang; jangan mengabaikan conflict dengan `--skip-deps` atau mengklaim install lulus karena test memakai environment yang sudah tersedia.

Validasi entry point, WorkingDirectory/PYTHONPATH, bind/ports, permissions state directory, config-file ownership, user-level systemd semantics, secret preservation, readiness, dependency startup order, graceful stop, restart behavior, installer idempotency, supervisor, migration/persistence compatibility, dan rollback konkret.

Jangan mengubah port layanan lain atau provider frozen. Bedakan frontend dashboard polling/SSE dari inference native SSE. Readiness harus mencerminkan syarat operasional tanpa melakukan generasi mahal atau membocorkan secret.

Pipeline CI harus otomatis menjalankan contract matrix provider eligible, native default acceptance, compatibility adapters, hot swap hidup, dependency resolution aktual, dan batas resource yang dipilih. Penambahan provider baru tidak boleh sekadar lolos daftar metadata.

## 12. Test plan dan production gates

Gunakan semua test repository yang relevan setelah memastikan scope dan isolation. Tambahkan behavioral tests untuk gap yang belum tercakup; jangan menulis test yang hanya membuktikan nama fungsi atau meniru implementasi.

Pisahkan gate berikut:

| Gate | Bukti kelulusan |
|---|---|
| G0 | HEAD/scope/instructions; frozen provider excluded sebelum access |
| G1 | Matriks sumber upstream dan capability/deployment aktual; gap unknown terlihat |
| G2 | Contract/auth/model/isolation/error/continuation regression pada seluruh provider aktif |
| G3 | Native defaults: ketiga client APIs, opaque fields/SSE, tools/reasoning, unsupported-route policy |
| G4 | Compatibility layers 1/2/3, discovery ambiguity, intentional adapters dan negative fallback cases |
| G5 | Hot swap hidup: add/remove/replace/zero-key/state-retention/atomic save/concurrency/PID |
| G6 | Dependency resolver aktual, clean install, unit rendering, startup/readiness, installer idempotency |
| G7 | Resource/performance baseline versus hasil, soak dan recovery tanpa leak/regresi material |
| G8 | OpenAI SDK Chat/Responses dan Anthropic SDK Messages, stream/non-stream, multi-turn tools; versi dicatat |
| G9 | Live upstream terbatas pada credential sah, model/client/features yang dicatat, tanpa data sensitif |
| G10 | Release manifest, rollback drill, staging/deployed revision dan readiness sesuai otorisasi |

Uji Claude Code, Codex, dan OpenCode client dengan tool/permission fixture yang terkendali bila runner tersedia. Hello-only bukan kualifikasi agent runtime. Jangan membuat akun, menaikkan limit, menerima terms, atau memakai key dari contoh untuk melewati gate live.

Audit persiapan menjalankan 421 selected tests dan dua E2E suites; hasil tersebut tidak menggantikan gate pada HEAD hasil perbaikan. Test yang mengatur `WRAPPER_SKIP_DOTENV=true` tidak menguji hot swap file. Test legacy adapter tidak menggantikan native default test. Report PASS harus menyebut scope dan pengecualian.

## 13. Urutan perbaikan

1. Audit ulang dan reproduksi; koreksi salah asumsi sebelum memodifikasi arsitektur.
2. Selesaikan P0/P1: auth/isolation/model fidelity, resource ownership, dependency install, hot swap benar, protocol policy dan native coverage.
3. Extract shared behavior bertahap dari bagian yang terbukti sama; pertahankan upstream-specific adapter.
4. Jalankan regression provider aktif setelah setiap perubahan shared yang material.
5. Ukur sebelum/sesudah, rapikan obsolete code/docs, dan periksa bahwa provider baru tidak perlu menyalin seluruh runtime.
6. Jalankan gate hasil akhir, review konsistensi kontrak/docs/config/evidence, lalu siapkan PR dan handoff operasional.

Tidak boleh menunda kontrak inti ke “rekomendasi opsional” agar status akhir terlihat hijau. Jika upstream tidak bisa memenuhi jaminan tertentu, nyatakan batasnya dan gate fitur terkait; jangan menyembunyikannya dengan fallback.

## 14. Deliverables dalam repository

Utamakan dokumen yang sudah ada dan hindari pengulangan. Minimal hasil berikut harus tersedia di `docs/` atau subfolder audit/report yang konsisten:

- Audit aktual: executive findings, HEAD, clause-by-clause/provider compliance, source evidence, severity, before/after dan residual risks.
- Upstream contract matrix dengan URL, retrieval date, deployment/model/version, documented versus tested.
- Shared-core/adapter design dan daftar duplikasi yang dihapus/dipertahankan beserta alasan, kompatibilitas, rollback.
- Compatibility matrix dan precedence configuration yang menggambarkan perilaku sebenarnya.
- Hot-swap specification/runbook: authoritative source, ownership, reload bound, generation/state/drain, failure recovery, multiworker dan test evidence.
- Performance/resource report: metode, baseline, hasil, raw evidence, budget/ambang dan keterbatasan.
- Release gates report, dependency/installer results, readiness serta rollout/rollback checklist yang bisa dieksekusi.
- Rekomendasi P0/P1/P2 dengan impact, effort, dependency dan acceptance criteria; bedakan wajib versus opsional.

Perbarui README, `WRAPPER_CONTRACT.md`, compatibility guide, wrapper template, production runbook, CI dan indeks docs sejauh diperlukan. Koreksi dokumen dengan bukti; jangan melemahkan kontrak, mengubah tujuan, atau mengarang hasil deployment.

## 15. Definition of Done dan laporan akhir

Pekerjaan selesai setelah seluruh perilaku relevan dipetakan, gap yang dapat diperbaiki telah diperbaiki, pengujian/perbandingan aktual tersedia, dan dokumentasi siap dipakai agent berikutnya tanpa kehilangan konteks.

Laporan akhir wajib menjawab dengan jelas:

1. Seberapa seragam konsep wrapper sekarang, bagian mana yang shared, dan perbedaan upstream yang sah.
2. Apakah seluruh provider aktif mematuhi compatibility policy, termasuk native defaults dan intentional conversion.
3. Apakah add/remove/replace API key benar-benar berjalan tanpa restart; tampilkan test hidup, state-retention, PID dan bound reload.
4. Penghematan source/maintenance serta perubahan resource yang terukur; jangan menjanjikan penghematan RAM dari source dedup saja.
5. Hasil tiap gate, failures/exclusions/blocked live checks, SDK/client versions dan HEAD evidence.
6. Readiness per fitur/provider dan secara fleet, release blocker, risiko residual, daftar dokumen/PR/commit, serta langkah berikutnya.

Jangan menyatakan “production ready”, “100% identik”, “semua model kompatibel”, “zero bug”, atau “tanpa masalah selamanya” dari mock saja. Klaim release harus sesuai bukti aktual. Mulai audit aktual, lanjutkan perbaikan terukur, dan tuntaskan sampai hasilnya konkret serta dapat direview.
