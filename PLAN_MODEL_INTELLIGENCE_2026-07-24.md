# End-to-end implementation plan
# Central Model Intelligence, Capability, and Call Contract Registry

Date: 2026-07-24
Status: APPROVED CONCEPT — PLANNING ONLY
Scope: entire wrappers monorepo; no implementation in this planning step

---

## 0. Non-negotiable product contract

This is a transparent wrapper project. The following rules are absolute and must be enforced in code, tests, documentation, and review:

```text
MODEL_SUBSTITUTION       = FORBIDDEN
PROVIDER_SUBSTITUTION    = FORBIDDEN
ENDPOINT_SUBSTITUTION    = FORBIDDEN unless the same model/call contract explicitly defines it
KEY_ROTATION             = ALLOWED
SAME_MODEL_RETRY         = ALLOWED according to key/retry policy
EXPLICIT_MODEL_PASSTHRU  = REQUIRED
SILENT_DEFAULT_MODEL     = FORBIDDEN
SILENT_DEPRECATED_REDIRECT = FORBIDDEN for concrete model IDs
```

For an explicit concrete request, the model identity must remain unchanged:

```text
requested_model == resolved_model == provider_model_id_sent_upstream
```

The only normal changing variable during retries is the credential/key.

Aliases are not fallbacks. An alias is allowed only when it has an explicit, deterministic binding. A concrete model ID must never be silently redirected to a different model.

---

## 1. Current-state findings that drive the plan

### 1.1 Model inference fallback exists in NVIDIA and must be removed

Current `nvidia-python/src/main.py` builds:

```python
candidates = [model_id] + self._build_fallback_candidates(...)
```

`_build_fallback_candidates()` selects other models by heuristic size/speed ranking and is enabled by default unless an environment variable disables it. This violates the approved contract and is the first remediation item.

The final design must not retain a model fallback flag. A forbidden behavior should not remain available behind configuration.

### 1.2 Discovery fallback must be separated from inference behavior

Nous, OpenCode, Blackbox, and NVIDIA contain discovery fallback/cached catalog behavior. A last-known-good catalog is acceptable for discovery if it is marked stale. It must never cause an inference request for model A to be sent as model B.

Curated lists must be represented as `manual_manifest` or `last_known_good` with explicit availability confidence. They must not be presented as current upstream entitlement unless verified.

### 1.3 Alias state is currently process-global and mutable

The current `dynamic_alias_target` means the last concrete model used by any client can change the meaning of `sonnet`, `opus`, or `haiku` for every other client. This is not an acceptable long-term model identity contract.

Alias bindings must be scoped and deterministic.

### 1.4 Model knowledge is distributed across wrappers

Capabilities, limits, endpoint selection, reasoning transformations, deprecated redirects, static metadata, catalog discovery, and error classifiers are distributed across provider-specific files. New model maintenance therefore requires scattered edits and creates drift between wrappers.

`common/model_state.py` is a useful persistence seed, but it is currently a state store, not a complete model knowledge/call-contract system.

### 1.5 Provider errors must remain scoped

Global catalog facts, provider endpoint facts, account/key availability, capability errors, and transient runtime errors must not be represented by one boolean such as `unavailable`.

---

## 2. Target architecture

The target is a two-plane design:

```text
                         MODEL INTELLIGENCE PLANE
       ┌────────────────────────────────────────────────────────┐
       │ Canonical identity                                     │
       │ Model profiles and capabilities                        │
       │ Call contracts and adapter versions                    │
       │ Error rules and retry decisions                        │
       │ Alias bindings and lifecycle                           │
       │ Provider catalogs and revisions                        │
       │ Account-scoped observations                            │
       └──────────────────────┬─────────────────────────────────┘
                              │ local cache / optional API
       ┌──────────────────────┼───────────────────────────────┐
       │                      │                               │
  NVIDIA wrapper        Nous wrapper                    OpenCode/Blackbox
       │                      │                               │
       └────────────── local wrapper data plane ──────────────┘
                              │
                       Provider upstream
```

### 2.1 Model Intelligence Plane

Authoritative for:

- model identity;
- provider model ID;
- aliases;
- lifecycle;
- capability profile;
- limits;
- protocol support;
- call recipe;
- adapter name/version;
- error classification rules;
- retry/key-rotation policy;
- model-specific parameter transformations;
- provider catalog revisions;
- sanitized account-scoped observations.

It is not a model router and it never selects an alternative model.

