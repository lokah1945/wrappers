"""Local model knowledge registry used by wrapper data planes.

This registry is intentionally exact-model only. It can describe an unknown
explicit model and produce a provider call plan without rejecting or replacing
it merely because a catalog/profile is incomplete.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from .call_plan import build_call_plan
from .contracts import (
    AliasBinding,
    CapabilityProfile,
    LimitProfile,
    ModelProfile,
    ModelRef,
    ProtocolProfile,
)
from .identity import AliasResolver
from .profile_store import ModelProfileStore


class LocalModelRegistry:
    def __init__(self, provider: str, manifest_root: str | Path | None = None,
                 profile_db_path: str | Path | None = None):
        self.provider = provider
        self.manifest_root = Path(manifest_root or Path(__file__).resolve().parents[2] / "model-registry")
        self.profiles: dict[str, ModelProfile] = {}
        self.profile_store = ModelProfileStore(profile_db_path) if profile_db_path else None
        self.aliases = AliasResolver()
        self.provider_manifest = self._load_provider_manifest()
        self.error_manifest = self._load_error_manifest()
        if self.profile_store:
            for profile in self.profile_store.load(self.provider):
                self.profiles[profile.canonical_id] = profile

    def _load_provider_manifest(self) -> dict[str, Any]:
        path = self.manifest_root / "manifests" / "providers" / f"{self.provider}.json"
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _load_error_manifest(self) -> dict[str, Any]:
        path = self.manifest_root / "manifests" / "errors" / f"{self.provider}.json"
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def bind_alias(self, binding: AliasBinding) -> None:
        self.aliases.bind(binding)

    def register_profile(self, profile: ModelProfile) -> None:
        if profile.provider != self.provider:
            raise ValueError(f"profile provider {profile.provider!r} != registry {self.provider!r}")
        if profile.policy.get("model_substitution", False) or profile.policy.get("provider_substitution", False):
            raise ValueError("transparent registry cannot register substitution-enabled profile")
        self.profiles[profile.canonical_id] = profile
        if self.profile_store:
            self.profile_store.save(profile)

    def _default_protocols(self) -> tuple[ProtocolProfile, ...]:
        adapters = self.provider_manifest.get("adapters", {})
        # Endpoint is completed by the provider adapter; path is the shared
        # protocol path and never changes model identity.
        result = []
        for surface, adapter in adapters.items():
            if surface == "openai_chat":
                upstream = "openai_chat"
                path = "/v1/chat/completions"
            elif surface == "openai_responses":
                upstream = "openai_responses"
                path = "/v1/responses"
            elif surface == "anthropic_messages":
                upstream = "openai_chat" if "chat" in adapter else "anthropic_messages"
                path = "/v1/chat/completions" if upstream == "openai_chat" else "/v1/messages"
            else:
                continue
            result.append(ProtocolProfile(surface, upstream, path, adapter, "1"))
        return tuple(result)

    def register_catalog(self, models: Iterable[Any], revision: str = "catalog") -> list[str]:
        """Register catalog identities with unknown capabilities by default."""
        ids = []
        for entry in models or []:
            if isinstance(entry, str):
                provider_model_id = entry.strip()
                metadata = {}
            elif isinstance(entry, dict):
                provider_model_id = str(entry.get("id") or entry.get("model") or "").strip()
                metadata = entry
            else:
                continue
            if not provider_model_id:
                continue
            prefix = f"{self.provider}/"
            canonical = provider_model_id if provider_model_id.startswith(prefix) else prefix + provider_model_id
            raw_id = provider_model_id[len(prefix):] if provider_model_id.startswith(prefix) else provider_model_id
            existing = self.profiles.get(canonical)
            if existing is not None:
                # Catalog refresh is observational. It may update provenance
                # and revision, but must never erase an authoritative/manual
                # capability, limit, protocol, or request-rule profile.
                provenance = dict(existing.provenance)
                provenance["catalog"] = {
                    "source": "provider_catalog",
                    "metadata": metadata,
                }
                profile = replace(
                    existing,
                    catalog_revision=revision,
                    provenance=provenance,
                )
            else:
                profile = ModelProfile(
                    canonical_id=canonical,
                    provider=self.provider,
                    provider_model_id=raw_id,
                    profile_revision=revision,
                    catalog_revision=revision,
                    capabilities=CapabilityProfile(
                        input_modalities=("text",),
                        output_modalities=("text",),
                    ),
                    limits=LimitProfile(),
                    protocols=self._default_protocols(),
                    provenance={"source": "provider_catalog", "metadata": metadata},
                )
            self.register_profile(profile)
            ids.append(canonical)
        return sorted(set(ids))

    def resolve(self, requested: str, scope_chain: list[tuple[str, str]] | None = None) -> ModelRef:
        return self.aliases.resolve(requested, self.provider, scope_chain, revision="local")

    def call_plan(self, requested: str, client_surface: str,
                  scope_chain: list[tuple[str, str]] | None = None):
        model_ref = self.resolve(requested, scope_chain)
        profile = self.profiles.get(model_ref.canonical_id)
        if profile is None:
            # Explicit unknown models remain pass-through. The provider adapter
            # may still know the default protocol while runtime observes facts.
            profile = ModelProfile(
                canonical_id=model_ref.canonical_id,
                provider=self.provider,
                provider_model_id=model_ref.provider_model_id,
                protocols=self._default_protocols(),
                profile_revision="unknown",
                policy={
                    "transparent": True,
                    "model_substitution": False,
                    "provider_substitution": False,
                    "key_rotation": True,
                },
            )
        return build_call_plan(profile, model_ref, client_surface)
