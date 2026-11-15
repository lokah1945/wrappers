# Audit Persiapan Integrasi Coarena

Tanggal: 15 November 2026, sekitar 20.26 WIB. Repository: <https://github.com/lokah1945/wrappers>. Snapshot yang dianalisis: branch `main`, HEAD [`5a02a0cb1773327fb719a8ab4c0412a187d5e60a`](https://github.com/lokah1945/wrappers/commit/5a02a0cb1773327fb719a8ab4c0412a187d5e60a).

## Kesimpulan utama

Integrasi Coarena sudah ada pada HEAD tersebut. `wrappers.json` mendaftarkannya sebagai provider production pada port `9107`. Jalur Chat, Responses, dan Messages mengirim body ke API upstream yang sama tanpa translator. Namun implementasinya belum memenuhi kontrak wrappers secara menyeluruh; label production tidak membuktikan kesiapan operasional.

Ingatan pemilik tentang kemampuan pemetaan protokol sebagian besar benar: repository sudah mempunyai jalur native dan jalur translasi, serta model/call-plan contracts. Ada nuansa penting: `COMPATIBILITY_LAYER` sendiri memilih satu dialek upstream, sedangkan OpenRouter dan Nous telah menambahkan mode native Messages yang independen. Jadi jangan menyimpulkan seluruh pemilihan native otomatis diselesaikan satu flag global.

Audit ini menyiapkan master prompt penyelesaian integrasi. Kode repository tidak diubah atau dideploy. Snapshot sumber diambil melalui GitHub; pengujian menggunakan credential palsu, upstream loopback, dan ASGI transport terisolasi. Tidak dilakukan inferensi bercredential ke Coarena, pengujian key dalam contoh konfigurasi, atau operasi akun upstream.

## Scope dan metode

`AGENTS.md`, `wrappers.json`, dan scope selector diperiksa sebelum implementasi provider. Provider eligible pada snapshot: NVIDIA, Nous, OpenRouter, Coarena, dan model-registry sebagai dependency; dashboard adalah control service terpisah. Implementasi provider BLACKBOX dan OpenCode yang frozen tidak dibaca atau diuji.

Area yang dibaca meliputi kontrak, kompatibilitas, implementasi Coarena, jalur native/retry provider aktif, shared HTTP/auth/SSE/model modules, registry, dashboard, installer/supervisor, readiness checker, dan sejumlah test terkait. Ini audit terarah integrasi Coarena, bukan sertifikasi seluruh baris kode atau seluruh runtime repository.

Selain pemeriksaan sumber, sembilan diagnostik direproduksi. Enam kasus positif menguji tiga route native dengan dua nama model sintetis. Public catalog dan OpenAPI Coarena diambil tanpa credential. Full production gates, SDK/client live, dan systemd production belum dijalankan.

## Apa yang benar-benar sudah ada

| Komponen | Bukti | Penilaian |
|---|---|---|
| Provider Coarena | `wrappers.json:248`, folder `coarena/` | IMPLEMENTED; eligibility metadata tersedia |
| Chat native | `coarena/src/main.py:145` | Body request langsung ke `/chat/completions` |
| Responses native | `coarena/src/main.py:158` | Body request langsung ke `/responses` |
| Messages native | `coarena/src/main.py:171` | Body request langsung ke `/messages` |
| Token count route | `coarena/src/main.py:184` | Route tersedia; live belum terverifikasi |
| Model list route | `coarena/src/main.py:140` | Ada, tetapi memakai fungsi proxy yang mensyaratkan key |
| Scope selector | `common/wrapper_scope.py` | Mengenali Coarena lewat metadata |
| General dialect control | `common/compat.py:50` | OpenAI, Anthropic, atau discovery; bukan capability set multi-API |
| Native Messages override | `openrouter/src/main.py:245,2391`; `nous/src/main.py:495,3885` | Pola yang sudah mendukung native Messages selain dialek umum |
| Call-plan infrastructure | `common/model/contracts.py`, `common/model/call_plan.py` | Sudah tersedia; Coarena belum menghubungkannya |

Baseline bukan kasus menambahkan provider dari nol. Menyalin template lama juga berisiko memperkenalkan translasi dan dashboard lokal yang bertentangan dengan kebijakan terbaru.

## Pemetaan protokol yang tepat

Coarena mendokumentasikan dua base URL SDK: OpenAI memakai `https://coarena.ai/api/v1`, Anthropic memakai `https://coarena.ai/api`. SDK Anthropic menambahkan `/v1/messages` sendiri. Hasil URL akhirnya konsisten dengan matriks berikut. [S1][S2]

