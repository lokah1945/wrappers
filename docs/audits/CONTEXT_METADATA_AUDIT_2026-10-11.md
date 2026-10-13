# Audit context metadata — 11 Oktober 2026

Audit dimulai 11 Oktober dan verifikasi akhir dilanjutkan 12 Oktober 2026 (WIB).

Perbaikan ini mencakup wrapper produksi yang dipilih melalui
`common/wrapper_scope.py`: NVIDIA, Nous, OpenRouter, dan integrasi registry
bersama. Provider berstatus pending tidak diperiksa atau diubah. Perubahan
worktree yang sudah ada dipertahankan. Tidak ada commit, push, deployment,
restart layanan produksi, atau pengujian inferensi dengan kredensial produksi.

## Temuan dan sumber nyata

Jalur discovery NVIDIA sebelumnya memakai nilai default/family heuristic dan
tidak menggabungkan metadata publik NVIDIA secara konsisten pada semua respons
model. Nilai default 131.072 tidak membuktikan batas model tertentu. Metadata
catalog juga harus tetap utuh ketika disimpan dan diteruskan ke registry.

Pemeriksaan publik tanpa autentikasi atau inferensi memberikan hasil berikut.
Nilai ini adalah kapasitas yang diiklankan provider pada saat pemeriksaan;
bukan hasil pengiriman prompt sebesar batas tersebut.

