# MASTER PROMPT — Audit dan Penyelesaian Integrasi Native Coarena pada Wrappers

Gunakan seluruh isi dokumen ini sebagai instruksi untuk agent pelaksana. Dokumen ini menugaskan audit, implementasi, pengujian, dan dokumentasi. Membaca dokumen ini untuk menyiapkan rencana belum berarti implementasinya telah dikerjakan.

## 1. Tujuan dan hasil yang diminta

Anda bekerja pada repository <https://github.com/lokah1945/wrappers>.

Selesaikan integrasi Coarena secara menyeluruh sambil mempertahankan seluruh konsep dasar wrappers: transparansi protokol, model yang dipilih pengguna, rotasi credential, ketahanan request, kesinambungan percakapan, autentikasi, model registry, observability, dashboard tunggal, serta keselamatan instalasi dan pengembangan berikutnya.

OpenAI Chat Completions, OpenAI Responses, dan Anthropic Messages adalah tiga wire API yang berbeda. Jika Coarena menyediakan API yang sama dengan API request masuk, teruskan melalui API tersebut tanpa konversi protokol. Nama atau vendor model tidak menentukan wire API.

Jangan berhenti setelah menambahkan folder, endpoint, daftar model, atau kartu dashboard. Buktikan implementasi memenuhi kontrak repository. Rekomendasi pengembangan harus tertulis dalam `docs/`, dengan bukti, prioritas, dan acceptance criteria.

## 2. Baseline yang wajib diverifikasi ulang

Audit persiapan dilakukan pada 15 November 2026, HEAD `5a02a0cb1773327fb719a8ab4c0412a187d5e60a`, branch `main`.

Pada baseline tersebut:

- `coarena/` SUDAH ADA; `wrappers.json` menyatakannya `production`, port `9107`.
- Jalur utama Coarena meneruskan body request langsung, tetapi belum memenuhi banyak kontrak bersama.
- Enam kasus route/body native berhasil pada mock; sembilan diagnostik mereproduksi celah. Ini bukan kelulusan production.
- Tidak ada inferensi Coarena bercredential atau deployment yang dilakukan dalam audit persiapan.

Baca `docs/COARENA_INTEGRATION_AUDIT.md` jika tersedia. Periksa HEAD terbaru, branch, worktree, serta perubahan yang sudah ada. Jangan menimpa pekerjaan agent lain atau menganggap temuan baseline masih terbuka setelah kode berubah. Jika folder/provider sudah lengkap, lakukan verifikasi dan perbaiki hanya gap yang terbukti.

## 3. Aturan scope dan cara bekerja

1. Baca `AGENTS.md`, kemudian `wrappers.json`, sebelum membaca implementasi provider. Gunakan `common/wrapper_scope.py`: `select_wrappers` untuk inferensi dan `select_services` untuk instalasi/dashboard. Metadata lifecycle terbaru mengendalikan scope.
2. Provider `blackbox/` dan `opencode/` pada baseline berada dalam status pending/frozen. Jangan membaca, mengimpor, menguji, memodifikasi, mem-probe, atau mengoperasikan implementasi, credential, dan runtime keduanya. OpenCode sebagai aplikasi CLIENT tetap merupakan konsumen yang didukung; berbeda dari provider yang frozen.
3. Jangan menggunakan instruksi lama tentang seluruh provider untuk melewati pembekuan. Saring collection test sebelum import/read terhadap provider frozen.
4. Pertahankan perubahan worktree yang sudah ada. Gunakan branch kerja terpisah untuk implementasi. Setelah prompt ini diinstruksikan untuk dieksekusi, branch, commit terfokus, dan draft PR untuk hasil implementasi termasuk dalam tugas apabila akses tulis tersedia. Jangan merge atau deploy production tanpa otorisasi yang berlaku.
5. Jangan mencetak credential, membaca `.env` production untuk audit awal, menguji token yang tampak dalam contoh, atau memasukkan secret ke commit, fixture, laporan, dan screenshot. Gunakan credential palsu dan state sementara untuk test.
6. Gunakan fungsi bersama yang sudah memenuhi kebutuhan. Jangan menyalin implementasi provider berukuran besar, menciptakan gateway baru, atau mengganti seluruh arsitektur untuk satu provider.
7. Lanjutkan pekerjaan yang dapat diselesaikan tanpa input tambahan. Jika akses atau credential live belum tersedia, selesaikan kode, fixture, pengujian lokal, dan dokumentasi; tandai gate live `BLOCKED`, bukan `PASS`.

