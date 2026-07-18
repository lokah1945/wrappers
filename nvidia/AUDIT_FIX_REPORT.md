# AUDIT-FIX REPORT — NVIDIA NIM Wrapper Comprehensive Audit

**Component:** `wrapper-nvidia` NVIDIA NIM API proxy wrapper
**Date:** 2026-07-09
**Scope:** Full end-to-end audit of every aspect: transparent proxy behavior, OpenAI +
Anthropic compatibility, Claude Code integration, dynamic model registry, key/model
failover, testing coverage, stale artifacts, and failure reporting.

---

## Executive Summary

The wrapper-nvidia wrapper is a **mature, production-grade** transparent proxy. It
correctly implements OpenAI Chat Completions + Anthropic Messages API translation,
Claude Code model aliasing + gateway discovery, a dynamic NGC-synced model registry,
and multi-key failover with per-(key,model) rate-limit isolation. All 20 E2E tests,
unit tests, and regression tests pass.

**Issues found and fixed in this audit:**

| # | Severity | Area | Issue | Fix |
|---|---|---|---|---|
| 1 | Medium | Transparent proxy | Context-length errors wrapped in custom envelope | Pass through upstream error verbatim |
| 2 | Low | Code quality | Redundant error check + confusing variable name in handleAnthropicMessages | Removed dead code, renamed `oaiBodyOrError` → `translated` → `oaiBody` |
| 3 | Low | Metrics | Missing metrics recording for exhausted stream retries | Added metrics.recordRequest() in fallback path |
| 4 | Low | Testing | Missing E2E coverage for count_tokens, Ollama, capabilities, context-error passthrough | Added 5 new E2E test checks (15→20) |
| 5 | Low | Housekeeping | Stale planning .md files in production reference | Removed PATCH_PLAN.md, ANALYSIS_E2E_WRAPPER_NVIDIA.md |

**No critical correctness bugs found.** The wrapper correctly routes, translates,
fails over, and reports errors.

---

## 1. Transparent Proxy Audit

### 1.1 Payload modification

**Verdict: PASS (with one fix applied)**

The wrapper performs ONLY necessary format translation:
- `anthropicToOpenai()` — Anthropic content blocks → OpenAI messages (required for NIM)
- `openaiToAnthropic()` / `streamOpenaiToAnthropic()` — OpenAI responses → Anthropic SSE (required for Anthropic clients)
- `sanitizeNvidiaPayload()` — splits parallel tool calls into sequential messages (required; NIM only supports single tool-calls per turn)
- `convertVisionImages()` — downloads HTTP image URLs → base64 data URIs (required; NIM needs inline images)
- Image-gen response normalization: `{artifacts:[{base64}]}` → `{data:[{b64_json}]}` (required; OpenAI clients expect this format)

No other payload fields are added, removed, or rewritten.

### 1.2 Error passthrough

**Verdict: PASS (one fix applied)**

- **404, 401, 403, 413, 429, 500+** errors: passed through verbatim with original upstream status code and error body. ✅
- **500-intercept for "single tool-calls":** NVIDIA returns HTTP 500 for parallel tool calls (should be 400). The wrapper converts to 400 with the original upstream message preserved. This is a **necessary format correction** — NVIDIA's status code is wrong for this validation error. ✅
- **DEGRADED response:** detected and triggers key failover (wrapper-level concern, not payload modification). ✅
- **Context-length errors (FIXED):** Previously wrapped in a custom "friendly" message (`getFriendlyContextLimitError()`). This violated the transparent-proxy principle — clients match upstream wording for retry/recovery. **Now passes through verbatim** (the original upstream `error.message` and `error.type` are preserved exactly). ✅

### 1.3 Model unavailability

`isModelUnavailable()` always returns `false` — transparent proxy mode. Verification sweep still runs and collects data, but never blocks requests proactively. This is correct for a transparent proxy.

---

## 2. OpenAI + Anthropic Compatibility Audit

### 2.1 Endpoints

| Endpoint | Status | Notes |
|---|---|---|
| `POST /v1/chat/completions` | ✅ | Full OpenAI Chat Completions API |
| `POST /v1/messages` | ✅ | Full Anthropic Messages API |
| `POST /v1/messages/count_tokens` | ✅ | Token counting (uses `estimateInputTokens`) |
| `POST /v1/embeddings` | ✅ | OpenAI Embeddings |
| `POST /v1/images/generations` | ✅ | OpenAI Images (with genai normalization) |
| `POST /v1/images/edits` | ✅ | OpenAI Image Edits |
| `POST /v1/ranking` | ✅ | Reranking |
| `GET /v1/models` | ✅ | Model discovery with `claude-*` aliases |
| `GET /v1/models/:id` | ✅ | Single model info |
| `GET /v1/capabilities` | ✅ | Rich capability metadata |
| `GET /v1/capabilities/params` | ✅ | Parameter definitions per capability type |
| `POST /api/chat` | ✅ | Ollama chat compatibility |
| `POST /api/generate` | ✅ | Ollama generate compatibility |
| `GET /api/tags` | ✅ | Ollama model list |

