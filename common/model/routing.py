"""Exact wrapper route metadata for model discovery.

Provider catalogs describe models, but many catalogs do not repeat the local
wrapper's public API route. This module adds a small, explicit routing contract
without guessing from a model name:

* the client-facing path is the path accepted by this wrapper;
* the resolved upstream URL is built from the configured base URL;
* the model remains the request's ``model`` field (no substitution);
* the source/confidence fields make clear that this is wrapper routing
  metadata, not a claim that the provider globally deploys the model.

The URL join handles providers whose configured base already ends in ``/v1``
(e.g. OpenRouter) and providers whose base is host-only (e.g. Nous).
"""
from __future__ import annotations

import copy
from typing import Any
from urllib.parse import urlsplit


def _clean_base(base_url: str) -> str:
    return str(base_url or "").strip().rstrip("/")


def join_upstream_url(base_url: str, client_path: str) -> str:
    """Join a configured provider base to a wrapper API path exactly once.

    ``https://openrouter.ai/api/v1`` + ``/v1/chat/completions`` becomes
    ``https://openrouter.ai/api/v1/chat/completions`` rather than the common
    erroneous ``.../api/v1/v1/...``. Host-only bases keep the full ``/v1``
    prefix.
    """
    base = _clean_base(base_url)
    path = "/" + str(client_path or "").lstrip("/")
    if not base:
        return path
    base_path = urlsplit(base).path.rstrip("/")
    if base_path.endswith("/v1") and path.startswith("/v1/"):
        path = path[3:]
    return base + path


def endpoint_descriptor(provider: str, base_url: str, *,
                        client_path: str = "/v1/chat/completions",
                        kind: str = "chat", streaming: bool = True,
                        client_surface: str = "openai_chat",
                        model_field: str = "model") -> dict[str, Any]:
    """Return one exact, JSON-safe endpoint descriptor."""
    base = _clean_base(base_url)
    url = join_upstream_url(base, client_path)
    upstream_path = urlsplit(url).path or "/"
    return {
        "path": client_path,
        "client_path": client_path,
        "upstream_path": upstream_path,
        "url": url,
        "base_url": base,
        "host": provider,
        "kind": kind,
        "protocol": "openai",
        "client_surface": client_surface,
        "model_field": model_field,
        "streaming": bool(streaming),
        "route_source": "wrapper_contract",
        "route_confidence": "exact",
    }


def enrich_openai_catalog_entry(entry: dict[str, Any], provider: str,
                                base_url: str, *,
                                client_path: str = "/v1/chat/completions",
                                kind: str = "chat", streaming: bool = True,
                                client_surface: str = "openai_chat",
                                model_field: str = "model") -> dict[str, Any]:
    """Attach exact wrapper route metadata while preserving provider facts.

    Existing provider-native ``endpoints`` metadata is retained under
    ``provider_endpoints``. When no native endpoint metadata exists, the exact
    wrapper contract is exposed in the conventional ``endpoints`` field too,
    so clients can immediately match model → route without custom knowledge.
    """
    result = copy.deepcopy(entry if isinstance(entry, dict) else {})
    descriptor = endpoint_descriptor(
        provider, base_url, client_path=client_path, kind=kind,
        streaming=streaming, client_surface=client_surface,
        model_field=model_field,
    )
    existing = result.get("endpoints")
    if isinstance(existing, list) and existing:
        result.setdefault("provider_endpoints", copy.deepcopy(existing))
        result["wrapper_endpoints"] = [descriptor]
    else:
        result["endpoints"] = [descriptor]
        result["wrapper_endpoints"] = [descriptor]
    result["routing_metadata"] = {
        "source": "wrapper_contract",
        "provider": provider,
        "client_surface": client_surface,
        "primary_endpoint": descriptor["url"],
        "client_path": client_path,
        "upstream_path": descriptor["upstream_path"],
        "model_field": model_field,
        "model_identity_preserved": True,
        "route_confidence": "exact",
    }
    return result