## 4. Pembacaan wajib dan jejak request

Baca versi aktual file berikut, hanya untuk provider yang eligible:

- `README.md`, `WRAPPER_CONTRACT.md`, `docs/COMPATIBILITY_LAYER.md`, `docs/TEMPLATE_WRAPPER.md`, `docs/DOCUMENTATION_INDEX.md`.
- Seluruh implementasi, requirements, contoh konfigurasi, dan unit service Coarena.
- `common/compat.py`, `common/base_wrapper.py`, `common/auth.py`, `common/body_guard.py`, `common/middleware.py`, `common/sse.py`, `common/http_headers.py`, `common/context_recovery.py`, dan helper translasi yang benar-benar relevan.
- `common/model/`, `common/model_catalog.py`, `common/model_state.py`, integrasi registry, provider/error manifests, serta model-registry service.
- Route native dan retry/resource ownership pada OpenRouter/Nous serta implementasi aktif lain yang relevan. Baca kode yang berjalan, bukan hanya docstring route.
- `start_wrappers.py`, `install.sh`, dependency constraints, CI, dashboard, production readiness checker, dan test gates yang terkait.

Untuk masing-masing API, telusuri: client route → auth/body guard → alias/model policy → protocol/call plan → key acquisition → URL/header/request → native response/SSE → outcome/metrics → release resource. Catat lokasi bukti dan gap, termasuk request cancellation dan startup failure.

## 5. Sumber upstream dan batas klaim

Periksa dokumentasi resmi terbaru:

- <https://coarena.ai/docs#models>
- <https://coarena.ai/for-agents>
- <https://coarena.ai/developers>
- <https://coarena.ai/api/openapi.json>
- <https://coarena.ai/api/v1/models>

Saat baseline, Coarena mendokumentasikan OpenAI SDK dengan base `https://coarena.ai/api/v1` dan Anthropic SDK dengan base `https://coarena.ai/api`; SDK Anthropic menambahkan `/v1/messages`. Semua jalur akhirnya berada di `/api/v1/...`.

Utamakan catalog runtime untuk inventaris yang tersedia. Pada snapshot persiapan, halaman docs menyebut 22 model, sedangkan GET publik mengembalikan 10. Jangan mengunci jumlah, nama, vendor, atau kemampuan model berdasarkan daftar statis tersebut. Simpan provenance dan waktu pengambilan; jangan menebak penyebab perbedaan.

Pisahkan: didokumentasikan upstream, tercantum dalam catalog, diterapkan wrapper, lulus mock, dan terbukti live. Dukungan SDK bukan bukti semua fitur SDK didukung oleh setiap model.

## 6. Keputusan protokol: native terlebih dahulu

### 6.1 Matriks wajib untuk Coarena

| API client lokal | URL upstream default | Perlakuan |
|---|---|---|
| `POST /v1/chat/completions` | `https://coarena.ai/api/v1/chat/completions` | Native Chat → Chat |
| `POST /v1/responses` | `https://coarena.ai/api/v1/responses` | Native Responses → Responses |
| `POST /v1/messages` | `https://coarena.ai/api/v1/messages` | Native Messages → Messages |
| `POST /v1/messages/count_tokens` | `https://coarena.ai/api/v1/messages/count_tokens` | Native token-count contract |
| `GET /v1/models` | `https://coarena.ai/api/v1/models` | Discovery; pisahkan dari inference credential reservation |

`gpt-*` melalui Messages tetap Messages. `claude-*` melalui Chat tetap Chat. Responses tidak boleh dialihkan ke Chat hanya karena keduanya berlabel OpenAI.

### 6.2 Gunakan kemampuan yang sudah ada

`COMPATIBILITY_LAYER=1/2/3` adalah pilihan dialek legacy/discovery, bukan bukti bahwa upstream hanya memiliki satu API. Pada baseline, OpenRouter dan Nous sudah memiliki `NATIVE_MESSAGES_MODE=native` selain jalur native Responses. `ProtocolProfile`, `ModelProfile.protocol_for()`, dan `build_call_plan()` juga sudah tersedia.