| API client pada wrapper | Upstream final | Translasi oleh wrapper |
|---|---|---|
| `/v1/chat/completions` | `/api/v1/chat/completions` | Tidak diperlukan |
| `/v1/responses` | `/api/v1/responses` | Tidak diperlukan |
| `/v1/messages` | `/api/v1/messages` | Tidak diperlukan |
| `/v1/messages/count_tokens` | `/api/v1/messages/count_tokens` | Tidak diperlukan untuk route native |

Kesamaan brand SDK tidak membuat Responses identik dengan Chat. Begitu pula model GPT melalui Messages tidak perlu diubah menjadi Chat apabila upstream sudah menerima Messages. Protokol request ditentukan endpoint, bukan nama model.

Komentar Coarena yang menyebut base Anthropic `/api/v1` tidak otomatis membuktikan URL salah: kode menambahkan `/messages`, sehingga URL final benar. Yang harus dibenahi adalah kejelasan SDK base versus API root serta join route generik, bukan mengganti string tanpa menelusuri hasil akhirnya.

## Bukti diagnostik aktual

Environment diagnostik: Python 3.12.14, FastAPI 0.141.1, Starlette 1.6.0, aiohttp 3.14.4, httpx 0.28.1. Ini environment audit, bukan dependency release yang telah dikualifikasi repository.

Hasil tersimpan pada [`artifacts/coarena_snapshot_checks.json`](artifacts/coarena_snapshot_checks.json). Script diagnostik pendamping: [`artifacts/verify_coarena_snapshot.py`](artifacts/verify_coarena_snapshot.py).

| ID | Pemeriksaan | Hasil teramati | Implikasi |
|---|---|---|---|
| P01 | Tiga API × nama model `claude-test-only`/`gpt-test-only` | Enam request tiba pada route sesuai API; bytes body tetap | Membuktikan route/body forwarding, bukan validitas schema atau fitur model live |
| D01 | Request inference tanpa local credential; fake `BEARER_TOKEN` sudah dikonfigurasi | HTTP 200, upstream dipanggil sekali | Auth lokal belum diterapkan |
| D02 | Fake key pertama menerima 429; key kedua tersedia | HTTP 429 setelah satu upstream call | Round-robin antar-request belum merupakan retry dalam satu request |
| D03 | Native SSE dari loopback upstream | `ClientConnectionError` saat konsumsi stream | Session/response keluar context sebelum iterator selesai |
| D04 | JSON upstream ber-whitespace dengan header length | `Content-Length: 33`, body downstream 23 bytes; Connection dan Set-Cookie ikut | Re-serialization disertai forwarding framing/header yang salah |
| D05 | GET route continuation dengan query | `/api/v1/v1/responses/resp_fake`, query kosong | Catch-all menggandakan versi dan menghilangkan query |
| D06 | DELETE `/key` ke mock upstream | Diteruskan ke `/api/v1/key` tanpa auth lokal | Catch-all menjembatani inference ke operasi akun dengan credential service |
| D07 | GET `/ready` | Diteruskan ke upstream `/api/v1/ready`; tidak ada local ready field | Readiness bukan pemeriksaan lokal yang dibutuhkan installer/dashboard |
| D08 | Helper iterator dibuat langsung menghasilkan `IDLE` | `AttributeError` saat serialisasi body | Sentinel langsung diberikan ke `StreamingResponse`; heartbeat wrapper tidak ada |
| D09 | Metadata dibandingkan struktur file snapshot | Installer mengharapkan `wrapper-coarena.service`, hanya `coarena.service` ada; `main:app` tidak menunjuk `src/main.py` dari workdir standar | Jalur startup/installer tidak konsisten |

D01–D08 dijalankan terhadap aplikasi snapshot dengan fake upstream. D08 sengaja men-stub helper agar idle terjadi segera, bukan menunggu 30 detik. D09 adalah pemeriksaan metadata/struktur, bukan eksekusi systemd. D05 tidak membuktikan upstream menyediakan GET Responses tersebut; ia membuktikan routing catch-all dan query rusak untuk path tambahan.

Sembilan diagnostik yang berhasil mereproduksi masalah tidak berarti sembilan gate production lulus. Tidak ada akun/key asli yang dihapus pada D06; seluruh request masuk ke server loopback milik diagnostik.

