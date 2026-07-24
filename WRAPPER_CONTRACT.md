# Wrapper Monorepo Contract

This monorepo contains provider-specific wrappers that must behave as one coherent product.  Upstreams differ (NVIDIA NIM, Nous, OpenCode Zen), but the wrapper contract is intentionally identical across descendants.

## Non-Negotiable Runtime Contract

Every wrapper must expose these client-facing surfaces where technically possible:

- OpenAI-compatible Chat Completions: `POST /v1/chat/completions`
- OpenAI-compatible Responses API: `POST /v1/responses`
- Anthropic-compatible Messages API: `POST /v1/messages`
- Anthropic token counting: `POST /v1/messages/count_tokens`
- Model discovery: `GET /v1/models`
- Capability/health/metrics endpoints

Every wrapper must preserve these invariants:

1. **Provider errors are not surfaced prematurely.** A single failed key/token is never a whole-wrapper failure.
2. **All-key retry before client error.** For retriable/key-level statuses (`401`, `402`, `403`, `408`, `409`, `429`, `5xx`), try every available credential path before returning an error to the agent/client.
3. **Per-key cooldown.** The key that failed is cooled down and skipped temporarily; other keys continue serving traffic.
4. **Exact in-flight accounting.** A key is reserved exactly once when selected and released exactly once after non-stream completion, stream completion, stream exception, or upstream EOF.
5. **Stream lifecycle is terminally complete.** OpenAI streams end with `data: [DONE]`; Anthropic streams end with `message_delta` + `message_stop`; Responses streams end with `response.completed` before `data: [DONE]`.
6. **No unstructured tool leakage.** Claude Code/Codex/Hermes/OpenClaw must receive structured tool calls/results, not raw DSML or provider-specific tool markup.
7. **Conversation continuity.** Responses `previous_response_id` stores enough assistant `tool_calls` context so the next `function_call_output`/tool result is never orphaned.
8. **Transparent model choice.** Wrappers do not silently substitute client-selected models. Aliases (`sonnet`, `haiku`, `opus`, `claude-*`) are dynamic/operator-bound, not hardcoded provider choices.
9. **SDK-shaped errors.** OpenAI surfaces return OpenAI-shaped errors; Anthropic surfaces return Anthropic-shaped errors.
10. **Provider-specific behavior stays behind the adapter boundary.** Client/agent semantics remain uniform even when upstream protocols differ.

## Shared Conceptual Pipeline

All wrappers follow the same conceptual request pipeline:

```text
Client/Agent
  ↓
Ingress endpoint (/v1/chat/completions, /v1/responses, /v1/messages)
  ↓
Auth + CORS + input validation
  ↓
Model alias resolution + FREE_ONLY/policy checks
  ↓
Protocol translation (Anthropic↔OpenAI, Responses↔Chat)
  ↓
Tool schema normalization + invalid placeholder drop
  ↓
Credential selection (effective-load key pool)
  ↓
Upstream provider call
  ↓
Retry/cooldown across credential pool on key-level/retriable errors
  ↓
Provider response normalization
  ↓
Strict SSE or JSON response lifecycle
  ↓
Metrics + exact key release
```

## Provider-Specific Adapter Boundaries

### `nvidia-python`

NVIDIA is the most feature-rich adapter because NIM has model catalog, multiple endpoint families, capability classes, model verification, and reasoning parameter injection.

Provider-specific code is allowed for:

- NIM model discovery and retired/unavailable model tracking
- NIM capability classification (`chat`, `vision`, `image`, `ranking`, etc.)
- NIM reasoning/thinking parameter mapping
- NVIDIA-specific base URLs (`integrate.api`, `ai.api`, `nvcf`)

But it must still obey the shared contract above. Current status:

- KeyPool owns per-key reservation exactly once.
- Server `_in_flight` is separate from per-key `in_flight`.
- `/v1/chat/completions`, `/v1/responses`, `/v1/messages`, and catch-all stream paths close streams deterministically.
- Responses API stores assistant tool calls for subsequent `previous_response_id` turns.

### `nous`

Nous upstream exposes OpenAI-style chat completions. The wrapper therefore translates Anthropic and Responses requests into Chat Completions.

Provider-specific code is allowed for:

- OAuth token loading from Hermes `AUTH_PATH`
- static `NOUS_API_KEY*` fallback pool
- curated free model catalog and Nous model metadata

Current status:

- OAuth token is tried first when configured.
- If OAuth fails with key-level/retriable failure, static `NOUS_API_KEY*` pool is tried.
- Static keys use `KeyEntry` state, cooldowns, RPM, and in-flight tracking.
- Runtime Chat/Responses/Messages use `post_nous_with_retries()`.
- Model/capability discovery uses `get_nous_json_with_retries()` and falls back to curated catalog if upstream is unavailable.

### `opencode`

OpenCode Zen exposes multiple native families (`chat`, `responses`, `messages`, `google` style model paths). The wrapper chooses the upstream family but keeps client-facing semantics uniform.

