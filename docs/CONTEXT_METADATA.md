# Context metadata and bounded recovery

Context limits come from the configured provider's catalog or an explicitly
identified provider publication. A model name, a family resemblance, an example
`max_tokens` value, or a rounded label such as `1M` is not a numeric limit.
Unknown capacity remains unknown; discovery omits the corresponding numeric
fields instead of presenting a default as provider metadata.

This behavior covers the production inference wrappers selected through
`common/wrapper_scope.py`: NVIDIA, Nous, and OpenRouter at the time of this
change. `wrappers.json` remains authoritative. Pending provider wrappers remain
frozen and are outside the implementation and verification scope.

## Sources and precedence

| Wrapper | Source | Selection rules |
| --- | --- | --- |
| NVIDIA hosted endpoint | The configured `/v1/models` response, NVIDIA NGC featured-model catalog, and exact NVIDIA Build model specification | Explicit endpoint fields take precedence. The publications fill missing fields only for `https://integrate.api.nvidia.com`. Model IDs must match exactly. |
| NVIDIA custom endpoint | The configured endpoint's `/v1/models` response | Public NVIDIA catalog enrichment is disabled. A self-hosted deployment may configure a different context window from the published model capacity. |
| Nous | The configured provider's `/v1/models` response | Use that provider's explicit limits, including for an alias's resolved model. Do not borrow another provider's values. |
| OpenRouter | The configured provider's `/models` response | Preserve the original catalog, including `top_provider`. Explicit top-level limits take precedence over nested `limits` and `top_provider` fields. |

Normalization recognizes explicit context, input, and output limit fields.
Only positive integers or exact positive integer strings are accepted. Boolean,
fractional, rounded, and malformed values are ignored. A standalone
`max_tokens` field is ambiguous and is not treated as a catalog limit.

The same model can legitimately have different advertised context limits on
different providers or routes. A catalog's model capacity also does not guarantee
that every selected backend or account accepts that full capacity. For example,
OpenRouter's top-level `context_length` and nested `top_provider.context_length`
remain separately visible when the provider reports different values.

NVIDIA Build enrichment reads an exact numeric `contextLength` from the selected
model's specification. It validates the page identity and model component,
rejects conflicting values, and ignores related models, prose, rounded display
labels, and request examples. Requests are bounded by a timeout, response size,
and concurrency limit. Missing or changed page structures leave the limit
unknown rather than producing a guess.

## Discovery and provenance

Known capacity is exposed through consistent `context_window`, `context_length`,
and `max_context_window` aliases. Known input and output limits are exposed
separately. Discovery also includes `context_metadata`:

| Field | Meaning |
| --- | --- |
| `context_window`, `max_input_tokens`, `max_output_tokens` | Exact normalized values when the source supplies them. |
| `source`, `scope` | The catalog URL or specification URL and the kind of evidence. |
| `fields` | Original source field names where available. |
| `checked_at`, `expires_at` | Fetch and expiry times as Unix timestamps. |
| `status` | `known` when a numeric context window is available; otherwise `unknown`. An output limit alone does not make the context window known. |
| `stale` | Whether the recorded expiry has passed or cannot be verified. |
| `field_provenance` | Per-field sources and timestamps when NVIDIA combines endpoint and published metadata. |

NVIDIA also retains `endpoint_context_metadata` separately so a published value
restored from disk cannot become an endpoint observation or block a newer
publication. Legacy catalog records without verifiable fetch times are marked
stale; loading a cache does not fabricate a fresh observation.

The local and central registries receive the enriched catalog. Catalog-owned
limits update when new metadata arrives. Existing explicit/manual profiles
remain separate from catalog-owned values. A stale or unknown discovery result
must not be interpreted as a successfully tested request size.

## Persistence and refresh

Catalog entries are persisted in each wrapper's SQLite model-state database and
held in memory. Nous and OpenRouter coalesce concurrent cold or forced refreshes
into one fetch. A cold discovery request can wait for the initial fetch; a stale
discovery result is served immediately while one refresh is scheduled. Failed
refreshes keep the last good catalog with its original age and throttle repeated
attempts. Inference requests do not fetch context metadata from the provider.

NVIDIA's existing model-list refresh stores the complete upstream entries and
updates the local and central registries. Its separate publication cache uses a
versioned JSON file, source identity checks, and atomic replacement. Previous
unverified static-seed caches are rejected. NGC refreshes retain missing models
at their original timestamps. Build enrichment checks models missing a fresh
NGC entry and persists both successful and unsuccessful attempt times, so
unavailable specifications are not fetched on every request or restart.

Settings documented in the provider `.env.example` files:

| Setting | Applies to | Example/default | Effect |
| --- | --- | --- | --- |
| `NVIDIA_BASE_URL` | NVIDIA | `https://integrate.api.nvidia.com` | Selects the endpoint; only this hosted base enables public NVIDIA enrichment. |
| `REGISTRY_REFRESH_SEC` | NVIDIA | `3600` | NGC refresh interval and metadata TTL; minimum 60 seconds. |
| `NGC_CONTEXT_CACHE_FILE` | NVIDIA | Optional | Publication cache path; defaults to `nvidia-python/ngc-featured-cache.json`. |
| `NVIDIA_BUILD_CONTEXT_ENABLED` | NVIDIA hosted endpoint | `true` | Enables background specification enrichment. |
| `NVIDIA_BUILD_CONTEXT_REFRESH_SEC` | NVIDIA hosted endpoint | `86400` | Per-model Build retry interval and metadata TTL; minimum 3600 seconds. |
| `NVIDIA_BUILD_CONTEXT_CONCURRENCY` | NVIDIA hosted endpoint | `3` | Parallel page fetches, clamped to 1–8. |
| `MODEL_CATALOG_TTL_SEC` | Nous, OpenRouter | `21600` | Catalog freshness period. |
| `MODEL_CATALOG_REFRESH_SEC` | Nous, OpenRouter | `86400` | Background cadence is `max(60, min(REFRESH, TTL))` seconds. |
| `MODEL_STATE_DB` | Nous, OpenRouter examples; shared store supports all active wrappers | Optional | Persistent catalog and scoped context-observation database path. |

The existing NVIDIA `MODEL_REFRESH_SEC` setting controls its endpoint model-list
refresh (default 600 seconds; a nonpositive value disables that periodic loop).
`MODEL_MAX_TOKENS_CAPS`, if explicitly configured, remains an operator output
policy, not provider-derived context evidence. `WRAPPER_AUTO_TRUNCATE` is retired;
it no longer authorizes deletion of message history or tool results.

## Recovery from an upstream rejection

The shared transport inspects unsuccessful HTTP 400, 413, and 422 responses for
definite token-limit evidence. It supports exact context/input counts, explicit
output ceilings, structured context errors, and recognized output-limit
validation errors. Generic payload-size errors, authentication failures, quota
errors, and server errors do not establish a context limit.

Recovery performs at most one output adjustment per incoming request:

1. Obtain exact remaining space from upstream context minus upstream input
   tokens, or an explicit upstream output ceiling.
2. Require exactly one positive integer output-limit field in the request:
   `max_tokens`, `max_completion_tokens`, or `max_output_tokens`.
3. Reduce that field only when the evidence requires a smaller value and the
   result remains above explicit minimum-output or reasoning allocations.
4. Release the rejected response, then retry the same model and input. If the
   normal key-rotation path subsequently handles a quota or authentication
   failure, retain the repaired output limit across those attempts.

The algorithm does not estimate input tokens, substitute a model, remove input,
summarize history, alter tools, replace continuation IDs, or replay a successful
stream. Matching native request fields and successful SSE data retain their
existing passthrough behavior. Cross-protocol translation follows the existing
adapter paths.

When the input alone exceeds the accepted context window, safe output reduction
cannot solve the request. Ambiguous limits, conflicting output fields,
insufficient reasoning budget, or a second rejection also stop recovery. The
wrapper returns the native upstream error body with actionable local headers;
the caller must compact the input or explicitly adjust its budgets. Arbitrary
oversized input cannot be delivered successfully without changing its contents.

| Response header | Meaning |
| --- | --- |
| `X-Context-Recovery` | The action, such as `reduced_output`, `compact_input`, `adjust_reasoning_budget`, or `retry_exhausted`. |
| `X-Context-Recovery-Hint` | Guidance explaining preservation of input and the required budget/compaction action. |
| `X-Context-Output-Tokens` | The output ceiling chosen by a completed adjustment. |

## Scoped rejection evidence

Exact numeric error observations are stored separately from the catalog, keyed
by provider, credential fingerprint, model, and endpoint. Their default TTL is
one hour. Endpoint user information and query strings are removed; raw
credentials, prompts, and upstream error text are not stored in this table.

An observation records what that account and route rejected. It does not lower
the advertised context for every account, overwrite a catalog entry, or
automatically truncate a later request. Recovery still requires evidence for the
current request. Expired observations are excluded when read and removed during
subsequent writes.

## Relevant implementation

- [`common/model/metadata.py`](../common/model/metadata.py): normalization and provenance.
- [`common/model_catalog.py`](../common/model_catalog.py): persistent cached discovery and coalesced refresh.
- [`common/context_recovery.py`](../common/context_recovery.py): error evidence, bounded planning, and transport response ownership.
- [`common/model_state.py`](../common/model_state.py): catalog persistence and scoped observations.
- [`common/nvidia_model_specs.py`](../common/nvidia_model_specs.py): selected NVIDIA Build specification parser.
- [`nvidia-python/src/registry.py`](../nvidia-python/src/registry.py): hosted publication cache and background enrichment.
