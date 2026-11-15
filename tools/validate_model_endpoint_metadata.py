#!/usr/bin/env python3
"""Validate wrapper model metadata and exact model-to-route matching via Python.

This intentionally uses urllib rather than curl. It checks that every live
catalog entry has a detail endpoint, a non-ambiguous wrapper route descriptor,
and that the single-model metadata route agrees with the list route.

It does not print or load provider secrets because metadata discovery is public.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import pathlib
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

CONFIGS = {
    "nvidia-python": {"port": 9101, "base": "https://integrate.api.nvidia.com",
                       "bases": ["https://integrate.api.nvidia.com", "https://ai.api.nvidia.com", "https://api.nvcf.nvidia.com"]},
    "nous": {"port": 9102, "base": "https://inference-api.nousresearch.com",
              "bases": ["https://inference-api.nousresearch.com"]},
    "openrouter": {"port": 9106, "base": "https://openrouter.ai/api/v1",
                    "bases": ["https://openrouter.ai/api/v1"]},
}


def get_json(url: str, timeout: float = 15.0):
    try:
        req = Request(url, headers={"Accept": "application/json"})
        with urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8"))
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:
            payload = {}
        return exc.code, payload
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return 0, {"error": str(exc)[:200]}


def route_errors(model: dict, config: dict) -> list[str]:
    errors = []
    endpoints = model.get("endpoints")
    if not isinstance(endpoints, list) or not endpoints:
        return ["missing endpoints"]
    for index, endpoint in enumerate(endpoints):
        if not isinstance(endpoint, dict):
            errors.append(f"endpoint[{index}] is not an object")
            continue
        path = endpoint.get("client_path") or endpoint.get("path")
        if not isinstance(path, str) or not path.startswith("/v1/"):
            errors.append(f"endpoint[{index}] missing client_path")
        if endpoint.get("route_confidence") != "exact":
            errors.append(f"endpoint[{index}] route_confidence is not exact")
        if endpoint.get("route_source") != "wrapper_contract":
            errors.append(f"endpoint[{index}] route_source is not wrapper_contract")
        url = endpoint.get("url") or endpoint.get("url_template")
        allowed_bases = config.get("bases") or [config["base"]]
        if not isinstance(url, str) or not any(url.startswith(base) for base in allowed_bases):
            errors.append(f"endpoint[{index}] URL does not use configured provider base")
        if "{model}" in str(endpoint.get("url_template", "")):
            if not endpoint.get("upstream_path_template"):
                errors.append(f"endpoint[{index}] template lacks upstream_path_template")
        elif not endpoint.get("upstream_path"):
            errors.append(f"endpoint[{index}] missing upstream_path")
        if endpoint.get("model_field") != "model":
            errors.append(f"endpoint[{index}] model_field is not model")
    return errors


def check_one(base_url: str, config: dict, model: dict):
    model_id = str(model.get("id") or "")
    detail_url = f"{base_url}/v1/models/{quote(model_id, safe='/')}"
    status, detail = get_json(detail_url)
    errors = []
    if status != 200:
        errors.append(f"detail HTTP {status}")
    if isinstance(detail, dict) and detail.get("id") != model_id:
        errors.append("detail id mismatch")
    return model_id, status, errors, route_errors(detail if isinstance(detail, dict) else {}, config)


def validate_wrapper(name: str, config: dict) -> dict:
    base_url = f"http://127.0.0.1:{config['port']}"
    list_status, payload = get_json(f"{base_url}/v1/models")
    models = payload.get("data", []) if isinstance(payload, dict) else []
    if not isinstance(models, list):
        models = []
    list_errors = []
    for model in models:
        list_errors.extend(
            f"{model.get('id')}: {error}"
            for error in route_errors(model, config)
        )

    detail_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(check_one, base_url, config, model) for model in models]
        for future in concurrent.futures.as_completed(futures):
            detail_results.append(future.result())
    detail_errors = []
    for model_id, status, errors, endpoint_errors in detail_results:
        detail_errors.extend(f"{model_id}: {error}" for error in errors)
        detail_errors.extend(f"{model_id}: {error}" for error in endpoint_errors)

    capabilities_status, capabilities = get_json(f"{base_url}/v1/capabilities")
    capability_models = capabilities.get("models", []) if isinstance(capabilities, dict) else []
    capability_errors = []
    if capabilities_status == 200 and isinstance(capability_models, list):
        for model in capability_models:
            capability_errors.extend(
                f"{model.get('id')}: {error}"
                for error in route_errors(model, config)
            )
    elif capabilities_status != 200:
        capability_errors.append(f"capabilities HTTP {capabilities_status}")

    return {
        "wrapper": name,
        "catalog_http": list_status,
        "catalog_models": len(models),
        "catalog_route_errors": list_errors,
        "detail_checked": len(detail_results),
        "detail_http_errors": detail_errors,
        "capabilities_http": capabilities_status,
        "capabilities_models": len(capability_models) if isinstance(capability_models, list) else 0,
        "capabilities_route_errors": capability_errors,
        "ok": list_status == 200 and not list_errors and len(detail_results) == len(models) and not detail_errors and capabilities_status == 200 and not capability_errors,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="/tmp/model_endpoint_metadata_validation.json")
    args = parser.parse_args()
    started = time.time()
    results = [validate_wrapper(name, config) for name, config in CONFIGS.items()]
    report = {
        "method": "python urllib metadata validation; no curl or provider secret required",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "duration_ms": round((time.time() - started) * 1000, 1),
        "results": results,
        "ok": all(item["ok"] for item in results),
    }
    pathlib.Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
