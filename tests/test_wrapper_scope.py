"""Production scope must reject frozen providers before any provider access."""
from __future__ import annotations

import json
import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from common import wrapper_scope

pytestmark = pytest.mark.scope_policy
ROOT = Path(__file__).resolve().parents[1]
ACTIVE = ("nvidia-python", "nous", "openrouter", "model-registry")


def metadata_only_repo(tmp_path):
    (tmp_path / "wrappers.json").write_text((ROOT / "wrappers.json").read_text())
    return tmp_path


def test_scope_uses_status_without_inspecting_provider_paths(monkeypatch, tmp_path):
    repo = metadata_only_repo(tmp_path)
    original = Path.read_text
    reads = []

    def read_metadata_only(path, *args, **kwargs):
        reads.append(path)
        assert path == repo / "wrappers.json", f"provider path was read: {path}"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_metadata_only)
    assert wrapper_scope.select_wrappers(repo=repo) == ACTIVE
    assert wrapper_scope.select_wrappers(["nous"], repo) == ("nous", "model-registry")
    for name in wrapper_scope.frozen_wrappers(repo):
        with pytest.raises(wrapper_scope.WrapperScopeError, match="frozen"):
            wrapper_scope.select_wrappers([name], repo)
    assert reads


def test_metadata_status_change_alone_controls_selection(tmp_path):
    repo = metadata_only_repo(tmp_path)
    metadata = json.loads((repo / "wrappers.json").read_text())
    metadata["wrappers"]["nous"]["status"] = "pending"
    (repo / "wrappers.json").write_text(json.dumps(metadata))
    assert wrapper_scope.production_wrappers(repo) == ("nvidia-python", "openrouter", "model-registry")
    with pytest.raises(wrapper_scope.WrapperScopeError, match="nous.*frozen"):
        wrapper_scope.require_production("nous", repo)


def test_registry_dependency_cannot_be_silently_omitted(tmp_path):
    repo = metadata_only_repo(tmp_path)
    metadata = json.loads((repo / "wrappers.json").read_text())
    metadata["wrappers"]["model-registry"]["status"] = "pending"
    (repo / "wrappers.json").write_text(json.dumps(metadata))
    with pytest.raises(wrapper_scope.WrapperScopeError, match="model-registry.*frozen"):
        wrapper_scope.select_wrappers(repo=repo)


def test_invalid_metadata_fails_closed(tmp_path):
    (tmp_path / "wrappers.json").write_text("{}")
    with pytest.raises(wrapper_scope.WrapperScopeError, match="authoritative"):
        wrapper_scope.select_wrappers(repo=tmp_path)


def test_collection_detects_mixed_provider_reads_without_importing_provider_code():
    spec = importlib.util.spec_from_file_location("scope_collection_hook", ROOT / "conftest.py")
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    frozen = wrapper_scope.frozen_wrappers()
    mixed = ast.parse("def test_mixed():\n    for wrapper in ('nous', 'blackbox'):\n        read(wrapper)\n").body[0]
    active = ast.parse("def test_active():\n    read('nous')\n").body[0]
    parametrized = ast.parse("@parametrize('wrapper', ('nous', 'blackbox'))\ndef test_param(wrapper):\n    read(wrapper)\n").body[0]
    assert hook._references_frozen(mixed, frozen)
    assert not hook._references_frozen(active, frozen)
    assert not hook._references_frozen(parametrized, frozen)
    global_loop = ast.parse("def test_global_loop():\n    for wrapper in WRAPPERS:\n        read(wrapper)\n").body[0]
    assert hook._references_frozen(global_loop, frozen, {"WRAPPERS"})


def test_frozen_file_guard_rejects_audit_event_before_file_io():
    # Emit only the Python audit event; no file is opened or inspected.
    for name in wrapper_scope.frozen_wrappers():
        with pytest.raises(RuntimeError, match="frozen provider file access blocked"):
            sys.audit("open", str(ROOT / name / "src/main.py"), "r", 0)


