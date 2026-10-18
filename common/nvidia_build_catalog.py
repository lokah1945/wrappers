"""NVIDIA Build/NIM contract catalogue.

This module is deliberately data-first.  Build is a superset of the hosted
``/v1/models`` feed: some entries are downloadable NIMs, some use native
GenAI/health/climate/CV routes, and some expose gRPC rather than OpenAI REST.
The JSON matrix records the route and request shape found on each Build/API
reference page.  Runtime availability is *not* stored here; it is always an
account/deployment-scoped observation from the verifier.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_MATRIX_PATH = Path(__file__).with_name("nvidia_build_contract_matrix.json")
try:
    _MATRIX = json.loads(_MATRIX_PATH.read_text(encoding="utf-8"))
except (OSError, ValueError):  # keep imports safe in minimal/unit environments
    _MATRIX = {"count": 0, "models": []}

BUILD_CONTRACTS: dict[str, dict[str, Any]] = {
    row["id"]: row for row in (_MATRIX.get("models") or [])
    if isinstance(row, dict) and isinstance(row.get("id"), str)
}
BUILD_MODEL_IDS: tuple[str, ...] = tuple(
    row["id"] for row in (_MATRIX.get("models") or [])
    if isinstance(row, dict) and isinstance(row.get("id"), str)
)
BUILD_CATALOG_SOURCE = _MATRIX.get("source", "https://build.nvidia.com/models")
BUILD_CATALOG_RETRIEVED = _MATRIX.get("retrieved")


def get_build_contract(model_id: str) -> dict[str, Any] | None:
    """Return a copy of the documented Build contract for ``model_id``."""
    row = BUILD_CONTRACTS.get(str(model_id or ""))
    if not row:
        return None
    # The matrix is immutable at runtime; shallow-copy the top level so
    # callers can attach observed state without mutating the shared catalogue.
    result = dict(row)
    result["contract"] = dict(row.get("contract") or {})
    return result


def build_contract_kind(model_id: str) -> str | None:
    row = BUILD_CONTRACTS.get(str(model_id or ""))
    contract = row.get("contract") if row else None
    return contract.get("kind") if isinstance(contract, dict) else None


def is_build_model(model_id: str) -> bool:
    return str(model_id or "") in BUILD_CONTRACTS