## Temuan dan rekomendasi prioritas

### P0 — fondasi yang harus dibereskan

| Temuan | Bukti sumber baseline | Perbaikan yang diminta | Acceptance criteria |
|---|---|---|---|
| Tidak ada local auth | `coarena/src/main.py:37–54,83–133` | Integrasikan `common.auth` dan middleware fail closed | Request tanpa/salah token ditolak sebelum memakai key upstream; OPTIONS/discovery mengikuti kebijakan exact path |
| Management dapat dicapai melalui catch-all | `coarena/src/main.py:205–209` | Ganti unrestricted proxy dengan route/method yang jelas; management terpisah | Route keys/account/exports/mutations tidak dapat dicapai dengan inference access |
| Stream kehilangan resource | `coarena/src/main.py:109–120` | Lifespan session, ownership transfer, cleanup pada seluruh terminal/cancel paths | SSE selesai; disconnect/timeout tidak membocorkan session/key/task |
| Sentinel idle tidak ditangani | `common/sse.py:99`; pemanggilan Coarena baris 114–120 | Gunakan generator passthrough/heartbeat yang tepat | Tidak ada object sentinel ke ASGI; heartbeat hanya pada batas frame |
| Header framing salah | `coarena/src/main.py:121–133` | Gunakan header helper bersama; raw native response bila sesuai | JSON/gzip/SSE valid melalui socket, tanpa length/encoding/hop headers yang basi |
| Startup service tidak konsisten | `wrappers.json:250–255`, unit Coarena, `install.sh:105` | Selaraskan entry point, nama unit, WorkingDirectory dan PYTHONPATH | Import command dan installer dry-run Coarena lulus dengan fake systemctl |
| Literal menyerupai key di env example | `coarena/.env.example`, tanpa menyalin nilainya | Placeholder yang jelas; pemilik merotasi jika ternyata pernah aktif | Tidak ada credential nyata pada contoh/fixture/laporan; validitas literal lama tidak diasumsikan |

Local auth tetap diperlukan walau service bind loopback, karena dashboard/reverse proxy atau aplikasi lokal dapat menjadi jalur masuk.

### P1 — menyelesaikan kontrak inti

| Area | Kondisi baseline | Rekomendasi dan bukti selesai |
|---|---|---|
| Pool/retry | `get_next_key()` hanya round-robin; lock tidak dipakai; tanpa cooldown/in-flight/retry loop | Pool bersama atau komponen setara dengan exactly-once accounting, bounded retry, hot reload, cooldown/error classification; model tetap |
| Native policy | Coarena native tetapi terpisah dari shared compatibility/call-plan | Manfaatkan native Messages pattern dan `ProtocolProfile`; semua native surface dipilih tanpa brand inference atau translator |
| Exact model | Tidak ada pemeriksaan model aktual/substitution | Fail closed pada substitusi terdeteksi dan dokumentasikan batas jaminan sebelum SSE; jangan klaim kontrak model fidelity penuh tanpa bukti |
| HTTP error handling | `json.loads` setiap non-stream; HTML detection sempit; native/local error shaping tidak lengkap | Native body/error utuh bila aman, local errors sesuai API client; HTTP 200 error body tidak dihitung sukses |
| Required surfaces | Health/version/props ada; ready/metrics/capabilities/tags dan dashboard policy tidak diimplementasikan lokal | Tabel kepatuhan seluruh klausul; metrics yang benar dan shaped unsupported capability sesuai kontrak |
| Continuation/isolation | Native body lewat, tetapi belum ada verifikasi ownership/account affinity | Two-turn tools, previous_response_id, key/account rotation dan cross-principal isolation teruji |
| Registry/catalog | Tidak memakai model modules; tree tidak memuat provider/error manifest Coarena | Integrasikan catalog/model profile/call plan/observations; refresh dan unknown/stale/account errors akurat |
| Context recovery | Belum menghubungkan mekanisme shared | Published limits dan rejection evidence; tanpa history truncation, tanpa model switch |
| Limits | Tidak ada account-aware pacing dan timeout config yang konsisten | Key rotation tidak mengabaikan account limits; Retry-After dan retry eligibility menghormati error aktual |
| Dashboard | Metadata polling saja, route telemetry lokal belum ada | Unified dashboard memperoleh readiness, counters, model/activity data yang valid dan teredaksi |
| Dependencies/gates | Requirements longgar; beberapa test spesifik hanya mencakup provider lama | Resolver constraints dan gate coverage Coarena nyata, tanpa akses provider frozen |