def test_audit_direct_url_cannot_bypass_frozen_status(tmp_path):
    metadata_only_repo(tmp_path)
    metadata = json.loads((tmp_path / "wrappers.json").read_text())["wrappers"]
    for name in wrapper_scope.frozen_wrappers(tmp_path):
        result = subprocess.run([sys.executable, str(ROOT / "productions/production_audit.py"),
                                 "--repo-dir", str(tmp_path), "--wrapper-url",
                                 f"http://localhost:{metadata[name]['port']}/v1"],
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        assert result.returncode == 2
        assert "pending wrapper is frozen" in result.stderr


@pytest.mark.parametrize("runner", ["tests/e2e_runtime/run_runtime_e2e.py", "tests/e2e_runtime/soak.py"])
@pytest.mark.parametrize("provider", ["blackbox", "opencode"])
def test_explicit_runtime_flags_cannot_start_frozen_provider(runner, provider):
    result = subprocess.run([sys.executable, str(ROOT / runner), "--wrapper", provider],
                            cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "pending wrapper is frozen" in result.stderr
    assert "mock upstream up" not in result.stdout


@pytest.mark.parametrize("provider", ["blackbox", "opencode"])
def test_installer_rejects_pending_before_any_provider_or_systemd_action(tmp_path, provider):
    metadata_only_repo(tmp_path)
    (tmp_path / "common").mkdir()
    (tmp_path / "common/wrapper_scope.py").write_text((ROOT / "common/wrapper_scope.py").read_text())
    installer = tmp_path / "install.sh"
    installer.write_text((ROOT / "install.sh").read_text())
    result = subprocess.run(["bash", str(installer), "--wrapper", provider, "--skip-deps"],
                            cwd=tmp_path, env={**os.environ, "PYTHON_BIN": sys.executable},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "pending wrapper is frozen" in result.stderr
    assert "missing wrapper dir" not in result.stderr
    assert not any(path.is_dir() for path in tmp_path.iterdir() if path.name != "common")


@pytest.mark.parametrize("provider", ["blackbox", "opencode"])
def test_audit_rejects_explicit_pending_before_probes(tmp_path, provider):
    metadata_only_repo(tmp_path)
    result = subprocess.run([sys.executable, str(ROOT / "productions/production_audit.py"),
                             "--repo-dir", str(tmp_path), "--required-wrapper", provider],
                            cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "pending wrapper is frozen" in result.stderr
    assert not (tmp_path / "productions").exists()


def test_supervisor_lists_active_metadata_with_registry_dependency_first():
    result = subprocess.run([sys.executable, str(ROOT / "start_wrappers.py"), "--list"],
                            cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    services = [line.split(":", 1)[0] for line in result.stdout.splitlines()
                if ":pending" not in line and line.rsplit(":", 1)[-1].isdigit()]
    assert services == ["model-registry", "nvidia-python", "nous", "openrouter", "dashboard"]
    assert "PID" not in result.stdout


def test_dashboard_deployment_is_separate_from_inference_scope(tmp_path):
    repo = metadata_only_repo(tmp_path)
    assert tuple(wrapper_scope.select_services(repo=repo)) == (*ACTIVE, 'dashboard')
    assert tuple(wrapper_scope.select_services(['dashboard'], repo)) == ('dashboard',)
    assert wrapper_scope.production_wrappers(repo, include_registry=False) == ('nvidia-python', 'nous', 'openrouter')
    for name in wrapper_scope.frozen_wrappers(repo):
        with pytest.raises(wrapper_scope.WrapperScopeError, match='frozen'):
            wrapper_scope.select_services(['dashboard', name], repo)


def test_dashboard_port_cannot_use_a_frozen_reserved_slot(tmp_path):
    repo = metadata_only_repo(tmp_path)
    metadata = json.loads((repo / 'wrappers.json').read_text())
    frozen_name = wrapper_scope.frozen_wrappers(repo)[0]
    metadata['dashboard']['port'] = metadata['wrappers'][frozen_name]['port']
    (repo / 'wrappers.json').write_text(json.dumps(metadata))
    with pytest.raises(wrapper_scope.WrapperScopeError, match='reserved wrapper port'):
        wrapper_scope.select_services(repo=repo)


@pytest.mark.parametrize("provider", ["blackbox", "opencode"])
def test_supervisor_rejects_pending_before_process_creation(provider):
    result = subprocess.run([sys.executable, str(ROOT / "start_wrappers.py"), "--wrapper", provider],
                            cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "pending wrapper is frozen" in result.stderr
    assert "Starting" not in result.stdout
