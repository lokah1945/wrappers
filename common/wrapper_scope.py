"""Choose executable wrappers from metadata without visiting their directories.

Only ``status: production`` enables a provider. Pending providers remain frozen
even when named explicitly; client applications with the same name are unrelated.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = "model-registry"


class WrapperScopeError(ValueError):
    """Requested execution is outside the metadata-approved production scope."""


def load_wrappers(repo: Path | None = None) -> dict[str, dict]:
    path = (repo or ROOT) / "wrappers.json"
    try:
        entries = json.loads(path.read_text())["wrappers"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise WrapperScopeError(f"cannot load authoritative wrapper status: {path}") from exc
    if not isinstance(entries, dict) or not entries:
        raise WrapperScopeError(f"wrapper metadata is missing or empty: {path}")
    for name, config in entries.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            raise WrapperScopeError("wrapper metadata contains an invalid service name")
        if not isinstance(config, dict) or not isinstance(config.get("status"), str):
            raise WrapperScopeError(f"wrapper metadata has no valid status for {name}")
    return entries


def production_wrappers(repo: Path | None = None, *, include_registry: bool = True) -> tuple[str, ...]:
    return tuple(name for name, config in load_wrappers(repo).items()
                 if config["status"] == "production" and (include_registry or name != REGISTRY))


def frozen_wrappers(repo: Path | None = None) -> tuple[str, ...]:
    return tuple(name for name, config in load_wrappers(repo).items() if config["status"] != "production")


def require_production(name: str, repo: Path | None = None) -> str:
    name = "nvidia-python" if name == "nvidia" else name
    entries = load_wrappers(repo)
    if name not in entries:
        raise WrapperScopeError(f"unknown wrapper: {name}")
    if entries[name]["status"] != "production":
        raise WrapperScopeError(
            f"{name}: status={entries[name]['status']}; pending wrapper is frozen. "
            "Do not read, analyze, edit, test, install, deploy, or probe this provider. "
            "An explicit wrapper flag does not reactivate it.")
    return name


def select_wrappers(requested: Iterable[str] | None = None, repo: Path | None = None,
                    *, include_registry: bool = True) -> tuple[str, ...]:
    active = production_wrappers(repo, include_registry=include_registry)
    if include_registry:
        require_production(REGISTRY, repo)
    if requested is None:
        selected = active
    else:
        chosen = {require_production(name, repo) for name in requested}
        if not include_registry and REGISTRY in chosen:
            raise WrapperScopeError("model-registry is a dependency, not an inference wrapper")
        if include_registry:
            chosen.add(require_production(REGISTRY, repo))
        selected = tuple(name for name in active if name in chosen)
    if not selected:
        raise WrapperScopeError("no production wrapper is enabled in wrappers.json")
    return selected


def select_services(requested: Iterable[str] | None = None, repo: Path | None = None) -> dict[str, dict]:
    """Deployment adds the dashboard without treating it as an AI provider."""
    requested = tuple(requested) if requested is not None else None
    provider_names = tuple(name for name in requested if name != 'dashboard') if requested is not None else None
    selected = select_wrappers(provider_names, repo) if provider_names != () else ()
    entries = load_wrappers(repo)
    services = {name: entries[name] for name in selected}
    try:
        dashboard = json.loads(((repo or ROOT) / 'wrappers.json').read_text()).get('dashboard')
    except (OSError, ValueError, TypeError) as exc:
        raise WrapperScopeError('cannot load dashboard metadata') from exc
    if dashboard is not None:
        if not isinstance(dashboard, dict) or dashboard.get('status') != 'production':
            raise WrapperScopeError('dashboard is not enabled for deployment')
        port = dashboard.get('port')
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise WrapperScopeError('invalid dashboard port')
        if port in {config.get('port') for config in entries.values()}:
            raise WrapperScopeError('dashboard port conflicts with a reserved wrapper port')
        services['dashboard'] = dashboard
    elif requested and 'dashboard' in requested:
        raise WrapperScopeError('dashboard metadata is missing')
    return services


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", type=Path, default=ROOT)
    parser.add_argument("--wrapper", action="append")
    parser.add_argument("--inference-only", action="store_true")
    parser.add_argument("--include-dashboard", action="store_true", help="Select deployment services including the separate dashboard")
    parser.add_argument("--format", choices=("names", "install", "json"), default="names")
    args = parser.parse_args()
    try:
        if args.include_dashboard and args.inference_only:
            raise WrapperScopeError('dashboard is a control service, not an inference wrapper')
        if args.include_dashboard:
            entries = select_services(args.wrapper, args.repo_dir)
            selected = tuple(entries)
        else:
            selected = select_wrappers(args.wrapper, args.repo_dir, include_registry=not args.inference_only)
            entries = load_wrappers(args.repo_dir)
        for name in frozen_wrappers(args.repo_dir):
            print(f"[wrapper-scope] {name}: pending/frozen; excluded without accessing provider files", file=sys.stderr)
        if args.format == "json":
            print(json.dumps({name: entries[name] for name in selected}))
        else:
            ordered = sorted(selected, key=lambda name: name != REGISTRY) if args.format == "install" else selected
            for name in ordered:
                if args.format == "install":
                    port = entries[name].get("port")
                    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                        raise WrapperScopeError(f"invalid service port for {name}")
                    print(f"{name}|{name}|wrapper-{name}.service|http://127.0.0.1:{port}/health")
                else:
                    print(name)
    except WrapperScopeError as exc:
        print(f"[wrapper-scope][ERROR] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
