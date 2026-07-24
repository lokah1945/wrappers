#!/usr/bin/env python3
"""Central Model Intelligence service.

This service is a knowledge/control plane, not a model router. It resolves the
exact requested model, returns one call plan, and stores sanitized observations.
It never selects a fallback model or provider.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from common.model import AliasBinding, LocalModelRegistry  # noqa: E402
from common.model_state import ModelStateStore  # noqa: E402
from common.model.validation import validate_catalog_entries, validate_observation  # noqa: E402

MODEL_REGISTRY_DB = os.environ.get(
    "MODEL_REGISTRY_DB", str(Path(__file__).resolve().parent / "registry-state.db")
)
ADMIN_TOKEN = os.environ.get("MODEL_REGISTRY_ADMIN_TOKEN", "").strip()


class CentralRegistry:
    def __init__(self) -> None:
        self.registries: dict[str, LocalModelRegistry] = {}
        self.states: dict[str, ModelStateStore] = {}

    def registry(self, provider: str) -> LocalModelRegistry:
        provider = str(provider).strip().lower()
        if not provider:
            raise ValueError("provider is required")
        if provider not in self.registries:
            registry = LocalModelRegistry(provider, ROOT / "model-registry", MODEL_REGISTRY_DB)
            state = ModelStateStore(provider, MODEL_REGISTRY_DB)
            cached = state.get_catalog(fresh_only=False)
            if cached:
                registry.register_catalog(cached, revision="cached-catalog")
            self.registries[provider] = registry
            self.states[provider] = state
        return self.registries[provider]

    def state(self, provider: str) -> ModelStateStore:
        self.registry(provider)
        return self.states[provider]

    def list_models(self, provider: str) -> list[dict[str, Any]]:
        registry = self.registry(provider)
        return [profile.to_dict() for profile in registry.profiles.values()]

    def register_catalog(self, provider: str, models: list[Any], revision: str) -> list[str]:
        registry = self.registry(provider)
        ids = registry.register_catalog(models, revision=revision or "catalog")
        self.state(provider).upsert_catalog(models, source=f"central:{provider}")
        return ids


central = CentralRegistry()
app = FastAPI(title="model-registry", version="0.1.0", docs_url=None, redoc_url=None)


def _require_internal(request: Request) -> None:
    """Fail closed: internal writes are disabled without an admin token."""
    if not ADMIN_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Internal registry writes are disabled; configure MODEL_REGISTRY_ADMIN_TOKEN",
        )
    auth = (request.headers.get("authorization") or "").strip()
    token = auth[7:] if auth.lower().startswith("bearer ") else auth
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _provider(body: dict[str, Any]) -> str:
    provider = str(body.get("provider") or "").strip().lower()
    if not provider:
        raise HTTPException(status_code=400, detail="provider is required")
    return provider


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "model-registry",
        "providers_loaded": sorted(central.registries),
        "model_substitution": False,
        "provider_substitution": False,
        "key_rotation": True,
    }


@app.get("/v1/models")
def models(provider: str) -> dict[str, Any]:
    provider = provider.strip().lower()
    profiles = central.list_models(provider)
    return {
        "object": "list",
        "provider": provider,
        "data": profiles,
        "model_substitution": False,
        "catalog_age_sec": central.state(provider).catalog_age_sec(),
    }


@app.get("/v1/models/{canonical_id:path}")
def model_info(canonical_id: str) -> dict[str, Any]:
    if "/" not in canonical_id:
        raise HTTPException(status_code=400, detail="canonical model id must include provider")
    provider = canonical_id.split("/", 1)[0].lower()
    profile = central.registry(provider).profiles.get(canonical_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Model profile not found")
    return profile.to_dict()


@app.post("/v1/resolve")
async def resolve(request: Request) -> dict[str, Any]:
    body = await request.json()
    provider = _provider(body)
    requested = str(body.get("requested_model") or body.get("model") or "").strip()
    if not requested:
        raise HTTPException(status_code=400, detail="requested_model is required")
    scope_chain = body.get("scope_chain") or []
    ref = central.registry(provider).resolve(requested, scope_chain)
    return {
        "requested_model": requested,
        "resolved": ref.to_dict(),
        "model_changed": False if not ref.is_alias else True,
        "model_substitution": False,
    }


@app.post("/v1/call-plan")
async def call_plan(request: Request) -> dict[str, Any]:
    body = await request.json()
    provider = _provider(body)
    requested = str(body.get("requested_model") or body.get("model") or "").strip()
    surface = str(body.get("client_surface") or "").strip()
    if not requested or not surface:
        raise HTTPException(status_code=400, detail="requested_model and client_surface are required")
    try:
        plan = central.registry(provider).call_plan(requested, surface, body.get("scope_chain") or [])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "plan": plan.to_dict(),
        "model_substitution": False,
        "provider_substitution": False,
    }


@app.post("/internal/catalog")
async def ingest_catalog(request: Request) -> dict[str, Any]:
    _require_internal(request)
    body = await request.json()
    provider = _provider(body)
    models_data = body.get("models") or body.get("data") or []
    if not isinstance(models_data, list):
        raise HTTPException(status_code=400, detail="models must be a list")
    try:
        models_data = validate_catalog_entries(models_data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ids = central.register_catalog(provider, models_data, str(body.get("revision") or "catalog"))
    return {"provider": provider, "registered": len(ids), "ids": ids}


@app.post("/internal/aliases")
async def ingest_aliases(request: Request) -> dict[str, Any]:
    _require_internal(request)
    body = await request.json()
    provider = _provider(body)
    bindings = body.get("bindings") or []
    if not isinstance(bindings, list) or len(bindings) > 1000:
        raise HTTPException(status_code=400, detail="bindings must be a list of at most 1000 items")
    registered = []
    try:
        for item in bindings:
            if not isinstance(item, dict):
                raise ValueError("alias binding must be an object")
            binding = AliasBinding(
                scope_type=str(item.get("scope_type") or "wrapper"),
                scope_id=str(item.get("scope_id") or provider),
                alias=str(item.get("alias") or ""),
                canonical_target=str(item.get("canonical_target") or ""),
                revision=str(item.get("revision") or ""),
                source=str(item.get("source") or "central"),
            )
            central.registry(provider).bind_alias(binding)
            registered.append(binding.to_dict())
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"provider": provider, "registered": len(registered), "bindings": registered}


@app.post("/internal/observations")
async def observation(request: Request) -> dict[str, Any]:
    _require_internal(request)
    body = await request.json()
    provider = _provider(body)
    model_id = str(body.get("canonical_model_id") or body.get("model") or "").strip()
    account_scope = str(body.get("account_scope_hash") or "unknown")
    if not model_id:
        raise HTTPException(status_code=400, detail="canonical_model_id is required")
    state = str(body.get("state") or "unknown")
    try:
        validated = validate_observation(
            model_id,
            account_scope,
            state,
            body.get("reason_code"),
            body.get("reason_detail"),
            body.get("endpoint"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = central.state(provider).record_status(
        model_id=validated["model_id"],
        account_scope=validated["account_scope"],
        state=validated["state"],
        status_code=int(body.get("http_status") or 0),
        reason_code=validated["reason_code"],
        reason_detail=validated["reason_detail"],
        endpoint=validated["endpoint"],
    )
    return {"provider": provider, "model": model_id, "observation": result}


@app.get("/internal/status")
def status(provider: str, request: Request) -> dict[str, Any]:
    _require_internal(request)
    provider = provider.strip().lower()
    return {"provider": provider, "states": central.state(provider).status_map()}


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"type": "registry_error", "message": str(exc.detail)}},
    )


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("MODEL_REGISTRY_HOST", "127.0.0.1"),
        port=int(os.environ.get("MODEL_REGISTRY_PORT", "9200")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