Evaluasi dan gunakan fasilitas tersebut sebelum menambah mekanisme baru. Representasikan dukungan per API/route secara eksplisit. Hindari boolean global yang memaksa provider dual-protocol memilih salah satu lalu menerjemahkan API lain tanpa kebutuhan.

Jika konfigurasi umum atau call plan perlu diperbaiki, buat perubahan terkecil yang menjaga perilaku semua provider aktif. Jelaskan precedence konfigurasi, capability yang diketahui/tidak diketahui, pilihan operator, dan native route. Jangan menambahkan nilai konfigurasi baru tanpa alasan, validasi, dan migration note.

### 6.3 Kapan translasi dibolehkan

Translasi hanya untuk pasangan API yang memang berbeda dan tidak memiliki jalur native yang memenuhi kebutuhan, sesuai kebijakan compatibility yang berlaku. Coarena default harus menggunakan ketiga jalur native di atas.

Pertahankan pengecualian Responses → Chat yang sudah disetujui repository untuk upstream yang benar-benar tidak menyediakan route native. Jangan memperluasnya secara sembarang pada Coarena yang mengiklankan native Responses. `404 model_not_found`, `previous_response_not_found`, auth/quota errors, parameter errors, timeout, 5xx, SSE malformed, atau `501 not_implemented` untuk fitur tertentu bukan bukti cukup bahwa route Responses tidak ada. Jika helper bersama saat ini hanya melihat status 405/501, periksa perlunya pembeda route versus fitur sebelum dipakai Coarena.

Pertahankan client-facing API, model, tools, reasoning, dan batas kehilangan informasi pada setiap adapter. Jangan menggunakan konversi sebagai cara menyembunyikan kerusakan upstream.

## 7. Transparansi dan HTTP boundary

- Pertahankan seluruh field native yang opaque: extensions, null, tool types, roles, multimodal blocks, reasoning/thinking, metadata, dan event baru. Jangan menormalisasi, menghapus, memperbaiki, atau membatasi schema native dengan whitelist translator lama.
- Preservasi payload berbeda dari framing HTTP. Body JSON idealnya diteruskan sebagai bytes saat tidak perlu diubah; inspeksi untuk policy tidak boleh mengubah isi. Perubahan alias atau operational limits harus eksplisit dan tercatat sesuai kontrak.
- Ganti credential client dengan credential upstream yang dipilih. Jangan meneruskan bearer lokal, cookie, atau credential provider lain. Gunakan header helpers bersama yang sesuai; pertahankan header versi/beta, request ID, rate limit, retry, serta metadata upstream yang aman.
- Jangan meneruskan `Content-Length`, encoding, connection-nominated hop headers, atau cookie secara buta setelah decoding/re-serialization. Gunakan `common/http_headers.py` atau perilaku yang setara dan sudah diuji.
- Gabungkan base URL dan route tepat sekali. Base SDK Anthropic dan internal API root tidak harus berupa string yang sama. Jangan melakukan koreksi string `/api/v1/messages` yang sudah benar menjadi `/api/messages`.
- Pertahankan query string yang didukung, termasuk key berulang. Jangan membangun proxy umum ke semua API akun Coarena demi kompatibilitas masa depan.
- Route internal `/health`, `/ready`, metrics, dashboard, dan props tidak boleh jatuh ke upstream. Endpoint inference tambahan hanya diteruskan melalui route/method yang terdefinisi dan disetujui.

## 8. Model identity: tidak ada substitusi diam-diam

Model pilihan client harus tetap sama selama retry, pergantian key, dan compatibility routing. Alias harus berasal dari binding eksplisit atau metadata alias resmi yang tervalidasi; jangan membuat alias berdasarkan substring atau vendor model.

Audit persiapan menemukan dokumentasi Coarena menyebut kemungkinan substitusi upstream, dengan informasi model aktual dan header substitusi. Wrapper tidak dapat memaksa provider mengubah kebijakan tersebut hanya melalui nama model pada request.

Implementasikan deteksi dan policy yang menjaga kontrak `model_substitution=false`:

