# Audit Insiden — Codex CLI vs Wrapper (nous :9102)

**Tanggal:** 2026-08-01
**Branch/Commit:** `main` @ `4a0485d`
**Sesi yang dianalisa:**
- `019fba0c-eb48-7122-8fc9-3142c30ef4ce` (2026-07-31 21:20 UTC / 04:20 WIB)
- `019fbc44-4000-7262-afe3-c9288bfc93d7` (2026-08-01 07:40 UTC / 14:40 WIB)
- Pembanding sukses: `019fbc50-d66c-73e2-a3aa-a678f1a3f360` (2026-08-01 07:54 UTC, `codex exec` live)
**Metode:** Forensik sesi JSONL codex, log wrapper (`wrapper_nous.log`, `nous.log`), `model-state.db`, `metrics-snapshot.json`, replay langsung HTTP ke wrapper hidup, dan satu sesi `codex exec` nyata sebagai kontrol.

---

## 1. Ringkasan Eksekutif

| Pertanyaan | Jawaban |
|---|---|
| Apakah wrapper nous memutus (truncate) stream di tengah jalan? | **TIDAK.** Bukti: semua request 200, tanpa error/warning di log; replay 1:1 pola codex (tools multi-round, `previous_response_id`, parameter lengkap codex) selalu mengalirkan teks final dengan benar; `codex exec` nyata melalui wrapper berhasil menyelesaikan jawaban final. |
| Mengapa `git pull` gagal di kedua sesi? | **Bukan bug wrapper.** Codex CLI v0.145 secara desain membuat `.git` **read-only** di sandbox `workspace-write` (dokumentasi resmi + GitHub issue openai/codex#7071). `git fetch` → `error: cannot open '.git/FETCH_HEAD': Read-only file system`. |
| Mengapa turn berakhir dengan pesan kosong ("proses berhenti di tengah jalan")? | Turn berakhir karena **upstream (model gratis `poolside/laguna-s-2.1:free`) mengembalikan completion kosong 1 token** setelah agent gagal berulang kali melakukan `git pull`. Wrapper meneruskan completion kosong itu dengan benar sesuai protokol (usage 1 token tercatat di sisi codex). |
| Temuan bug wrapper nyata dari audit ini? | **2 temuan nyata** (F1: 403 anti-bot Cloudflare salah klasifikasi → seluruh key pool ikut diblokir 300 detik; F2: `delta.content` tanpa guard tipe list → potensi 500 mid-stream). Detail di §6. |
| Status repo vs remote? | Sudah sinkron: lokal `4a0485d` = `origin/main` (verifikasi `git fetch` dari host berhasil). |

---

## 2. Kronologi Forensik

### 2.1 Sesi 019fba0c (2026-07-31 21:20Z) — 7 request, 7 pesan agent, SEMUA kosong

| # | Timestamp (Z) | Event | Bukti |
|---|---|---|---|
| 1 | 21:20:39.358 | user prompt | "pull update … lakukan analisa … report komprehensif … tanpa merubah file" |
| 2 | 21:20:44.662 | agent msg | **`"message":""`** (kosong) + 3× `exec_command` paralel (git status/remote/branch) |
| 3 | 21:20:49.537 | agent msg | kosong + `git fetch --all` → **exit 255: `.git/FETCH_HEAD` Read-only** |
| 4 | 21:20:55.586 | agent msg | kosong + `cat .git/HEAD`, `git log` |
| 5 | 21:21:00.248 | agent msg | kosong + `git log origin/main..HEAD` |
| 6 | 21:21:01.989 | agent msg | kosong (1 output token) |
| 7 | 21:21:02.005 | **task_complete** | `last_agent_message: null` — turn berakhir TANPA teks final |

### 2.2 Sesi 019fbc44 (2026-08-01 07:40Z) — turn 1 (7 request) + turn 2 "lanjutkan" (6 request)

| # | Timestamp (Z) | Event | Bukti |
|---|---|---|---|
| 1 | 07:40:45.849 | agent msg | **SATU-SATUNYA yang berisi teks**: "Saya akan memulai dengan memeriksa status repository…" + 4 tool call paralel |
| 2 | 07:40:48.677 | agent msg | kosong + `git pull origin main` → **exit 1: Read-only file system** (disetujui user, tetap gagal) |
| 3 | 07:40:51.730 | agent msg | kosong + tool `shell` → codex balas `unsupported call: shell` (model mengarang nama tool) |
| 4 | 07:40:55.035 | agent msg | kosong + `git fetch && git merge` → **exit 255 Read-only** |
| 5 | 07:40:58.358 | agent msg | kosong + `git config --global --add safe.directory` → **Read-only** (kedua config) |
| 6 | 07:41:01.430 | agent msg | kosong + `git fetch --depth=1` → **exit 255 Read-only** |
| 7 | 07:41:03.875 | agent msg | kosong (1 output token) |
| 8 | 07:41:03.886 | **task_complete** | `last_agent_message: null` |

Turn 2 ("lanjutkan", 07:41:21): 6 request, 6 pesan agent SEMUA kosong, hanya `git log`/`rev-parse` (read-only OK), berakhir `task_complete` tanpa teks.

### 2.3 Pola yang teridentifikasi

1. **19 dari 20 pesan agent kosong dalam sesi SUKSES juga** (kontrol 019fbc50). Pesan kosong + tool call adalah perilaku agent-loop yang NORMAL. Yang abnormal hanyalah **pesan FINAL yang kosong**.
2. Pada kedua sesi gagal, **completion terakhir = 1 output token** (akuntansi usage di sisi codex). Ini = upstream benar-benar menghasilkan kompletion (hampir) kosong — bukan truncation di wrapper.
3. Semua request wrapper 200, latensi sehat (875–3826 ms), **nol** error/warning (`dropping unparsable`, `timeout`, `Traceback`) di `wrapper_nous.log` (34.7k baris) dan `nous.log`.

---

## 3. Akar Masalah

### R1 — `git fetch/pull` gagal: proteksi `.git` read-only bawaan Codex 0.145
- Dokumen resmi Codex: *"Protected paths in writable roots — `<writable_root>/.git` is protected as read-only whether it appears as a directory or file"* (+ `.agents`, `.codex`).
- Sesuai persis dengan `permission_profile` di sesi: `<entry access="read"><path>/root/wrapper/.git</path></entry>`.
- GitHub issue openai/codex#7071 (*"CLI sandbox: cannot commit because .git is read-only"*) — perilaku **by design**, workaround hanya via `danger-full-access`/approval-rule git, atau permission profile kustom.
- **Konsekuensi:** agent tidak bisa melakukan tugas "pull update" yang diminta user; setelah 4–6 percobaan gagal, model berhenti.

### R2 — Turn berakhir tanpa teks final: upstream mengembalikan completion kosong
- Rantai bukti:
  1. Akuntansi token codex mencatat output terakhir = **1 token** (nilai ini bersumber dari event usage upstream yang diteruskan wrapper — bila wrapper memotong stream, jumlah token tetap mencerminkan generasi upstream penuh; di sini 1 token = upstream memang hanya menghasilkan ~1 token).
  2. Replay langsung (lihat §4) membuktikan wrapper **selalu** mengalirkan teks final dengan benar ketika upstream menghasilkan teks.
  3. Sesi kontrol nyata (`codex exec`, 07:54Z) melalui wrapper yang sama menyelesaikan jawaban final 20-request dengan benar.
- **Kesimpulan:** model gratis `poolside/laguna-s-2.1:free` mengakhiri turn dengan completion kosong setelah agent berulang kali menemui jalan buntu (sandbox read-only). Wrapper meneruskan apa adanya — perilaku protokol yang benar (ada `output_text.done("")` + `response.completed`).

---

## 4. Verifikasi Wrapper — Bukti Replay Live

Semua tes langsung ke `http://127.0.0.1:9102` (wrapper hidup, token `wrapper-local-key`, UA codex/0.145.0):

| # | Skenario | Hasil |
|---|---|---|
| 1 | `/v1/responses` non-stream, model free | ✅ 200, teks lengkap |
| 2 | `/v1/responses` stream biasa | ✅ 143 event reasoning_text.delta + 56 output_text.delta + completed; teks utuh |
| 3 | stream + tools (turn 1) | ✅ function_call.delta + output_item.done + completed |
| 4 | stream + `function_call` + `function_call_output` (turn 2) | ✅ 117 output_text.delta, teks final lengkap |
| 5 | `previous_response_id` (mekanisme stateless codex) | ✅ teks final lengkap |
| 6 | Parameter penuh codex: `instructions`, `tool_choice`, `parallel_tool_calls`, `reasoning:{effort}`, `store`, `include`, `prompt_cache_key`, `client_metadata` — 3 round tool loop | ✅ round 1: teks + tool call; round 2: teks final 1215 char lengkap |
| 7 | `codex exec` nyata melalui wrapper (sandi read-only, prompt analisis) | ✅ 20 request, jawaban final berisi teks lengkap |

**Catatan:** req `Python-urllib/3.11` diblokir Cloudflare upstream (403) — wrapper meneruskan header client transparan (`user-agent` ada di allowlist `build_forward_headers`). Ini menjadi temuan F1.

---

## 5. Kondisi Operasional yang Ditemukan

1. **Semua 5 static key (`NOUS_API_KEY_1..5`) hard-blocked `auth_or_quota`** saat audit (terpicu oleh tes replay ber-UA Python — lihat F1). Semua trafik sukses dilayani **OAuth token** (`/root/.hermes/profiles/ilma/auth.json`). **Single point of failure**: bila OAuth kedaluwarsa, wrapper tidak punya fallback sehat.
2. `model_account_status` untuk `poolside/laguna-s-2.1:free` = `available/OK` (200, consecutive_successes ≥ 2).
3. Repo lokal sudah sinkron dengan `origin/main` (verifikasi dari host berhasil; ada remote branch baru `arena/019fba14-wrappers` di upstream).

---

## 6. Temuan Bug Nyata pada Kode Wrapper

### F1 (MEDIUM) — 403 anti-bot Cloudflare diklasifikasikan sebagai `auth_or_quota` → seluruh key pool di-cooldown 300 detik
- **Rantai:** `post_nous` (`nous/src/main.py:721-733`) → body HTML Cloudflare diubah `normalize_upstream_error` (`common/translations/shared.py:136-192`) menjadi `{"error":{"type":"authentication_error","code":403,"message":"<!doctype html>…Access denied…Cloudflare…"}}` → `_is_retriable_upstream_status(403)` TRUE (`classify_upstream_error` `common/model/errors.py:87-94` selalu `ACCOUNT_FORBIDDEN` untuk 403) → `should_cooldown_key(403)` TRUE (`shared.py:300`) → `KEY_POOL.mark_failure(…, 'auth_or_quota')` untuk SEMUA key → 5 menit down untuk semua kredensial.
- **Bukti langsung:** `/health` menunjukkan `keys:5, available:0`, kelima key `hard_blocked: true, block_reason: auth_or_quota` tepat setelah beberapa request ber-UA Python. Cooldown `AUTH_KEY_COOLDOWN_SEC=300`.
- **Dampak:** bot-block yang bersifat sementara (UA-based) membuat seluruh pool kredensial tidak terpakai 5 menit; error HTML mentah juga bocor ke client SDK.
- **Fix:** deteksi isi anti-bot (marker `<!doctype`, `cloudflare`, `access denied`, `cf-ray`, `captcha`) → jangan cooldown key (bukan kegagalan kredensial), bersihkan pesan error agar tidak bocor HTML mentah.

### F2 (LOW-MEDIUM) — `translate_chunk` nous tanpa guard tipe untuk `delta.content`
- `nous/src/main.py:1853`: `if delta.get("content"): events.append(self.delta(delta["content"]))`. Bila upstream mengirim `content` sebagai **list** (gaya OpenAI multi-part: `[{"type":"text","text":"…"}]` — didukung wrapper lain), maka `self.final_text + <list>` → `TypeError` → HTTP 500 **mid-stream**, turn mati persis seperti gejala yang dikeluhkan user.
- Wrapper pembanding `nvidia-python/src/responses_compat.py:603,674` sudah pakai guard `isinstance(d.get('content'), str)`. Nous tertinggal.
- **Fix:** guard `isinstance(content, str)`, dan untuk list: gabungkan part `type=="text"` (meniru `_convert_content_parts` nvidia).

### F3 (INFO) — Parameter client di-drop diam-diam (transparansi)
- Codex mengirim `reasoning:{effort:"xhigh"}`, `store`, `include`, `prompt_cache_key`, `client_metadata`. `responses_to_chat` (`nous/src/main.py:1012-1017`) hanya meneruskan subset tetap; `reasoning` dsb di-drop tanpa log. Tidak menyebabkan kegagalan (model tetap reasoning — 143 event reasoning di tes), tapi intent client tidak tersampaikan.
- **Fix (opsional):** log-drop satu baris; forward `reasoning.effort` → `reasoning_effort` (hanya jika upstream terbukti menerimanya).

### F4 (INFO) — Forensik insiden sulit: tidak ada logging body request/response
- `wrapper_nous.log` hanya mencatat method/path/status/latency (`request_id=N/A` selalu). Menelusuri "siapa memotong stream" membutuhkan replay manual seperti audit ini.
- **Fix (opsional):** log ringkas (model, stream?, input token, output token, finish_reason, key label, OAuth vs static) per request; jangan pernah log isi percakapan.

---

## 7. Verdict Wrapper vs Codex

| Lapisan | Verdict | Keterangan |
|---|---|---|
| Wrapper nous (streaming /v1/responses) | ✅ BEBAS dari truncation | Replay + sesi kontrol buktikan teks final selalu mengalir |
| Wrapper nous (retry/key pool) | ⚠️ F1 | 403 anti-bot salah klasifikasi |
| Wrapper nous (robustness) | ⚠️ F2 | `delta.content` list → potensi 500 mid-stream |
| Codex CLI sandbox | ❌ By-design | `.git` read-only memblokir `git fetch/pull` (bukan wrapper) |
| Upstream model free | ⚠️ | Completion kosong 1-token saat agent jalan buntu |

**Akar insiden yang dirasakan user:** bukan wrapper memotong proses, melainkan kombinasi (a) sandbox codex memblokir `git pull` yang diminta user, dan (b) model free mengakhiri turn dengan completion kosong setelah berkali-kali gagal — sehingga tidak ada laporan final yang pernah ditampilkan.

---

## 8. Rekomendasi

1. **User/codex config** — agar `git pull` bisa jalan dari codex CLI di repo ini:
   - Gunakan sandbox/permission profile yang mengizinkan tulis `.git` (contoh `--sandbox danger-full-access` bila aman), ATAU
   - Approval-rule `git` di `~/.codex/rules/default.rules` (bekerja parsial — lihat issue #7071), ATAU
   - Jalankan `git pull` dari luar codex (terbukti bekerja dari host) dan biarkan codex menganalisa.
2. **Wrapper (patch pada audit ini):** F1 + F2 (lihat §9).
3. **Opsional:** F3/F4 hardening.

---

## 9. Rencana Patch (diterapkan setelah audit)

| ID | File | Perubahan |
|---|---|---|
| F1 | `common/translations/shared.py` | Tambah deteksi anti-bot HTML pada 403 (401/403 dengan body anti-bot tidak memicu cooldown key; pesan error dibersihkan) |
| F1 | `common/model/errors.py` | `classify_upstream_error`: 403 anti-bot → state `UNKNOWN`/transient (bukan `ACCOUNT_FORBIDDEN`/rotate) |
| F2 | `nous/src/main.py` | `translate_chunk`: guard `isinstance(content, str)` + ekstraksi text dari list multi-part |

Setelah patch: jalankan `pytest tests -q` + verifikasi manual endpoint.

---

## 10. Status Patch — DITERAPKAN & TERVERIFIKASI (2026-08-01)

### Perubahan kode

| ID | File | Perubahan aktual |
|---|---|---|
| F1 | `common/model/errors.py` | `looks_anti_bot_challenge(payload)`: deteksi HTML anti-bot (prefix `<!doctype`/`<html>/<?xml`, marker kuat `cf-ray`/`cf-chl`/`anti-bot protection blocked the request`, atau ≥2 marker koroborasi). `classify_upstream_error(403, ...)`: body anti-bot → `TRANSIENT_FAILURE`/`ANTI_BOT_CHALLENGE` dengan `retry_same_model=True` (tetap coba semua key), TANPA `rotate_key`/`account_scoped` |
| F1 | `common/translations/shared.py` | `should_cooldown_key`: 401/402/403 dengan body anti-bot → `False` (pool tidak mati 300s). `normalize_upstream_error`: pesan HTML mentah diganti `"Upstream anti-bot protection blocked the request (transient transport block, not an authentication failure)"` dengan `type=api_error` (bukan `authentication_error`), dipakai SEBELUM mapping status→type |
| F2 | `nous/src/main.py` `translate_chunk` | Guard `isinstance(content, str)`; bila list, ekstrak part `text` (parity dengan `nvidia-python/responses_compat.py:603/674`) |

### Verifikasi

1. `pytest tests -q` → **127 passed**.
2. **Uji live F1** (fake upstream 403 HTML Cloudflare + instance 9109, UA `python-urllib/3.11`):
   - Response error bersih: `"Upstream anti-bot protection blocked the request ..."`, `type: api_error`, tanpa HTML bocor. Sebelum patch: HTML mentah bocor + semua key `hard_blocked auth_or_quota ~65s`.
   - `/health` setelah 4 key dicoba: `available: 4/4`, `hard_blocked: False` untuk semua — **tidak ada cooldown**.
   - Request kedua langsung berhasil diproses (pool tetap hidup).
3. **E2E 9102 production** (UA `codex/0.145.0`): `poolside/laguna-s-2.1:free` → 200 + content `'ok'`.
4. Semua wrapper (9101 nvidia-python, 9102 nous, 9103 opencode, 9104 blackbox, 9106 openrouter) di-restart dengan kode baru; `/health` semua OK.

### Catatan penting (bootstrap `common`)

- `nous/src/main.py:55` menyisipkan `Path(__file__).parents[1]` ke `sys.path` — sejak refactor `src/`, `parents[1]` = `/root/wrapper/nous` (bukan `/root/wrapper`), jadi komentar "Ensure /root/wrapper" sudah usang; yang menyelamatkan impor adalah bootstrap `.git`-walk di baris 42 (`parents[2]`). Fix opsional: ganti `parents[1]` → `parents[2]` agar konsisten.
