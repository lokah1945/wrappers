"""NVIDIA Build/NIM contract catalogue.

The checked-in contract matrix remains the exact-route fallback.  At runtime a
live snapshot from ``build.nvidia.com/models`` overlays the website access
labels and metadata, and can add newly published website rows.  Runtime health
is never stored in this module; it comes from account/deployment observations.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

_MATRIX_PATH = Path(__file__).with_name("nvidia_build_contract_matrix.json")
try:
    _MATRIX = json.loads(_MATRIX_PATH.read_text(encoding="utf-8"))
except (OSError, ValueError):  # keep imports safe in minimal/unit environments
    _MATRIX = {"count": 0, "models": []}

_BASE_BUILD_CONTRACTS: dict[str, dict[str, Any]] = {
    row["id"]: row for row in (_MATRIX.get("models") or [])
    if isinstance(row, dict) and isinstance(row.get("id"), str)
}

# These two objects are intentionally mutable.  Other modules import them by
# reference, so a successful live website refresh can update the running
# process without requiring a process restart.
BUILD_CONTRACTS: dict[str, dict[str, Any]] = copy.deepcopy(_BASE_BUILD_CONTRACTS)
BUILD_MODEL_IDS: list[str] = list(_BASE_BUILD_CONTRACTS)
BUILD_CATALOG_SOURCE = _MATRIX.get("source", "https://build.nvidia.com/models")
BUILD_CATALOG_RETRIEVED = _MATRIX.get("retrieved")
BUILD_WEBSITE_CATALOG_SOURCE = "https://build.nvidia.com/models"
BUILD_WEBSITE_CATALOG_RETRIEVED: float | None = None
_BUILD_WEBSITE_ROWS: dict[str, dict[str, Any]] = {}


def _website_contract(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return a safe placeholder for a new website row without guessing route."""
    return {
        "kind": "website_catalog",
        "verification": "contract_unverified",
        "source": row.get("build_url") or BUILD_WEBSITE_CATALOG_SOURCE,
    }


def _copy_base_row(model_id: str) -> dict[str, Any]:
    base = _BASE_BUILD_CONTRACTS.get(model_id)
    return copy.deepcopy(base) if base else {
        "id": model_id,
        "build_url": None,
        "title": model_id.rsplit("/", 1)[-1],
        "badges": [],
        "downloadable": False,
        "free_endpoint": False,
        "partner_endpoint": False,
        "download_available": False,
        "deprecated": False,
        "hosted_function": False,
        "api_reference": None,
        "docs": [],
        "contract": {},
    }


def apply_build_website_catalog(rows: Iterable[Mapping[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """Overlay the last-good Build website snapshot onto the contract matrix.

    Website rows are ordered first, preserving the website's current sort
    order.  Checked-in contract rows not present in a live snapshot remain as
    a conservative fallback; they are not silently deleted during a website
    outage or a publication transition.  The website flags always override
    stale matrix flags for rows that are currently listed.
    """
    global BUILD_WEBSITE_CATALOG_RETRIEVED
    normalized: dict[str, dict[str, Any]] = {}
    for raw in rows or []:
        if not isinstance(raw, Mapping):
            continue
        model_id = str(raw.get("id") or "").strip()
        if not model_id:
            continue
        normalized[model_id] = dict(raw)

    merged: dict[str, dict[str, Any]] = {}
    website_order: list[str] = []
    retrieved_values = []
    for model_id, website in normalized.items():
        row = _copy_base_row(model_id)
        contract = row.get("contract")
        if not isinstance(contract, dict) or not contract:
            row["contract"] = _website_contract(website)
        row["build_url"] = website.get("build_url") or row.get("build_url")
        row["title"] = website.get("title") or website.get("display_name") or row.get("title")
        row["website_listed"] = True
        row["website"] = copy.deepcopy(website)
        row["website_source"] = website.get("website_source") or BUILD_WEBSITE_CATALOG_SOURCE
        row["website_access_modes"] = list(website.get("website_access_modes") or [])
        row["website_badges"] = list(website.get("website_badges") or [])
        row["access_badges"] = list(website.get("access_badges") or row.get("badges") or [])
        # Exact website classifications.  ``downloadable`` is retained as the
        # old internal spelling and is intentionally an alias of Download
        # Available, not a separate availability claim.
        row["free_endpoint"] = bool(website.get("free_endpoint"))
        row["partner_endpoint"] = bool(website.get("partner_endpoint"))
        row["download_available"] = bool(website.get("download_available"))
        row["downloadable"] = bool(website.get("download_available"))
        row["badges"] = list(row["access_badges"])
        merged[model_id] = row
        website_order.append(model_id)
        try:
            retrieved = float(website.get("retrieved_at"))
            if retrieved > 0:
                retrieved_values.append(retrieved)
        except (TypeError, ValueError):
            pass

    # Keep old exact contracts as a last-good/verified backend surface.  This
    # is additive and prevents a transient website failure from shrinking the
    # public model surface.
    for model_id in _BASE_BUILD_CONTRACTS:
        if model_id in merged:
            continue
        row = _copy_base_row(model_id)
        row.setdefault("website_listed", False)
        row.setdefault("website_source", BUILD_WEBSITE_CATALOG_SOURCE)
        row.setdefault("website_access_modes", [])
        merged[model_id] = row

    BUILD_CONTRACTS.clear()
    BUILD_CONTRACTS.update(merged)
    BUILD_MODEL_IDS[:] = website_order + [
        model_id for model_id in _BASE_BUILD_CONTRACTS if model_id not in normalized
    ]
    _BUILD_WEBSITE_ROWS.clear()
    _BUILD_WEBSITE_ROWS.update(copy.deepcopy(normalized))
    if retrieved_values:
        BUILD_WEBSITE_CATALOG_RETRIEVED = max(retrieved_values)
    return {key: copy.deepcopy(value) for key, value in _BUILD_WEBSITE_ROWS.items()}


def get_build_website_row(model_id: str) -> dict[str, Any] | None:
    row = _BUILD_WEBSITE_ROWS.get(str(model_id or ""))
    return copy.deepcopy(row) if row else None


def get_build_contract(model_id: str) -> dict[str, Any] | None:
    """Return the current live-overlaid Build contract for ``model_id``."""
    row = BUILD_CONTRACTS.get(str(model_id or ""))
    if not row:
        return None
    result = copy.deepcopy(row)
    result["contract"] = dict(row.get("contract") or {})
    return result


def build_contract_kind(model_id: str) -> str | None:
    row = BUILD_CONTRACTS.get(str(model_id or ""))
    contract = row.get("contract") if row else None
    return contract.get("kind") if isinstance(contract, dict) else None


def is_build_model(model_id: str) -> bool:
    return str(model_id or "") in BUILD_CONTRACTS


__all__ = [
    "BUILD_CATALOG_RETRIEVED",
    "BUILD_CATALOG_SOURCE",
    "BUILD_CONTRACTS",
    "BUILD_MODEL_IDS",
    "BUILD_WEBSITE_CATALOG_RETRIEVED",
    "BUILD_WEBSITE_CATALOG_SOURCE",
    "apply_build_website_catalog",
    "build_contract_kind",
    "get_build_contract",
    "get_build_website_row",
    "is_build_model",
]