### 2.2 Wrapper data plane

Remains authoritative for:

- client ingress;
- authentication;
- key pool and credential selection;
- per-key rate limits;
- request translation execution;
- upstream connection and streaming;
- response normalization;
- in-flight accounting;
- local cache usage;
- emitting observations.

The data plane must continue to work from a last-known-good local cache if the central control plane is temporarily unavailable.

### 2.3 Central service is not required on every inference call

The hot path must be:

```text
client
→ local model profile/cache
→ exact call plan
→ key pool
→ upstream
```

It must not be:

```text
client
→ network call to registry
→ upstream
```

for every request.

A central registry service may be introduced later, but wrappers must cache profiles locally and use stale-while-revalidate behavior.

---

## 3. Shared model domain objects

Create typed shared contracts in `common/model/`.

### 3.1 `ModelRef`

Represents what the client asked for:

```text
requested_name
normalized_name
canonical_id
provider
provider_model_id
is_alias
is_concrete
resolution_revision
```

### 3.2 `ModelProfile`

Represents the complete knowledge profile:

```text
canonical_id
provider
provider_model_id
family
version
lifecycle_state
catalog_source
catalog_revision
profile_revision
adapter_version
capabilities
limits
protocols
request_rules
policy
provenance
```

### 3.3 `CapabilityProfile`

Minimum fields:

```text
input_modalities: text/image/audio/video
output_modalities: text/image/audio/video
streaming
openai_chat
openai_responses
anthropic_messages
embeddings
vision
audio
image_generation
tools
parallel_tools
structured_output
reasoning
thinking
computer_use
```

Unknown capabilities must be represented as `unknown`, not false.

### 3.4 `LimitProfile`

```text
context_window
max_input_tokens
max_output_tokens
max_tools
max_images
max_request_bytes
max_stream_duration
supported_parameter_types
```

### 3.5 `ProtocolProfile`

```text
client_surface
upstream_surface
path
method
adapter_name
adapter_version
model_field
streaming_mode
response_adapter
```

Example:

```text
Anthropic Messages client
→ NVIDIA OpenAI Chat upstream
→ adapter anthropic-to-nvidia-chat.v2
```

### 3.6 `CallPlan`

A call plan is an exact instruction for the requested model, not a candidate list:

```text
canonical_model_id
provider_model_id
provider_endpoint
client_surface
adapter_name
adapter_version
parameter_transform
timeout_class
retry_policy
key_rotation_allowed
model_substitution_allowed = false
```

### 3.7 `AliasBinding`

```text
scope_type: global|wrapper|tenant|client|session
scope_id
alias
canonical_target
revision
active_from
active_until
source
```

Alias resolution must be deterministic and observable.

### 3.8 `AvailabilityObservation`

```text
provider
endpoint
model_id
account_scope_hash
credential_scope_hash
state
http_status
reason_code
reason_detail_sanitized
checked_at
consecutive_failures
consecutive_successes
confidence
source: catalog|probe|runtime|manual
```

Raw API keys and OAuth tokens are never stored.

---

## 4. Provider manifest design

Create source-controlled manifests:

```text
model-registry/manifests/
  providers/
    nvidia.yaml
    nous.yaml
    opencode.yaml
    blackbox.yaml
  models/
    nvidia/
    nous/
    opencode/
    blackbox/
  errors/
    nvidia.yaml
    nous.yaml
    opencode.yaml
    blackbox.yaml
  aliases/
```

### 4.1 Example model manifest

```yaml
canonical_id: nvidia/provider/model-a
provider: nvidia
provider_model_id: provider/model-a
lifecycle: active

capabilities:
  streaming: true
  tools: true
  parallel_tools: false
  vision: false
  reasoning: false
  openai_chat: true
  openai_responses: false
  anthropic_messages: translated

limits:
  context_window: 131072
  max_output_tokens: 16384

protocols:
  openai_chat:
    adapter: nvidia.chat.v1
    path: /v1/chat/completions
  anthropic_messages:
    adapter: anthropic-to-nvidia-chat.v2
    path: /v1/chat/completions

request_rules:
  remove_parameters: []
  clamp_parameters: {}

policy:
  transparent: true
  model_substitution: false
  provider_substitution: false
  key_rotation: true
```

### 4.2 Deprecated model IDs

A deprecated concrete model ID must not silently become another model.

Allowed behavior:

