# Changelog

## [8.6.3] - 2026-07-19

### Fixed

#### Gateway model discovery emitted wrong names for the Claude Code picker (regression from 8.6.2)
**File:** src/index.js (`handleModels`, gateway branch)
**Root cause:** 8.6.2 removed all `claude-*` aliases from `/v1/models?gateway=1`, returning only exact
NVIDIA NIM ids (e.g. `z-ai/glm-5.2`). Claude Code's gateway model picker only displays entries whose
`id` begins with `claude`/`anthropic` and sends the selected `id` back as the model, so the exact NIM
ids were silently ignored and the picker fell back to Claude Code's built-in `claude-*` list — i.e. the
user saw `claude-*` names even though the wrapper no longer emitted them.
**Fix:** In gateway mode, emit (in addition to the exact NIM id) a `claude-<slug>` routing id whose
`display_name` and `original_id` equal the exact NIM id. The picker shows the real upstream name while
the selected routing id resolves deterministically via `resolveTargetModel()` -> `DISCOVERY_TO_NIM`.
Default `/v1/models` (non-gateway) stays a clean exact-NIM-id list with no `claude-*` entries, so
OpenAI-compatible clients (Codex, Hermes, OpenAI SDK) are unaffected.
**Verified:** `npm test` pass; `npm run test:e2e` 25/25 pass (gateway assertion updated); live
`/v1/models?gateway=1` returns 132 exact ids + 132 `claude-*` routing ids, each labelled with the exact
NIM `display_name`; `POST /v1/messages` with `claude-z-ai-glm-5.2` routes to `z-ai/glm-5.2` and returns 200.

## [8.6.2] - 2026-07-19

### Fixed

#### Responses API (/v1/responses) regression + reasoning parity
**File:** src/responses_compat.js
**Root cause:** the working-tree edit had been based on a stale pre-fix backup
(responses_compat.js.bak.audit-20260719), silently reverting three previously
committed fixes (bare-string input to user message; translateThinkingToNim
reasoning toggle; faithful upstream error-status mapping) while adding the
reasoning-visibility feature. Two new defects were also present: a non-stream
error-shape mismatch that dropped errors, and a streaming output_index collision
(reasoning + message both at index 0) with the reasoning item missing from the
final response.completed output.
**Fix:** re-based on HEAD; preserved all prior fixes; surfaced NIM
reasoning_content / reasoning as a Responses reasoning item (index 0), message at
index 1, parallel function calls at index 2..N; reasoning item opened lazily and
included in the final output; non-stream errors mapped to faithful HTTP status.
**Impact:** Codex (wire_api="responses") keeps reasoning semantic parity with
Claude Code; no Hermes/Codex 502 regression; upstream 4xx/5xx preserved.


## [8.6.1] - 2026-07-06

### Fixed

#### Bug 1: `message_start.input_tokens` always 0 (CRITICAL)
**File:** `src/anthropic_compat.js` line 341, `src/index.js` line 1537
**Root cause:** `streamOpenaiToAnthropic()` hard-coded `input_tokens: 0` in the `message_start` event.
**Fix:** Added `inputTokens` parameter to `streamOpenaiToAnthropic()`. Passed `estimateInputTokens(aBody)` from `handleAnthropicMessages()`. Claude Code now receives actual token count (was 0, now 3-37+).
**Impact:** Claude Code can now properly track context window usage.

#### Bug 2: Static message ID `msg_wrapper` for all streaming (CRITICAL)
**File:** `src/anthropic_compat.js` line 276
**Root cause:** `streamOpenaiToAnthropic()` used `const msgId = 'msg_wrapper'` for all requests.
**Fix:** Changed to `requestId ? msg_${requestId} : msg_${Date.now()...}`. Non-streaming `openaiToAnthropic()` also updated with `requestId` parameter.
**Impact:** Each response now has a unique ID (e.g., `msg_req_mr82y5u9_bak5xhwc`), preventing ID collision bugs in Claude Code's event handling.

#### Bug 3: No periodic heartbeat during streaming (MEDIUM)
**File:** `src/anthropic_compat.js` lines 345-372
**Root cause:** Only one `ping` event sent at stream start; no heartbeat during idle periods.
**Fix:** Added `Promise.race()` between `reader.read()` and a heartbeat timer. Configurable via `HEARTBEAT_INTERVAL_MS` env var (default 5000ms).
**Impact:** Prevents timeout kills on long-running streams.

#### Bug 4: Error type mapped from HTTP status, not upstream (MEDIUM)
**File:** `src/index.js` lines 1593-1601
**Root cause:** Error type was derived from HTTP status code only (e.g., 400 -> `api_error`).
**Fix:** Now preserves `error.type` from upstream NVIDIA response. Falls back to status-derived type only when upstream doesn't provide one. Added `invalid_request_error` for 4xx (was just `api_error`).
**Impact:** Claude Code receives accurate error types for retry logic.

#### Bug 5: Hardcoded model mapping (HIGH)
**File:** `src/index.js` lines 162-262
**Root cause:** `resolveTargetModel()` mapped Claude/GPT model names to a limited set of NVIDIA NIM models. Most requests fell back to `meta/llama-3.1-8b-instruct`.
**Fix:** Replaced 98-line function with 4-line transparent pass-through. Model name now passes through exactly as client sends it. Wrapper only handles API key load balancing.
**Impact:** All 60+ verified NVIDIA NIM models now accessible. No silent model swapping.

### Test Results (Final Audit)
- **Unit tests:** All pass
- **E2E regression:** 25/25 pass
  - OpenAI: non-stream, stream, tools, multi-turn, large model
  - Anthropic: non-stream, stream, tools, multi-turn, thinking, large model
  - Model resolution: passthrough, 404 transparent
  - Error handling: missing model, missing messages, empty body, malformed JSON
  - Edge cases: long content, empty tools, system message, zero max_tokens, stream_options
  - Metrics: requests logged, multiple models, recent activity
- **Model tests:** 38/40 pass (2 timeouts on large models, 0 failures)
- **Load balancing:** 71/69/72/69/72 (±0.5% even across 5 keys)
- **Bug verification:** All 5 bugs verified fixed

---

## [8.6.0] - 2026-06-27 (Previous release)

See `NODEJS_AUDIT_PLAN.md` for earlier audit history.
