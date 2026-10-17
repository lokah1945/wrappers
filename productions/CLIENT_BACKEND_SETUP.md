# Koneksi klien ke backend aktif

Scope mengikuti `wrappers.json`: NVIDIA, Nous, OpenRouter dan dependency model-registry. OpenCode **aplikasi klien** dapat digunakan; provider `opencode/` dan `blackbox/` tetap pending/frozen.

| Backend | Origin | OpenAI base URL | Claude Code endpoint |
| --- | --- | --- | --- |
| NVIDIA | `http://127.0.0.1:9101` | `http://127.0.0.1:9101/v1` | `/v1/messages` |
| Nous | `http://127.0.0.1:9102` | `http://127.0.0.1:9102/v1` | `/v1/messages` |
| OpenRouter | `http://127.0.0.1:9103` | `http://127.0.0.1:9103/v1` | `/v1/messages` |

Dashboard tunggal berada di **`http://127.0.0.1:9106/`**: halaman home
menampilkan status global, lalu dropdown di navigasi atas membuka dashboard
wrapper pilihan. Dashboard bukan endpoint inferensi; gunakan origin backend
di tabel untuk konfigurasi agent. Port 9103–9104 tetap dicadangkan untuk
provider pending yang frozen, sedangkan model-registry internal tetap 9200.
Port 9100 sudah dipakai layanan catalog/MCP di luar scope wrapper, sehingga
9105 adalah slot kosong terendah. Origin backend yang sudah ada tetap sama.

Token klien adalah `BEARER_TOKEN` lokal milik wrapper yang dipilih. API key provider tetap di server dan dirotasi oleh wrapper. Pilih **ID model persis** dari `/v1/models`; jangan mengganti model ketika melakukan retry. Untuk klien di mesin lain, ganti origin dengan URL deployment yang dapat dijangkau.

Pada **2026-10-11**, tiga CLI dan tiga SDK tool loop per backend lulus melalui endpoint produksi: **18 lulus, 0 gagal**. Setiap tool loop menjalankan function call, hasil function, dan jawaban akhir pada Chat, Responses native, atau Messages. [Bukti live terbaru](reports/live-client-qualification-20261011.json) mencatat model persis dan versi klien; [rollout terbaru](reports/active-production-rollout-20261011.json) mencatat 31 pemeriksaan lulus serta source final yang dimuat service. Sembilan tugas baca berkas oleh CLI agent juga lulus, sehingga total qualification live adalah **27/27**. Release lulus verifikasi produksi untuk model, versi klien dan fitur yang diuji; lihat [readiness 2026-10-11](PRODUCTION_READINESS_2026-10-11.md) serta [pemeriksaan service final 14/14](reports/active-production-final-verification-20261011.json).

| Backend | ID model persis | CLI smoke + SDK tool loop live 2026-10-11 |
| --- | --- | --- |
| NVIDIA | `nvidia/nemotron-3-super-120b-a12b` | [6/6](reports/live-client-qualification-20261011.json) |
| Nous | `poolside/laguna-xs-2.1:free` | [6/6](reports/live-client-qualification-20261011.json) |
| OpenRouter | `liquid/lfm-2.5-2.6b:free` | [6/6](reports/live-client-qualification-20261011.json) |

Qualification SDK terbaru memakai schema tool terstruktur dengan `strict=true`, argumen persis `{"value":"OK"}`, dan history lengkap. Argumen JSON rusak atau field tambahan tetap gagal validasi. Ini uji dengan prompt dan budget terbatas, bukan bukti semua model, workflow agent, SLA upstream atau pembaruan masa depan. Percobaan sebelumnya yang gagal tetap tersimpan: [attempt 1](reports/live-client-qualification-attempt1-20261011.json) dan [attempt 2](reports/live-client-qualification-attempt2-20261011.json); hasil tersebut tidak diubah menjadi lulus oleh rerun.

Model OpenRouter yang diuji pernah menghasilkan argumen JSON rusak untuk schema object kosong; native payload tetap diteruskan, sehingga jangan memakai hasil schema terstruktur di atas untuk mengklaim semua schema tool lulus. Pada Nous, jawaban smoke pernah menambah tanda baca dan gagal syarat literal `OK`; final rerun lulus tanpa normalisasi output atau mengganti model. Workflow yang memerlukan format persis tetap perlu memvalidasi output model. [Bukti attempt 1](reports/live-client-qualification-attempt1-20261011.json), [bukti attempt 2](reports/live-client-qualification-attempt2-20261011.json).

