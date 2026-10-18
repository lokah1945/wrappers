# NVIDIA Build website and NGC NIM synchronization

The NVIDIA wrapper keeps four facts separate:

- the live NVIDIA Build website catalog and its access labels;
- the checked-in exact Build/API contract matrix used as a last-good fallback;
- public NGC NIM repository/tag/profile metadata;
- hosted/account/deployment runtime observations.

The official Build website is treated as a **lower bound**, not a static list and
not an upper bound on the wrapper catalog. The public model surface is the union
of the current website catalog and additive backend/provider models that are
actually discovered or verified by the wrapper.

## Live website synchronization

The wrapper fetches:

```text
https://build.nvidia.com/models
https://build.nvidia.com/models?page=2
https://build.nvidia.com/models?page=3
...
```

It reads the website's server-rendered search result, including `resultTotal`,
pagination, canonical publisher/model IDs, `nimType` access labels, publishers,
use cases, inference providers, GPU labels, descriptions, timestamps, and
website attributes. It does not assume that the current count is 99 forever.

A successful snapshot is written atomically to:

```text
nvidia-python/build-web-catalog-cache.json
```

The cache is ignored by git. If the website is unavailable, returns an error,
returns an incomplete page set, or loses its access labels, the wrapper retains
the last-good snapshot and then falls back to the checked-in Build contract
surface. A failed refresh can never shrink the public catalog.

Configuration:

```text
NVIDIA_BUILD_WEB_CATALOG_ENABLED=true
NVIDIA_BUILD_WEB_CATALOG_URL=https://build.nvidia.com/models
NVIDIA_BUILD_WEB_CATALOG_REFRESH_SEC=900
NVIDIA_BUILD_WEB_CATALOG_TIMEOUT_SEC=45
NVIDIA_BUILD_WEB_CATALOG_MAX_PAGES=50
NVIDIA_BUILD_WEB_CATALOG_CACHE_FILE=/root/wrapper/nvidia-python/build-web-catalog-cache.json
NVIDIA_MODELS_DEFAULT_VISIBILITY=catalog
```

The live website's `resultTotal` is the source of the website model count. The
NGC NIM metadata API does not replace this source: NGC supplies deployment
metadata for known NIM repositories/tags, but it does not define the Build UI's
complete model list or its Free/Partner/Download access facets.

## Access contract

The website labels are mapped exactly as follows:

```text
nim_type_preview           -> Free Endpoint
nim_type_upgrade_available -> Partner Endpoint
nim_type_run_anywhere     -> Download Available
```

The collection filters are available on both `/v1/models` and
`/v1/nim/metadata`:

```text
GET /v1/models?access=all
GET /v1/models?access=free_endpoint
GET /v1/models?access=partner_endpoint
GET /v1/models?access=download_available
GET /v1/nim/metadata?access=all
```

`downloadable` remains a backwards-compatible query/field alias for
`download_available`. `hosted_endpoint` remains a separate legacy/provider
runtime dimension and is deliberately **not** used as the website's Partner
Endpoint count.

A model may carry multiple website modes. The wrapper preserves all of them in
`website_access_modes`, `access_modes`, `access_badges`, and the boolean fields:

```text
website_listed
free_endpoint
partner_endpoint
download_available
downloadable
catalog_usable
```

The public invariants are lower bounds: the wrapper must never expose fewer
website rows or fewer website access-mode rows than the current official
snapshot. Backend-only verified rows may increase the totals.

## Default model surface

Without a query filter:

```text
GET /v1/models
```

returns the catalog union: all current website rows plus additive backend rows.
The explicit runtime-only view is:

```text
GET /v1/models?availability=available
```

The response keeps the counts separate:

```text
catalog_count
catalog_usable_count
website_catalog_count
website_access_counts
runtime_available_count
available_count
```

`available_count` is retained as a backward-compatible runtime-observation
count. `catalog_usable_count` and `website_catalog_count` are the counts used to
verify that the official website lower bound has not been lost. Website
publication makes a row catalog-usable through its documented access path, but
it does not fabricate a hosted runtime health result. A Download Available row
that needs a local deployment remains visible with a state such as:

```text
catalog_usable=true
availability_state=deployment_required
available=false
```

This means the model is usable through NVIDIA's deployment contract, not that a
hosted process is already running on this wrapper. A failed generic probe is
not evidence of retirement; the model-specific Build/API contract must be used
first. Native, gRPC, WebSocket, Riva, deployment-required, wrong-route,
timeout, capability-mismatch, contract-unverified, and globally-retired states
remain distinct.

## Backend-only models

A model not present on the website but present in a verified provider/backend
catalog is additive. It is retained with provenance such as:

```json
{
  "website_listed": false,
  "source": "backend_provider",
  "available": true
}
```

The backend row must have a provider-derived or contract-aware availability
observation before it is marked runtime available. Provider omission never
removes a current website row.

## Wrapper routes

```text
GET /v1/nim/metadata
GET /v1/nim/metadata?model=nvidia/nemotron-ocr-v2
GET /v1/nim/metadata?model=nvidia/nemotron-ocr-v2&profiles=full
GET /v1/nim/metadata/nvidia/nemotron-ocr-v2?profiles=full
```

The metadata collection contains the complete current Build/website union. It
includes website access metadata and, where applicable, exact NGC
`resource_id`, container tag, repository URL, metadata URL, profile count,
hardware requirements, release details, features, and related URLs.

## NGC synchronization

The NGC layer uses the public read-only APIs:

```text
GET https://api.ngc.nvidia.com/v2/repos/{resource}
GET https://api.ngc.nvidia.com/v2/nim/metadata?resourceId={resource}&tag={tag}
```

NGC metadata proves deployment information, not a running endpoint. A missing
NGC metadata document never removes a website or backend row. Metadata `404`
becomes `metadata_not_published`, not `unavailable`.

Useful NGC statuses include:

- `available`: repository and exact tag metadata returned successfully;
- `metadata_not_published`: repository exists but exact-tag metadata is absent;
- `repository_not_found`: no conservative repository candidate resolved;
- `repository_lookup_error` / `metadata_error`: transient or server error;
- `not_applicable`: row is not a downloadable NIM;
- `pending`: synchronization has not produced a row yet.

## Profile fit

Optional target hardware variables only label profile fit; they do not invent
compatibility:

```text
NVIDIA_NIM_TARGET_GPU_DEVICE
NVIDIA_NIM_TARGET_GPU_COUNT
NVIDIA_NIM_TARGET_VRAM_GB
NVIDIA_NIM_TARGET_DRIVER_VERSION
```

Missing requirements remain unknown. Profile IDs remain tied to the exact
repository/tag.