1. send the exact ID upstream and let the provider respond;
2. return a clear `MODEL_DEPRECATED` error;
3. use a replacement only when the client explicitly requested an alias whose documented target changed.

Automatic concrete-ID redirects such as `old-model → new-model` must be removed or converted into explicit alias bindings.

---

## 5. Exact request execution contract

Every wrapper must execute this pipeline:

```text
1. Receive requested_model
2. Parse and normalize syntax only
3. Resolve exact alias if and only if alias binding exists
4. Load exact ModelProfile
5. Build exactly one CallPlan
6. Validate/transform parameters without changing model identity
7. Acquire key from the wrapper's native KeyPool
8. Send upstream with the same provider_model_id
9. On permitted retry, rotate only the key
10. Classify the upstream result
11. Return success or the error for the same model
12. Persist/schedule observation
```

Required audit fields in every request event:

```text
requested_model
resolved_model
upstream_model
wrapper
provider
call_surface
adapter_version
profile_revision
key_scope_hash
attempt_number
model_changed=false
```

A runtime assertion/test must verify:

```python
assert upstream_model == resolved_model
assert model_changed is False
```

The only exception is a declared alias resolution, where both the requested alias and exact resolved target are recorded.

---

## 6. Retry and error policy

### 6.1 Key rotation matrix

| Situation | Rotate key? | Change model? | Result |
|---|---:|---:|---|
| key-specific 401/403 | Yes | No | retry same model |
| key-level 429 | Yes | No | retry same model |
| model/deployment 429 | Optional same-model retry | No | return same-model error if unresolved |
| account-specific 404 | Try other configured key/account | No | return account error |
| timeout | Yes according to policy | No | same-model retry |
| 5xx | Yes according to policy | No | same-model retry |
| invalid parameter 400/422 | No | No | return capability/request error |
| global EOL 410 | No | No | return retirement error |
| protocol adapter error | No | No | return adapter error |

### 6.2 Error states

Central classifier states:

```text
available
catalog_listed
unknown
account_unavailable
account_forbidden
invalid_credential
key_rate_limited
model_rate_limited
upstream_capacity
wrong_route
capability_mismatch
invalid_parameter
transient_failure
network_timeout
globally_retired
deprecated
```

There must be no generic `is_model_unavailable` decision without scope and reason.

### 6.3 Client error response

Responses should preserve:

```json
{
  "requested_model": "provider/model-a",
  "resolved_model": "provider/model-a",
  "model_changed": false,
  "error": {
    "type": "model_not_deployed_for_account",
    "message": "The requested model is not deployed for the configured account",
    "provider_code": "NOT_DEPLOYED_FOR_ACCOUNT"
  }
}
```

The exact envelope remains OpenAI-shaped or Anthropic-shaped according to the client surface.

---

## 7. Repository migration plan

### Phase 0 — Contract hardening and inventory

No central service yet.

Tasks:

1. Inventory every model-related branch across all wrappers.
2. Add a static check that fails on model fallback candidate generation.
3. Remove NVIDIA `_build_fallback_candidates()` and the `candidates` loop.
4. Remove `MODEL_FALLBACK_ENABLED` and `MODEL_FALLBACK_MAX_HOPS` semantics.
5. Audit catch-all and legacy paths for model substitution.
6. Audit all deprecated model redirect maps.
7. Decide which redirects are aliases and which must become explicit errors.
8. Review `FREE_ONLY` as an explicit policy gate, never as a substitution mechanism.
9. Make curated discovery lists report `source` and `catalog_stale`.
10. Add request-level exact-model assertions to test doubles.

Deliverables:

```text
MODEL_TRANSPARENCY_CONTRACT.md
model fallback removal
exact-model contract tests
model substitution grep/static check
```

### Phase 1 — Shared domain library

Create:

```text
common/model/
  __init__.py
  contracts.py
  identity.py
  profiles.py
  capabilities.py
  limits.py
  call_plan.py
  aliases.py
  errors.py
  policies.py
  observations.py
  local_store.py
```

Migrate wrappers to use these objects while preserving endpoint URLs and client behavior.

At this phase the registry is a Python library, not a network service.

### Phase 2 — Provider adapters

Create a common adapter interface:

```python
class ProviderAdapter(Protocol):
    provider_name: str

    def normalize_model_id(self, requested: str) -> str: ...
    def resolve_call_plan(self, profile, client_surface) -> CallPlan: ...
    def build_request(self, plan, request_body) -> dict: ...
    def normalize_success(self, plan, response) -> dict: ...
    def classify_error(self, plan, status, payload) -> ErrorClassification: ...
    async def refresh_catalog(self) -> CatalogSnapshot: ...
```