### 2.2 Anthropic Messages API spec compliance

| Spec requirement | Status | Implementation |
|---|---|---|
| `id`: `msg_` prefix | ✅ | `msg_${requestId}` or `msg_${timestamp}_${random}` |
| `type`: `"message"` | ✅ | Hardcoded |
| `role`: `"assistant"` | ✅ | Hardcoded |
| `model`: matches request | ✅ | From request body |
| `content`: array of blocks | ✅ | text, thinking, tool_use blocks |
| `stop_reason`: enum | ✅ | `end_turn`, `max_tokens`, `tool_use`, `refusal` |
| `stop_sequence`: null | ✅ | NIM doesn't support stop_sequences well |
| `usage`: input/output/cache | ✅ | `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` |
| SSE event types | ✅ | `message_start`, `content_block_start`, `content_block_delta`, `content_block_stop`, `message_delta`, `message_stop`, `ping`, `error` |
| Thinking contract | ✅ | Synthetic thinking block emitted when client requests extended thinking but model doesn't reason |

### 2.3 Header forwarding

`forwardHeaders()` forwards ALL client headers except hop-level/auth headers (`host`, `connection`, `content-length`, `transfer-encoding`, `accept-encoding`, `x-forwarded-for`, `x-real-ip`, `authorization`, `x-api-key`, `api-key`). This includes:

- `anthropic-version` ✅
- `anthropic-beta` ✅
- `x-hermes-*` ✅
- `nv-*` / `x-nv-*` ✅
- `OpenAI-Beta` ✅
- `x-stainless-*` ✅

### 2.4 SSE streaming

`streamOpenaiToAnthropic()` reads from the upstream undici `ReadableStream` and yields SSE chunks as they arrive — **real-time, not buffered**. Heartbeat pings every 5s during idle periods keep the connection alive through long reasoning think times. ✅

---

## 3. Claude Code Integration Audit

### 3.1 Model aliases

`ALIAS_TO_NIM` maps Claude Code's built-in family aliases to real NIM models:

| Alias | Default NIM model | Configurable via |
|---|---|---|
| `haiku`, `claude-haiku`, `claude-3-5-haiku`, etc. | `meta/llama-3.1-8b-instruct` | `CLAUDE_CODE_DEFAULT_HAIKU_MODEL` |
| `sonnet`, `claude-sonnet`, `claude-3-5-sonnet`, etc. | `deepseek-ai/deepseek-v4-pro` | `CLAUDE_CODE_DEFAULT_SONNET_MODEL` |
| `opus`, `claude-opus`, `claude-3-opus`, etc. | `nvidia/nemotron-3-ultra-550b-a55b` | `CLAUDE_CODE_DEFAULT_OPUS_MODEL` |

All Claude Code model family variants (including `claude-sonnet-4-5`, `claude-opus-4-5-latest`, etc.) are covered. Custom aliases can be added via `ANTHROPIC_ALIAS_MAP` JSON env var. ✅

### 3.2 Gateway model discovery

`GET /v1/models` returns every NIM model aliased to a `claude-<owner>-<model>` id. Claude Code's gateway model picker only lists ids beginning with `claude`/`anthropic`, so this makes the wrapper visible. The real NIM id is preserved in `original_id`/`aliases` fields, and `resolveTargetModel()` reverse-maps at request time. ✅

### 3.3 Capability declarations

`enrichModelMetadata()` reports accurate capabilities:
- `supports_parallel_tool_calls: false` — critical; NIM rejects parallel tool calls with HTTP 500
- `supports_vision` — only for vision models (classified by capabilities.js)
- `context_window` — from NGC-synced registry (authoritative), falling back to heuristic map
- `max_output_tokens` — from NGC registry or sensible default
- `supports_function_calling`, `supports_tool_choice`, `supports_streaming`, etc. — per model type

Non-chat models (embedding, image, rerank, etc.) correctly omit `context_window` and `max_output_tokens`. ✅

### 3.4 Context-window suffix stripping

Claude Code appends `[1m]`-style context-window suffixes to model ids. `_stripContextSuffix()` removes these before resolution. ✅

---

## 4. Dynamic Model Registry Audit

### 4.1 Data source