1. Cari opsi upstream resmi untuk menjamin exact model. Jangan mengarang parameter opt-out. Jika tidak tersedia, tulis keterbatasannya.
2. Bandingkan model aktual dan indikator substitusi dengan model yang telah di-resolve. Perhitungkan hanya ekuivalensi alias/canonical yang sah.
3. Untuk non-stream, jangan menyajikan jawaban dari model berbeda sebagai sukses yang memenuhi permintaan. Kembalikan error berformat client, alasan, dan metadata yang aman.
4. Untuk SSE, evaluasi metadata header sebelum downstream memulai respons. Jika model baru diketahui dalam event, tentukan mekanisme inspeksi awal yang dibatasi dan tetap mempertahankan bytes native. Jangan menahan seluruh generasi atau menyusun ulang SSE menjadi protokol lain.
5. Jika exact-model fidelity tidak dapat dipastikan sebelum output, dokumentasikan batas tersebut dan jangan memberikan kualifikasi exact-model yang tidak terbukti. Jika pelanggaran diketahui setelah stream terlanjur dimulai, akhiri sebagai kegagalan, jangan menerbitkan success terminal palsu atau memulai replay.
6. Jangan retry substitusi secara tak terbatas, mengubah model request, atau mengaktifkan fallback provider untuk membuat test hijau.

Pisahkan kelulusan native protocol dari jaminan identitas model. Integrasi tidak boleh dinyatakan sepenuhnya memenuhi konsep wrappers jika risiko ini masih disembunyikan.

## 9. Key pool, retry, dan account limits

- Gunakan atau perluas pool bersama yang benar-benar memenuhi kontrak: load-aware selection, locking async, per-key cooldown, RPM, model/account-scoped classification, serta release tepat sekali.
- Dukung pola env repository, termasuk `COARENA_API_KEY`, `COARENA_API_KEY_1..N`, dan bentuk plural yang sudah digunakan jika dipertahankan untuk kompatibilitas. Deduplikasi credential. Dokumentasikan precedence, hot reload, serta migrasi konfigurasi tanpa mencetak nilainya.
- Retry seluruh credential yang eligible untuk kegagalan retriable sebelum mengembalikan exhaustion, selama belum ada output client. Simpan attempted set, deadline, dan retry budget; jangan loop tanpa batas.
- Bedakan invalid credential, key rate limit, account quota/concurrency, kapasitas model, invalid request, dan model tidak tersedia. Error model tidak boleh mematikan semua key secara global.
- Rotasi key pada akun yang sama tidak menambah kuota akun. Hormati `Retry-After`, `x-should-retry`, dan reset yang tersedia. Jangan menjadikan rotasi sebagai cara menghindari limit upstream.
- Exhaustion key yang sudah dikonfigurasi mengikuti kontrak 429. Tidak ada konfigurasi credential, auth lokal belum siap, dan readiness gagal adalah kondisi berbeda; jangan disamakan dengan exhaustion biasa.
- Reservasi harus dilepas pada success, error, retry, timeout, EOF, client disconnect, failure sebelum generator dimulai, dan shutdown.
- Jangan retry diam-diam setelah SSE sudah diterima client. Jangan mengganti credential ke akun lain untuk continuation yang dimiliki akun sebelumnya.

## 10. Streaming dan lifecycle

Gunakan satu HTTP session yang dikelola lifespan. Resource upstream harus tetap hidup sampai iterator downstream selesai. Jangan mengembalikan `StreamingResponse` dari dalam context manager session/response yang segera keluar.

Gunakan penutupan stream yang menjamin cleanup, termasuk pola `ClosingStreamingResponse` dan `on_close` bila sesuai. Tangani sentinel `IDLE` dari `iter_chunks_with_idle`; sentinel bukan bytes untuk dikirim langsung. Heartbeat hanya pada batas frame yang lengkap, tanpa menyisipkan data di tengah JSON/UTF-8/CRLF.

Bedakan total timeout, connect timeout, read-idle timeout, dan genuine upstream failure. Terapkan backpressure; jangan membuat antrean/buffer tak terbatas. Native Chat, Responses, dan Messages harus mempertahankan terminator serta error upstream masing-masing, termasuk event yang belum dikenal.

Tangani response non-stream yang lambat, leading whitespace, HTTP 200 dengan error body, invalid JSON, content-type yang salah, dan HTML error sebagai kondisi yang jelas. Jangan mengubah non-stream menjadi stream ke client, menyatakan body error sebagai sukses, atau menghapus error untuk memperbaiki tampilan.

Shutdown harus menguras request sesuai konfigurasi, menutup session, menghentikan task yang dimiliki service, dan menyimpan telemetry sesuai kontrak. Test hanya menghentikan proses yang dibuatnya sendiri.