There must be no interface method such as:

```python
select_fallback_model()
select_alternative_provider()
```

Provider adapters:

```text
NvidiaAdapter
NousAdapter
OpenCodeAdapter
BlackboxAdapter
```

### Phase 3 — Manifest and local cache

Migrate current scattered metadata into manifests and local cache.

Required local behavior:

- read profile from local cache first;
- use last-known-good profile if registry is unreachable;
- mark profile stale;
- do not invent availability;
- do not reject explicit model only because catalog is stale;
- do not change model identity.

### Phase 4 — Central registry service

Introduce only after the shared library and manifests are stable.

Suggested components:

```text
model-registry/
  api/
    models.py
    resolve.py
    call_plans.py
    observations.py
  service/
    profile_service.py
    alias_service.py
    error_service.py
    catalog_sync.py
    observation_aggregator.py
  providers/
  manifests/
  migrations/
  tests/
```

Suggested APIs:

```text
GET  /v1/models
GET  /v1/models/{canonical_id}
GET  /v1/models/{canonical_id}/capabilities
POST /v1/resolve
POST /v1/call-plan
POST /internal/observations
GET  /internal/health
```

`/v1/resolve` performs exact identity and alias resolution only. It never returns an alternative model candidate.

### Phase 5 — Central sync and runtime observation

Catalog workers:

- fetch provider catalogs on schedule;
- store ETag/checksum/revision;
- preserve last-known-good data;
- mark removed entries as `not_seen`, not immediately retired;
- require provider evidence for global retirement.

Observation workers:

- accept sanitized runtime outcomes from wrappers;
- aggregate by provider/account/model/endpoint;
- apply confidence and consecutive failure policy;
- never promote account-scoped failure to global retirement.

### Phase 6 — Operational hardening

Add:

- schema migrations;
- profile revisioning;
- audit trail;
- admin read-only dashboard;
- metrics and alerts;
- central service health/readiness;
- local cache age metrics;
- registry outage simulation;
- backup and restore procedure;
- canary rollout per wrapper.

---

## 8. Testing strategy

### 8.1 Contract tests for no model substitution

For every wrapper, mock upstream and send:

```text
requested model = provider/model-a
key-1 returns 429
key-2 returns 500
key-3 returns 200
```

Assert every upstream request contains:

```text
model = provider/model-a
```

Then test all terminal errors and assert no request contains model B.

### 8.2 Error matrix tests

Test:

- account-specific 404;
- global EOL 410;
- wrong-route 404;
- key-level 429;
- model-level 429;
- 401/403;
- 400/422 capability mismatch;
- timeout;
- 5xx;
- malformed provider response.

### 8.3 Alias tests

Test:

- exact alias binding;
- unknown alias returns an error;
- alias scope isolation;
- client A cannot mutate client B's alias;
- alias revision is recorded;
- alias target is never selected due to a failed model.

### 8.4 Catalog tests

Test:

- fresh provider catalog;
- stale catalog;
- provider catalog outage;
- empty provider response;
- removed catalog model;
- restart persistence;
- catalog metadata does not imply account availability.

### 8.5 Capability and call-plan tests

For every provider/model profile, verify:

- endpoint;
- adapter;
- supported protocol surfaces;
- max tokens;
- parameter transformation;
- streaming behavior;
- tool behavior;
- exact model passed upstream.

### 8.6 Cross-wrapper tests

Maintain shared tests that run against all wrappers:

```text
same model identity
same error classification semantics
same key-rotation semantics
same no-fallback invariant
same model discovery fields
same observability fields
```

### 8.7 Static checks

CI must fail if it detects:

```text
_build_fallback_candidates
MODEL_FALLBACK_ENABLED
MODEL_FALLBACK_MAX_HOPS
select_alternative_model
fallback_model
model_candidates
```

Exceptions must be explicitly annotated as discovery cache or test fixture, never inference execution.

---

## 9. Observability

Every model request should emit structured fields:

```text
request_id
wrapper
provider
client_surface
requested_model
resolved_model
upstream_model
model_changed
canonical_model_id
profile_revision
adapter_version
alias_scope
key_scope_hash
attempt
http_status
state
reason_code
latency_ms
ttft_ms
streaming
```