`Registry` class fetches from NVIDIA's official NGC featured-models catalog:
- URL: `https://assets.ngc.nvidia.com/products/api-catalog/featured-models.json`
- Configurable via `NGC_FEATURED_MODELS_URL` env var
- Refresh interval: 1 hour (`REGISTRY_REFRESH_SEC=3600`)

### 4.2 Fallback chain

1. **Live NGC fetch** (primary) — 20s timeout
2. **On-disk cache** (`nvidia/ngc-featured-cache.json`) — survives restarts/outages
3. **Static seed** (5 models) — last resort for first-ever air-gapped boot
4. **Heuristic map** (`MODEL_CONTEXT_WINDOWS` / `getContextWindow()`) — final fallback

Never silently guesses. Every fallback is logged with source. ✅

### 4.3 Usage

`registry.getOfficialContext(modelId)` is called in `enrichModelMetadata()` to provide authoritative `context_window` and `max_output_tokens`. The NGC value always wins over heuristics. ✅

### 4.4 Limitation noted

The static seed and current cache contain only 5 models (same set). The live NGC endpoint is unreachable from this environment, so the full catalog isn't populated. When connectivity returns, the live fetch will populate the full map. The heuristic fallback in `getContextWindow()` covers all other models adequately.

---

## 5. Key/Model Failover Audit

### 5.1 Architecture

`KeyPool` manages N API keys with:
- **Two-tier rate limiting:** soft limit (pacing) + hard limit (blocking)
- **Per-key RPM tracking:** sliding 60s window
- **Per-model blocking on keys:** `modelBlocks[modelId]` isolates model-level 429s to specific keys
- **429 classification:** corroboration-based (multi-key-for-model → model-level; multi-model-on-key → key-level)
- **FIFO admission queue:** with configurable `QUEUE_LIMIT_PER_KEY_PER_SEC` pacing
- **Load shedding:** rejects when queue ≥ `MAX_QUEUE_SIZE` or total in-flight ≥ `INFLIGHT_SOFT_CAP`
- **inFlight healing:** periodic sweep resets stuck counters (threshold: 600s, safe for reasoning models)

### 5.2 Failover behavior

`proxyOpenai()` retries across keys on:
- 429 (rate limit) → next key
- 400 DEGRADED → next key
- 400 with strippable unsupported params → retry with stripped params
- 500+ → next key
- Network error → next key

Max retries = `MAX_RETRIES + 1` or `pool.totalKeys`, whichever is larger. Each retry acquires a fresh key via `pool.acquire(modelId)`. ✅

### 5.3 Key release correctness

All code paths release keys correctly:
- Non-streaming responses: key released inside `proxyOpenai()` before return
- Streaming responses: `keyReleased = true` flag set; caller releases in `finally` block
- Error paths: `finally` block in `proxyOpenai()` catches any unreleased key
- Stream retry loop: old key released in `finally`, new key acquired for retry

No key leaks found. ✅

---

## 6. End-to-End Testing Audit

### 6.1 Test suites

| Suite | Command | Coverage | Result |
|---|---|---|---|
| Unit | `npm test` | KeyPool, Anthropic compat, Capabilities, Metrics | ✅ All pass |
| Regression | `npm run test:regression` | Dead-upstream fail-fast (504 within budget) | ✅ Pass |
| E2E mock | `npm run test:e2e` | Full wrapper surface against mock NIM upstream | ✅ 20/20 pass |

### 6.2 E2E test matrix (20/20)

| # | Surface | Assertion |
|---|---|---|
| 1 | Health | `GET /health` → 200 |
| 2 | Model discovery | `/v1/models` returns `claude-*` ids |
| 3 | NGC context | `deepseek-v4-pro` context_window ≥ 200000 |
| 4 | OpenAI non-stream | `/v1/chat/completions` → content + usage |
| 5 | OpenAI stream | SSE `data:` … `[DONE]` |
| 6 | Anthropic non-stream | `/v1/messages` → `type:message`, text block |
| 7 | Anthropic stream | `message_start`/`content_block_delta`/`message_stop` |
| 8 | Alias routing | `haiku` → `meta/llama-3.1-8b-instruct` |
| 9 | Discovery alias | `claude-<slug>` → real NIM id |
| 10 | Error passthrough | 404 `not_found_error` verbatim |
| 11 | Tool calling | Anthropic `tool_use` ⇄ OpenAI `tool_calls` |
| 12 | Extended thinking | Anthropic `thinking` block ⇄ OpenAI `reasoning_content` |
| 13 | Embeddings | 4096-dim vector |
| 14 | Ranking | Passthrough |
| 15 | Image gen | `data[].b64_json` normalization |
| 16 | Token counting | `/v1/messages/count_tokens` → positive `input_tokens` |
| 17 | Context error verbatim | Upstream error message preserved exactly (no wrapper envelope) |
| 18 | Ollama tags | `/api/tags` → model list |
| 19 | Ollama chat | `/api/chat` → Ollama-format response |
| 20 | Capabilities | `/v1/capabilities` → `supports_parallel_tool_calls: false` |

