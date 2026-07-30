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
pool is the only retry dimension. A central registry service may be added later;
local wrapper caches remain authoritative for hot-path resilience.

Planned layout:

```text
manifests/providers/   provider adapter metadata
manifests/models/      model profiles
manifests/errors/      provider error rules
manifests/aliases/     scoped alias bindings
schemas/               versioned JSON schemas
```