Provider-specific code is allowed for:

- Zen family routing (`responses`, `messages`, `google`, `chat`)
- Zen model id normalization (`opencode/` prefix removal)
- native pass-through for GPT Responses and Claude Messages where appropriate

Current status:

- `KeyPool` uses effective-load selection and per-key cooldowns.
- `proxy_request_with_pool()` retries all available keys for retriable statuses before surfacing errors.
- Chat, Responses native, Responses translated, Messages native, Messages translated, model discovery, and capabilities use unified retry semantics.
- Native Anthropic streams do not get OpenAI `[DONE]`; OpenAI-compatible streams do.


### `blackbox`

BLACKBOX AI exposes an OpenAI-compatible public API (`/chat/completions`) with a broad model catalog. The wrapper keeps BLACKBOX provider details behind the adapter boundary while exposing the same monorepo contract.

Provider-specific code is allowed for:

- BLACKBOX base URL and model id policy
- optional `FREE_ONLY` policy; transparent default is `no`
- curated discovery manifest and free allowlist
- translating Responses and Anthropic Messages into BLACKBOX chat completions

Current status:

- `KeyPool` uses effective-load selection and per-key cooldowns.
- `proxy_request_with_pool()` retries all available BLACKBOX keys for retriable statuses before surfacing errors.
- Chat, Responses, Anthropic Messages, model discovery, and capabilities use unified retry semantics.
- FREE_ONLY may be enabled explicitly; when enabled, aliases must be seeded to a permitted concrete model.

## Retriable Status Semantics

These statuses are treated as credential/provider-transient and should trigger retry on another key if available:

- `401`, `402`, `403`: key/auth/quota related; cooldown longer
- `408`, `409`: transient/request contention
- `429`: rate limit; cooldown per `Retry-After` when available
- `5xx`: upstream transient/server-side

Non-retriable client/request errors (for example malformed JSON, invalid roles, invalid tool schema, policy block like `FREE_ONLY`) are returned immediately because retrying another key cannot fix them.

## Model Catalog and Availability Contract

**Model substitution is forbidden.** A failed model must never be replaced by another model or provider; only the native credential/key pool may rotate credentials for the same model.

Model discovery and invocation availability are separate facts:

- A provider `/models` catalog is a provider-level inventory.
- A successful invocation is scoped to the provider endpoint and credential/account.
- `404` messages such as `Function ... not found for account` mean `account_unavailable`, not global retirement.
- Only explicit provider end-of-life/retirement evidence may become `globally_retired` and a default local hard block.
- `401/403`, `429`, timeouts, `5xx`, and invalid parameters must retain their own error classes.
- Background verification may inform discovery and observability, but must not reject an explicit concrete model because of a transient or account-scoped result and must never select another model.
- Each wrapper persists a last-good catalog and account-scoped state in its ignored SQLite `model-state.db`; raw keys are never stored.

See [MODEL_AVAILABILITY.md](MODEL_AVAILABILITY.md) for schema, TTL, and regression requirements.

## Stream Contract

### OpenAI Chat stream

- Forward upstream chunks.
- If upstream closes without `[DONE]`, synthesize `data: [DONE]`.
- If upstream errors mid-stream, emit a best-effort SDK-shaped SSE error and close with `[DONE]`.

### OpenAI Responses stream

Required order:

```text
response.created
response.in_progress
response.output_item.added
response.content_part.added
response.output_text.delta / response.function_call.delta / reasoning deltas
response.output_text.done
response.content_part.done
response.output_item.done
response.completed
data: [DONE]
```

No deltas are allowed after `response.completed`.

### Anthropic Messages stream

Required order:

```text
message_start
content_block_start / content_block_delta / content_block_stop ...
message_delta
message_stop
```

No raw OpenAI `[DONE]` is appended to native Anthropic SSE.

## Test Contract

Regression tests must cover at least:

- Anthropic tool transparency (no DSML leakage)
- OpenAI Responses terminal lifecycle
- `previous_response_id` tool-call continuity
- zero-chunk/EOF stream closure
- multi-key retry after first key 429
- key cooldown skip behavior
- exact NVIDIA in-flight release

Current validation target:

```bash
python -m compileall -q nvidia-python/src nous/wrapper_nous.py opencode/src tests
pytest -q
python tests/run_transparency_check.py
python -m ruff check nvidia-python/src nous/wrapper_nous.py opencode/src tests --select F,E9,B
python -m bandit -q -r nvidia-python/src nous/wrapper_nous.py opencode/src -lll
python -m pip_audit -r nvidia-python/requirements.txt
python -m pip_audit -r nous/requirements.txt
python -m pip_audit -r opencode/requirements.txt
```

## Development Rule

When adding a new wrapper descendant, start from this contract rather than copying a single provider implementation blindly. The provider adapter may differ, but the client-facing lifecycle, key retry semantics, stream closure semantics, and tool/result semantics must remain identical.
