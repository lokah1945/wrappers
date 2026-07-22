# wrapper-nous

Proxy lokal Python (stdlib murni) yang menjembatani **dua format klien** ke
Nous Research inference portal (yang hanya implementasikan OpenAI
Chat Completions):

| Klien | Endpoint masuk | Translator |
|-------|---------------|-----------|
| **Codex** (OpenAI Responses) | `POST /v1/responses` | Responses → chat → Responses (SSE) |
| **Claude Code / Anthropic SDK** | `POST /v1/messages` | Anthropic → chat → Anthropic (SSE) |
| OpenAI SDK langsung | `POST /v1/chat/completions` | pass-through |
| Metadata | `GET /v1/models` | proxy list model Nous |

**Satu port (9106) menangani kedua format secara otomatis** (auto-detect by
path), mirip wrapper-nvidia yang 1 port untuk 2 format.

## Konsep

Sederhana — Python stdlib, tidak ada Node.js / metrics.db / Docker / .env
hierarchy (berbeda dengan wrapper-nvidia yang berat). Konsep translation
diambil dari studi repo Rust
[anthropic-proxy-rs](https://github.com/m0n0x41d/anthropic-proxy-rs) —
kita ambil **hanya konsep & kemampuan** (Anthropic↔OpenAI mapping, block-lifecycle
streaming state machine, tool/stop_reason mapping), lalu re-implement di Python
terintegrasi dengan translator Responses yang sudah verified. Tidak pakai repo
Rust 100%.

## Mengapa dibutuhkan

- Codex v0.144.5 hanya bicara wire API `responses` (`/v1/responses`)
- Claude Code / Anthropic SDK hanya bicara `/v1/messages` (Anthropic)
- Nous portal **hanya** punya `/v1/chat/completions` (`/v1/responses` & `/v1/messages` → 404)
- Proxy ini menjembatani keduanya + menyuntikkan token OAuth Nous **segar**
  per-request (otomatis handle expiry) dibaca live dari
  `/root/.hermes/profiles/ilma/auth.json`.

## Port

`127.0.0.1:9106` (terendah yang kosong, exclude 9100–9105; 9101/9103/9191 terpakai).

> ⚠️ **Catatan pembaruan:** README versi lama mencantumkan port `9107`. Port
> sebenarnya yang dipakai `wrapper_nous.py` (dan `wrapper-nous.service`) adalah
> **`9106`**. Selalu rujuk port ini.

## Model free di Nous (suffix `:free`)

- `tencent/hy3:free`  ← default + reasoning model
- `poolside/laguna-s-2.1:free`
- `poolside/laguna-xs-2.1:free`
- `stepfun/step-3.7-flash:free`

Proxy meneruskan model apa pun yang diminta klien (bukan hardcode).

## Kemampuan (terverifikasi)

- ✅ OpenAI Responses batch + SSE (Codex)
- ✅ Anthropic Messages batch + SSE (Claude Code)
- ✅ Anthropic → OpenAI tool calling (`tools[]` → function, `tool_use` output)
- ✅ Extended thinking routing (`thinking.enabled` → reasoning model)
- ✅ `stop_reason` mapping: `tool_calls→tool_use`, `stop→end_turn`, `length→max_tokens`
- ✅ Token usage mapping: `prompt_tokens→input_tokens`, `completion_tokens→output_tokens`
- ✅ System prompt (string/array) → `role:system`
- ✅ Image (base64), thinking block, tool_result → role:tool

## Jalankan

```bash
systemctl --user enable --now wrapper-nous.service
curl http://127.0.0.1:9106/healthz
```

Manual (tanpa systemd):
```bash
python3 /root/wrapper/nous/wrapper_nous.py
```

Verifikasi endpoint:
```bash
curl http://127.0.0.1:9106/healthz      # {"ok": true}
curl http://127.0.0.1:9106/v1/models    # daftar model Nous
```

## Claude Code

Gunakan settings JSON yang menunjuk ke proxy ini. File siap pakai ada di
`/root/.claude/wrapper-nous.settings.json`:

```bash
claude --settings /root/.claude/wrapper-nous.settings.json
```

Isinya (port **9106**, OAuth ditangani proxy):

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:9106",
    "ANTHROPIC_AUTH_TOKEN": "wrapper-local-key",
    "ANTHROPIC_API_KEY": "bearer-token-clone",
    "ANTHROPIC_MODEL": "tencent/hy3:free",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "tencent/hy3:free",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "tencent/hy3:free",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "tencent/hy3:free",
    "ENABLE_TOOL_SEARCH": "true",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
    "CLAUDE_CODE_GATEWAY_MODEL_DISCOVERY_URL": "http://localhost:9106/v1/models"
  },
  "skipWorkflowUsageWarning": true,
  "theme": "auto"
}
```

Atau letakkan sebagai settings project di `~/wrapper/nous/.claude/settings.json`.

## File

- `wrapper_nous.py` — proxy tunggal (Responses + Anthropic + Chat) — **aktif**
- `nous_proxy.py` — versi Codex-only awal (legacy, tidak dipakai service)
- `wrapper-nous.service` — systemd unit aktif (port 9106)
- `wrapper-nous-unified.service` — ⚠️ **STALE**: menunjuk ke `unified_proxy.py`
  yang tidak ada; jangan dipakai. Gunakan `wrapper-nous.service`.
- `README.md` — dokumentasi

## Arsitektur

```
Claude Code ──POST /v1/messages──▶ wrapper_nous.py :9106 ──▶ Nous /v1/chat/completions
Codex       ──POST /v1/responses─▶        │                     (Bearer OAuth segar
OpenAI SDK  ──POST /v1/chat/completions▶  │                      dari auth.json)
                                    GET /v1/models ◀───────────┘
```
