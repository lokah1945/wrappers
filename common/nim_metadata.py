"""NVIDIA NIM Metadata API and NGC repository integration.

This module is deliberately separate from the hosted NVIDIA Build contract.
NGC metadata describes downloadable NIM containers, their exact repository/tag,
and deployment profiles; it does not prove that a hosted inference endpoint is
available. Callers must therefore keep ``metadata_status`` separate from the
account/deployment availability state recorded by the wrapper.

The client is read-only, does not use credentials, and treats missing public
metadata as an informational state rather than model unavailability.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote, urlencode

import aiohttp

logger = logging.getLogger("nim-metadata")

DEFAULT_NGC_BASE_URL = "https://api.ngc.nvidia.com/v2"
DEFAULT_REFRESH_SEC = 21600
DEFAULT_TTL_SEC = 21600
DEFAULT_CONCURRENCY = 8
MAX_CACHE_ENTRIES = 2000
MAX_TAGS = 200
MAX_PROFILES = 500
MAX_RELATED_URLS = 50

# A small set of identities which are known to be aliases on NGC rather than
# a reason to guess across arbitrary repositories. The automatic candidates
# still handle the normal publisher/model -> nim/publisher/repository mapping.
# These aliases were verified against the public NGC repository API.
KNOWN_RESOURCE_ALIASES: dict[str, tuple[str, ...]] = {
    "nvidia/nemotron-parse-2.0": ("nim/nvidia/nemotron-parse",),
    "nvidia/qwen-image-edit-nvpcb-ovsl2sl": ("nim/qwen/qwen-image-edit",),
    "nvidia/cosmos3-nano-reasoner": (
        "nim/nvidia/cosmos3-reasoner",
        "nim/nvidia/cosmos-reason2-8b",
    ),
    "arc/evo2-40b-forward": ("nim/arc/evo2-40b",),
    "arc/evo2-7b-forward": ("nim/arc/evo2-7b", "nim/arc/evo2"),
    "nvidia/riva-translate-1_6b": ("nim/nvidia/megatron-1b-nmt",),
    "nvidia/parakeet-1_1b-rnnt-multilingual-asr": (
        "nim/nvidia/parakeet-1-1b-rnnt-multilingual",
    ),
    "nvidia/canary-1b-asr": ("nim/nvidia/canary-1b",),
}


def _positive_number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return value
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except (TypeError, ValueError):
            return None
        if number > 0:
            return int(number) if number.is_integer() else number
    return None


def _positive_int(value: Any) -> int | None:
    number = _positive_number(value)
    if isinstance(number, int):
        return number
    if isinstance(number, float) and number.is_integer():
        return int(number)
    return None


def _bounded_copy(value: Any, *, depth: int = 0, max_depth: int = 5,
                  max_items: int = 200, max_string: int = 8192) -> Any:
    """Copy future/unknown NGC fields without allowing unbounded cache data."""
    if depth >= max_depth:
        if isinstance(value, (dict, list)):
            return "<truncated>"
        return value if not isinstance(value, str) else value[:max_string]
    if isinstance(value, str):
        return value[:max_string]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        return [_bounded_copy(item, depth=depth + 1, max_depth=max_depth,
                             max_items=max_items, max_string=max_string)
                for item in value[:max_items]]
    if isinstance(value, dict):
        return {
            str(key)[:256]: _bounded_copy(item, depth=depth + 1,
                                          max_depth=max_depth,
                                          max_items=max_items,
                                          max_string=max_string)
            for key, item in list(value.items())[:max_items]
        }
    return str(value)[:max_string]


def _slug_variants(value: Any) -> list[str]:
    """Generate conservative repository spelling variants from a Build ID."""
    text = str(value or "").strip().lower()
    if not text:
        return []
    candidates = [text]
    # Build uses underscores for some decimal/version names while NGC uses a
    # dot. Keep both spellings; never alter publisher identity here.
    candidates.extend((text.replace("_", "."), text.replace("_", "-")))
    candidates.extend((text.replace(".", "-"), text.replace("-", ".")))
    cleaned: list[str] = []
    for item in candidates:
        item = re.sub(r"\s+", "-", item)
        item = re.sub(r"[^a-z0-9._-]+", "-", item)
        item = re.sub(r"-+", "-", item).strip("-.")
        if item and item not in cleaned:
            cleaned.append(item)
    return cleaned


def _load_overrides() -> dict[str, Any]:
    raw = os.environ.get("NVIDIA_NIM_RESOURCE_OVERRIDES", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("[nim-metadata] ignoring invalid NVIDIA_NIM_RESOURCE_OVERRIDES")
        return {}
    return value if isinstance(value, dict) else {}


def resource_candidates(row: Mapping[str, Any]) -> list[str]:
    """Return ordered exact NGC resource candidates for one Build row.

    The first successful repository response is accepted as identity. A
    repository response is required before any tag or metadata is joined, so a
    spelling heuristic cannot silently attach another model's metadata.
    """
    model_id = str(row.get("id") or "").strip()
    if not model_id or not row.get("downloadable"):
        return []
    overrides = _load_overrides().get(model_id)
    values: list[str] = []
    if isinstance(overrides, str):
        values.append(overrides)
    elif isinstance(overrides, dict):
        for key in ("resource_id", "resourceId", "repository"):
            if overrides.get(key):
                values.append(str(overrides[key]))
    for container in (row, row.get("contract") or {}):
        if isinstance(container, Mapping):
            for key in ("nim_resource_id", "nimResourceId", "ngc_resource_id", "resource_id"):
                if container.get(key):
                    values.append(str(container[key]))
    publisher, _, slug = model_id.partition("/")
    title = str(row.get("title") or slug)
    names: list[str] = []
    for raw in (slug, title):
        for variant in _slug_variants(raw):
            if variant not in names:
                names.append(variant)
    # NGC commonly uses the Build publisher, but several NVIDIA-published
    # aliases (for example OpenAI Whisper) live in nim/nvidia.
    for publisher_name in (publisher, "nvidia"):
        if not publisher_name:
            continue
        for name in names:
            values.append(f"nim/{publisher_name}/{name}")
    values.extend(KNOWN_RESOURCE_ALIASES.get(model_id, ()))
    result: list[str] = []
    for value in values:
        value = str(value).strip().strip("/")
        if not value.startswith("nim/") or any(part in {"", ".", ".."} for part in value.split("/")):
            continue
        if value not in result:
            result.append(value)
    return result


def _path_resource(base: str, resource_id: str) -> str:
    return f"{base.rstrip('/')}/repos/" + "/".join(
        quote(part, safe="") for part in resource_id.split("/")
    )


def _metadata_url(base: str, resource_id: str, tag: str) -> str:
    query = urlencode({"resourceId": resource_id, "tag": tag})
    return f"{base.rstrip('/')}/nim/metadata?{query}"


def _version_key(tag: str) -> tuple:
    numbers = tuple(int(x) for x in re.findall(r"\d+", tag or ""))
    # Prefer stable tags over a variant suffix when the numeric version is the
    # same. The repository's explicit latestTag remains higher priority.
    variant = 1 if "variant" in str(tag).lower() else 0
    return numbers, -variant, str(tag)


def choose_tag(repository: Mapping[str, Any], preferred: str | None = None) -> str | None:
    tags = [str(tag) for tag in (repository.get("tags") or []) if str(tag)]
    latest = str(repository.get("latestTag") or "")
    choices: list[str] = []
    if preferred:
        choices.append(str(preferred))
    if latest and latest.lower() != "latest":
        choices.append(latest)
    stable = [tag for tag in tags if tag.lower() != "latest"]
    choices.extend(sorted(stable, key=_version_key, reverse=True))
    choices.extend(tags)
    for tag in choices:
        if tag and (not tags or tag in tags or tag == latest):
            return tag
    return None


def _request_id(body: Any) -> str | None:
    status = body.get("requestStatus") if isinstance(body, dict) else None
    if not isinstance(status, dict):
        return None
    value = status.get("requestId") or status.get("request_id")
    return str(value)[:256] if value else None


def _repository_summary(body: Mapping[str, Any]) -> dict[str, Any]:
    """Keep repository identity and useful URLs without copying huge labels."""
    keys = (
        "isPublic", "isReadOnly", "isMultinodeEnabled", "isMultiArchitecture",
        "canGuestPull", "canPublicList", "namespace", "name", "displayName",
        "shortDescription", "description", "latestTag", "latestImageSize",
        "imageDigest", "digest", "buildUrl", "build_url", "sdkExampleUrl",
        "sdk_example_url", "license", "licenseTerms", "modelNames",
        "architectures", "architectureVariants", "image", "scanStatus",
        "signed", "signedImage", "labels", "systemLabels", "policyLabels",
    )
    result: dict[str, Any] = {}
    for key in keys:
        if key in body:
            result[key] = _bounded_copy(body[key], max_items=100, max_string=4096)
    tags = body.get("tags")
    if isinstance(tags, list):
        result["tags"] = [str(tag)[:256] for tag in tags[:MAX_TAGS] if str(tag)]
    return result


def normalize_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    result = _bounded_copy(dict(profile), max_items=100, max_string=4096)
    if not isinstance(result, dict):
        result = {}
    tp = _positive_int(profile.get("tensor_parallel_size")) or 1
    pp = _positive_int(profile.get("pipeline_parallel_size")) or 1
    result["required_gpus"] = tp * pp
    result["tensor_parallel_size"] = tp
    result["pipeline_parallel_size"] = pp
    for key in ("min_vram_per_device_gb", "disk_space_per_device_gb",
                "system_memory_per_device_gb"):
        if key in profile:
            value = _positive_number(profile.get(key))
            if value is not None:
                result[key] = value
    if "max_gpu_count" in profile:
        value = _positive_int(profile.get("max_gpu_count"))
        if value is not None:
            result["max_gpu_count"] = value
    for key in ("profile_id", "gpu", "gpu_device", "precision",
                "inference_backend", "workspace_hash"):
        if key in profile and profile[key] is not None:
            result[key] = str(profile[key])[:4096]
    for key in ("tested_gpu_devices", "languages"):
        if key in profile:
            value = profile[key]
            if isinstance(value, list):
                result[key] = [str(item)[:512] for item in value[:MAX_TAGS]]
            elif value is not None:
                result[key] = [str(value)[:512]]
    for key in ("feat_lora", "buildable_profile"):
        if key in profile:
            result[key] = bool(profile[key])
    return result


def normalize_metadata(resource_id: str, tag: str, body: Mapping[str, Any],
                       *, repository: Mapping[str, Any], metadata_url: str,
                       repository_url: str, checked_at: float,
                       request_id: str | None = None) -> dict[str, Any]:
    raw_profiles = body.get("profiles") if isinstance(body, Mapping) else []
    profiles = [normalize_profile(profile) for profile in (raw_profiles or [])
                if isinstance(profile, Mapping)][:MAX_PROFILES]
    result = {
        "source": "ngc_nim_metadata_api",
        "metadata_status": "available",
        "resource_id": resource_id,
        "container_tag": tag,
        "container_image": f"nvcr.io/{resource_id}:{tag}",
        "metadata_url": metadata_url,
        "repository_url": repository_url,
        "checked_at": checked_at,
        "expires_at": checked_at + DEFAULT_TTL_SEC,
        "request_id": request_id,
        "repository": _repository_summary(repository),
        "profiles": profiles,
        "profile_count": len(profiles),
        "release_details": _bounded_copy(body.get("release_details") or {}, max_items=100),
        "features": _bounded_copy(body.get("features") or {}, max_items=100),
        "related_urls": [str(url)[:2048] for url in
                         (body.get("related_urls") or [])[:MAX_RELATED_URLS]
                         if str(url)],
    }
    return result


def profile_fit(profile: Mapping[str, Any], hardware: Mapping[str, Any] | None = None) -> str:
    """Return fit/does_not_fit/unknown without inferring absent requirements."""
    hardware = hardware or {}
    if not hardware:
        return "unknown"
    target_gpu = str(hardware.get("gpu_device") or "")
    tested = {str(item) for item in (profile.get("tested_gpu_devices") or [])}
    required = _positive_int(profile.get("required_gpus")) or 1
    count = _positive_int(hardware.get("gpu_count"))
    vram = _positive_number(hardware.get("vram_per_device_gb"))
    minimum = _positive_number(profile.get("min_vram_per_device_gb"))
    if count is not None and required > count:
        return "does_not_fit"
    if vram is not None and minimum is not None and vram < minimum:
        return "does_not_fit"
    declared = str(profile.get("gpu_device") or "")
    profile_gpu = declared or str(profile.get("gpu") or "") or bool(tested)
    if profile_gpu and not target_gpu:
        return "unknown"
    if target_gpu and declared and target_gpu != declared and target_gpu not in tested:
        # An explicit profile GPU mismatch is a negative only when the profile
        # also declares a tested/target device set. GPU-agnostic profiles stay
        # unknown rather than being rejected.
        return "does_not_fit"
    if (not target_gpu or not declared) and minimum is None and count is None and vram is None:
        return "unknown"
    return "fit"


def _profile_summary(profile: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("profile_id", "gpu", "gpu_device", "tested_gpu_devices",
            "precision", "inference_backend", "tensor_parallel_size",
            "pipeline_parallel_size", "required_gpus", "min_vram_per_device_gb",
            "disk_space_per_device_gb", "system_memory_per_device_gb",
            "min_cpu_count", "min_driver_version", "max_gpu_count",
            "form_factor", "feat_lora", "buildable_profile", "languages")
    return {key: _bounded_copy(profile[key], max_items=50, max_string=2048)
            for key in keys if key in profile}


def public_metadata(entry: Mapping[str, Any] | None, *, include_profiles: bool = False,
                    hardware: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """Return metadata safe for a model descriptor or a full metadata route."""
    if not isinstance(entry, Mapping):
        return None
    result = copy.deepcopy(dict(entry))
    if not include_profiles:
        profiles = result.pop("profiles", [])
        summaries = [_profile_summary(profile) for profile in profiles
                     if isinstance(profile, Mapping)]
        if hardware is not None:
            for summary, profile in zip(summaries, profiles):
                if isinstance(profile, Mapping):
                    summary["hardware_fit"] = profile_fit(profile, hardware)
        result["profile_summaries"] = summaries
    else:
        profiles = result.get("profiles") or []
        if hardware is not None:
            for profile in profiles:
                if isinstance(profile, dict):
                    profile["hardware_fit"] = profile_fit(profile, hardware)
    return result


class NimMetadataRegistry:
    """Cached async NGC repository/tag/metadata join for Build NIM rows."""

    def __init__(self, session: aiohttp.ClientSession | None = None, *,
                 base_url: str | None = None, cache_file: str | os.PathLike[str] | None = None,
                 enabled: bool | None = None, refresh_sec: int | None = None,
                 ttl_sec: int | None = None, concurrency: int | None = None):
        self.base_url = (base_url or os.environ.get("NVIDIA_NIM_METADATA_BASE_URL")
                         or DEFAULT_NGC_BASE_URL).rstrip("/")
        self.session = session
        self.enabled = ((os.environ.get("NVIDIA_NIM_METADATA_ENABLED", "true").lower()
                         not in {"0", "false", "no", "off"})
                        if enabled is None else bool(enabled))
        self.refresh_sec = max(60, int(refresh_sec or os.environ.get(
            "NVIDIA_NIM_METADATA_REFRESH_SEC", str(DEFAULT_REFRESH_SEC))))
        self.ttl_sec = max(300, int(ttl_sec or os.environ.get(
            "NVIDIA_NIM_METADATA_TTL_SEC", str(DEFAULT_TTL_SEC))))
        self.concurrency = max(1, min(32, int(concurrency or os.environ.get(
            "NVIDIA_NIM_METADATA_CONCURRENCY", str(DEFAULT_CONCURRENCY)))))
        default_file = Path.cwd() / "nim-metadata-cache.json"
        self.cache_file = Path(cache_file or os.environ.get(
            "NVIDIA_NIM_METADATA_CACHE_FILE", str(default_file)))
        self._entries: dict[str, dict[str, Any]] = {}
        self._last_attempt = 0.0
        self._last_sync = 0.0
        self._last_error: str | None = None
        self._lock = asyncio.Lock()
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            raw = json.loads(self.cache_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("schema") != 1:
                return
            entries = raw.get("entries")
            if not isinstance(entries, dict):
                return
            self._entries = {
                str(mid): value for mid, value in list(entries.items())[:MAX_CACHE_ENTRIES]
                if isinstance(value, dict) and isinstance(value.get("model_id", mid), str)
            }
            self._last_sync = float(raw.get("checked_at") or 0)
        except (OSError, ValueError, TypeError):
            return

    def _save_cache(self) -> None:
        temporary = None
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", dir=self.cache_file.parent,
                                             delete=False, encoding="utf-8") as output:
                temporary = output.name
                json.dump({"schema": 1, "checked_at": self._last_sync,
                           "entries": self._entries}, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.cache_file)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("[nim-metadata] cache write failed: %s", type(exc).__name__)
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self._entries)

    def get(self, model_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self._entries.get(str(model_id or "")))

    def status(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for entry in self._entries.values():
            state = str(entry.get("metadata_status") or "unknown")
            counts[state] = counts.get(state, 0) + 1
        return {
            "enabled": self.enabled,
            "source": "ngc_nim_metadata_api",
            "base_url": self.base_url,
            "entries": len(self._entries),
            "statuses": counts,
            "last_sync": self._last_sync or None,
            "last_attempt": self._last_attempt or None,
            "last_error": self._last_error,
        }

    def _fresh(self, entry: Mapping[str, Any] | None) -> bool:
        expiry = entry.get("expires_at") if isinstance(entry, Mapping) else None
        return isinstance(expiry, (int, float)) and expiry > time.time()

    async def _get_json(self, session: aiohttp.ClientSession, url: str) -> tuple[int, Any, str | None]:
        try:
            async with session.get(url, headers={"Accept": "application/json"},
                                   timeout=aiohttp.ClientTimeout(total=20)) as response:
                status = response.status
                if status != 200:
                    # Do not retain provider error bodies. The request ID is
                    # useful for support without exposing response payloads.
                    try:
                        body = await response.json(content_type=None)
                    except Exception:
                        body = None
                    return status, None, _request_id(body)
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    return status, None, None
                return status, body, _request_id(body)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("[nim-metadata] GET failed (%s): %s", type(exc).__name__, url)
            return 0, None, None

    async def _repository(self, session: aiohttp.ClientSession, resource_id: str):
        url = _path_resource(self.base_url, resource_id)
        status, body, request_id = await self._get_json(session, url)
        return status, body if isinstance(body, dict) else None, request_id, url

    async def _one(self, session: aiohttp.ClientSession, row: Mapping[str, Any], semaphore: asyncio.Semaphore) -> tuple[str, dict[str, Any]]:
        model_id = str(row.get("id") or "")
        async with semaphore:
            if not row.get("downloadable"):
                checked = time.time()
                return model_id, {
                    "model_id": model_id,
                    "metadata_status": "not_applicable",
                    "source": "ngc_nim_metadata_api",
                    "checked_at": checked,
                    "expires_at": checked + self.ttl_sec,
                }
            overrides = _load_overrides().get(model_id)
            preferred_tag = overrides.get("tag") if isinstance(overrides, dict) else None
            repository = None
            resource_id = None
            repository_url = None
            repo_request_id = None
            last_status = 0
            for candidate in resource_candidates(row):
                status, body, request_id, url = await self._repository(session, candidate)
                last_status = status
                repo_request_id = request_id or repo_request_id
                if status == 200 and body:
                    resource_id, repository, repository_url = candidate, body, url
                    break
            checked = time.time()
            if not repository or not resource_id:
                return model_id, {
                    "model_id": model_id,
                    "metadata_status": "repository_not_found" if last_status == 404 else "repository_lookup_error",
                    "source": "ngc_nim_metadata_api",
                    "checked_at": checked,
                    "expires_at": checked + self.ttl_sec,
                    "request_id": repo_request_id,
                    "resource_candidates": resource_candidates(row)[:20],
                }
            repo = _repository_summary(repository)
            tag_candidates: list[str] = []
            selected = choose_tag(repository, preferred_tag)
            if selected:
                tag_candidates.append(selected)
            latest = str(repository.get("latestTag") or "")
            if latest and latest not in tag_candidates:
                tag_candidates.append(latest)
            tags = [str(tag) for tag in (repository.get("tags") or []) if str(tag)]
            for tag in sorted((tag for tag in tags if tag.lower() != "latest"),
                              key=_version_key, reverse=True):
                if tag not in tag_candidates:
                    tag_candidates.append(tag)
                if len(tag_candidates) >= 3:
                    break
            if not tag_candidates:
                tag_candidates = ["latest"]
            metadata_status = 0
            metadata_body = None
            metadata_tag = None
            metadata_request_id = None
            for tag in tag_candidates[:3]:
                metadata_url = _metadata_url(self.base_url, resource_id, tag)
                status, body, request_id = await self._get_json(session, metadata_url)
                metadata_status = status
                metadata_request_id = request_id or metadata_request_id
                if status == 200 and isinstance(body, dict):
                    metadata_body, metadata_tag = body, tag
                    break
            if metadata_body is None:
                return model_id, {
                    "model_id": model_id,
                    "metadata_status": "metadata_not_published" if metadata_status == 404 else "metadata_error",
                    "source": "ngc_nim_metadata_api",
                    "resource_id": resource_id,
                    "repository_url": repository_url,
                    "repository": repo,
                    "container_tag": tag_candidates[0],
                    "container_image": f"nvcr.io/{resource_id}:{tag_candidates[0]}",
                    "metadata_url": _metadata_url(self.base_url, resource_id, tag_candidates[0]),
                    "checked_at": checked,
                    "expires_at": checked + self.ttl_sec,
                    "request_id": metadata_request_id or repo_request_id,
                    "metadata_http_status": metadata_status,
                }
            normalized = normalize_metadata(
                resource_id, metadata_tag or tag_candidates[0], metadata_body,
                repository=repository, metadata_url=_metadata_url(
                    self.base_url, resource_id, metadata_tag or tag_candidates[0]),
                repository_url=repository_url, checked_at=checked,
                request_id=metadata_request_id or repo_request_id,
            )
            normalized["model_id"] = model_id
            normalized["expires_at"] = checked + self.ttl_sec
            normalized["resource_candidates"] = resource_candidates(row)[:20]
            return model_id, normalized

    async def refresh(self, rows: Iterable[Mapping[str, Any]], *, force: bool = False) -> dict[str, dict[str, Any]]:
        if not self.enabled:
            return self.snapshot()
        rows = [row for row in rows if isinstance(row, Mapping) and row.get("id")]
        started = time.time()
        async with self._lock:
            if (not force and self._last_attempt and
                    started - self._last_attempt < min(self.refresh_sec, self.ttl_sec)):
                return self.snapshot()
            self._last_attempt = time.time()
            own_session = self.session is None
            session = self.session or aiohttp.ClientSession()
            semaphore = asyncio.Semaphore(self.concurrency)
            try:
                jobs = []
                for row in rows:
                    model_id = str(row.get("id"))
                    current = self._entries.get(model_id)
                    if not force and self._fresh(current):
                        continue
                    jobs.append(self._one(session, row, semaphore))
                for result in await asyncio.gather(*jobs):
                    model_id, entry = result
                    # Keep last good data on transport errors. A legitimate
                    # 404/metadata_not_published is stored explicitly, since
                    # absent public metadata must remain visible to callers.
                    if (entry.get("metadata_status") in {"repository_lookup_error", "metadata_error"}
                            and self._entries.get(model_id, {}).get("metadata_status") == "available"):
                        old = copy.deepcopy(self._entries[model_id])
                        old["refresh_error"] = entry.get("metadata_status")
                        old["last_attempt"] = time.time()
                        self._entries[model_id] = old
                    else:
                        self._entries[model_id] = entry
                self._last_sync = time.time()
                self._last_error = None
                self._save_cache()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = type(exc).__name__
                logger.warning("[nim-metadata] refresh failed: %s", type(exc).__name__)
            finally:
                if own_session:
                    await session.close()
            return self.snapshot()


__all__ = [
    "DEFAULT_NGC_BASE_URL",
    "KNOWN_RESOURCE_ALIASES",
    "NimMetadataRegistry",
    "choose_tag",
    "normalize_metadata",
    "normalize_profile",
    "profile_fit",
    "public_metadata",
    "resource_candidates",
]
