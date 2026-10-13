"""Keep frozen provider implementations outside collection and execution.

This hook inspects only test source and wrappers.json. Existing tests that mix
provider source inspections are skipped as a whole; parametrized tests continue
to exercise every production provider. Scope-policy tests use a dedicated marker
because their metadata-only checks must verify that pending flags are rejected.
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
from pathlib import Path

import pytest

from common.wrapper_scope import ROOT, frozen_wrappers


def pytest_configure(config):
    # Metrics are constructed during module import by some source fixtures.
    # Isolate before collection, not in a fixture that runs after imports.
    config._metrics_snapshot_state = tempfile.TemporaryDirectory(prefix='wrapper-pytest-metrics-')
    config._previous_metrics_snapshot = os.environ.get('METRICS_SNAPSHOT_FILE')
    os.environ['METRICS_SNAPSHOT_FILE'] = str(Path(config._metrics_snapshot_state.name) / 'metrics.json')
    config.addinivalue_line("markers", "scope_policy: metadata-only pending wrapper enforcement")
    frozen = frozen_wrappers()

    def prevent_frozen_reads(event, arguments):
        if event != "open" or not arguments or not isinstance(arguments[0], (str, bytes, os.PathLike)):
            return
        raw_path = os.fsdecode(arguments[0])
        # os.open(name, dir_fd=...) also emits a relative single name. Its
        # parent is unknown to the audit event, so do not mistake pytest's
        # temporary fixture cleanup for access to a repository provider.
        if not os.path.isabs(raw_path) and len(Path(raw_path).parts) == 1:
            return
        path = Path(os.path.abspath(raw_path))
        try:
            parts = path.relative_to(ROOT).parts
        except ValueError:
            return
        if parts and parts[0] in frozen:
            raise RuntimeError(f"{parts[0]}: frozen provider file access blocked before read by wrappers.json")

    # Last-line enforcement also catches an overlooked helper or a dynamically
    # constructed source path. The open audit event occurs before file I/O.
    sys.addaudithook(prevent_frozen_reads)
    for argument in config.args:
        path = Path(os.path.abspath(str(argument).split("::", 1)[0]))
        try:
            parts = path.relative_to(ROOT).parts
        except ValueError:
            continue
        if parts and parts[0] in frozen:
            raise pytest.UsageError(f"{parts[0]}: pending provider wrapper is frozen by wrappers.json; collection is excluded")


def pytest_ignore_collect(collection_path, config):
    frozen = frozen_wrappers()
    try:
        parts = Path(str(collection_path)).absolute().relative_to(ROOT).parts
    except ValueError:
        return None
    return bool(parts and parts[0] in frozen)


def pytest_unconfigure(config):
    previous = getattr(config, '_previous_metrics_snapshot', None)
    if previous is None:
        os.environ.pop('METRICS_SNAPSHOT_FILE', None)
    else:
        os.environ['METRICS_SNAPSHOT_FILE'] = previous
    state = getattr(config, '_metrics_snapshot_state', None)
    if state is not None:
        state.cleanup()


def _references_frozen(function: ast.FunctionDef | ast.AsyncFunctionDef, frozen: tuple[str, ...],
                       frozen_globals: set[str] | None = None) -> bool:
    # Decorators contain all parametrized providers; inspect only the body so
    # active parameter cases retain their complete assertions.
    for statement in function.body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in frozen or any(node.value.startswith(f"{name}/src/") for name in frozen):
                    return True
            if isinstance(node, ast.Name) and any(node.id.startswith(f"_load_{name}") for name in frozen):
                return True
            if isinstance(node, ast.Name) and node.id in (frozen_globals or set()):
                return True
    return False


def pytest_collection_modifyitems(config, items):
    frozen = frozen_wrappers()
    source_functions: dict[Path, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    provider_globals: dict[Path, set[str]] = {}
    for item in items:
        if item.get_closest_marker("scope_policy"):
            continue
        params = getattr(getattr(item, "callspec", None), "params", {})
        named_provider = any(value in frozen for value in params.values() if isinstance(value, str))
        name = getattr(item, "originalname", None) or item.name.split("[", 1)[0]
        named_test = any(f"_{provider}_" in f"_{name}_" for provider in frozen)
        path = Path(str(item.path))
        if path not in source_functions:
            tree = ast.parse(path.read_text())
            source_functions[path] = {node.name: node for node in ast.walk(tree)
                                      if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
            provider_globals[path] = set()
            for node in tree.body:
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                if node.value is not None and any(isinstance(child, ast.Constant) and child.value in frozen
                       for child in ast.walk(node.value) if isinstance(child, ast.Constant) and isinstance(child.value, str)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    provider_globals[path].update(target.id for target in targets if isinstance(target, ast.Name))
        function = source_functions[path].get(name)
        mixed_sources = function is not None and _references_frozen(function, frozen, provider_globals[path])
        if named_provider or named_test or mixed_sources:
            item.add_marker(pytest.mark.skip(
                reason="Pending provider wrapper is frozen by wrappers.json; source access and testing are excluded."
                       " Existing mixed-provider checks are deferred as a whole."))
