# Transparency Audit — /root/wrapper proxy wrappers (nous, nvidia-python, opencode, blackbox + common) — 2026-07-27

**Bottom line:** None of the four wrappers is a transparent proxy. No endpoint anywhere forwards raw client bytes upstream; every request is `json.loads` → re-serialized, every non-stream response is re-serialized via `JSONResponse` with **all upstream response headers dropped**. `/v1/messages` and `/v1/responses` are protocol *emulators* (all upstreams are OpenAI-chat only), not proxies. nvidia-python additionally injects/overrides thinking and reasoning parameters and mutates even the plain `/v1/chat/completions` path.

---

## 1. Wrapper: nous (`nous/wrapper_nous.py`, 2002 lines)

Flow: `/v1/chat/completions` = quasi-passthrough (still mutated); `/v1/responses` and `/v1/messages` = full emulation over upstream `/v1/chat/completions` via `post_nous()` (L554) which sends `sess.post(url, json=payload)`; `/v1/messages/count_tokens` and `/v1/models` are fabricated locally.

| # | file:line | What is changed | Condition | Verdict |
|---|---|---|---|---|
| N1 | wrapper_nous.py:573,580 | Request body re-serialized (`json=payload`); raw client bytes never forwarded | Always | VIOLATES (byte-level) |
| N2 | :585, :1848 | Non-stream response `json.loads` → re-`dumps`; all upstream response headers dropped (rate-limit headers, request IDs) | Always | VIOLATES |
| N3 | :1807-1808 | **Deletes request params**: `n`, `logprobs`, `logit_bias`, `user`, `frequency_penalty`, `presence_penalty` | Always, /v1/chat/completions | **VIOLATES** |
| N4 | :1810-1823 | Tools without `name` dropped; `tools` key removed entirely if all dropped | Tools present | VIOLATES |
| N5 | :433-440, :475-489, :1793-1797 | Model rewriting: alias names (`sonnet`/`opus`/`haiku`/`claude-*`) → `DYNAMIC_ALIAS_TARGET` (env-seeded at :1504-1506) | Alias names only, target bound | VIOLATES (aliases only; concrete ids pass through) |
| N6 | :1787-1791, :1802-1806, :1912-1919 | Wrapper-invented 400s: max_tokens ≤0 or >1,000,000; role whitelist; `tool` role requires tool_call_id; Anthropic `system`/`input_schema` type checks | Matching input, never reaches upstream | VIOLATES |
| N7 | :390-411, :1798-1801, :1856-1862, :1921-1929 | FREE_ONLY gate → wrapper-origin 400 | Only `FREE_ONLY` env | VIOLATES (policy feature) |
| N8 | :1826, :1864, :1931, :567-568 | **Header allowlist**: only `anthropic-beta`, `anthropic-version`, `openai-beta`, `x-request-id` forwarded (after sanitize); all other client headers dropped (User-Agent, Accept-Encoding, custom x-*); aiohttp substitutes its own UA/Accept-Encoding/Host | Always | VIOLATES (drop-all-but-4); Host/Content-Length regeneration ACCEPTABLE |
| N9 | :563 | `Authorization: Bearer <pool key>` replaces client credentials | Always | **ACCEPTABLE** (core purpose) |
| N10 | :556-560; :1469-1476, :1783-1785 | Fabricated 503 when circuit breaker open; fabricated per-IP 429 (60 rpm default) | Breaker open / over limit | VIOLATES |
| N11 | :577, :583, :705, :724 | Upstream error bodies replaced via `common.translations.normalize_upstream_error` | Every upstream error | VIOLATES |
| N12 | :679-683 | Error message rewritten to "All configured Nous credentials failed… Last error: …" (truncated 2000 chars) | Pool exhausted | VIOLATES |
| N13 | :631-638 | Pre-flight `MODEL_REGISTRY.call_plan` can return wrapper 400 `MODEL_CALL_PLAN_INVALID` / 500 `MODEL_ID_MUTATION` | Registry rejects model | VIOLATES (wrapper-origin errors) |
| N14 | :646-677 | Retry/rotation replays the **identical** payload (no per-retry body mutation) | Retriable statuses | **ACCEPTABLE** |
| N15 | :837-849 → :1840-1847 | Response patch: `message.content: null` → `""`; missing `usage` → `{0,0,0}` injected | Non-stream chat 200s | VIOLATES |
| N16 | :1059, :1080 | **`: heartbeat` SSE comment injected** every ~5 s of upstream silence | All streaming | VIOLATES |
| N17 | :1067-1074, :1038 | SSE re-framing: only `data:` lines forwarded; upstream `event:`/`id:`/`retry:` lines and comments silently dropped | All streaming | VIOLATES |
| N18 | :1084-1099 | Synthetic `data: [DONE]` + terminal state events fabricated on abnormal upstream EOF | Upstream EOF w/o DONE | VIOLATES (anti-hang mitigation) |
| N19 | :812-813 | `/v1/responses`: `max_tokens` **default 4096 injected** when client omits; client value **floored to ≥1024** | Always on /responses | **VIOLATES** |
| N20 | :919 | `/v1/messages`: `max_tokens` **floored to ≥1024** (client's smaller value overridden), default 4096 | Always on /messages | **VIOLATES** |
| N21 | :814-815, :920-921 | Param whitelist: only `temperature`, `top_p` (+`tool_choice` on /responses) forwarded; `top_k`, `stop_sequences`, `metadata`, `stream_options`, `parallel_tool_calls`, thinking config etc. **silently dropped** | /responses, /messages | **VIOLATES** |
| N22 | :869 | `strip_cache_control` deletes all `cache_control` keys from request (in place) | /messages always | VIOLATES |
| N23 | :755-771, :810 | Orphan `tool` messages rewritten into `user` messages with fabricated "Tool result for …" text | /responses | VIOLATES |
| N24 | :741-750, :817-825, :924 | Tool schema rewrite: `null` values dropped, `format: "uri"` dropped, `required: []` injected; nameless tools dropped | Tools on /responses, /messages | VIOLATES |
| N25 | :775-777, :829-834, :1887, :1904 | `_RESPONSE_STORE` server-side conversation history injected into upstream `messages` on `previous_response_id` | /responses | VIOLATES |
| N26 | :894, :914-915; :936-963 | Thinking blocks ↔ `reasoning_content` conversion; DSML markup parsed out of response text into fabricated `tool_use` blocks with synthetic `toolu_<ts>` ids | /messages | VIOLATES (translation) |
| N27 | :903-916, :922 | Message restructuring (tool_result hoisting, empty shells skipped, images → data-URI `image_url`); `stop_sequences` renamed → `stop` | /messages | VIOLATES (translation) |
| N28 | :969-973, :1201-1214 | `stop_reason` **forced to `tool_use`** whenever any tool call exists, regardless of upstream finish_reason | /messages | VIOLATES |
| N29 | :1161-1164 | Streaming content deltas containing "DSML" **suppressed** (never emitted as text) | /messages stream | **VIOLATES** (content dropped) |
| N30 | :1108-1241, :1244-1406 | Anthropic/Responses streams fully synthesized: fabricated `msg_*`/`resp-*` ids, zeroed cache-token usage, synthetic terminal events on EOF | Emulated endpoints | VIOLATES (inherent to emulation) |
| N31 | :978, :1131, :1873, :1943 | Response `model` field echoes the resolved (post-alias) id, not the client string | Alias used | VIOLATES |
| N32 | :1769-1773 | `count_tokens` never forwarded; fabricated estimate `len(str(body))//4` | Always | VIOLATES |
| N33 | :1657-1739, :1602-1606, :534-549 | `/v1/models` fabricated: SQLite cache, curated hardcoded models injected (incl. `big-pickle`), alias entries, invented metadata, FREE_ONLY filter | Always | VIOLATES |
| N34 | :585-587 | Non-JSON upstream 200 body replaced with fabricated `{"error":…}` (truncated) | Malformed upstream body | VIOLATES |
| N35 | :1936-1940 | Upstream errors re-wrapped into Anthropic `{"type":"error",…}` envelope | /messages errors | VIOLATES |
| N36 | :1550-1551 | `RequestSizeLimiter` → fabricated 413 | Bodies >10 MB | ACCEPTABLE (guard) |

Clean in nous: **no** injection of `thinking`/`reasoning_effort`/`chat_template_kwargs`/`enable_thinking`/`extra_body`; no temperature/top_p/stream defaults on the chat path; retries never modify the replayed body.

---

## 2. Wrapper: nvidia-python (`nvidia-python/src/`)

Translation-layer activation: `anthropic_compat.py` activates on **every** `/v1/messages` request; `responses_compat.py` on **every** `/v1/responses` request (NIM has no native Anthropic/Responses surface). Neither activates on plain `/v1/chat/completions` — **but** the plain chat path is still mutated (V4–V13 below), because everything funnels through `proxy_openai()` (main.py:2076).

| # | file:line | What is changed | Condition | Verdict |
|---|---|---|---|---|
| V1 | main.py:621-642 (`REASONING_CONFIGS`), :730-751 (`apply_default_reasoning`), call :1997 | **Injects `chat_template_kwargs: {enable_thinking: true, thinking: true}`** for models with `requires_reasoning: True` (deepseek-r1/-v4/-reasoner, `-reasoning`/`reason` patterns) when client sent no explicit reasoning fields | `/v1/messages` path; client omitted `chat_template_kwargs`/`reasoning_effort`/`extra_body` reasoning keys | **VIOLATES** |
| V2 | main.py:697-727 (`translate_thinking_to_nim`), calls :2000-2001 and responses_compat.py:397-398 | Anthropic `thinking` / Responses `reasoning` mapped to NIM controls: injects `chat_template_kwargs` (qwen, glm, phi-4, yi-, llama-4, nemotron, gemma-3, deepseek…) or **`reasoning_effort: 'high'`/`'low'`** (gpt-oss, kimi, mistral-, nemotron); nemotron mechanism also copies `reasoning_budget` into `extra_body` (:724-727) | `/v1/messages` with `thinking` dict; `/v1/responses` with `reasoning` | **VIOLATES** (translation-layer injection) |
| V3 | main.py:634 + :712-716 (commit 0781634) | GLM: config params `{thinking: False}` + `opt_out_default_thinking`. Because `translate_thinking_to_nim` does `obj[k] = v if enabled else False` and `v` is already `False`, GLM gets `chat_template_kwargs.thinking=false` injected **even when the client explicitly requests thinking enabled** — the "only enable when client requests it" comment is not implemented; client intent is overridden | `/v1/messages` with any `thinking` dict, GLM models | **VIOLATES** (contradicts its own stated intent) |
| V4 | main.py:673-674, :1104-1107 (`sanitize_nvidia_payload`), applied :2077 and :2126-2128 (`PROACTIVE_DROP`) | **Always deletes** from every proxied body: `think`, `context_length`, `context_window`, `context_len`, `max_position_embeddings`, `max_context_length`, `max_input_tokens`, `max_output_tokens`, `token_limit` (extendable via `DROP_PARAMS` env) | Always — **including plain /v1/chat/completions** | **VIOLATES** |
| V5 | main.py:659-672, :2104-2107 (`DEFAULT_PARAMS`) | Env-driven parameter defaults injected when client omits (e.g. `WRAPPER_PARAMS=temperature` + `DEFAULT_TEMPERATURE=0.7`) | Only when env configured; incl. plain chat | VIOLATES (operator opt-in) |
| V6 | main.py:2068-2070 (`_prepare_proxy_body`) | **`max_completion_tokens` renamed to `max_tokens`** (original key deleted) | Always, all proxied bodies, when `max_tokens` absent | **VIOLATES** |
| V7 | main.py:2072-2073 | **Injects `stream_options: {include_usage: true}`** (merged over client's stream_options) | Every streamed request, incl. plain chat | **VIOLATES** |
| V8 | main.py:996-1040 (`_model_output_cap`/`clamp_max_tokens_for_model`), calls :1882, :2003, :2071 | `max_tokens`/`max_completion_tokens` **silently clamped** to per-model caps (kimi-k2.6 → 16384 hardcoded; `MODEL_MAX_TOKENS_CAPS` env) | Matching models when value>cap; incl. plain chat | **VIOLATES** |
| V9 | main.py:2114-2124 | `nvext.stream` deleted from `nvext` and `extra_body.nvext`; empty `extra_body.nvext` removed; `extra_body.nvext` merged into top-level `nvext` | When present | VIOLATES |
| V10 | main.py:1871-1876 | `temperature`, `top_p`, `top_k`, `presence_penalty`, `frequency_penalty`, `min_p` cast string→float | Plain chat, when present | VIOLATES (type/byte mutation; SDK-compat motivated) |
| V11 | main.py:827-839, :931-960, calls :1714, :1727, :1792, :1805, :1974, :2421 | Model rewriting: alias names (`sonnet`/`haiku`/`opus`/`claude-*`) → `DYNAMIC_ALIAS_TARGET`; discovery names `claude-org-name` → `org/name`; `body['model']` mutated in place | Alias/discovery names only; concrete ids pass through (:959-960) | VIOLATES (aliases) |
| V12 | main.py:644-656, :803-809, :1690-1692 | Deprecated model ids → wrapper-fabricated **410** telling client to rename (e.g. glm-5 → glm-5.2) | Only when `DEPRECATED_MODEL_REDIRECT_ERROR=1` | VIOLATES (opt-in gate) |
| V13 | main.py:1694-1712, :1959-1972 | Wrapper-invented 400s: max_tokens ≤0/>1M (incl. `max_completion_tokens`), role whitelist, `tool_call_id` required, Anthropic system/input_schema checks | Matching input | VIOLATES |
| V14 | main.py:773-782 (`guard_stream_unsupported`), calls :1878, :1980, :2082 | Wrapper 400 for `stream:true` on models the local capability classifier deems non-chat | Capability-classified non-chat models | VIOLATES |
| V15 | main.py:1110-1122 (`ensure_nonempty_content`), call :1896 (commit 0781634) | **Response mutation on plain chat**: empty `message.content` replaced with the model's `reasoning_content`/`reasoning`/think-tag-extracted text ("surface reasoning as content"); else `content: null` → `""` | Non-stream /v1/chat/completions 200s where content empty and no tool_calls | **VIOLATES** (reasoning surfaced as answer text) |
| V16 | anthropic_compat.py:48-82 (`extract_internal_reasoning`) | Response content re-split: leading `<think>`/`<thinking>` tag blocks stripped out of `content` and reclassified as reasoning; text `.strip()`ed | Wherever used (V15, /messages, /responses conversions) | VIOLATES |
| V17 | main.py:1949-1953 | `anthropic-version` **defaulted to `2023-06-01`** when client omits | /messages | VIOLATES (default injection; never forwarded anyway) |
| V18 | anthropic_compat.py:169 | `strip_cache_control(a)` — deletes every `cache_control` key from request in place | Every /messages request | VIOLATES |
| V19 | anthropic_compat.py:171-192 | **Silent history truncation**: if estimated input tokens exceed `context − max_tokens − 2000`, oldest messages are **dropped from the conversation** before forwarding | /messages when estimate exceeds window | **VIOLATES (severe — silent content loss)** |
| V20 | anthropic_compat.py:194-200 | Wrapper-invented 400 "Invalid message order" | /messages, tool→non-assistant sequence | VIOLATES |
| V21 | anthropic_compat.py:202-330 | Full A→O rewrite: system+developer messages merged into one system string (:205-222); thinking blocks → `reasoning_content` (:259-264, :322-323); images → data-URI `image_url`; `tool_result` → `role:tool`; empty assistant shells skipped (:325-326); synthetic tool-call ids `toolu_<ms>` when missing (:313); **`max_tokens` defaulted to 8192** when absent (:330) | Every /messages request | VIOLATES (translation; note max_tokens is Anthropic-required so default rarely fires) |
| V22 | anthropic_compat.py:146-159 (`sanitize_anthropic_tools`) | Tools with `type: tool_search_tool_*` **silently dropped**; `defer_loading` popped from every tool | /messages with tools | VIOLATES |
| V23 | anthropic_compat.py:332-343 | Params forwarded: temperature, top_p, top_k, `stop_sequences`→`stop`, stream, extra_body, nvext; **`metadata`, `stream_options`, `service_tier` etc. dropped** | /messages | VIOLATES (lossy whitelist) |
| V24 | anthropic_compat.py:445-548 (`openai_to_anthropic`) | Response fabrication: `reasoning_content` → thinking block (:456); DSML parsed out of text into fabricated `tool_use` blocks with synthetic `call_dsml_*` ids (:459-497); text `.strip()`ed (:483); empty text block injected (:516-517); usage estimated from char count when upstream omits `completion_tokens` (:531); cache tokens partially zeroed (:532); `stop_reason` **forced to `tool_use`** if any tool_use present (:542-544); fabricated `msg_*` id | /messages non-stream | VIOLATES |
| V25 | anthropic_compat.py:513-514 and :636-660 (`emit_synthetic_thinking`) | **Fabricated thinking block injected**: text "[Reasoning not supported by this model; responding directly.]" presented as model thinking | /messages when client requested thinking but model emitted none (non-stream and stream) | **VIOLATES (severe — fabricated model output)** |
| V26 | anthropic_compat.py:551-1083 (`stream_openai_to_anthropic`) | Anthropic stream fully synthesized: fabricated `msg_*` ids, DSML stream-parsing into tool_use blocks with synthetic ids (:695+), think-tag splitting, synthetic terminal events, estimated usage | /messages stream | VIOLATES (inherent to emulation) |
| V27 | responses_compat.py:28-51, :650 | `_RESPONSE_STORE` server-side history injected into upstream `messages` on `previous_response_id` (bounded 200 entries) | /responses | VIOLATES |
| V28 | responses_compat.py:120-151 (`_repair_orphan_tool_messages`) | Orphan `role:tool` messages rewritten to `role:user` with fabricated "Tool result…" text | /responses | VIOLATES |
| V29 | responses_compat.py:353-400 (`build_chat_body`) | Whitelist rebuild: only model/messages/stream/temperature/top_p/max_tokens/tools/tool_choice/reasoning survive; **everything else dropped** (`text`, `response_format`, `metadata`, `store`, `truncation`, `parallel_tool_calls`, …); `instructions` → system message; temp/top_p float-cast; `max_output_tokens`→`max_tokens` | Every /responses request | VIOLATES |
| V30 | responses_compat.py:262-350, :433-640 | Response/stream fabrication: synthetic `resp_*`/`msg_*`/`rsn_*`/`fc_*` ids, `reasoning_content` surfaced as a `reasoning` output item (:286-288, :320-321), usage remapped/zero-defaulted, full `response.created`→`response.completed` SSE envelope synthesized | Every /responses | VIOLATES (inherent) |
| V31 | main.py:1072-1086 (`forward_headers`) | Header allowlist: only `x-forwarded-for`, `x-real-ip`, `user-agent`, `accept`, `anthropic-version`, `anthropic-beta`, `openai-beta`, `x-request-id` forwarded (sanitized); all others dropped; **synthetic `x-request-id` generated** when client omits (:1084-1085) | Always | VIOLATES (drops + injected header) |
| V32 | main.py:2164-2166 | `Authorization: Bearer <pool key>` replaces client auth | Always | **ACCEPTABLE** |
| V33 | main.py:2086-2092 | Pre-flight `MODEL_REGISTRY.call_plan` → wrapper 400/500 before upstream | Registry rejects | VIOLATES (wrapper-origin errors) |
| V34 | main.py:2096-2098 | Body deep-copied via `json.loads(json.dumps(body))` — non-JSON-serializable edge cases would fail; bytes re-serialized | Always | VIOLATES (byte-level, same class as N1) |
| V35 | main.py:1144-1164 (`_normalize_upstream_error`), calls :2192, :2250 | Upstream error bodies rewritten to OpenAI shape; 404 "page not found" → **rewritten to 400 `model_not_found`** (status changed) | Upstream errors | VIOLATES |
| V36 | main.py:1130-1142 (`_safe_response_body`) | Non-JSON upstream bodies replaced with fabricated error envelope | Malformed upstream | VIOLATES |
| V37 | main.py:2144-2158, :2285; :1330-1332 | Fabricated 503 (circuit breaker / pool exhausted); fabricated per-IP 429 in auth middleware | Breaker open / exhausted / over limit | VIOLATES |
| V38 | main.py:1899-1947 (`_stream_chat`) | Plain-chat streams: chunks decoded/re-encoded (`errors='replace'`); on missing `[DONE]`: **fabricated SSE error event** ("context/history too large…" guess) and synthetic `data: [DONE]` injected | Streaming chat; fabrications only on abnormal EOF | VIOLATES |
| V39 | main.py:1717-1728 (`/v1/complete`) | Legacy endpoint: `prompt` rewritten into `messages`, `prompt` deleted | /v1/complete | VIOLATES (translation endpoint) |
| V40 | main.py:1789-1791 | `/v1/embeddings`: **injects `input_type: 'query'`** when client omits and input is a string | Embeddings, field absent | VIOLATES |
| V41 | main.py:1580-1595 | `/v1/models` fabricated: local catalog build, enrichment metadata, alias pseudo-entries, `dynamic_alias_target` key | Always | VIOLATES |
| V42 | main.py:2394-2529 (catch-all) | Body re-serialized (`json=body`), `body['model']` overwritten with resolved id (:2423), unknown models → wrapper 404, headers allowlisted; error bodies passed closer to verbatim here (:2503) | All other /v1 paths | VIOLATES (same classes) |
| V43 | main.py:2152-2283 | Retry/rotation replays identical prepared body; rotation only | Retriable statuses | **ACCEPTABLE** (mechanics; body already mutated once) |
| V44 | capabilities.py:278-355 | Per-capability `defaults` tables (temperature/top_p/max_tokens etc.) exist but are **only served via GET /v1/capabilities/params — never applied to request bodies** (verified: no apply call sites) | — | ACCEPTABLE (informational) |

**Answer to the critical question:** the compat layers activate only for `/v1/messages`, `/v1/responses`, and `/v1/complete` — never for a plain `/v1/chat/completions` call. However a plain passthrough chat request is **still not transparent**: it is subject to V4 (param deletion), V6 (max_completion_tokens rename), V7 (stream_options injection), V8 (max_tokens clamp), V10, V11, V15 (reasoning surfaced as content in the response), V31, V34–V38.

---

## 3. Wrapper: opencode (`opencode/src/main.py`, 1459 lines)

Flow: `/v1/chat/completions` quasi-passthrough; `/v1/responses` native only for gpt-* (`_zen_family == "responses"`), otherwise translated; `/v1/messages` native only for claude-*/qwen3.5* families, otherwise translated. All upstream calls via `proxy_request_with_pool` (L468).

| # | file:line | What is changed | Condition | Verdict |
|---|---|---|---|---|
| O1 | main.py:560-561 | `Authorization: Bearer <pool key>`; `Content-Type: application/json` forced | Always | **ACCEPTABLE** |
| O2 | :561 | `Accept-Encoding: identity` injected (disables upstream compression) | Always | ACCEPTABLE (wire-level), differs from client |
| O3 | :563-569 | Header allowlist: only `anthropic-beta`, `anthropic-version`, `openai-beta`, `x-api-key`, `x-request-id` forwarded (sanitized); all others dropped (incl. user-agent) | Always | VIOLATES (drops) |
| O4 | :485-486 | **Injects `anthropic-version: 2023-06-01`** when client omitted it | Native /messages path | VIOLATES (default injection) |
| O5 | :228-235, :366-383, :1077, :1131, :1328 | Model rewriting: `opencode/` prefix stripped (:377-378); alias names (`sonnet`, `claude-*`…) → `_dynamic_alias_target` (env-seeded :877-880); `body["model"]` mutated in place | Prefix: always; alias: bound target only | VIOLATES |
| O6 | :401-403, :414-416 + all `JSONResponse` | No raw-byte passthrough: request + non-stream response JSON round-tripped | Always | VIOLATES |
| O7 | :1103-1107, :1114, :1147-1151, :1162, :1283, :1343-1347, :1399, :1405 | Upstream response headers dropped; fixed SSE headers added | Always | VIOLATES |
| O8 | :412, :424 | Upstream ≥400 bodies rewritten via `normalize_upstream_error` | Any upstream error | VIOLATES |
| O9 | :425-427 | 2xx non-dict body replaced with `{"error":…}` (status kept 200) | Malformed upstream 2xx | VIOLATES |
| O10 | :492-498 | Post-call `call_plan` + identity check can **discard a successful upstream response** and return 500 `MODEL_ID_MUTATION` / 400 `MODEL_CALL_PLAN_INVALID` | Any request with model | VIOLATES |
| O11 | :527-528 | Error rewritten "All configured OpenCode keys failed…" | Pool exhausted | VIOLATES |
| O12 | :531-554 → :1114 | `_ensure_chat_message`: `content: null` → `""`; missing `usage` → zeros | Non-stream chat 200s | VIOLATES |
| O13 | :818-820 | `: heartbeat` SSE comments injected (incl. native Anthropic streams via :1344) | Streaming, ≥5 s gaps | VIOLATES |
| O14 | :821-822 | Synthetic `data: [DONE]` on upstream EOF (disabled for native /messages, `terminal_done=False`) | Chat/responses streams | VIOLATES |
| O15 | :1070-1087, :1317-1324 | Wrapper-invented 400s (max_tokens bounds/1M cap, role whitelist, tool_call_id, Anthropic checks) | Matching input | VIOLATES |
| O16 | :187-224, :1078-1081, :1329-1332 | FREE_ONLY 400 gate | Only FREE_ONLY=yes | VIOLATES (policy) |
| O17 | :1160-1161 | Upstream 200 with `{"type":"error"}` body remapped to **HTTP 400** | /responses native path | VIOLATES (status change) |
| O18 | :690-761 (`responses_to_chat`) | Full rewrite: `input`→`messages`, `developer`→`system` (:720-721), `instructions` merged into system (:726-730), orphan tool repair (:731), `max_output_tokens`→`max_tokens` w/ default 4096 floor 1 (:628, :733-736), temp/top_p float-cast, nameless tools dropped; **drops `reasoning`, `response_format`, `stop`, `seed`, `parallel_tool_calls`, `metadata`, `stream_options`, …** | /responses, non-gpt models | VIOLATES |
| O19 | :695-696, :1280-1299 | `_RESPONSE_STORE` server-side history injected into upstream messages | /responses translate path | VIOLATES |
| O20 | :763-781 (`chat_to_responses`) | Response rewrite: `reasoning_content`/`reasoning` → synthesized `reasoning` output item; synthetic ids; usage remapped | /responses translate, non-stream | VIOLATES |
| O21 | :1174-1282 | Fully synthesized Responses SSE envelope; usage recomputed (:1214-1218) | /responses translate, stream | VIOLATES |
| O22 | :1258-1260 | Stream exception → literal `[upstream stream error: …]` **injected as model output text** | Stream error, no prior output | **VIOLATES (severe — fabricated content)** |
| O23 | :574-634 (`anthropic_to_openai`) | Full A→O rewrite: `cache_control` stripped (:575); system flattened; thinking → `reasoning_content` (:605-624); tool_result flattened (non-text blocks dropped); `max_tokens` clamped `max(x or 4096, 1)` (:628); **drops `temperature`, `top_p`, `top_k`, `stop_sequences`, `tool_choice`, `metadata`, thinking config entirely** | /messages, non-claude/qwen3.5 models | **VIOLATES (severe — sampling params silently dropped)** |
| O24 | :639-683 (`openai_to_anthropic`) | Response rewrite: `reasoning_content` → thinking block (:644); DSML parsed into fabricated tool_use blocks (:652-653); stop_reason mapping (:674-678); synthetic ids | Same path, non-stream | VIOLATES |
| O25 | :1362-1400 | OpenAI SSE → Anthropic SSE via shared `AnthropicStreamState` (incl. `force_done` fabricated endings) | /messages translate, stream | VIOLATES |
| O26 | :1053-1060 | `count_tokens` fabricated (`len//4`), never proxied | Always | VIOLATES |
| O27 | :948-1023 | `/v1/models` mutated: hardcoded fallback catalog (:953-964), alias pseudo-models (:993-1002), per-model availability annotations injected (:1008-1016), extra top-level keys, FREE_ONLY filter, cache instead of live upstream | Always | VIOLATES |
| O28 | :1310-1312 | Wrapper per-IP 429 (120 rpm) | /messages only | VIOLATES |
| O29 | :479-526 | Retry rotation replays same (already-normalized) body; no per-retry mutation | Retriable statuses | ACCEPTABLE (mechanics) |
| O30 | :428-429 | Transport exceptions → synthesized 502 | On exception | ACCEPTABLE (unavoidable) |
| O31 | :1091-1096 | Family routing: "google" family posts the OpenAI body to `{base}/models/{model}` (different upstream shape/URL) | google-family models | VIOLATES (semantics differ per model) |

`key_pool.py`/`metrics.py`: no request/response content touched.

---

## 4. Wrapper: blackbox (`blackbox/src/main.py`, 1213 lines)

Flow: `/v1/chat/completions` quasi-passthrough; `/v1/responses` **always** translated; `/v1/messages` **always** translated (no native Anthropic upstream).

| # | file:line | What is changed | Condition | Verdict |
|---|---|---|---|---|
| B1 | main.py:304-305 | `Authorization: Bearer <pool key>`; `Content-Type` forced | Always | **ACCEPTABLE** |
| B2 | :305 | `Accept-Encoding: identity` injected | Always | ACCEPTABLE (wire-level) |
| B3 | :307-313 | Header allowlist: only `anthropic-beta`, `anthropic-version`, `openai-beta`, `x-request-id`, `user-agent` forwarded (sanitized); `x-api-key` and all others dropped | Always | VIOLATES (drops); x-api-key replacement ACCEPTABLE |
| B4 | :176-183, :256-265, :915-917, :944-947, :1101-1104 | Alias names (`sonnet`/`opus`/`haiku`/`claude-*`) → `_dynamic_alias_target` (env-seeded :721-724); `body['model']` mutated in place | Alias names, target bound | VIOLATES |
| B5 | :332, :342 + all `JSONResponse` (:934, :966, :1157…) | No raw-byte passthrough: JSON round-trip both directions | Always | VIOLATES |
| B6 | :929, :934, :960, :966, :1152, :1157 | Upstream response headers dropped; fixed SSE headers added | Always | VIOLATES |
| B7 | :340, :349 | Upstream ≥400 bodies rewritten via `normalize_upstream_error` | Any upstream error | VIOLATES |
| B8 | :350-352 | 2xx non-dict body replaced with error envelope | Malformed 2xx | VIOLATES |
| B9 | :400-409 | Post-call `call_plan` check can void a successful response → 500/400 | Any request | VIOLATES |
| B10 | :438-439 | Error rewritten "All configured Blackbox keys failed…" | Pool exhausted | VIOLATES |
| B11 | :443-457 → :934 (incl. :454 `setdefault('usage', zeros)`) | `content: null` → `""`; missing usage → zeros | Non-stream chat 200s | VIOLATES |
| B12 | :886-899 → :922 | `_clean_tools`: request `tools` filtered (nameless removed); **`tools` key deleted if none survive** | /chat, tools present | VIOLATES |
| B13 | :660-664 | `: heartbeat` injected; synthetic `data: [DONE]` on EOF | Streaming chat | VIOLATES |
| B14 | :872-883, :1093-1100 | Wrapper-invented 400s (max_tokens bounds/1M cap, roles, tool_call_id, Anthropic checks) | Matching input | VIOLATES |
| B15 | :202-226, :918-921, :948-951, :1105-1108 | FREE_ONLY 400 gate | Only FREE_ONLY=yes | VIOLATES (policy) |
| B16 | :563-622 (`responses_to_chat`) | Full rewrite (developer→system :590-591, instructions merged :596-600, orphan tool repair :601, `max_output_tokens`→`max_tokens` :603-606 w/ default `max(x or 4096, 1)` :517, temp/top_p cast, nameless tools dropped); **drops `reasoning`, `response_format`, `stop`, `seed`, `metadata`, `stream_options`, …** | **Every** /responses request | VIOLATES |
| B17 | :566-568, :965, :1072, :1075-1080 | `_RESPONSE_STORE` history injected into upstream messages | /responses | VIOLATES |
| B18 | :625-634 (`chat_to_responses`) | Response rewrite; synthetic ids/status; usage remapped; **`reasoning_content` silently discarded** (unlike opencode) | /responses non-stream | VIOLATES |
| B19 | :969-1072 (`_responses_stream`) | Fully synthesized Responses SSE envelope; usage recomputed (:1011-1013) | /responses stream | VIOLATES |
| B20 | :1048-1052 | Stream exception → `[upstream stream error: …]` **injected as model output text** | Stream error, no prior output | **VIOLATES (severe — fabricated content)** |
| B21 | :462-526 (`anthropic_to_openai`) | Full A→O rewrite on **every** /messages request: cache_control stripped (:463); system flattened; base64 images → data-URLs (:488-495); thinking → `reasoning_content` (:496-497, :514-515); tool_result flattened, non-text blocks dropped (:500-503); `max_tokens` clamped `max(x or 4096, 1)` (:517); orphan tool repair; **drops `temperature`, `top_p`, `top_k`, `stop_sequences`, `tool_choice`, `metadata`, thinking config** | Always on /messages | **VIOLATES (severe)** |
| B22 | :537-555 (`openai_to_anthropic`) | Response rewrite: reasoning → thinking block (:540-543); stop_reason mapping incl. `content_filter→refusal`, forced `tool_use` when tool calls present (:553); usage remap; synthetic ids | /messages non-stream | VIOLATES |
| B23 | :1116-1152 | OpenAI SSE → Anthropic SSE via shared `AnthropicStreamState` (fabricated endings via `force_done`) | /messages stream | VIOLATES |
| B24 | :1154-1156 | Upstream errors re-wrapped into Anthropic envelope, extra error fields stripped | /messages errors | VIOLATES |
| B25 | :862-869 | `count_tokens` fabricated (`len//4`) | Always | VIOLATES |
| B26 | :170-174, :792-853 | `/v1/models` mutated: curated hardcoded fallback, cache instead of live, availability annotations injected (:836-845), alias pseudo-models (:846-852), FREE_ONLY filter | Always | VIOLATES |
| B27 | :905-907, :1086-1088 | Wrapper per-IP 429 (chat **and** messages) | >120 rpm/IP | VIOLATES |
| B28 | :387-440 | Retry rotation replays same body; no per-retry mutation | Retriable statuses | ACCEPTABLE (mechanics) |
| B29 | :353-354 | Transport exceptions → synthesized 502 | On exception | ACCEPTABLE |

---

## 5. Shared code (`common/` + `model-registry/service.py`) — mutation primitives invoked by wrappers

| # | file:line | What is changed | Condition | Verdict |
|---|---|---|---|---|
| C1 | translations/shared.py:17-70 (`parse_dsml_from_text`) | Response text rewritten: DSML markup removed, fabricated `tool_use` blocks with synthetic `toolu_dsml_*` ids; text `.strip()`ed; fullwidth-bar normalization alters text | Only when DSML present in text | VIOLATES when invoked (nous, opencode) |
| C2 | translations/shared.py:73-114 (`repair_orphan_tool_messages`) | Request roles rewritten (tool→user + injected prefix text); non-text blocks dropped; non-dict messages deleted | Orphan tool msgs | VIOLATES (request path) |
| C3 | translations/shared.py:117-133 (`strip_cache_control`) | Deletes every `cache_control` key, mutating request in place | Wherever called | VIOLATES |
| C4 | translations/shared.py:136-192 (`normalize_upstream_error`) | Upstream error body replaced; message truncated 2000 chars; error `type` overridden by HTTP status (402 relabeled `authentication_error`); empty msg → "HTTP <status>" | Every routed upstream error | VIOLATES |
| C5 | translations/anthropic_stream.py:43-206 (`AnthropicStreamState`) | Full O→A stream rewrite: fabricated `msg_<ms>` ids; zeroed cache/usage defaults; only `choices[0]` kept; non-string deltas dropped; `reasoning_content`/`reasoning` → thinking blocks (no signature); synthetic `toolu_<idx>` ids; **stop_reason forced to `tool_use` whenever tool_map non-empty even if upstream said stop/length** (:163); unknown finish_reason → `end_turn`; `force_done` fabricates clean endings on truncation (:181-203) | O→A translation paths | VIOLATES per item; CONDITIONAL as required emulation |
| C6 | model/identity.py:11-20, :67-98 | Model normalization: URL-decode (`unquote` can corrupt IDs with literal `%xx`), whitespace strip, case-insensitive alias lookup, alias → `canonical_target`, `provider/` prefix strip for upstream id | Every resolve; alias rewrite only when binding exists | CONDITIONAL/VIOLATES when bindings loaded |
| C7 | model/contracts.py:42-51 + service.py:163-165 | `model_changed` **hardcoded `False`** even when an alias rewrote the model — self-reported transparency metadata is misleading | Always | Flag: do not trust this field |
| C8 | model/call_plan.py:35 | `profile.request_rules` → `parameter_rules` conduit (empty in all manifests today; DB-stored profiles could carry overrides) | Profile-dependent | Watchlist |
| C9 | middleware.py:88-103 | `sanitize_header_value`: strips control chars + surrounding whitespace of forwarded headers | All forwarded headers | ACCEPTABLE (injection defense; still a mutation) |
| C10 | middleware.py:22-65 | 413 for bodies >10 MB | Oversized only | ACCEPTABLE |
| C11 | model/sanitize.py:21-60 | Redacts/truncates error details — telemetry path only, not client-facing | Observations only | ACCEPTABLE |
| C12 | model/registry.py:92-112 | Routes `anthropic_messages`/`openai_responses` surfaces to upstream `/v1/chat/completions` for all four providers — makes cross-protocol translation the configured default | Manifest-driven | CONDITIONAL (root enabler of all emulation) |
| C13 | service.py:204-229 (`/internal/aliases`) | Admin-gated alias installation — the operational channel for model rewriting across wrappers | Admin token | CONDITIONAL |

---

## Final summary — transparency violations ranked by severity

### CRITICAL — client semantics silently overridden or content fabricated/lost
1. **nvidia-python reasoning/thinking injection** (V1–V3): `chat_template_kwargs {enable_thinking/thinking}`, `reasoning_effort: high` injected per model-pattern on the /messages and /responses paths; GLM `thinking: false` injected even when the client explicitly enables thinking (commit 0781634 regression vs. its own stated intent).
2. **nvidia-python "surface reasoning as content"** (V15, main.py:1110-1122, commit 0781634): on the *plain* `/v1/chat/completions` path, empty responses get the model's private reasoning substituted as the answer text.
3. **Fabricated model output**: synthetic thinking block "[Reasoning not supported…]" presented as model thinking (nvidia V25); `[upstream stream error: …]` injected as assistant text (opencode O22 :1258-1260, blackbox B20 :1048-1052).
4. **Silent conversation truncation** (nvidia V19, anthropic_compat.py:171-192): oldest messages dropped from /messages requests when a local token estimate exceeds the window — invisible content loss.
5. **Sampling parameters silently dropped in translation**: temperature/top_p/top_k/stop_sequences/tool_choice/metadata/thinking-config discarded on /messages (opencode O23, blackbox B21) and broad param whitelists on /responses+/messages everywhere (N21, O18, B16, V23, V29).
6. **max_tokens overridden**: nous injects default 4096 and floors client values to ≥1024 (N19/N20); opencode/blackbox default `max(x or 4096, 1)`; nvidia silently clamps to per-model caps (V8) and renames `max_completion_tokens` (V6).
7. **nvidia-python unconditional param deletion** (V4): `max_output_tokens`, `max_input_tokens`, `think`, `context_*` etc. removed from every body including plain chat.

### HIGH — protocol/response integrity
8. **stop_reason/finish_reason overrides**: forced `tool_use` regardless of upstream finish (common C5 :163; nous N28; nvidia V24; blackbox B22); nvidia 404→400 `model_not_found` rewrite (V35).
9. **Upstream error bodies replaced everywhere** (`normalize_upstream_error`, C4 + N11/O8/B7): types remapped (402→authentication_error), messages truncated, "All configured … keys failed" rewrites.
10. **Successful upstream responses can be voided post-hoc** by the call_plan identity check → wrapper 500/400 (O10, B9; pre-flight in nous N13 / nvidia V33).
11. **Model-name substitution** via dynamic alias binding in all four wrappers + `opencode/` prefix strip (N5, V11, O5, B4; common C6/C13), with `model_changed` self-reported as `False` (C7). Confined to alias names — concrete ids pass through — per contract rule 8, but responses echo the substituted model.
12. **stream_options injection** (nvidia V7) and `DEFAULT_PARAMS` env-injection (V5).

### MEDIUM — byte/wire-level and stream framing
13. **No raw-byte passthrough anywhere**: JSON round-trip of requests and responses; all upstream response headers dropped (N1/N2, V34, O6/O7, B5/B6).
14. **SSE stream mutation**: heartbeat comments injected (N16, O13, B13); nous drops `event:`/`id:`/`retry:` SSE lines (N17); synthetic `[DONE]`/terminal events fabricated on EOF (N18, V38, O14, B13); nous suppresses deltas containing "DSML" (N29).
15. **Header allowlisting**: 4–8 headers forwarded, everything else dropped; `anthropic-version` defaulted (O4, V17); synthetic `x-request-id` (V31); `Accept-Encoding: identity` forced (O2, B2).
16. **Wrapper-invented validation** rejecting requests upstream might accept (max_tokens 1M cap, role whitelists, etc. — all four) and wrapper-local per-IP 429s / circuit-breaker 503s.

### LOW / structural (endpoint emulation & discovery)
17. `/v1/messages`, `/v1/responses`, `/v1/complete` are emulators with fully fabricated response envelopes, synthetic ids, estimated/zeroed usage, server-side `_RESPONSE_STORE` history injection, thinking↔reasoning extraction, orphan-tool rewriting — 100% of such traffic on nvidia/blackbox/nous, most of it on opencode.
18. `/v1/messages/count_tokens` fabricated (`len//4`) in all four wrappers — never proxied.
19. `/v1/models` fabricated/annotated in all four wrappers (curated hardcoded entries, alias pseudo-models, availability annotations, caches instead of live upstream).

### ACCEPTABLE (proxying necessities)
- Authorization/x-api-key replacement with pool keys; Host/Content-Length regeneration; header-value control-char sanitization; retry rotation that replays the body unmodified between attempts; 502 on transport exceptions; request-size limiting; embeddings `input_type` default is borderline (V40) but NIM-required.

**Note on the project's own framing:** `WRAPPER_CONTRACT.md` explicitly *licenses* much of this (protocol translation, tool normalization, error normalization, reasoning parameter mapping for NVIDIA). So the codebase is internally consistent with its contract — but it does **not** meet the stricter "fully transparent, body/headers/semantics unchanged" standard this audit was asked to verify. The clearest contract-vs-code contradictions are V3 (GLM thinking override against client intent), V15 (reasoning surfaced as content on the plain chat path), V19 (silent history truncation), and C7 (`model_changed` always `False`).
