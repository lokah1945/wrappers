# NVIDIA NIM / NGC metadata synchronization

The NVIDIA wrapper keeps three facts separate:

- the 99-row NVIDIA Build contract surface;
- public NGC NIM repository/tag/profile metadata;
- hosted/account/deployment availability observations.

A missing NGC metadata document never removes a Build row and never means the
NIM is unavailable. Hosted and deployed availability remains the result of the
contract-aware verifier and the account/deployment state store.

## Wrapper route

```text
GET /v1/nim/metadata
GET /v1/nim/metadata?model=nvidia/nemotron-ocr-v2
GET /v1/nim/metadata?model=nvidia/nemotron-ocr-v2&profiles=full
GET /v1/nim/metadata/nvidia/nemotron-ocr-v2?profiles=full
```

The collection route always reports the complete Build catalog. The default
view contains profile summaries; `profiles=full` includes normalized profile
records. The response includes `sync.statuses`, exact `resource_id`, selected
container tag, `container_image`, repository URL, metadata URL, profile count,
hardware requirements, release details, features, and related URLs.

The standard `/v1/models` response includes a compact `nim_metadata` object for
Build rows. Its `nim_metadata_status` is informational and is not used as the
hosted/deployment availability verdict.

Build-style access filters are available on both `/v1/models` and the metadata
collection route:

```text
GET /v1/models?access=all
GET /v1/models?access=free_endpoint
GET /v1/models?access=downloadable
GET /v1/models?access=hosted_endpoint
GET /v1/nim/metadata?access=all
```

`free_endpoint` and `downloadable` follow the Build catalog badges. A row can
have both. An explicit access filter shows the complete matching catalog so a
Downloadable row is not removed just because it is not currently deployed on
this wrapper. Each row still carries `availability_state`, `available`,
`usability_state`, and `catalog_usable`; those fields must not be conflated.
The no-filter `/v1/models` view remains runtime-safe and only returns observed
available rows.

## NGC synchronization

The background task uses the public read-only APIs:

```text
GET https://api.ngc.nvidia.com/v2/repos/{resource}
GET https://api.ngc.nvidia.com/v2/nim/metadata?resourceId={resource}&tag={tag}
```

It discovers the repository from conservative Build-ID candidates, confirms the
repository response before joining metadata, chooses an exact repository tag,
and caches the result atomically. Metadata `404` becomes
`metadata_not_published`, not `unavailable`.

Useful statuses are:

- `available`: repository and exact tag metadata returned successfully;
- `metadata_not_published`: repository exists but the exact tag has no public metadata;
- `repository_not_found`: no conservative candidate resolved to a repository;
- `repository_lookup_error` / `metadata_error`: transient or server error;
- `not_applicable`: Build row is not downloadable;
- `pending`: boot synchronization has not produced a row yet.

## Profile fit

Optional target hardware variables only label profile fit; they do not invent
compatibility:

```text
NVIDIA_NIM_TARGET_GPU_DEVICE
NVIDIA_NIM_TARGET_GPU_COUNT
NVIDIA_NIM_TARGET_VRAM_GB
NVIDIA_NIM_TARGET_DRIVER_VERSION
```

The normalized `required_gpus` value is
`tensor_parallel_size * pipeline_parallel_size`. Missing requirements remain
unknown. Profile IDs are opaque and remain tied to the exact repository/tag.

## Availability contract

NGC metadata proves deployment information, not a running endpoint. A model is
only advertised as available in the default `/v1/models` list after the
contract-aware hosted/deployment observation says it is usable. Downloadable,
native-protocol, deployment-required, wrong-route, timeout, and
contract-unverified states remain distinct in the diagnostic catalog.
