# Upstream protocol selection

Current production scope: NVIDIA, Nous and OpenRouter. BLACKBOX and the OpenCode provider are pending and frozen; root `AGENTS.md` and `wrappers.json` override historical all-provider instructions.

`COMPATIBILITY_LAYER` selects the upstream dialect. The request endpoint selects the client protocol. Selection never changes the requested model; only API keys rotate.

| Setting | Upstream dialect |
|---|---|
| `1` (default) | OpenAI compatible |
| `2` | Anthropic Messages compatible |
| `3` | Credential-free startup discovery, then cached effective dialect |

Auto discovery probes once during startup with a bounded five-second client timeout. It examines the models endpoint, then Messages and Chat routes using unauthenticated minimal requests. If the gateway gives ambiguous authentication responses or masks routes, use an explicit dialect. Failed discovery falls back to OpenAI; the configured `3` remains visible. It does not guarantee detection of every gateway implementation.

| Client API | OpenAI upstream | Anthropic upstream |
|---|---|---|
| Chat Completions | Native Chat forwarding | Chat ↔ Messages conversion |
| Responses | Native Responses forwarding | Responses ↔ Messages conversion |
| Messages | Messages ↔ Chat conversion | Native Messages forwarding |

Native paths preserve unknown request fields, tool types, null content, response metadata and SSE event bytes. They never guess a different response protocol or rewrite it. Authentication headers are replaced with the selected upstream credential; hop-by-hop headers are removed. Explicit aliases and documented request/token limits still apply. Optional idle heartbeat comments are inserted only between complete events. Upstream native errors and terminators remain native.

`RESPONSES_UPSTREAM_MODE=auto` is the default. It sends Responses to the native `/responses` endpoint and forwards `previous_response_id` unchanged. The owner authorized a Chat adapter only for a native endpoint that is definitively unsupported: a route-not-found 404, 405 or 501. Model/account 404, generic 400, auth/quota failures, timeouts, 5xx and malformed successful streams do not trigger fallback. `RESPONSES_UPSTREAM_MODE=native` disables even this approved route fallback.

`RESPONSES_UPSTREAM_MODE=chat` forces the historical Responses ↔ Chat adapter on an OpenAI upstream. The owner requires native passthrough when supported, so **do not force this mode in production on a supported upstream**. Isolated legacy regression fixtures set it to exercise the adapter. Cross-dialect Anthropic conversion does not depend on this exception.

On the legacy adapter, continuation is local, tenant-scoped and bounded by entry count, UTF-8 bytes and TTL. One worker is required unless shared state or affinity is configured. Native continuation belongs to the upstream, and its storage, expiry and credential/account affinity rules apply. Key hotswap does not transfer upstream-owned stored Responses between unrelated accounts; clients may send complete conversation input instead.

Invalid dialect or Responses mode values fail startup validation. Current regression and real-SDK gates cover native forwarding, cross conversion, tools, reasoning, streaming, key retries, cancellation and continuation. Supporting vendor updates requires preserving opaque native fields and updating cross converters and versioned tests as new schemas appear; unpublished future features cannot be certified in advance.

OpenRouter documents a native [Responses endpoint](https://openrouter.ai/docs/api/api-reference/responses/create-responses). NVIDIA's self-hosted NIM API also documents [native Responses](https://docs.nvidia.com/nim/large-language-models/latest/thinking-budget-control.html); hosted endpoint readiness must be verified separately. The current VPS configured routes were probed without generation, and actual inference evidence is recorded in the production report.
