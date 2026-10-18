"""Live NVIDIA Build model-catalog synchronisation.

The Build models page is a dynamic catalog, not the same thing as the NGC NIM
metadata API.  This module reads the server-rendered Build search result,
including its resultTotal and per-model nimType labels, and keeps a last-good
atomic cache.  It deliberately does not infer runtime health from a website
label.

The page currently exposes its search result in a React Server Components
payload.  Parsing that payload gives us the same current result set and
facets shown by build.nvidia.com/models without depending on a private API or
assuming that the number of models is static.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable


DEFAULT_BUILD_MODELS_URL = "https://build.nvidia.com/models"
DEFAULT_CACHE_FILE = "build-web-catalog-cache.json"
DEFAULT_REFRESH_SEC = 900.0
DEFAULT_TIMEOUT_SEC = 45.0
DEFAULT_MAX_PAGES = 50

_ACCESS_LABELS = {
    "Free Endpoint": "free_endpoint",
    "Partner Endpoint": "partner_endpoint",
    "Download Available": "download_available",
}


def _env_bool(name: str, default: bool = True) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _positive_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _script_payloads(html: str) -> Iterable[str]:
    """Yield decoded RSC payload strings from a Build HTML document."""
    decoder = json.JSONDecoder()
    marker = "self.__next_f.push("
    cursor = 0
    while True:
        position = html.find(marker, cursor)
        if position < 0:
            return
        start = position + len(marker)
        try:
            value, end = decoder.raw_decode(html[start:])
        except (TypeError, ValueError, json.JSONDecodeError):
            cursor = start
            continue
        cursor = start + end
        if isinstance(value, list) and len(value) > 1 and isinstance(value[1], str):
            yield value[1]


def _labels(resource: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for label in resource.get("labels") or []:
        if not isinstance(label, dict):
            continue
        key = str(label.get("key") or "").strip()
        values = label.get("values")
        if not key or not isinstance(values, list):
            continue
        result[key] = [str(value) for value in values if str(value).strip()]
    return result


def _attributes(resource: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for attribute in resource.get("attributes") or []:
        if not isinstance(attribute, dict):
            continue
        key = str(attribute.get("key") or "").strip()
        if key and "value" in attribute:
            result[key] = str(attribute.get("value") or "")
    return result


def _int_attribute(attributes: dict[str, str], *keys: str) -> int | None:
    for key in keys:
        value = attributes.get(key)
        if value is None:
            continue
        try:
            parsed = int(value)
            return parsed if parsed >= 0 else None
        except (TypeError, ValueError):
            continue
    return None


def _extract_result_objects(payload: str) -> tuple[int | None, int | None, list[dict[str, Any]]]:
    """Extract the Build search result group from one decoded RSC payload."""
    total_match = re.search(r'"resultTotal"\s*:\s*(\d+)', payload)
    page_match = re.search(r'"resultPageTotal"\s*:\s*(\d+)', payload)
    result_total = int(total_match.group(1)) if total_match else None
    page_total = int(page_match.group(1)) if page_match else None

    decoder = json.JSONDecoder()
    cursor = 0
    resources: list[dict[str, Any]] = []
    marker = '{"groupValue"'
    while True:
        position = payload.find(marker, cursor)
        if position < 0:
            break
        try:
            value, end = decoder.raw_decode(payload[position:])
        except (TypeError, ValueError, json.JSONDecodeError):
            cursor = position + len(marker)
            continue
        cursor = position + end
        if not isinstance(value, dict):
            continue
        page_resources = value.get("resources")
        if not isinstance(page_resources, list):
            continue
        for resource in page_resources:
            if isinstance(resource, dict) and resource.get("name"):
                resources.append(resource)
    return result_total, page_total, resources


def _website_row(resource: dict[str, Any]) -> dict[str, Any]:
    labels = _labels(resource)
    publisher_values = labels.get("publisher") or []
    publisher = publisher_values[0] if publisher_values else ""
    name = str(resource.get("name") or "").strip()
    if not publisher or not name:
        raise ValueError("Build resource has no canonical publisher/name")
    model_id = f"{publisher}/{name}"

    nim_types = labels.get("nimType") or []
    access_modes = [
        _ACCESS_LABELS[label]
        for label in ("Free Endpoint", "Partner Endpoint", "Download Available")
        if label in nim_types
    ]
    attributes = _attributes(resource)
    website_badges = [label for label in nim_types if label in _ACCESS_LABELS]
    deprecation = attributes.get("DEPRECATION")
    if deprecation:
        website_badges.insert(0, f"Deprecation in {deprecation}")

    website_available = attributes.get("AVAILABLE")
    if website_available is not None:
        website_available = website_available.lower() == "true"

    return {
        "id": model_id,
        "name": name,
        "publisher": publisher,
        "display_name": resource.get("displayName") or name,
        "title": resource.get("displayName") or name,
        "description": resource.get("description") or "",
        "build_url": f"https://build.nvidia.com/{publisher}/{name}",
        "resource_id": resource.get("resourceId"),
        "resource_type": resource.get("resourceType"),
        "date_created": resource.get("dateCreated"),
        "date_modified": resource.get("dateModified"),
        "msg_timestamp": resource.get("msgTimestamp"),
        "website_listed": True,
        "website_source": DEFAULT_BUILD_MODELS_URL,
        "nim_type": list(nim_types),
        "website_access_modes": list(access_modes),
        "free_endpoint": "free_endpoint" in access_modes,
        "partner_endpoint": "partner_endpoint" in access_modes,
        "download_available": "download_available" in access_modes,
        # Backward-compatible internal spelling.  The public badge remains
        # "Download Available", exactly as on the website.
        "downloadable": "download_available" in access_modes,
        "website_badges": website_badges,
        "access_badges": website_badges,
        "use_cases": list(labels.get("usecase") or []),
        "inference_providers": list(labels.get("cloudPartnerType") or []),
        "publisher_labels": list(labels.get("publisher") or []),
        "general_labels": list(labels.get("general") or []),
        "nim_container_gpus": list(labels.get("gpuType") or []),
        "hardware_types": list(labels.get("hardwareType") or []),
        "website_available": website_available,
        "last_month_api_invocation_count": _int_attribute(
            attributes, "lastMonthApiInvocationCount", "last_month_api_invocation_count"
        ),
        "last_month_download_count": _int_attribute(
            attributes, "lastMonthDownloadCount", "last_month_download_count"
        ),
        "attributes": attributes,
        "labels": labels,
    }


def parse_build_models_page(html: str) -> dict[str, Any]:
    """Parse one live ``/models`` HTML response.

    The result is deliberately independent of the current count: callers use
    ``result_total`` to determine how many pages to request.
    """
    result_total: int | None = None
    page_total: int | None = None
    resources: list[dict[str, Any]] = []
    for payload in _script_payloads(html):
        current_total, current_pages, current_resources = _extract_result_objects(payload)
        if current_total is not None:
            result_total = current_total
        if current_pages is not None:
            page_total = current_pages
        if current_resources:
            resources.extend(current_resources)

    # A simple server-rendered fallback is useful if NVIDIA changes the RSC
    # transport but keeps the card markup. It does not invent access labels;
    # it only recovers the canonical IDs, while callers will reject a page
    # without the authoritative nimType data.
    if not resources:
        card_pattern = re.compile(
            r'data-nvtrack-nav-object="artifact-card"[^>]*href="/([^/]+)/([^\"]+)"',
            re.I,
        )
        for publisher, name in card_pattern.findall(html):
            resources.append({
                "name": name,
                "labels": [{"key": "publisher", "values": [publisher]}],
            })

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for resource in resources:
        row = _website_row(resource)
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        rows.append(row)
    return {
        "result_total": result_total,
        "page_total": page_total,
        "models": rows,
    }


async def fetch_build_web_catalog(
    session,
    *,
    url: str = DEFAULT_BUILD_MODELS_URL,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    max_pages: int = DEFAULT_MAX_PAGES,
    retries: int = 2,
) -> dict[str, Any]:
    """Fetch every current Build models page and return one validated snapshot."""
    timeout = __import__("aiohttp").ClientTimeout(total=timeout_sec)

    async def fetch_page(page: int) -> dict[str, Any]:
        params = {"page": str(page)} if page > 1 else None
        last_error: Exception | None = None
        for attempt in range(max(1, retries + 1)):
            try:
                async with session.get(
                    url,
                    params=params,
                    headers={
                        "Accept": "text/html,application/xhtml+xml",
                        "User-Agent": "wrapper-nvidia-build-catalog/1.0",
                    },
                    timeout=timeout,
                ) as response:
                    body = await response.text()
                    if response.status != 200:
                        raise RuntimeError(f"Build catalog HTTP {response.status}")
                    parsed = parse_build_models_page(body)
                    if not parsed["models"]:
                        raise RuntimeError("Build catalog page contained no model resources")
                    return parsed
            except Exception as exc:  # bounded retry; caller keeps last good cache
                last_error = exc
                if attempt < retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"Build catalog page {page} failed: {last_error}")

    first = await fetch_page(1)
    total = int(first.get("result_total") or 0)
    if total <= 0:
        raise RuntimeError("Build catalog did not publish a positive resultTotal")
    observed_page_total = int(first.get("page_total") or 0)
    page_total = observed_page_total or int(math.ceil(total / max(1, len(first["models"]))))
    if page_total <= 0 or page_total > max_pages:
        raise RuntimeError(f"Build catalog page count {page_total} is outside safety bound")

    pages = [first]
    if page_total > 1:
        rest = await asyncio.gather(*(fetch_page(page) for page in range(2, page_total + 1)))
        pages.extend(rest)

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in pages:
        for row in page.get("models") or []:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            rows.append(row)
    if len(rows) < total:
        raise RuntimeError(f"Build catalog returned {len(rows)} unique rows, expected {total}")

    retrieved = time.time()
    return {
        "schema": 1,
        "source": url,
        "retrieved_at": retrieved,
        "result_total": total,
        "page_total": page_total,
        "models": rows,
    }


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


class NvidiaBuildWebCatalogRegistry:
    """Last-good live Build catalog with bounded refresh and atomic cache."""

    def __init__(
        self,
        session,
        *,
        cache_file: str | Path | None = None,
        url: str | None = None,
        refresh_sec: float | None = None,
        timeout_sec: float | None = None,
        max_pages: int | None = None,
        enabled: bool | None = None,
    ):
        self.session = session
        self.url = url or os.environ.get("NVIDIA_BUILD_WEB_CATALOG_URL", DEFAULT_BUILD_MODELS_URL)
        self.refresh_sec = refresh_sec or _positive_float(
            "NVIDIA_BUILD_WEB_CATALOG_REFRESH_SEC", DEFAULT_REFRESH_SEC
        )
        self.timeout_sec = timeout_sec or _positive_float(
            "NVIDIA_BUILD_WEB_CATALOG_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC
        )
        self.max_pages = max_pages or _positive_int(
            "NVIDIA_BUILD_WEB_CATALOG_MAX_PAGES", DEFAULT_MAX_PAGES
        )
        self.enabled = _env_bool("NVIDIA_BUILD_WEB_CATALOG_ENABLED", True) if enabled is None else enabled
        self.cache_file = Path(cache_file or os.environ.get(
            "NVIDIA_BUILD_WEB_CATALOG_CACHE_FILE", DEFAULT_CACHE_FILE
        ))
        self._models: dict[str, dict[str, Any]] = {}
        self._last_success: float | None = None
        self._last_attempt: float | None = None
        self._last_error: str | None = None
        self._result_total: int = 0
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            payload = json.loads(self.cache_file.read_text(encoding="utf-8"))
            rows = payload.get("models") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                return
            loaded = {str(row.get("id")): row for row in rows if isinstance(row, dict) and row.get("id")}
            if not loaded:
                return
            self._models = loaded
            self._result_total = int(payload.get("result_total") or len(loaded))
            self._last_success = float(payload.get("retrieved_at") or 0) or None
        except (OSError, ValueError, TypeError):
            return

    def rows(self) -> list[dict[str, Any]]:
        return list(self._models.values())

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {key: dict(value) for key, value in self._models.items()}

    def status(self) -> dict[str, Any]:
        counts = {"free_endpoint": 0, "partner_endpoint": 0, "download_available": 0}
        for row in self._models.values():
            for key in counts:
                if row.get(key):
                    counts[key] += 1
        return {
            "enabled": self.enabled,
            "source": self.url,
            "cache_file": str(self.cache_file),
            "entries": len(self._models),
            "result_total": self._result_total,
            "access_counts": counts,
            "last_success": self._last_success,
            "last_attempt": self._last_attempt,
            "last_error": self._last_error,
        }

    async def refresh(self, *, force: bool = False) -> list[dict[str, Any]]:
        if not self.enabled:
            return self.rows()
        now = time.time()
        if not force and self._last_success and now - self._last_success < self.refresh_sec:
            return self.rows()
        self._last_attempt = now
        try:
            payload = await fetch_build_web_catalog(
                self.session,
                url=self.url,
                timeout_sec=self.timeout_sec,
                max_pages=self.max_pages,
            )
            rows = payload.get("models") or []
            if not rows or int(payload.get("result_total") or 0) < len(rows):
                raise RuntimeError("Build catalog validation rejected the fetched snapshot")
            # The access facets are part of the authoritative website result.
            # A shell/error page may still contain card-like markup, but it
            # must never replace a last-good snapshot with zero access labels.
            if not any(row.get("nim_type") for row in rows if isinstance(row, dict)):
                raise RuntimeError("Build catalog response contained no nimType access labels")
            _atomic_json_write(self.cache_file, payload)
            self._models = {row["id"]: row for row in rows}
            self._result_total = int(payload.get("result_total") or len(rows))
            self._last_success = float(payload.get("retrieved_at") or time.time())
            self._last_error = None
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"[:500]
        return self.rows()


__all__ = [
    "DEFAULT_BUILD_MODELS_URL",
    "NvidiaBuildWebCatalogRegistry",
    "fetch_build_web_catalog",
    "parse_build_models_page",
]