Bukti **historis 2026-10-07** tetap tersedia: [rollout NVIDIA/Nous 23 pemeriksaan](reports/nvidia-nous-production-rollout-20261007.json), [tool loop NVIDIA 3/3](reports/nvidia-python-production-tool-loops-20261007.json), [tool loop Nous 3/3](reports/nous-production-tool-loops-20261007.json), serta [tool loop OpenRouter 3/3](reports/active-production-tool-loops-20261007.json). Native Responses accumulator OpenAI 2.31/2.54 juga lulus pada staging Nemotron dan Laguna; lihat [SDK NVIDIA 2.54](reports/nvidia-native-latest-sdk-qualification-20261007.json) dan [CLI, tools, SDK Nous staging](reports/nous-native-production-clients-20261007.json). Pada Nous, request nyata juga pulih dari 429 melalui key lain sambil mempertahankan Laguna persis. Hasil staging dan snapshot lama tetap terpisah dari hasil release terbaru.

Batas native yang sebelumnya ditemukan tetap berlaku pada **`openai/gpt-oss-20b`** di NVIDIA dan **`upstage/solar-mini4:free`** di Nous; lihat [diagnosis model tersebut](reports/active-native-response-diagnosis-20261007.json). Memilih model qualified dilakukan oleh klien/operator, tanpa fallback model otomatis. Native history di upstream ketika key berganti belum dibuktikan oleh roundtrip dengan history lengkap.

Versi terpasang yang digunakan pada qualification live terbaru: Claude Code **2.1.202**, Codex CLI **0.160.1**, OpenCode **1.17.20**, SDK OpenAI **2.54.0**, dan SDK Anthropic **0.125.0**. Source staging dan deployment produksi dicatat terpisah; hasil staging tidak membuktikan proses produksi sudah memuat source yang sama.

## Environment bersama

Pilih **satu** pasangan origin/model berikut. Contoh ini tidak mengganti model ketika terjadi limit.

```bash
# NVIDIA
export WRAPPER_ORIGIN='http://127.0.0.1:9101'
export WRAPPER_MODEL='nvidia/nemotron-3-super-120b-a12b'
```

```bash
# Nous
export WRAPPER_ORIGIN='http://127.0.0.1:9102'
export WRAPPER_MODEL='poolside/laguna-xs-2.1:free'
```

```bash
# OpenRouter
export WRAPPER_ORIGIN='http://127.0.0.1:9103'
export WRAPPER_MODEL='liquid/lfm-2.5-2.6b:free'
```

Lalu set token wrapper yang sesuai dan buat direktori smoke:

```bash
read -r -s -p 'Token wrapper: ' WRAPPER_API_KEY
export WRAPPER_API_KEY
WRAPPER_CLIENT_DIR="$(mktemp -d /tmp/wrapper-client-smoke.XXXXXX)"
```

`read -s` menerima token tanpa menampilkannya. Jangan memasukkan token ke argument CLI, berkas konfigurasi, atau log debug.

## Claude Code: Anthropic Messages

