# Antigravity OpenAI-Compatible API Wrapper (Stateless & Isolated)

Wrapper ini memungkinkan agen AI eksternal (seperti Hermes Agent, Claude Code, OpenCode, dll.) untuk menggunakan **Google Antigravity Agent** sebagai default model backend melalui endpoint API yang kompatibel dengan OpenAI (`/v1/chat/completions`).

## Cara Kerja & Keamanan Concurrency

Wrapper ini menggunakan arsitektur **Stateless & Isolated** untuk memastikan keamanan terhadap race condition ketika diakses oleh banyak agen secara simultan:

1. **Isolasi HOME Directory**: Untuk setiap request yang masuk, wrapper akan membuat direktori `HOME` sementara yang unik (misalnya `/tmp/antigravity-home-<uuid>`) dan menyalin file konfigurasi serta token OAuth asli ke dalamnya.
2. **Stateless Prompting**: Semua riwayat pesan (`messages`) dikompilasi menjadi satu prompt terstruktur dan dijalankan melalui `env HOME=tmp_home agy --prompt "<prompt>"`.
3. **Pembersihan Otomatis**: Setelah eksekusi `agy` selesai (baik sukses maupun gagal), direktori `HOME` sementara beserta seluruh database SQLite internal yang dibuat oleh `agy` akan dihapus secara bersih.
4. **Bebas Race Condition**: Karena setiap request berjalan di environment `HOME` yang terisolasi secara penuh, tidak ada konflik penulisan pada database SQLite, sehingga aman melayani puluhan agen secara bersamaan.

---

## Model Support & Mapping

Wrapper ini mendukung seluruh model yang disediakan oleh `agy`. Ketika Anda memanggil `GET /v1/models`, wrapper akan mengembalikan daftar seluruh model beserta aliasnya.

### Model Utama yang Didukung:
* **Gemini 3.5 Flash** (`Gemini 3.5 Flash (High)`, `gemini-3.5-flash`, dll.)
* **Gemini 3.1 Pro** (`Gemini 3.1 Pro (High)`, `gemini-3.1-pro-high`, dll.)
* **Claude Sonnet 4.6** (`Claude Sonnet 4.6 (Thinking)`, `claude-3-5-sonnet`, dll.)
* **Claude Opus 4.6** (`Claude Opus 4.6 (Thinking)`, `claude-opus`, dll.)
* **GPT-OSS 120B** (`GPT-OSS 120B (Medium)`, `gpt-oss-120b-medium`, dll.)

*Catatan: Jika model tertentu kehabisan kuota pada GCP/Vertex Anda (misalnya Claude Sonnet mengembalikan error `RESOURCE_EXHAUSTED` / 429), silakan beralih ke model lain seperti Gemini.*

---

## Cara Menjalankan Server Wrapper

Jalankan script start yang sudah disediakan:
```bash
./start.sh
```

Atau jalankan secara manual menggunakan Uvicorn:
```bash
python3 -m uvicorn src.main:app --host 0.0.0.0 --port 9101
```

Server akan aktif di `http://localhost:9101`.

---

## Pengujian Concurrency

Anda dapat menguji kekuatan konkurensi (20 request secara bersamaan) dengan menjalankan script pengujian yang tersedia:
```bash
python3 src/test_concurrency.py
```

---

## Konfigurasi Klien (Claude Code, Hermes, dll.)

Ekspor variabel lingkungan berikut sebelum menjalankan agen eksternal Anda:

```bash
export OPENAI_API_BASE="http://localhost:9101/v1"
export OPENAI_API_KEY="dummy-key-antigravity"
```
Di agen Anda, pilih nama model (misalnya `gemini-3.1-pro-high` atau `claude-3-5-sonnet`).
