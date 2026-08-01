# COMPATIBILITY_LAYER — Operator-Declared Upstream Dialect

**Status:** Proposal → Normative (2026-08-01)  \
**Applies to:** all 5 wrappers (`nvidia-python`, `nous`, `opencode`, `blackbox`, `openrouter`)

---

## 1. Why this exists

Today **every wrapper hardcodes the assumption that its upstream speaks the
OpenAI Chat Completions protocol** (`POST {base}/chat/completions`). All three
client-facing surfaces are built on that single assumption:

| Client surface | What the wrapper does today (upstream = OpenAI) |
|---|---|
| `POST /v1/chat/completions` | **Passthrough** (verbatim forward) |
| `POST /v1/responses` | translate Responses → OpenAI Chat → forward → translate back |
| `POST /v1/messages` (Claude Code) | translate Anthropic → OpenAI Chat → forward → translate back |

This is why **wrapper-nvidia + Claude Code already reaches a final response**:
NVIDIA NIM is an OpenAI-compatible upstream, and the Anthropic surface is
correctly translated to/from it.

But the moment an operator points a wrapper at an **Anthropic-native upstream**
(e.g. a Claude-compatible gateway), every wrapper breaks — it POSTs OpenAI
shapes to `/chat/completions`, which 404s or rejects them. The wrapper has no
way to know the upstream dialect except guessing.

**The fix:** an operator-declared `COMPATIBILITY_LAYER` variable. The person
who configures the wrapper *knows* what the upstream speaks; they declare it
explicitly, and the wrapper picks the exact translation path — no guessing.

---

## 2. The variable

```
# ============================================
# UPSTREAM COMPATIBILITY LAYER
# ============================================
# Which API protocol does the UPSTREAM speak?
#   1 = OpenAI Compatible  (chat completions: POST /chat/completions)
#   2 = Anthropic Compatible (messages: POST /v1/messages)
#   3 = Auto Discovery     (probe upstream at startup / first use)
# Default: 1 (OpenAI Compatible) — preserves current behaviour exactly.
COMPATIBILITY_LAYER=1
```

| Value | Meaning | Effect |
|---|---|---|
| `1` | upstream speaks **OpenAI** | today's behaviour, byte-for-byte |
| `2` | upstream speaks **Anthropic** | surfaces translated to/from Anthropic Messages |
| `3` | **auto** | probe upstream once, cache the result, fall back to 1 |
| *(unset/invalid)* | — | default `1`; invalid values fail fast at startup (`validate_config`) |

The operator is the source of truth: "saya atau user harus dengan sadar
menentukan upstream menggunakan Compatibility Layer apa, sehingga translation
dari agent/client akan lebih presisi daripada wrapper menebak".

---

## 3. Translation matrix per layer

### `COMPATIBILITY_LAYER=1` (OpenAI upstream) — unchanged

| Surface | Request to upstream | Response from upstream |
|---|---|---|
| `/v1/chat/completions` | passthrough | passthrough |
| `/v1/responses` | `responses_to_chat` | `chat_to_responses` |
| `/v1/messages` | `anthropic_to_openai` (request) | `openai_to_anthropic` (JSON) / OpenAI SSE → Anthropic SSE |

### `COMPATIBILITY_LAYER=2` (Anthropic upstream) — new

| Surface | Request to upstream (`POST {base}/v1/messages`) | Response from upstream |
|---|---|---|
| `/v1/chat/completions` | `openai_chat_to_anthropic_request` (new shared converter) | `anthropic_to_openai_response` (JSON) / `stream_anthropic_to_openai` (SSE) |
| `/v1/responses` | `responses_to_chat` → `openai_chat_to_anthropic_request` | Anthropic → OpenAI Chat → `chat_to_responses` / Responses SSE translator |
| `/v1/messages` | **passthrough** (verbatim, model alias only) | **passthrough** (Anthropic JSON / Anthropic SSE, no `[DONE]`) |

Layer 2 is *more precise* for Anthropic-native upstreams: the Anthropic
surface needs no translation at all, and the OpenAI surfaces are translated
exactly once.

### `COMPATIBILITY_LAYER=3` (Auto Discovery)

At first use (per base URL, cached with TTL):

1. `GET {base}/v1/models` (or `{base}/models`) → 200 with a model list → **OpenAI**
2. `POST {base}/v1/messages` (minimal body) → status ≠ 404 → **Anthropic**
3. `POST {base}/v1/chat/completions` (minimal body) → status ≠ 404 → **OpenAI**
4. otherwise → **OpenAI** (the historical default)

The probe is best-effort, never blocks startup, and its result is cached so it
runs once per upstream.

---

## 4. What stays identical (regression guarantee)

- **`COMPATIBILITY_LAYER` unset or `1` ⇒ zero behaviour change.** All existing
  tests (209 unit, 445 E2E, SDK-compat gate, soak) must stay green untouched.
- Multi-key rotation, per-key cooldown, retries, metrics, heartbeat, SSE
  hygiene, auth, rate limiting — all orthogonal to the dialect and unchanged.
- wrapper-nvidia + Claude Code keeps working exactly as it does on `main`.

---

## 5. Implementation plan

1. `common/compat.py` — `compat_layer()`, `validate_compat_layer()`,
   `is_anthropic_upstream()`, `probe_upstream_compatibility()` (+ cache).
2. `common/translations/shared.py` — `openai_chat_to_anthropic_request()`
   (system, images base64, tool_calls→tool_use, tool results, tools→input_schema,
   tool_choice, stop→stop_sequences, params) + unit tests.
3. `.env.example` × 5 + root `README.md` + `WRAPPER_CONTRACT.md` §9.2 row.
4. `validate_config()` × 5 — fail fast on invalid `COMPATIBILITY_LAYER`.
5. Layer-2 wiring in the three surfaces of all five wrappers, reusing each
   wrapper's existing proxy/key-pool/retry machinery with shared converters.
6. Mock upstream **Anthropic-native mode** + E2E runs at `COMPATIBILITY_LAYER=2`
   and `=3`; layer-1 E2E must stay green unchanged.
7. Docs: this file + audit index.

---

## 6. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Breaking the working nvidia+Claude Code path | layer 1 = exact current code path; regression suite must stay green untouched |
| Double translation (Responses via Chat via Anthropic) | acceptable: `responses_to_chat` then `openai_chat_to_anthropic_request`; responses are rare on Anthropic-only upstreams |
| Anthropic has no model-list endpoint | `/v1/models` + `/api/tags` return the alias/curated list with a note under layer 2 |
| HTTP-URL images (Anthropic only accepts base64) | data URIs → base64 image blocks; http(s) URLs → text placeholder (documented), never dropped silently |
| Auto-probe cost | once per base URL, cached with TTL; probes use minimal bodies and never burn key quota (no auth on probe) |