Recommended metrics:

```text
wrapper_model_requests_total{provider,model,state}
wrapper_model_key_rotations_total{provider,model}
wrapper_model_substitution_total
wrapper_model_substitution_total must always be zero
wrapper_model_catalog_age_seconds{provider}
wrapper_model_profile_stale_total{provider}
wrapper_model_account_unavailable_total{provider,model}
wrapper_model_capability_mismatch_total{provider,model}
```

A nonzero `wrapper_model_substitution_total` must page or fail deployment validation.

---

## 10. Security requirements

- Never store raw API keys in central registry.
- Never log Authorization headers.
- Use credential/account fingerprints only.
- Separate operator/admin endpoints from client discovery endpoints.
- Do not expose internal reason details containing secrets.
- Sanitize upstream error bodies before central observation submission.
- Validate profile/manifests before activation.
- Sign or checksum profile revisions if central service is deployed separately.
- Keep model catalog public metadata separate from account entitlement data.
- Apply migrations atomically.

---

## 11. Deployment and rollout strategy

### 11.1 Development

1. Implement Phase 0 in a feature branch.
2. Run all current tests.
3. Add no-fallback contract tests.
4. Run wrappers against mocked upstream fixtures.
5. Verify request model identity with a recording fake upstream.

### 11.2 Shadow mode

Add a registry mode that computes the profile/call plan but does not change runtime behavior:

```env
MODEL_REGISTRY_MODE=shadow
```

Compare:

```text
current resolved model
registry resolved model
current endpoint
registry endpoint
current parameters
registry parameters
```

Any difference is reviewed before activation.

### 11.3 Local mode

```env
MODEL_REGISTRY_MODE=local
```

Wrappers use local manifest/cache while central service is not required.

### 11.4 Central-read mode

```env
MODEL_REGISTRY_MODE=central_read
```

Wrappers query central registry only on cache miss or profile revision change. The hot path remains local.

### 11.5 Canary

Roll out one wrapper at a time:

1. nvidia-python;
2. nous;
3. opencode;
4. blackbox.

For each wrapper, verify:

- exact model identity;
- key rotation;
- no model substitution;
- model profile correctness;
- catalog stale behavior;
- central registry outage behavior.

### 11.6 Rollback

Rollback must be possible by:

- reverting the profile revision;
- switching to local last-known-good cache;
- reverting wrapper code;
- restoring SQLite/PostgreSQL schema backup.

Rollback must never re-enable model fallback.

---

## 12. Acceptance criteria before production activation

The implementation is not complete until all criteria pass:

1. No inference code contains model fallback candidate selection.
2. No configured flag can enable model substitution.
3. Every upstream attempt for an explicit model uses the same model ID.
4. Only key rotation changes between attempts.
5. Alias resolution is explicit, scoped, and recorded.
6. Deprecated concrete IDs are not silently redirected.
7. Account-scoped failures remain account-scoped.
8. Global retirement requires provider evidence.
9. Capability mismatch returns an error rather than another model.
10. Catalog outage serves stale metadata only, never a substituted inference model.
11. Central registry outage does not stop cached wrapper inference.
12. All wrappers expose the same model identity and status fields.
13. No raw credentials appear in model state, logs, or observations.
14. Full test suite and no-fallback static checks pass.
15. `wrapper_model_substitution_total == 0` in staging and production.

---

## 13. Decisions to lock before implementation

The following defaults are recommended:

1. Model substitution: permanently forbidden.
2. Provider substitution: permanently forbidden.
3. Key rotation: enabled according to each wrapper's existing native pool.
4. Alias: allowed only with explicit scoped binding.
5. Concrete deprecated ID: no silent redirect.
6. Unknown explicit model: pass through and observe, unless the client request is syntactically invalid.
7. Catalog cache: stale-while-revalidate, marked stale.
8. Catalog metadata: never treated as account entitlement.
9. Capability mismatch: return error; do not change model.
10. Fallback candidate list: absent from production code and schema.

---

## Final target

The finished project should have this behavior:

```text
Client asks for Model A
        ↓
Central/shared registry explains exactly how Model A is called
        ↓
Wrapper calls Model A
        ↓
Wrapper may rotate key-1 → key-2 → key-3
        ↓
Wrapper still calls Model A
        ↓
Success: return Model A result
Failure: return Model A/provider/account error
        ↓
Central registry receives sanitized observation
```

There is no path from Model A failure to Model B.
