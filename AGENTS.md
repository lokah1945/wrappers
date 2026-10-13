# Repository instructions

## Required scope check

Read `wrappers.json` before analyzing, changing, testing, installing, auditing or deploying this project. Its lifecycle status controls the eligible scope. Use `common/wrapper_scope.py` to select production wrappers when available. Do not hardcode an all-provider list.

The owner placed **BLACKBOX (`blackbox/`) and OpenCode provider (`opencode/`) in `pending` on 2026-10-07**. Pending means frozen: **do not read, analyze, edit, patch, test, probe, restart, stop, install or deploy these provider wrappers**, their environment files, logs or runtime state. Explicit CLI selection must not bypass this rule. Only an explicit instruction from the owner to reactivate a pending wrapper can change this scope. Existing uncommitted changes in these directories must be preserved untouched.

Production work currently covers `nvidia-python/`, `nous/`, `openrouter/`, their shared `common/` code, `model-registry/`, and the unified `dashboard/` control service authorized on 2026-10-11. Root metadata and documentation may record pending status without inspecting pending implementations. Shared fixes are verified against active wrappers only. Tests containing pending-provider reads or imports must be excluded before those reads occur; report excluded tests honestly.

The unified dashboard reads lifecycle metadata and monitors active services only. Pending cards and disabled menu entries are metadata only; do not fetch pending dashboards, endpoints, credentials or logs. Dashboard port 9105 is the lowest available slot: 9100 belongs to an unrelated catalog MCP service, 9101/9102 to active providers, and 9103/9104 remain reserved for frozen providers. OpenRouter stays on 9106; the registry remains an internal dependency on 9200. Do not change the unrelated catalog service or move frozen providers to fill slots. Deployment service selection uses `select_services`; inference tests use `select_wrappers`.

An **OpenCode client application** is a supported consumer of active backends. It is distinct from the frozen `opencode/` provider wrapper.

## Production behavior required by the owner

- Active wrappers serve OpenAI and Anthropic compatible clients, including Claude Code, Codex and OpenCode clients, through the endpoints and protocol versions actually supported and tested.
- Retry available API keys automatically for quota/rate-limit and invalid-key failures. Keep the exact requested model during retries. Release key reservations on success, error and cancellation. Return an actionable exhaustion response when no eligible key remains.
- Forward matching native protocols transparently: OpenAI Chat to OpenAI Chat, native OpenAI Responses to native OpenAI Responses, and Anthropic Messages to Anthropic Messages. Preserve unknown native fields and SSE event data for protocol evolution. Apply authentication, required provider routing metadata, key selection, explicit alias resolution and documented operational limits locally. Nous requires honest product/client tags when absent; preserve caller-supplied tags, and never impersonate another client.
- Translate OpenAI ↔ Anthropic only across protocols. The owner explicitly authorized a Responses → Chat adapter on 2026-10-07 **only when the native Responses endpoint is unsupported**. Default `RESPONSES_UPSTREAM_MODE=auto` tries native first and permits fallback for a definite unsupported-route 404, 405 or 501. Authentication, quota, invalid model, generic 400, 5xx and malformed successful native streams do not qualify. Keep native passthrough when available; do not force `chat` in production on a supported upstream. If native history cannot be retrieved by the local adapter, return an actionable context error.
- Validate streaming, tools, reasoning, response continuation, key rotation, cancellation and both OpenAI/Anthropic dialects with isolated controlled upstreams. Do not claim compatibility with every future update; add regression coverage when protocols or clients change.
- Production gates must use current results, not historical audit snapshots. Keep reports clear about passed tests, exclusions, source-only verification and real upstream verification.

These owner instructions supersede historical documentation requesting changes or tests across all five provider wrappers. Never touch a pending provider just to satisfy an older cross-wrapper rule.

## Safe collaboration

Preserve pre-existing worktree changes. Do not commit, push, disclose secrets, or send messages externally without authorization. Test servers must use isolated ports, fake credentials, temporary state and terminate only processes they started. Keep production service changes separate from test fixtures and document the actual deployed revision.