## 11. Autentikasi dan pemisahan management

Gunakan `common.auth`: fail closed, token dapat berubah tanpa restart, Bearer dan x-api-key diterima dengan aturan yang sama seperti provider aktif, public path exact/method-gated, serta OPTIONS yang benar.

Shared module wajib tidak boleh dianggap opsional dengan fallback yang diam-diam menonaktifkan guard. Validasi dependency/config saat startup dan gagal secara jelas jika komponen wajib hilang. Konfigurasikan CORS dengan aturan origin/credential yang konsisten; jangan memakai CORS sebagai pengganti autentikasi.

Semua route inference, count_tokens, unsupported POST surface, dan fallback route terlindungi. Jangan bergantung pada loopback sebagai pengganti auth. Gunakan body-size guard dan validasi JSON object yang tidak membuang field native.

Hapus jalur yang memungkinkan credential upstream service digunakan melalui catch-all untuk `/keys`, `/key`, history/exports, pengajuan limit, feedback, atau account operations. Management API opsional harus terpisah, dibatasi scope/admin, dan tidak diaktifkan hanya karena inference berhasil.

Contoh konfigurasi wajib placeholder yang jelas. Pada baseline ada literal menyerupai API key di `.env.example`; jangan mengujinya atau menyalinnya. Bersihkan contoh dan catat perlunya pemilik merotasi jika literal tersebut pernah merupakan credential nyata. Sanitasi log/error; jangan mengganti request user secara diam-diam atas nama redaksi.

## 12. Registry, context, dan continuation

Integrasikan model catalog, provider/error manifests, call plans, capability metadata, dan observations melalui infrastruktur yang sudah ada. Tambahkan manifest Coarena bila memang belum ada. Bedakan capability provider API dengan dukungan fitur tiap model.

Catalog harus dapat refresh, menyimpan provenance/age, mempertahankan metadata provider, dan menunjukkan unknown secara jujur. Kosong, unavailable, stale, removed, dan globally retired bukan status yang identik. Jangan menandai model globally dead karena satu credential gagal.

Peta context/output limits dari metadata resmi yang sedang berlaku. Pertahankan mekanisme context recovery bersama yang berbasis evidence: tidak boleh memotong riwayat, mengganti model, memperkirakan limit lalu menyatakannya pasti, atau mengklaim context lebih besar dari bukti. Dokumentasikan setiap pengurangan output ceiling yang dibolehkan kontrak.

Native `previous_response_id` harus tetap native, beserta account affinity dan isolation principal. Verifikasi cross-principal access ketika beberapa client wrapper berbagi akun upstream; ID yang sulit ditebak bukan pengganti otorisasi. Jangan membuka seluruh history akun upstream ke pengguna wrapper.

Jika penyimpanan lokal diperlukan untuk ownership/affinity atau adapter yang sah, batasi entries, bytes, TTL, dan jelaskan multiworker/restart behavior. Jangan membuat penyimpanan isi percakapan kedua tanpa kebutuhan.

## 13. Seluruh kontrak operasional tetap berlaku

Sediakan semua public/shared surfaces yang diwajibkan `WRAPPER_CONTRACT.md` versi aktual. Buat tabel kepatuhan per klausul; jangan mendeklarasikan lengkap hanya karena tiga endpoint inference tersedia.

Required surfaces mencakup discovery/capabilities, `/api/tags`, health/readiness, metrics yang dibutuhkan dashboard, version, serta shaped unsupported responses untuk capability yang tidak ada seperti embeddings. Route dashboard lokal mengikuti kebijakan terbaru `unified_only`/410, bukan panduan template lama yang menyuruh membuat dashboard terpisah.

Integrasikan observability: total/error/retry, latency, token usage bila tersedia, active requests, cooldown/exhaustion, model outcome, dan state persist sesuai kontrak. Jangan menganggap usage yang tidak dilaporkan sebagai nol. Catat keputusan routing/policy tanpa isi prompt, response, atau secret secara default.

Dashboard unified tetap port `9106`; Coarena pada `9107`, model-registry internal `9200`. Jangan mengubah port milik layanan lain atau provider frozen. Polling status bukan native inference SSE; label keduanya harus tepat.

