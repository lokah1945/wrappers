# Model Intelligence registry

This directory is the source-controlled model knowledge plane for the wrapper
monorepo. It is intentionally **not** a fallback router.

The registry describes the exact model requested by the client:

- canonical identity and provider model ID;
- aliases with explicit scope;
- capabilities and limits;
- protocol/call profiles and adapter versions;
- lifecycle and catalog provenance;
- error classification rules.

It must never select a different model or provider. The wrapper's native key
pool is the only retry dimension. `service.py` implements the central registry
on port 9200, storing catalogs, scoped aliases, model profiles and sanitized
account observations in SQLite. Wrappers sync catalogs and observations with
the service asynchronously; inference uses local profiles and caches so a
registry outage does not block requests.

Current layout:

```text
manifests/providers/   provider adapter metadata
manifests/errors/      provider error rules
service.py            HTTP knowledge/control plane
systemd/              internal-service unit
registry-state.db     runtime SQLite profiles, aliases, catalogs and observations
```

## Status

- **Port:** 9200 (`service:app`) — plain service, not a wrapper.
- **Entry point:** `python3 model-registry/service.py`
- **Read API:** `/v1/models`, `/v1/models/{canonical_id}`, `/v1/resolve`,
  `/v1/call-plan`.
- **Internal API:** `/internal/catalog`, `/internal/aliases`,
  `/internal/observations`, `/internal/status`; requires
  `MODEL_REGISTRY_ADMIN_TOKEN` and fails closed when it is unset.
- **Deployment:** bind to loopback for the local fleet, or restrict access to
  port 9200 to the internal network when configuring a remote registry.
- **Validation:** registry service, security and boundary regressions run as
  part of the offline production gates in `tests/run_production_gates.sh`.
- **COMPATIBILITY_LAYER:** not applicable (internal service).

## Context metadata

Catalog ingestion extracts explicit context/input/output limits into `LimitProfile`,
retaining source, scope and fetch time in `provenance.catalog_limits`. Subsequent
catalog refreshes update catalog-owned fields while preserving explicit profiles.
Unknown fields remain unset; a model name or another provider's limit is never
used as evidence. Wrapper discovery includes `context_metadata` and context
aliases for clients, backed by persistent local caches. See
[context metadata and recovery](../docs/CONTEXT_METADATA.md) for precedence,
refresh settings, runtime observations and recovery limits.
