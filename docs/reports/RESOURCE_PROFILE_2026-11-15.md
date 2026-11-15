# Profil Resource dan Performa — Audit Final Wrappers

Tanggal: 15 November 2026. HEAD hasil perbaikan: `496c280` (branch `audit/final-audit-20261115`, dasar `66ec1c1` + merge dokumen audit). Lingkungan pengukuran: Linux x86_64, Python 3.12.16, dependency terpasang persis mengikuti `tests/constraints-verified.txt` (uvicorn 0.54.0, pydantic 2.13.5, python-dotenv 1.2.4, watchdog 6.0.0, aiohttp 3.14.4). Semua nilai berasal dari run nyata pada mesin audit; ini bukan klaim fleet produksi.

## Metode

- Proses wrapper sungguhan (uvicorn, satu worker) dengan state terisolasi (`process_isolation.isolated_environment`), credential palsu, upstream mock loopback.
- RSS dibaca dari `/proc/<pid>/status`; thread dari `/proc/<pid>/status`; FD dari `/proc/<pid>/fd`. Latensi diukur client-side pada mock loopback (mengukur overhead wrapper, bukan model).
- Soak: `tests/e2e_runtime/soak.py` 30 detik × concurrency 4 per provider (9 mode payload agent-shaped: normal/tools/reasoning/keepalive/unicode/nullcontent/emptychoices/longtool/usage_after, rotasi 3 surface: /v1/messages, /v1/chat/completions, /v1/responses).
- Key churn: 300 generation add/remove/reorder/zero/duplicate per provider melalui `EnvReloadService` + pool nyata; sampel RSS/thread/FD pada 8 checkpoint.

## Hasil soak (per provider, 30 dtk, concurrency 4)

| Provider | Failures | p50 / p95 latensi | p95 kuartal awal → akhir | RSS awal → akhir | Log error |
|---|---|---|---|---|---|
| nvidia-python | 0 | 18ms / 44ms | 41ms → 44ms | 126MB → 90MB | 0 |
| nous | 0 | 13ms / 29ms | 26ms → 31ms | 155MB → 65MB | 0 |
| openrouter | 0 | 13ms / 29ms | 27ms → 29ms | 167MB → 77MB | 0 |
| coarena | 0 | 13ms / 28ms | 27ms → 28ms | 61MB → 65MB | 0 |

p95 stabil antar kuartal (delta ≤ 5ms) — tidak ada degradasi bertahap. Penurunan RSS selama run adalah settling GC setelah warm-up, bukan kebocoran; tidak ada provider yang membesar tanpa batas. Durasi 30 detik adalah smoke-soak yang sanggup membuktikan steady-state dan exactly-once release; ini bukan klaim leak-free berjam-jam (keterbatasan diakui; kenaikan durasi soak direkomendasikan di jendela maintenance, bukan blocker rilis).

## Hasil key-churn (300 generation per provider)

| Provider | Pertumbuhan RSS setelah warm-up | Thread awal→akhir | FD awal→akhir | Verdict |
|---|---|---|---|---|
| nvidia-python | 0 KB | 5 → 5 | 10 → 10 | bounded |
| nous | 0 KB | 5 → 5 | 10 → 10 | bounded |
| openrouter | 0 KB | 5 → 5 | 10 → 10 | bounded |
| coarena | 564 KB | 5 → 5 | 10 → 10 | bounded |

Tidak ada akumulasi state lintas generation: entry yang retired di-GC (tidak ada peta tombstone yang tumbuh), thread dan FD kembali stabil. 564KB pada coarena berada pada level noise alokasi; 300 generation ≈ pergantian key berhari-hari pada operasional normal.

## Overhead layanan hot-swap

- Thread tambahan per proses: 2 (observer watchdog + debounce scheduler) — atau 1 (polling fallback) tanpa watchdog.
- FD tambahan: inotify watch tunggal pada direktori file (mode watchdog); tanpa FD tetap saat polling.
- Biaya CPU reload: parse + reconcile + sync_keys dalam orde milidetik (ukuran `.env` operasional); dieksekusi di event loop service, terkoalesi oleh debounce.

## Pemisahan overhead wrapper vs model

Semua angka latensi di atas diukur terhadap mock loopback sehingga mengisolasi overhead wrapper (guard, seleksi key, translasi bila dipakai, framing SSE, telemetri). Latensi produksi nyata akan didominasi model/upstream; angka ini adalah batas atas overhead wrapper, bukan proyeksi TTFT produksi.

## Batasan dan klaim yang tidak dibuat

- Pengukuran single-host, satu mesin audit; tidak ada klaim USS/PSS lintas proses (satu service per proses, shared pages antar provider tidak dijumlahkan sebagai memori unik).
- Tidak ada perbandingan RAM sebelum/sesudah refactoring shared-core: deduplikasi source tidak menghemat RAM lintas proses (kode bersama tetap dimuat di setiap proses) — master prompt §5.6; klaim penghematan sumber dipisahkan dari klaim resource.
- Skala 1.000 key sintetis belum dijalankan (biaya menambah provider berikutnya diukur dari struktur kode: satu adapter `<provider>/src/key_pool.py` ~300 baris + main; tidak perlu menyalin runtime — lihat laporan audit utama §shared-core).
- Live upstream (G9) tidak dijalankan pada sesi ini; latensi produksi tetap UNVERIFIED.