Selaraskan entry point `src.main:app`, WorkingDirectory, PYTHONPATH, nama unit yang diharapkan installer, dependency transaction/constraints, readiness checker, supervisor, CI, dan dokumentasi. Perbaiki mismatch `main:app` serta `coarena.service` pada baseline jika masih ada. Jangan mengoperasikan systemd production untuk menguji rendering unit.

Dokumentasikan perbedaan tarif catalog model dan biaya/kuota layanan gateway; jangan menandai model free hanya dari suffix atau nilai tarif provider tanpa sumber yang sesuai. Tulis implikasi retensi data Coarena dalam setup/runbook berdasarkan sumber resminya; jangan menjanjikan zero retention.

## 14. Rencana eksekusi dan prioritas

| Tahap | Pekerjaan | Kriteria selesai |
|---|---|---|
| A | Audit HEAD/scope, trace seluruh jalur, reproduksi gap | Peta bukti dan status per kontrak tersedia |
| B — P0 | Auth, management boundary, cleanup stream, header framing, key safety, startup/unit | Tidak ada celah baseline terkait yang tersisa pada fixture aktual |
| C — P1 | Native selection, model fidelity, pool/retry, continuation, registry/context, surfaces, dashboard | Perilaku lengkap teruji; keterbatasan upstream diberi gate eksplisit |
| D — P1 | Dependency/installer/supervisor/CI dan regresi seluruh provider aktif | Tidak ada regresi atau provider frozen yang tersentuh |
| E | SDK/client checks, docs, laporan, draft PR | Hasil bisa direview dan setiap gate punya evidence |

Jangan menunda bagian kontrak inti ke backlog hanya karena perubahan folder utama selesai. Pisahkan keterbatasan upstream yang tidak dapat diperbaiki wrapper dari pekerjaan implementasi yang memang masih harus diselesaikan.

## 15. Matriks pengujian yang wajib dibuktikan

Gunakan server upstream terisolasi dengan credential palsu, state sementara, dan port yang tidak bertabrakan. Jalankan route ASGI dan server/socket/SDK sungguhan untuk memeriksa framing serta disconnect; AST/unit checks saja tidak cukup.

| Area | Kasus minimum dan oracle |
|---|---|
| Native selection | Ketiga API × model bernama GPT/Claude; upstream path sesuai API client, translator tidak terpanggil |
| Native payload | Unknown fields/tools, null, multimodal, reasoning, beta/version, query; nilai/body native dan metadata tidak hilang |
| SSE | Split byte/UTF-8/CRLF, unknown event, idle, EOF, malformed/error, terminator, backpressure; native bytes tidak ditulis ulang |
| Cleanup | Disconnect sebelum/sesudah chunk pertama, retry cancellation, timeout, shutdown; session/key/task dilepas tepat sekali |
| Auth | Tanpa konfigurasi, token hilang/salah/non-ASCII, Bearer/x-api-key, hot reload, OPTIONS; upstream tidak dipanggil untuk request terlarang |
| HTTP headers | JSON/gzip/SSE, Content-Length berubah, hop-by-hop/Connection tokens/cookies; downstream framing valid |
| Retry/pool | Key pertama 401/429, key kedua berhasil, semua exhausted, account limit, model capacity, key duplikat; model tetap dan budget terbatas |
| No substitution | Header/body/event menunjukkan model lain; sukses pengganti tidak lolos, alias sah tidak false-positive, tidak ada replay setelah output |
| No accidental fallback | Model/account 404, parameter 400, quota/auth, feature 501, 5xx, malformed successful stream tidak memicu Chat adapter |
| Continuation/tools | Two-turn tools dan native previous_response_id, account-affinity, principal isolation, restart/expiry, full explicit history |
| Context/registry | Metadata update/stale/empty/unknown, alias, account-scoped failures, output/context rejection, tidak ada history truncation |
| Boundaries | Catch-all tidak mengakses akun/keys/exports/limit mutations; readiness/metrics lokal, unsupported route terformat |
| Operations | Rendering unit, import entry point, installer dry-run dengan fake systemctl, dependency constraints, supervisor, dashboard Coarena |
| Active regressions | Native/translation policy provider aktif tidak berubah; provider frozen dikecualikan sebelum dibaca |

Reproduksi seluruh D01–D09 dari audit persiapan jika masih relevan. Tambahkan regression test yang membuktikan hasil benar, bukan sekadar keberadaan fungsi atau assertion bahwa bug masih ada.