`ANTHROPIC_BASE_URL` memakai **origin tanpa `/v1`**; SDK menambahkan `/v1/messages`. Untuk smoke `--bare`, isi `ANTHROPIC_API_KEY` dengan token lokal wrapper dan `ANTHROPIC_AUTH_TOKEN` dengan token yang sama. Autentikasi wrapper tetap wajib; key provider tidak diberikan ke klien. Pada versi yang diuji, `--bare` memilih autentikasi API key dan menghindari OAuth/keychain, sedangkan `--safe-mode` saja tetap memakai mekanisme autentikasi normal. [Referensi CLI Claude Code](https://code.claude.com/docs/en/cli-reference), [gateway Claude Code](https://code.claude.com/docs/en/llm-gateway-connect).

```bash
export ANTHROPIC_BASE_URL="$WRAPPER_ORIGIN"
export ANTHROPIC_AUTH_TOKEN="$WRAPPER_API_KEY"
export ANTHROPIC_API_KEY="$WRAPPER_API_KEY"
unset CLAUDE_CODE_OAUTH_TOKEN
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
export CLAUDE_CODE_MAX_OUTPUT_TOKENS=256
export MAX_THINKING_TOKENS=0

cd "$WRAPPER_CLIENT_DIR"
claude --bare --safe-mode --setting-sources '' --strict-mcp-config \
  --tools '' --no-session-persistence \
  --system-prompt 'Reply briefly. Do not use tools.' \
  --model "$WRAPPER_MODEL" --output-format json \
  --print 'Reply exactly OK.'
```

Uji terkontrol membuktikan konfigurasi `--bare` + API key mencapai wrapper, mempertahankan model persis, dan meneruskan `max_tokens=256`; lihat [bukti bentuk request](reports/active-client-budgeted-shapes-20261007.json). Tanpa budget tersebut, versi klien ini mengirim `max_tokens=32000`. Budget 256 dan thinking 0 hanya untuk smoke sederhana; gunakan budget sesuai kemampuan model untuk pekerjaan agent. `--tools ''` menonaktifkan built-in tools pada smoke. Model non-Claude melalui gateway harus divalidasi sebagai kompatibilitas proyek; Anthropic hanya memberikan dukungan resmi untuk model Claude.

Pada uji versi ini, `MAX_THINKING_TOKENS=0` menghapus field `thinking`, tetapi klien masih mengirim `output_config.effort=high`; flag tersebut bukan jaminan reasoning upstream dinonaktifkan. [Bukti terkontrol tanpa inference nyata](reports/active-client-thinking-zero-shapes-20261007.json) mencatat `reasoning.effort=high` sesudah konversi. Gunakan konfigurasi effort yang didukung model untuk workload sebenarnya.

Pada cross-conversion OpenRouter, `thinking` dan `output_config.effort` diterjemahkan ke konfigurasi `reasoning` resmi provider: disabled menang, effort mendahului adaptive atau budget, dan ekstensi `reasoning` lain tetap dipertahankan. Nilai yang tidak didukung tetap menjadi error upstream, tanpa mengganti model. Native Chat/Responses dan native Messages pada layer yang sesuai tetap diteruskan apa adanya. [Mapping resmi OpenRouter](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens#reasoning-with-the-anthropic-messages-api).

Pada cross-conversion model NVIDIA **`nvidia/nemotron-3-super-120b-a12b`**, `thinking.type=disabled` menjadi `reasoning_effort=none`; `output_config.effort=low/high` memakai effort yang sama. Enabled/adaptive tanpa effort memakai default high provider. `thinking.budget_tokens` harus integer 1–32768 dan lebih kecil dari `max_tokens`; nilai yang tidak bisa direpresentasikan menghasilkan error 400 yang jelas. Opsi `force_nonempty_content` caller tetap dipertahankan, dengan default true. Mapping ini khusus model tersebut; native request tetap passthrough. [Kontrak NVIDIA model tersebut](https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3-super-120b-a12b-infer).

Kelulusan CLI dan output reasoning tidak membuktikan setiap kontrol adaptive/effort klien dijalankan oleh semua model. Kontrol Messages→Chat pada Nous belum qualified setara dengan kontrol Anthropic; jangan menyatakan budget/adaptive/effort tersebut dijalankan oleh model hanya berdasarkan hasil smoke. Per-message effort updates, encrypted reasoning details, dan fitur baru lain memerlukan regression khusus sebelum dinyatakan didukung melalui cross-conversion. Pilih layer native jika upstream dan workflow mendukungnya; `COMPATIBILITY_LAYER` menentukan jalur, bukan tebakan berdasarkan payload.

## Codex: native OpenAI Responses

Gunakan provider khusus dengan `wire_api="responses"`, `env_key="WRAPPER_API_KEY"`, dan `supports_websockets=false`. Konfigurasi server `RESPONSES_UPSTREAM_MODE=auto` mencoba native dahulu. Sesuai izin owner, adapter Responses → Chat hanya digunakan jika route native terbukti tidak didukung; error autentikasi, quota, model, timeout, atau respons native yang invalid tidak memicu downgrade. Model pilihan tetap persis sama. [Custom model providers](https://learn.chatgpt.com/docs/config-file/config-advanced), [referensi konfigurasi Codex](https://learn.chatgpt.com/docs/config-file/config-reference).

Konfigurasi persistent yang dapat digabungkan ke konfigurasi Codex pengguna:

```toml
model_provider = "wrapper_active"
model = "ID_MODEL_PERSIS"
# Aktifkan setelah katalog model yang terverifikasi tersedia di path ini:
# model_catalog_json = "/absolute/path/to/verified-wrapper-models.json"

[model_providers.wrapper_active]
name = "Wrapper aktif"
base_url = "http://127.0.0.1:9103/v1"
wire_api = "responses"
env_key = "WRAPPER_API_KEY"
requires_openai_auth = false
supports_websockets = false
```

Untuk ID yang tidak dikenal katalog bawaan Codex, sediakan `model_catalog_json` dengan entry `slug` **persis sama** dengan `model` dan ID yang diterima wrapper. Gunakan format katalog versi Codex terpasang, path absolut berkas yang sudah tersedia, dan letakkan opsi ini sebelum tabel TOML pertama. Katalog memuat instruksi, kemampuan tools, reasoning, dan context; metadata tersebut harus sesuai kemampuan model dan route yang diverifikasi. Katalog tidak mengubah ID model pada request atau mengizinkan fallback ke model lain. Restart Codex setelah menggantinya. [Panduan katalog gateway resmi](https://learn.chatgpt.com/docs/enterprise/roll-out-a-gateway#use-a-model-catalog-for-custom-names).

Helper qualification menyediakan katalog sementara khusus tugas baca untuk model persis yang diuji. Context 8192, input teks, dan tools terbatas di katalog tersebut merupakan batas fixture; **jangan menyalinnya sebagai profil kapasitas model produksi**. Untuk workload coding, isi kapasitas context/output serta kemampuan tools/reasoning dari kontrak model dan hasil uji workload yang dipilih. Kehadiran model di `/v1/models` saja belum membuktikan kemampuan tersebut.

Smoke berikut memakai instruksi minimal dan context 8192 yang telah lulus pada ketiga model eksplisit di atas. `model_instructions_file` mengganti instruksi bawaan Codex: **jangan jadikan contoh ini instruksi global produksi**. Context 8192 membatasi ukuran smoke dan bukan deklarasi kapasitas semua model. Konfigurasi persistent di atas tetap terpisah dari pengaturan smoke ini. [Arti kedua opsi](https://learn.chatgpt.com/docs/config-file/config-reference).

```bash
cat > "$WRAPPER_CLIENT_DIR/minimal-instructions.md" <<'EOF'
Reply exactly OK. Do not use tools.
EOF

codex exec --ignore-user-config --ignore-rules --ephemeral \
  --skip-git-repo-check --sandbox read-only --cd "$WRAPPER_CLIENT_DIR" \
  -c "model_instructions_file=\"${WRAPPER_CLIENT_DIR}/minimal-instructions.md\"" \
  -c 'model_context_window=8192' \
  -c 'model_provider="wrapper_active"' \
  -c 'model_providers.wrapper_active.name="Wrapper aktif"' \
  -c "model_providers.wrapper_active.base_url=\"${WRAPPER_ORIGIN}/v1\"" \
  -c 'model_providers.wrapper_active.wire_api="responses"' \
  -c 'model_providers.wrapper_active.env_key="WRAPPER_API_KEY"' \
  -c 'model_providers.wrapper_active.requires_openai_auth=false' \
  -c 'model_providers.wrapper_active.supports_websockets=false' \
  -c 'features.shell_tool=false' -c 'features.unified_exec=false' \
  -c 'features.multi_agent=false' -c 'agents.enabled=false' \
  -c 'features.hooks=false' -c 'features.apps=false' \
  -c 'features.plugins=false' -c 'web_search="disabled"' \
  --model "$WRAPPER_MODEL" --json 'Reply exactly OK. Do not use tools.'
```

Pengaturan di atas menonaktifkan shell, search, agents, hooks dan plugins yang dipilih; klien masih mengiklankan beberapa tools lain. Sandbox read-only membatasi perubahan berkas. Instruksi meminta tanpa tool call, tetapi bukan jaminan semua tools tidak tersedia. Keberhasilan harus berupa hasil akhir klien, bukan sekadar HTTP 200 atau proses yang sempat dimulai. Codex tidak mengirim `max_output_tokens` dalam uji terkontrol; context dan instruksi minimal tidak menjamin batas output per request.

## OpenCode client: OpenAI Chat atau Responses

Simpan contoh berikut sebagai `$WRAPPER_CLIENT_DIR/opencode.json`. Ganti kedua `ID_MODEL_PERSIS` dan `baseURL` sesuai backend yang dipilih. Token memakai substitusi environment. `@ai-sdk/openai-compatible` memilih Chat Completions; untuk Responses gunakan `@ai-sdk/openai`. [Provider SDK OpenCode](https://opencode.ai/docs/providers/#custom-provider), [environment config](https://opencode.ai/docs/config/#variables).

```json
{
  "$schema": "https://opencode.ai/config.json",
  "enabled_providers": ["wrapper_active"],
  "model": "wrapper_active/ID_MODEL_PERSIS",
  "permission": "deny",
  "provider": {
    "wrapper_active": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Wrapper aktif",
      "options": {
        "baseURL": "http://127.0.0.1:9103/v1",
        "apiKey": "{env:WRAPPER_API_KEY}"
      },
      "models": {"ID_MODEL_PERSIS": {"name": "ID_MODEL_PERSIS", "limit": {"context": 8192, "output": 256}}}
    }
  }
}
```

```bash
XDG_CONFIG_HOME="$WRAPPER_CLIENT_DIR/xdg-config" \
XDG_DATA_HOME="$WRAPPER_CLIENT_DIR/xdg-data" \
XDG_STATE_HOME="$WRAPPER_CLIENT_DIR/xdg-state" \
XDG_CACHE_HOME="$WRAPPER_CLIENT_DIR/xdg-cache" \
opencode run --pure --dir "$WRAPPER_CLIENT_DIR" --format json \
  --model "wrapper_active/$WRAPPER_MODEL" 'Reply exactly OK. Do not use tools.'
```

Nilai `limit` 8192/256 pada contoh OpenCode membatasi smoke, bukan kapasitas atau budget yang direkomendasikan untuk workload sebenarnya. `--pure` menonaktifkan external plugins pada versi terpasang; `permission="deny"` menolak tools. Untuk smoke bersih, gunakan direktori XDG config/data/state/cache tersendiri supaya konfigurasi dan credentials OpenCode pengguna tidak ikut digabungkan. [Permissions OpenCode](https://opencode.ai/docs/permissions/).

## Qualification tool baca oleh agent

Qualification ini terpisah dari smoke tanpa tools dan tool loop SDK: setiap kombinasi tiga CLI × tiga backend membaca satu `qualification.txt` sementara. File berisi nonce acak yang **tidak ada dalam prompt**. Validator mensyaratkan proses berhasil, terminal event klien, tool baca yang benar-benar selesai, nonce dalam jawaban akhir, tidak ada tool lain, dan isi fixture tetap sama. HTTP 200 atau jawaban tanpa tool baca tidak cukup. Helper yang dipakai tersedia di [live_agent_read_qualification.py](../tests/e2e_runtime/live_agent_read_qualification.py).

Hasil final: [sembilan kombinasi CLI × backend 9/9 lulus](reports/live-agent-read-qualification-20261011.json), termasuk OpenCode pada ketiga backend setelah perbaikan konfigurasi permission helper. Hasil ini melengkapi 18 smoke/SDK menjadi **27 pemeriksaan live yang lulus**. [Uji terarah NVIDIA + OpenCode 1/1](reports/nvidia-opencode-read-worktree-qualification-20261011.json) menjadi bukti diagnosis tambahan. [Attempt 1](reports/live-agent-read-qualification-attempt1-20261011.json) dan [attempt 2](reports/live-agent-read-qualification-attempt2-20261011.json) tetap menjadi bukti kegagalan awal, bukan diubah menjadi lulus.

Konfigurasi helper menghindari kendala klien yang ditemukan pada uji nyata:

- **Claude Code:** aktifkan hanya `Read` dengan allow rule ke file sementara persis; smoke `--tools ''` di atas sengaja tidak melakukan qualification tool baca.
- **Codex:** gunakan katalog exact-model untuk metadata tugas terbatas, aktifkan shell read-only, dan jalankan `/usr/bin/cat -- qualification.txt`. Environment shell fixture tidak mewarisi `PATH`, sehingga path executable absolut diperlukan. Jangan tambahkan `tools.view_image`: opsi tersebut tidak didukung pada Codex 0.160.1 yang diuji.
- **OpenCode:** gunakan satu sumber `permission` dengan wildcard deny lebih dulu dan allow path file persis sesudahnya. Jangan mencampur legacy `agent.tools` dengan permission: normalisasi tools dapat menambahkan wildcard deny belakangan dan menyembunyikan tool `read`.

Pada OpenCode 1.17.20, direktori sementara non-Git memiliki worktree global `/` walaupun `--dir` menunjuk direktori tersebut. ReadTool memeriksa path relatif terhadap worktree itu. Helper mengizinkan **path absolut fixture unik dan path unik relatif terhadap `/`**, misalnya `/tmp/wrapper-read-abc/qualification.txt` dan `tmp/wrapper-read-abc/qualification.txt`. Aturan basename `qualification.txt` saja tidak dipakai karena dapat mengizinkan file lain di root. Untuk proyek sendiri, sesuaikan path dengan worktree yang benar; jangan menyalin allow path sementara. [Diagnosis Codex](reports/live-agent-read-diagnosis-20261011.json), [diagnosis permission OpenCode](reports/live-agent-read-opencode-diagnosis-20261011.json).

Saat smoke selesai, jalankan `unset WRAPPER_API_KEY ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_MAX_OUTPUT_TOKENS MAX_THINKING_TOKENS` dan hapus direktori sementara yang dibuat sendiri. Pertahankan `HOME` dan `CODEX_HOME` asli. Simpan hanya status, versi klien, model yang dipilih, dan hasil akhir yang diperlukan; jangan menyimpan headers autentikasi.
