# Runbook Hot Swap API Key — Spesifikasi dan Operasional

Tanggal: 15 November 2026. Berlaku untuk seluruh wrapper aktif: NVIDIA (9101), Nous (9102), OpenRouter (9103), Coarena (9107). Implementasi: `common/env_reload.py` (layanan bersama) + `sync_keys` state-preserving pada tiap pool. Bukti gate: `docs/artifacts/hotswap_fleet.json` (40/40) dan `docs/artifacts/fleet_snapshot_diagnostics_post_fix.json` (24/24).

Dokumen ini adalah spesifikasi operasional yang dijanjikan master prompt audit final (§7 dan §14).

## 1. Sumber konfigurasi authoritative dan precedence

| Sumber | Peran |
|---|---|
| OS environment saat startup (systemd `EnvironmentFile`, `export` shell) | Menang saat boot untuk variabel yang juga ada di file (semantik `setdefault` bawaan) |
| File `.env` provider (default `<provider>/.env`, dapat dipindah via `WRAPPER_ENV_FILE`) | **Sumber runtime authoritative** untuk variabel yang didefinisikannya |

Aturan kepemilikan (ownership):

1. Variabel yang didefinisikan file dimiliki file: setelah startup, perubahan nilai pada file berlaku dengan semantik override.
2. Menghapus baris dari file menghapus variabel tersebut dari `os.environ` — tetapi hanya bila nilai saat ini masih sama dengan nilai terakhir yang diterapkan file (sumber lain yang mengambil alih tidak diganggu).
3. Variabel yang tidak pernah didefinisikan file apa pun tidak pernah disentuh (milik systemd/shell/control API).
4. File hilang atau berisi nol key adalah **kondisi valid**: seluruh variabel milik file dihapus dan pool boleh kosong tanpa restart.
5. File tidak terbaca/korup (NUL byte, non-UTF-8, I/O error) mempertahankan last-known-good; kegagalan terlihat di status (§5) dan tidak ada penerapan parsial.

Catatan systemd: mengedit `EnvironmentFile` tidak mengubah proses yang sudah berjalan. Jalur reload yang didukung adalah file `.env` provider (di-watch oleh layanan bersama). Operator yang memakai `EnvironmentFile` harus tetap memindahkan key ke `.env` (atau `WRAPPER_ENV_FILE`) untuk hot swap.

## 2. Naming, deduplikasi, dan identitas

- Pola resmi per provider: `NVIDIA_API_KEY(_<n>)?`, `NOUS_API_KEY(_<n>)?`, `OPENROUTER_API_KEY(_<n>)?`, `COARENA_API_KEY(_<n>)?`. Alias `SOFT_LIMIT_RPM`/`HARD_LIMIT_RPM` lama tetap dihormati saat boot.
- Nilai dideduplikasi; urutan mengikuti nama variabel terurut sehingga reorder tidak mengubah identitas.
- **Identitas credential = nilai api_key**, bukan label. State (cooldown, jendela RPM, in-flight, counter, model block) melekat pada object `KeyEntry` yang sama selama credential bertahan; label `key1..keyN` hanya tampilan dan dapat dinomori ulang setelah reorder.
- Credential yang dihapus segera retired dari akuisisi baru setelah generation diterapkan. Request yang sudah memegangnya menyelesaikan stream/request pada object-nya sendiri dan release tetap tepat sekali; `release()` pada credential retired tidak menurunkan counter pool-level (perbaikan `_in_flight_total`).
- Retry dalam satu request menggunakan exclusion berbasis **api_key** (nous ikut disamakan; sebelumnya label ordinal bisa tertukar oleh reorder mid-request).

## 3. Perilaku watcher