`WRAPPER_CONTRACT.md` menetapkan banyak hal di luar tiga endpoint: auth, shared surfaces, key reservation, cleanup, bounded continuation store bila dipakai, metrics persistence, graceful shutdown, dan cross-wrapper parity. Semua harus dipetakan; menulis “native proxy sudah ada” belum menyelesaikannya.

Beberapa dokumentasi masih menyebut tiga active provider dan template masih menyuruh dashboard lokal serta translasi umum. Koreksi dokumentasi berdasarkan perilaku final; jangan melemahkan kontrak agar implementasi yang kurang lengkap tampak sesuai.

### P2 — peningkatan yang tetap sederhana

1. Buat decision metadata yang aman untuk menjelaskan client surface, upstream surface, native/translated, dan alasan. Hindari isi prompt/response. Gunakan metadata call-plan yang sudah ada sebelum membuat abstraction baru.
2. Pisahkan lifecycle status dari feature/client qualification. Provider eligible maintenance belum tentu terbukti pada seluruh model atau client version.
3. Tambahkan fixture standar bagi provider multi-API supaya provider baru tidak mengulang pemilihan dialek yang salah. Jangan mengganti seluruh provider sekaligus jika shared helper kecil sudah cukup.
4. Sinkronkan guide/template dengan unified dashboard dan native-by-default agar penambahan provider berikutnya tidak menyalin pola lama.

P0/P1 bukan backlog opsional. P2 dapat dijadwalkan setelah seluruh kontrak dasar terbukti, kecuali bagian yang diperlukan untuk membuat status/policy tidak menyesatkan.

## Batas upstream yang memengaruhi desain

### Substitusi model

Dokumentasi Coarena menjelaskan bahwa sebagian request dapat dijawab model lain; body/header menampilkan model aktual dan alasan substitusi. Dokumentasi yang diperiksa tidak memberi jaminan exact-model untuk semua request. [S3]

Ini berbenturan dengan kontrak wrappers yang melarang silent model fallback. Kesimpulan desain: wrapper dapat mendeteksi/menolak substitusi yang teramati, tetapi tidak dapat menjamin provider tidak pernah menjalankan model pengganti. Perlu policy eksplisit sebelum downstream success/SSE, handling setelah stream dimulai, dan status qualification yang jujur. Jangan menulis parameter opt-out rekaan.

### Catalog dan fitur

Halaman docs saat dibaca mencantumkan 22 model, tetapi snapshot endpoint catalog publik memberikan 10. Catalog mengandung capability/limit/alias metadata yang dapat dipakai adapter discovery. Ini snapshot, bukan daftar permanen atau bukti seluruh fitur model berhasil. [S1][S5]

OpenAPI menyediakan spesifikasi terpisah bagi tiga API inference, token counting, dan discovery. Kehadiran endpoint tidak menyatakan seluruh fitur kedua vendor setara. Uji capability yang benar-benar dipakai, pertahankan error native, dan jangan mengubah unsupported feature menjadi sukses buatan. [S4]

### Kuota, non-stream, dan retensi

Dokumentasi provider menyebut kuota yang dapat berlaku pada account, perilaku non-stream lambat yang lebih dulu mengirim HTTP 200, serta penyimpanan request/reply yang tidak mengikuti janji `store=false` milik client. [S3] Ketentuan resminya juga menjelaskan bahwa withdrawal tidak otomatis menghapus prompt/reply Model API dari data/dataset mereka. [S6]

Karena itu, gunakan metadata dan error aktual untuk pacing/retry, periksa body sebelum menganggap non-stream sukses, dan cantumkan data handling dalam setup/runbook. Wrapper tidak boleh mengklaim zero retention atau membuat akun/permintaan kenaikan limit otomatis sebagai bagian dari inferensi.

## Arsitektur yang direkomendasikan

Pertahankan `coarena/` sebagai provider adapter. Gunakan shared auth, header helpers, SSE lifecycle, key-pool/error classification, model/catalog/registry, dan telemetry yang sudah terbukti. Pada boundary native, hindari translator dan native schema rewrite.

Representasikan ketiga native API melalui protocol/call-plan metadata yang sudah ada. Jika helper pemilihan route diperlukan, buat perbaikan kecil dengan regression coverage. Jangan membuat jenis gateway kedua atau menyalin seluruh file main provider lain.