### 6.3 Coverage gaps (noted, not critical)

- **Live upstream E2E:** `integrate.api.nvidia.com` is blackholed from this environment. All functional testing uses a faithful mock. Re-run against live NIM when connectivity returns.
- **Generation endpoints (video/audio/TTS/ASR/OCR):** Exercised only through the catch-all proxy path. The mock returns synthetic responses. True NIM generation latency/quality is not tested.
- **Load/concurrency testing:** Not implemented. The key pool's pacing and load-shedding logic is unit-tested but not stress-tested with concurrent requests.

---

## 7. Failure Reporting Audit

### 7.1 Model verification

`verifyModels()` runs periodic sweeps (configurable interval, default 10min) probing every cached model with a minimal "ping" request. Failures are recorded via `markModel()` and persisted in the metrics database.

### 7.2 Visibility

- `GET /metrics/model-status` — returns unavailable model list, verified count, learned model limits
- `GET /stats` — includes catalog summary
- `GET /health` — returns `degraded` when no keys available
- Prometheus metrics — `wrapper_nvidia_keys_blocked`, `wrapper_nvidia_exhaustions_total_24h`
- SSE real-time events — `rate-limit` events broadcast to dashboard

### 7.3 Transparent proxy mode note

`isModelUnavailable()` always returns `false` — models are never proactively blocked. The verification data is collected for observability but does not affect routing. This is correct for a transparent proxy: the upstream decides what models are available; the wrapper reports but doesn't gatekeep.

---

## 8. Stale Artifacts Cleanup

### 8.1 Production reference (`/root/wrapper/nvidia`)

Removed stale planning documents:
- `nvidia/PATCH_PLAN.md` — initial build planning doc (superseded by implementation)
- `nvidia/ANALYSIS_E2E_WRAPPER_NVIDIA.md` — initial analysis doc (superseded by implementation)

---

## 9. Code Quality Notes

### 9.1 Fixed in this audit

- **Redundant error check:** `handleAnthropicMessages()` had two identical `if (oaiBodyOrError.error)` checks (lines 1757 and 1768). The second was unreachable dead code. Removed.
- **Confusing variable naming:** `oaiBodyOrError` persisted through the function even after the error case was handled. Renamed to `translated` (result of `anthropicToOpenai()`) then `oaiBody` (after error check confirms it's a valid body).
- **Missing metrics:** Exhausted stream retries in `handleAnthropicMessages()` didn't record metrics. Added `metrics.recordRequest()` in the `!finalCapture` fallback path.

### 9.2 Existing quality practices (noted positively)

- `readBody()` has timeout + size limit guards
- `jsonResp()` has `res.headersSent`/`res.writableEnded` guard against double-write
- `proxyOpenai()` has a `finally` block that catches unreleased keys
- `handleRequest()` has a pre-response watchdog (`PRE_RESPONSE_TIMEOUT_MS`) against blackholed upstreams
- `streamOpenaiToAnthropic()` has heartbeat pings during idle periods
- `KeyPool` has `healInFlight()` periodic sweep against stuck counters
- `.env` file is watched for hot-reload without restart
- CORS headers include all Anthropic SDK headers (`anthropic-version`, `anthropic-beta`, `x-api-key`)

---

## 10. Recommendations (not blocking)

1. **Live E2E re-validation:** When `integrate.api.nvidia.com` becomes reachable, run the full E2E suite against the live upstream to confirm streaming behavior with real reasoning models (deepseek-v4-pro, Qwen3-thinking).
2. **Load testing:** Add a concurrency/load test that exercises the key pool's pacing and load-shedding logic under parallel requests.
3. **Registry seed expansion:** The static seed (5 models) is adequate as a last-resort fallback but could be expanded with more models from a one-time NGC fetch when connectivity is available.
4. **Generation endpoint tests:** Add dedicated E2E tests for video (cosmos), audio (fugatto), TTS, and ASR endpoints when live upstream is reachable.
5. **Streaming retry metrics:** The stream retry loop in `handleAnthropicMessages()` records metrics only on final outcome. Per-retry-attempt metrics would improve debuggability but are not required for correctness.

---

## 11. Verification

All changes verified via:

```
npm test                # Unit tests — all pass
npm run test:regression # Dead-upstream fail-fast — 504 within budget
npm run test:e2e        # E2E mock — 20/20 checks pass
```

No regressions. The wrapper is production-ready.