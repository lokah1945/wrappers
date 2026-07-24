"""Exact model identity and deterministic alias resolution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote

from .contracts import AliasBinding, ModelRef

_CONTEXT_SUFFIX = re.compile(r"\[[0-9]+[mk]?\]$", re.IGNORECASE)


def normalize_model_syntax(requested: str) -> str:
    """Normalize transport syntax only; never substitute a model.

    Provider-specific model IDs remain intact. The only transformations here
    are whitespace, URL decoding, and a documented context annotation suffix.
    """
    if requested is None:
        return ""
    value = unquote(str(requested)).strip()
    return _CONTEXT_SUFFIX.sub("", value).strip()


class AliasResolutionError(ValueError):
    pass


@dataclass
class AliasResolver:
    """Scoped alias map. There is intentionally no "last model wins" behavior."""

    bindings: dict[tuple[str, str, str], AliasBinding]

    def __init__(self) -> None:
        self.bindings = {}

    def bind(self, binding: AliasBinding) -> None:
        alias = normalize_model_syntax(binding.alias).lower()
        if not alias or not binding.canonical_target:
            raise AliasResolutionError("alias and canonical target are required")
        self.bindings[(binding.scope_type, binding.scope_id, alias)] = binding

    def _lookup_scopes(self, scope_chain: list[tuple[str, str]]) -> list[tuple[str, str]]:
        # Caller supplies the most specific scope first. Global is last.
        seen = set()
        ordered = []
        for item in scope_chain + [("global", "*")]:
            if item not in seen:
                ordered.append(item)
                seen.add(item)
        return ordered

    def resolve(self, requested: str, provider: str,
                scope_chain: list[tuple[str, str]] | None = None,
                revision: str = "") -> ModelRef:
        normalized = normalize_model_syntax(requested)
        if not normalized:
            raise AliasResolutionError("model name is empty")
        scopes = self._lookup_scopes(scope_chain or [])
        alias = normalized.lower()
        binding = next((self.bindings.get((kind, scope, alias)) for kind, scope in scopes), None)
        if binding:
            canonical = binding.canonical_target
            if "/" not in canonical:
                raise AliasResolutionError(f"alias target is not canonical: {canonical}")
            target_provider, provider_model_id = canonical.split("/", 1)
            if target_provider != provider:
                raise AliasResolutionError(
                    f"alias target provider {target_provider!r} does not match wrapper provider {provider!r}"
                )
            return ModelRef(
                requested_name=normalized,
                canonical_id=canonical,
                provider=target_provider,
                provider_model_id=provider_model_id,
                is_alias=True,
                resolution_revision=binding.revision or revision,
            )

        # A concrete name is never rejected merely because it is absent from
        # catalog. The provider adapter may pass it through and observe the
        # upstream result. Canonicalization adds only the wrapper namespace.
        canonical = normalized if normalized.startswith(f"{provider}/") else f"{provider}/{normalized}"
        provider_model_id = normalized.split("/", 1)[1] if normalized.startswith(f"{provider}/") else normalized
        return ModelRef(
            requested_name=normalized,
            canonical_id=canonical,
            provider=provider,
            provider_model_id=provider_model_id,
            is_alias=False,
            resolution_revision=revision,
        )