| Model/rute | Context yang dipublikasikan | Bukti |
| --- | ---: | --- |
| NVIDIA hosted `nvidia/nemotron-3-ultra-550b-a55b` | 1.048.576 | [NGC featured models](https://assets.ngc.nvidia.com/products/api-catalog/featured-models.json), [Build model specification](https://build.nvidia.com/nvidia/nemotron-3-ultra-550b-a55b/build) |
| Nous, ID Ultra yang sama | 262.144 | [Catalog Nous](https://inference-api.nousresearch.com/v1/models) |
| OpenRouter, ID Ultra yang sama | 262.144; `top_provider.context_length` 202.800 | [Catalog OpenRouter](https://openrouter.ai/api/v1/models) |
| OpenRouter Ultra `:free` | 1.000.000 | [Catalog OpenRouter](https://openrouter.ai/api/v1/models) |

Snapshot publik pertama mencatat 80 model di endpoint NVIDIA `/v1/models`,
tanpa field context eksplisit. Karena itu hosted NVIDIA memakai publikasi
NGC dan spesifikasi Build yang cocok dengan ID model persis. Keduanya tidak
dipakai untuk mengklaim batas endpoint NIM kustom. Catalog Nous (422 model)
dan OpenRouter (458 model) menyediakan context eksplisit pada snapshot tersebut.
Detail sumber dan nilai mentah terpilih tersedia dalam
[snapshot metadata publik](../../artifacts/context-audit-20261011/public-uplink-metadata.json).

Parser Build memvalidasi canonical URL, ID komponen model, nilai numerik, dan
framing data halaman. Spesifikasi model terkait, contoh request, label `1M`,
atau nilai yang bertentangan tidak menjadi sumber angka. Struktur halaman yang
tidak didukung menghasilkan `unknown`.

Pemeriksaan ulang memakai parser final menemukan **14 dari 80 ID catalog
NVIDIA memiliki context numerik terverifikasi** melalui publikasi NVIDIA;
**66 masih unknown**. Sebanyak 78 halaman Build diperiksa dengan concurrency 3;
12 memberi spesifikasi yang dapat diverifikasi, ditambah dua model catalog
aktif yang sudah tersedia melalui NGC. Beberapa ID merupakan model non-chat.
Nilai unknown tidak berarti kapasitas nol atau model tidak dapat digunakan.
[Laporan cakupan final](../../artifacts/context-audit-verified-20261011/nvidia-published-context-coverage.json)
menyimpan daftar lengkap, provenance, waktu pemeriksaan, dan hash parser yang
digunakan. Angka Ultra dikonfirmasi ulang dari NGC: context 1.048.576 dan batas
output publikasi 8.192; kedua angka tersebut berbeda fungsi.

## Perilaku setelah perbaikan

- Context/input/output berasal dari field eksplisit dengan sumber, scope,
  waktu pemeriksaan, expiry, dan status stale/unknown. Nilai tidak ditebak
  dari nama/family model dan tidak disalin antar-provider.
- Metadata model disimpan di SQLite, cache memori, serta cache publikasi
  NVIDIA atomik. Refresh berkala mengikuti konfigurasi; request inferensi
  tidak memanggil endpoint context/catalog. Concurrent cold discovery
  Nous/OpenRouter digabung menjadi satu fetch.
- Metadata endpoint mendapat prioritas. Hasil gabungan yang dimuat ulang dari
  disk tetap membedakan sumber endpoint dan publikasi, sehingga pembaruan
  publikasi tidak tertutup nilai cache lama. Cache dari endpoint berbeda atau
  tanpa provenance yang dapat diverifikasi tidak dipromosikan menjadi hasil
  discovery baru.
- Model/deployment ID tetap persis. Prefix seperti `stg/` tidak dihapus saat
  discovery karena dapat membedakan model dengan batas berbeda.
- Saat upstream memberi error context dengan angka pasti, wrapper dapat
  menurunkan satu field budget output lalu mencoba sekali lagi dengan model
  dan input yang sama. Budget hasil perbaikan tetap dipakai bila kemudian
  terjadi pergantian key. Budget reasoning dan minimum output dilindungi.
- Input, system prompt, history, tool call/result, continuation ID, dan native
  SSE tidak dihapus untuk mengejar keberhasilan. Jika input sendiri terlalu
  panjang, wrapper mengembalikan error asli dengan header petunjuk compaction.
  Tidak ada algoritma yang dapat mengirim input arbitrer melewati batas keras
  tanpa mengubah isinya.
- Bukti numerik dari error disimpan dengan scope provider, fingerprint
  kredensial, model, dan endpoint selama satu jam. Bukti ini tidak menimpa
  kapasitas catalog global. Prompt, key mentah, dan teks error tidak disimpan
  dalam tabel observasi context.

Konfigurasi, format metadata, prioritas sumber, dan rincian recovery dijelaskan
di [CONTEXT_METADATA.md](../CONTEXT_METADATA.md).

## Status verifikasi

- **Delapan gate lulus**: runtime, SDK, matrix, compatibility, HTTP acceptance,
  soak, transparency, dan unit. Hasil akhir digabung dari run terakhir yang
  sukses untuk tiap gate; empat gate pertama diulang setelah perubahan terakhir
  pada identitas model NVIDIA. Lihat
  [ringkasan gate](../../artifacts/context-audit-verified-20261011/production-gates-summary.json).
- Suite unit/regresi terbaru: **1.065 lulus, 105 skipped**. Pengecualian
  diterapkan sebelum tes membaca/import provider pending, termasuk tes lama
  yang menggabungkan beberapa provider. Log mencantumkan alasan tiap kelompok.
- Penerimaan HTTP: **195 lulus, 0 gagal**, termasuk **54 pemeriksaan context,
  cache, dan recovery** pada Chat, Messages, Responses, serta stream/nonstream.
- Soak terisolasi: **6.330 request berhasil, 0 gagal** pada tiga wrapper aktif
  (15 detik per wrapper, concurrency 6). Ini uji beban singkat, bukan bukti
  stabilitas operasional jangka panjang atau kapasitas provider nyata.
- Tes recovery memastikan output turun dari 4.000 ke 2.000 berdasarkan error
  upstream, request kedua memakai key yang sama, dan budget yang telah turun
  tetap dipakai ketika request berikutnya harus berganti key akibat quota.
- Pengulangan suite unit memperbaiki fixture fake transport yang belum
  menerima parameter state recovery baru. Kegagalan percobaan awal disimpan
  dalam `unit-first-attempt.log`; hasil terbaru ada dalam `unit.log`.

Laporan gate dan cakupan metadata final berada di
[`artifacts/context-audit-verified-20261011/`](../../artifacts/context-audit-verified-20261011/).
Manifest [source-state.json](../../artifacts/context-audit-verified-20261011/source-state.json)
mencatat base commit dan SHA-256 sumber worktree yang diuji; base commit saja
tidak mewakili perubahan yang belum di-commit. Hasil terkontrol menggunakan
port terisolasi, fake credentials, dan state sementara.

Perubahan ini masih berupa source yang diuji. Revisi layanan produksi tidak
diubah atau diklaim sudah memakai implementasi ini. Pengujian context penuh
1M token terhadap layanan inferensi nyata belum dilakukan.
