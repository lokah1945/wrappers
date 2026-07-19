# DEBUG MAP — Claude Code → wrapper-nvidia → NVIDIA NIM (z-ai/glm-5.2)

Live test: Claude Code `settings.json` `model: "z-ai/glm-5.2"` → `POST /v1/messages`.
Service runs OLD build (PID 1632666) on `:9100`; do NOT restart mid-session.

## Static facts for z-ai/glm-5.2 (from live /v1/capabilities)
- Resolves transparently: `resolveTargetModel("z-ai/glm-5.2")` → passes through (no alias).
- `model_family: "glm"` → hits REASONING_CONFIGS glm entry:
  `chat_template_kwargs: { thinking: true }`, `requires_reasoning: false`.
- `context_window: 202752`, `max_output_tokens: 8192`.
- `supports_function_calling: true`, `supports_streaming: true`.
- Base URL: https://integrate.api.nvidia.com/v1/chat/completions

## Request path (where each failure appears)
1. Route: `index.js:3480` `POST /v1/messages` → `handleAnthropicMessages` (`index.js:2086`).
2. JSON parse (`2089`): bad body → 400 `Invalid JSON body` → **client JSON error**.
3. `anthropicToOpenai` (`anthropic_compat.js:162`):
   - Strips `cache_control` + `tool_search_tool_*`.
   - Context pruning uses `registry.getOfficialContext(resolveTargetModel(model))`
     (NGC 202752) — over-pruning ⇒ history dropped, possible NIM 400.
   - Invalid message order ⇒ 400 `Invalid message order` (`anthropic_compat.js`).
   - GLM thinking block → DSML plaintext (`<thinking>`/`tool_result` embedding).
   - Error returned as `{error}` ⇒ 400 from handler (`index.js:2114`).
4. `translateThinkingToNim(oaiBody, model, aBody.thinking)` (`index.js:2139`):
   - Sets `chat_template_kwargs:{thinking:true}` for glm. If you see glm NOT
     thinking, check this fired (only when Claude Code sends `thinking`).
5. `proxyOpenai(oaiBody, ..., '/v1/messages')` (`index.js:2152`):
   - Key acquire / rate-limit / 429 pacing / 503 all-keys-exhausted.
   - 400/422 param-strip + retry; 404/413 context → verbatim; 500 retry.
   - Returns `{status, data}` (non-stream err) or `{status:200, stream}` (SSE).
6. Streaming (`index.js:2160+`):
   - SSE headers written once; `streamOpenaiToAnthropic` (`anthropic_compat.js:611`)
     emits `message_start` → `content_block_start/delta/stop` → `message_stop`.
   - message_start `input_tokens` from `estimateInputTokens` (computed BEFORE pruning).
   - Retry loop: `MAX_STREAM_RETRIES=2` on transient err before first content delta.
   - GLM thinking → `content_block` type `thinking` (or synthetic shim if none).
   - Empty content (thinking-only) ⇒ synthetic text shim to satisfy Anthropic SDK.

## Symptom → file:line to inspect
- Claude Code shows 502 / "stream interrupted":
  `handleAnthropicMessages` stream error branch `index.js:2280-2290`; check
  `capture.errorMessage` / upstream `reasoning_content` after `</thinking>`.
- "history too large" / 400 session-too-large:
  `index.js:2296` `onlyFriendlyErr` branch (no content + no stop).
- Model "thinks" forever / hangs: glm `requires_reasoning:false` so NOT auto-enabled;
  must come from Claude Code `thinking` block → `translateThinkingToNim`.
- No thinking block visible in Claude Code: upstream GLM returned reasoning inline
  in `content` (not `reasoning_content`) — expected for some glm outputs; check raw SSE.
- Tool calls mis-invoked / parallel: `supports_parallel_tool_calls:false` for glm;
  `sanitizeNvidiaPayload` splits parallel tool calls into sequential messages.
- Token count 0 in dashboard Activity: `stream_options.include_usage` forced at
  `index.js:2144`; capture.usage in generator (`anthropic_compat.js` ~ line 599+).

## Logs to watch (console of the running service)
- `[anthropicToOpenai] Called with: ...`  (request entered translation)
- `[REASONING] Model "..." is NOT in REASONING_CONFIGS` (thinking requested, no cfg)
- `[proxyOpenai] Preserving chat_template_kwargs` (glm thinking toggle injected)
- `[UPSTREAM ERROR] status: N ...` / `[DEGRADED]` / `[RETRY-CYCLE]`
- `[stream retry] rid=... retry=k/2` / `[stream error] ...`

## Dynamic model discovery (long-term requirement)
Claude Code `settings.json.model` is a static field — cannot be "fetched" into it
directly. Use the wrapper's discovery surfaces instead:
- `GET /v1/models` → live catalog (`pool.refreshModels()`), original NIM ids.
- `GET /v1/models?gateway=1` → returns the EXACT NVIDIA NIM ids (e.g. `z-ai/glm-5.2`), NOT
  `claude-*` aliases, so Claude Code's model picker shows the real upstream catalog names.
  Point `CLAUDE_CODE_GATEWAY_MODEL_DISCOVERY_URL` here to let Claude Code pick dynamically.
  Inbound `claude-*` ids still resolve via resolveTargetModel (DISCOVERY_TO_NIM rebuilt from exact ids).
  Code PICK from the live NVIDIA catalog dynamically.
- `GET /v1/capabilities` → per-model metadata (context_window, supports_*, etc.).
- `GET /v1/capabilities?model=X` → ad-hoc metadata for any id (heuristic-adhoc if unknown).
- `GET /v1/models?refresh=true` → force re-sync the catalog.
Routing stays transparent: any id not in the list still passes through to NIM and
returns the real upstream error. resolveTargetModel (`index.js:363`) handles
gateway aliases, explicit aliases, and raw NIM passthrough.