Gunakan OpenAI SDK untuk Chat dan Responses serta Anthropic SDK untuk Messages, stream/non-stream. Catat versi yang benar-benar dipakai. Lakukan tool loop dan continuation, bukan hanya respons teks Hello. Uji Codex, Claude Code, dan OpenCode client dengan fixture izin/tool yang terkontrol jika kemampuan runner tersedia. Jangan mengklaim seluruh versi client atau semua fitur upstream berhasil.

Jalankan production gates yang sudah ada setelah memastikan scope dan harness mendukung Coarena. Metadata-driven selection tidak otomatis membuat fixture provider-specific mengerti Coarena; periksa coverage nyata. Pisahkan fixture legacy yang sengaja menerjemahkan dari native production acceptance.

Untuk live smoke, gunakan hanya credential yang memang disediakan untuk tugas dan payload sintetis, dengan batas biaya/request. Jangan menggunakan token dari contoh atau melakukan auto-accept terms/permintaan kenaikan limit. Jika tidak tersedia, tetap selesaikan seluruh gate lokal dan tulis live qualification `UNVERIFIED/BLOCKED`.

## 16. Dokumen yang harus diserahkan

Perbarui atau buat hanya dokumen yang diperlukan, utamakan struktur yang sudah ada:

1. `docs/COARENA_INTEGRATION_AUDIT.md`: HEAD aktual, gap, evidence, status sebelum/sesudah, clause-by-clause compliance.
2. `docs/COARENA_INTEGRATION.md`: arsitektur akhir, matriks native route, konfigurasi placeholder, SDK/client setup, registry, batas fitur, data handling, dan troubleshooting.
3. `docs/COARENA_INTEGRATION_RECOMMENDATIONS.md`: prioritas, alasan, impact, acceptance criteria, serta perbedaan pekerjaan wajib versus peningkatan opsional.
4. Laporan eksekusi di `docs/reports/` dan evidence yang aman di lokasi artefak repository yang sesuai; jangan menyalin credential atau isi percakapan nyata.
5. Koreksi `README.md`, indeks docs, compatibility guide, template, dan `WRAPPER_CONTRACT.md` sejauh diperlukan untuk mencerminkan perilaku final; pertahankan maksud normatif, native policy, exact model, dan freeze.
6. Catatan rollout/rollback pada runbook yang sudah ada. Persiapkan langkah konkret tanpa menyatakan deployment telah dilakukan.

Rekomendasi opsional tidak boleh dipakai untuk menyamarkan kontrak dasar yang belum dipenuhi. Jangan memperbanyak dokumen dengan isi berulang apabila bagian dalam dokumen yang sudah ada cukup.

## 17. Definition of Done dan respons akhir agent

Integrasi baru boleh disebut memenuhi kontrak setelah:

- Seluruh klausul relevan dipetakan dan dilaksanakan; yang tidak didukung memiliki perilaku eksplisit yang diizinkan kontrak.
- Ketiga API native Coarena tidak mengalami translasi yang tidak diperlukan.
- Auth, pool/retry, streaming/cleanup, model identity, continuation/isolation, registry, operasional, dan dashboard memiliki bukti aktual.
- Tidak ada model fallback tersembunyi, perubahan provider frozen, secret baru dalam repo, atau regresi aktif yang diketahui dan disembunyikan.
- Semua gate wajib memiliki hasil `PASS`, `FAIL`, `BLOCKED`, atau `UNVERIFIED` beserta alasan. `production` pada lifecycle metadata bukan bukti kualifikasi live.
- Perubahan telah siap direview pada branch/draft PR jika akses tulis tersedia; tidak ada merge/deploy yang diklaim tanpa eksekusi dan otorisasi.

Respons akhir harus menyebut: HEAD awal/akhir, ringkasan perubahan, matriks API dan translasi aktual, hasil gate beserta versi/evidence, status exact-model terhadap substitusi upstream, keterbatasan, dokumen rekomendasi, risiko tersisa, dan handoff berikutnya. Jangan menulis klaim “100% kompatibel”, “future-proof”, “zero bug”, atau “production ready” hanya berdasarkan mock dan keberadaan metadata.

Mulai dari audit aktual, lalu lanjutkan implementasi sampai hasilnya konkret, teruji, terdokumentasi, dan siap direview.