- Event yang ditangani: `on_modified` (tulis biasa), `on_created` (file dibuat ulang), `on_moved` (atomic rename/save — **destinasi** diperiksa, bukan sumber), `on_deleted` (file dihapus → snapshot kosong valid).
- Path deterministik: hanya file `.env` yang di-resolve dari `WRAPPER_ENV_FILE` atau `<provider>/.env`; event pada file lain (mis. `.env.incoming`, `.env.tmp`) diabaikan. Watch non-rekursif pada direktori file.
- Burst event editor dikoaleskan lewat debounce 0,25 detik (dapat diatur per instans layanan).
- Reload selalu dieksekusi di event loop service yang dicapture sekali di `start()`; penjadwalan dari thread watcher memakai `asyncio.run_coroutine_threadsafe` (perbaikan atas `asyncio.get_event_loop()` dari thread watchdog yang gagal di Python 3.10+).
- **Fallback polling**: bila `watchdog` tidak terpasang, thread polling dengan interval 1 detik (bandingkan hash konten hasil parse) menjaga hot swap tetap hidup; mode aktif terekspos di status. Ketiadaan watchdog tidak mematikan hot swap secara diam-diam.
- `WRAPPER_SKIP_DOTENV` (nilai truthy apa pun) mematikan layanan — dipakai untuk isolasi test; **test hot swap hidup wajib memakai `WRAPPER_ENV_FILE` + file sementara, bukan flag ini.**

## 4. Bound reload dan deployment

- Bound terukur pada filesystem lokal yang diuji: **≤ 5 detik** dari tulis file hingga generation aktif (typical ~0,3 dtk; debounce 0,25 dtk + latensi event). Gate `hot_swap_acceptance.py` menegakkan angka ini pada 4 provider.
- Multiworker: deployment resmi saat ini satu worker per service (systemd unit + `wrappers.json`); kontrak generation berlaku per proses. Menjalankan beberapa worker berarti setiap worker membaca file yang sama dan menerapkan generation-nya sendiri — batas per akun upstream tetap berlaku per key, tidak dilipatgandakan oleh worker karena key-nya identik. Pergeseran antar-worker hanya sebesar bound reload masing-masing.
- Atomic replace (tulis `.env.incoming` lalu `rename`) dan tulis biasa keduanya didukung dan teruji.

## 5. Observability

Status layanan dapat dibaca dari proses (log `[env-reload]`) dengan field: `source` (path file), `watcher` (`watchdog`/`polling`/`disabled`/`stopped`), `generation` (monotonic), `last_result` (`ok`/`failed`/`not_run`), `last_error`, `last_event_at`, `last_applied_at`, `last_reload_ms`, `owned_variables`. Log generation mencatat jumlah set/removed. Credential mentah tidak pernah ditulis ke log; test hidup memakai fingerprint sha256 aman.

## 6. Skenario operasional cepat

| Aksi operator | Hasil yang diharapkan (tanpa restart) |
|---|---|
| Tambah key baru ke `.env` | Generation baru ≤ 5 dtk; key segera eligible; state key lama utuh |
| Hapus satu baris key | Key retired dari akuisisi baru ≤ 5 dtk; stream aktif menyelesaikan; counter tidak bocor |
| Ganti nilai / reorder / duplikat | Identitas dan state credential tidak berubah; duplikat tidak dihitung ganda |
| Kosongkan seluruh file (atau hapus file) | Nol eligible; request baru gagal jujur (429 + body error); service hidup |
| Tambah kembali key | Pulih otomatis; request berikutnya dilayani |
| File korup sementara | Last-known-good dipertahankan; status `failed`; tidak ada penerapan parsial; perbaiki file → pulih otomatis |
| Restart service | Snapshot boot dicatat ulang; watcher dimulai setelah pool load; shutdown menghentikan dan join thread watcher |

## 7. Batasan yang diakui

- Pemisahan partial-write yang benar-benar terpotong (mis. key terpotong saat editor menulis) tidak dapat dideteksi secara umum; debounce mengurangi jendela, dan key pendek (< 10 karakter) ditolak oleh pemindaian pool.
- File `~/.env` lama hanya dibaca nous saat boot bila `WRAPPER_ENV_FILE` tidak di-set; jalur runtime selalu file tunggal yang deterministik.
- Status reload saat ini terekspos lewat log; endpoint status HTTP per provider dicatat sebagai rekomendasi P2.
