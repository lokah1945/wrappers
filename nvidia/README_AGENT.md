# README_AGENT — wrapper-nvidia (maintainer/agent guide)

> Single source of truth: `/root/wrapper/nvidia`. The repo `lokah1945/wrappers` subtree `nvidia/` is the only active codebase.

wrapper-nvidia is a **stateless protocol-translation layer** for NVIDIA NIM. It is NOT an orchestration engine, scheduler, router, retry engine, or policy engine. It validates, normalizes, translates, forwards to NIM, normalizes the response, and returns it deterministically. All runtime policy (model choice, fallback, retry, cascade) lives in the client/agent/user.

## What It Actually Does

- **OpenAI Chat Completions** → NIM `/v1/chat/completions` (`/v1/chat/completions`).
- **Anthropic Messages** → NIM `/v1/chat/completions` via `anthropic_compat.js` (`/v1/messages`, `/v1/messages/count_tokens`).
- **OpenAI Responses API** → NIM `/v1/chat/completions` via `responses_compat.js` (`/v1/responses`). Added for Codex (`wire_api="responses"`).
- **Embeddings / Ranking / Image generation** → respective NIM endpoints.
- **Model transparency:** names pass through verbatim; no hardcoded mapping or silent swap.
- **Key rotation & rate-limit pacing** in `key_pool.js`; multi-key, per-(key,model) isolation.

## Endpoints (verified in `src/index.js`)

| Endpoint | Method | Notes |
|---|---|---|
| `/v1/chat/completions` | POST | OpenAI path (Hermes ILMA profile). |
| `/v1/messages` | POST | Anthropic path (Claude Code). |
| `/v1/messages/count_tokens` | POST | Token estimate. |
| `/v1/responses` | POST | OpenAI Responses path (Codex). |
| `/v1/embeddings` | POST | |
| `/v1/ranking` | POST | |
| `/v1/images/generations` | POST | |
| `/v1/models` | GET | Public; exact NVIDIA NIM ids. `?gateway=1` adds `claude-*` routing ids (with `display_name`) for the Claude Code picker. NGC-synced context windows. |
| `/v1/capabilities` | GET | Public; single source of model capability truth. |
| `/v1/capabilities/params` | GET | Public; supported param matrix. |
| `/health`, `/stats`, `/metrics/prom`, `/metrics/activity`, `/metrics/model-status`, `/version` | GET | Monitoring. |

## Clients (verified configs)

### Claude Code → `/v1/messages`
- `ANTHROPIC_BASE_URL=http://127.0.0.1:9100` (do NOT append `/v1`).
- `ANTHROPIC_API_KEY` = any non-empty value (auth is `BEARER_TOKEN` from `.env`, not the Anthropic key).
- Header `anthropic-version: 2023-06-01` forwarded to NIM verbatim.

### Codex → `/v1/responses`
- `model_provider` base_url `http://127.0.0.1:9100/v1` and `wire_api="responses"`.
- The wrapper translates Responses `input`/`instructions`/`tools`/`reasoning` into NIM chat/completions and streams back Responses SSE.

### Hermes Agent (ILMA profile) → `/v1/chat/completions`
- `base_url=http://127.0.0.1:9100`, `custom:wrapper-nvidia`.
- Uses OpenAI chat format; `x-hermes-*` headers forwarded to NIM.

## Gateway Model Discovery (Claude Code model picker)

Claude Code's gateway model picker calls `CLAUDE_CODE_GATEWAY_MODEL_DISCOVERY_URL` (configured to
`http://localhost:9100/v1/models?gateway=1`) and **only displays entries whose `id` begins with
`claude`/`anthropic`**, sending the selected `id` straight back as the model. To satisfy this
contract without lying about upstream naming:

- **Default `/v1/models`** returns the EXACT NVIDIA NIM ids only (clean passthrough for OpenAI-compatible
  clients: Codex, Hermes, OpenAI SDK, OpenCode).
- **`/v1/models?gateway=1`** emits, per model, the exact NIM id PLUS an additional `claude-<slug>` routing
  id whose `display_name` = the exact NIM id and `original_id` = the exact NIM id. The picker shows the
  real upstream name while the selected id routes deterministically.
- Inbound `claude-<slug>` ids resolve back to the exact NIM id via `resolveTargetModel()` ->
  `DISCOVERY_TO_NIM` (rebuilt from the exact ids in `refreshDiscoveryMap()`). No routing ambiguity,
  no hardcoded name maps beyond the capability-driven alias resolution.

## Reasoning / Thinking Handling

- **Anthropic path:** client `thinking` block → `translateThinkingToNim()` sets the model-specific NIM toggle (`chat_template_kwargs` or `reasoning_effort`); upstream reasoning surfaces as Anthropic `thinking` blocks.
- **Responses path:** `body.reasoning` → `translateThinkingToNim()` (same single-source logic). Upstream NIM `reasoning_content`/`reasoning` is surfaced as a Responses `reasoning` item (index 0); the assistant message is index 1; parallel function calls are index 2..N. Items are opened lazily and closed with matching `output_item.done`, and included in the final `response.completed` `output`.
- **Chat path:** NIM `reasoning_content`/`reasoning` passes through in the OpenAI message.

## Error Model (transparent passthrough)

- `proxyOpenai` returns non-stream errors as `{ status, data: {error:{message,type}} }`. Upstream HTTP status + normalized `{error:{message,type}}` envelope are preserved (NIM FastAPI `detail` is reshaped into `error.message`).
- `/v1/responses` maps the error to a faithful HTTP status (upstream status first, else derived from `error.type`): `invalid_request_error`→400, `rate_limit_error`→429, server → 500.
- 404/401/403/413/422/429/500 are passed through; a NIM "parallel tool-calls" 500 is corrected to 400 with the original message preserved.

## Capability-Driven Design

All behavioral variation (reasoning mechanism, tool calling, streaming, multimodal, structured output) is represented as semantic capability and discovered from `/v1/capabilities`. The wrapper does not infer behavior from model/publisher name strings except where no official metadata exists.

## Testing

```bash
npm test          # unit (test/test.js)
npm run test:e2e  # mock-NIM E2E (test/e2e-mock.js), 25/25
```

`responses_compat.js` is additionally covered by a module-level harness that loads `createResponsesHandler` with stubbed deps and asserts non-stream/stream reasoning, tool-calling, and error-status passthrough.

## Operating Notes

- Do NOT restart `wrapper-nvidia.service` mid-audit until validations pass.
- `.env` `BEARER_TOKEN` gates all POST endpoints except the public GETs above.
- `ngc-featured-cache.json` is an NGC-synced cache (`syncedAt` bumped on refresh); a `syncedAt` change alone is not a logic change.