Konfigurasi operator harus tetap dapat mengatur endpoint dan batas operasional dengan default yang benar. Default Coarena mengarah ke route native; konfigurasi compatibility yang bertentangan harus dijelaskan/ditolak secara jelas, bukan diam-diam menerjemahkan. Pertahankan kebijakan seluruh provider aktif dan jangan mengubah provider frozen.

## Gate kelulusan implementasi berikutnya

| Gate | Yang harus dibuktikan | Status audit persiapan |
|---|---|---|
| G0 — scope/baseline | HEAD, status metadata, frozen exclusion | VERIFIED untuk snapshot |
| G1 — native request routing | API sesuai endpoint dan body tidak ditranslasikan | 6 kasus mock lolos; fitur live UNVERIFIED |
| G2 — auth/boundary/HTTP/startup | D01/D04/D05/D06/D07/D09 diperbaiki | FAIL pada snapshot |
| G3 — retry/stream/resources | D02/D03/D08 diperbaiki, accounting/cancel teruji | FAIL pada snapshot |
| G4 — full contract | Registry, continuation/isolation, context, metrics, dashboard, exact model | PARTIAL/NOT IMPLEMENTED; belum memenuhi kontrak |
| G5 — regression/SDK | Production gates provider aktif dan SDK sungguhan | NOT RUN |
| G6 — live qualification | Credential sah, model/client/version/evidence terukur | UNVERIFIED |
| G7 — deployment | Revision aktual, readiness, rollout/rollback | NOT RUN |

Jangan mempromosikan hasil mock menjadi bukti live. Untuk gate production, gunakan daftar scope metadata serta uji bahwa harness sungguh menjalankan Coarena; label provider dalam scope.json saja tidak cukup.

## Handoff

Jalankan [`MASTER_PROMPT_COARENA_INTEGRATION.md`](MASTER_PROMPT_COARENA_INTEGRATION.md) pada agent yang memiliki checkout dan kemampuan implementasi. Prompt memerintahkan audit ulang HEAD, menyelesaikan gap seluruh kontrak, menguji, memperbarui docs, dan menyiapkan hasil review. Keterbatasan live harus tetap eksplisit bila credential atau runner belum tersedia.

Dokumen ini disiapkan sebagai isi `docs/`. Koneksi GitHub pada sesi persiapan memberikan akses baca (`push=false`), sehingga belum ada commit, branch, PR, atau file baru yang ditulis ke repository remote.

## Sumber dan evidence

- [S1 — Coarena Model API docs](https://coarena.ai/docs#models), dibaca 15 November 2026.
- [S2 — Coarena for agents](https://coarena.ai/for-agents), contoh SDK dan wire APIs.
- [S3 — Coarena developer reference](https://coarena.ai/developers), routing/limits/non-stream/data handling.
- [S4 — Coarena OpenAPI](https://coarena.ai/api/openapi.json), snapshot specification version `2026-11-02`, diambil tanpa credential.
- [S5 — Public model catalog](https://coarena.ai/api/v1/models), snapshot 15 November 2026; jumlah bukan konstanta.
- [S6 — Coarena Terms, bagian Withdrawal](https://coarena.ai/terms), dibaca 15 November 2026. Ini catatan perilaku provider, bukan penilaian hukum.
- [Source — Coarena main](https://github.com/lokah1945/wrappers/blob/5a02a0cb1773327fb719a8ab4c0412a187d5e60a/coarena/src/main.py).
- [Source — metadata lifecycle](https://github.com/lokah1945/wrappers/blob/5a02a0cb1773327fb719a8ab4c0412a187d5e60a/wrappers.json).
- [Source — instruksi repository](https://github.com/lokah1945/wrappers/blob/5a02a0cb1773327fb719a8ab4c0412a187d5e60a/AGENTS.md).
- [Source — wrapper contract](https://github.com/lokah1945/wrappers/blob/5a02a0cb1773327fb719a8ab4c0412a187d5e60a/WRAPPER_CONTRACT.md).
- [Source — shared compatibility](https://github.com/lokah1945/wrappers/blob/5a02a0cb1773327fb719a8ab4c0412a187d5e60a/common/compat.py).
- [Source — OpenRouter native Messages](https://github.com/lokah1945/wrappers/blob/5a02a0cb1773327fb719a8ab4c0412a187d5e60a/openrouter/src/main.py#L2379).
- [Source — installer](https://github.com/lokah1945/wrappers/blob/5a02a0cb1773327fb719a8ab4c0412a187d5e60a/install.sh).
