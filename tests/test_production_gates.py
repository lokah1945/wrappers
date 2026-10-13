"""Regression checks for safe and honest production validation gates."""
from __future__ import annotations

import argparse
import importlib.util
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AUDIT = load_module("production_audit_gates", ROOT / "productions/production_audit.py")
ISOLATION = load_module("gate_process_isolation", ROOT / "tests/e2e_runtime/process_isolation.py")


@pytest.mark.parametrize("status,body,expected", [
    (200, {"ready": False}, False), (200, {}, False), (200, None, False),
    (503, {"ready": True}, False), (200, {"ready": "true"}, False),
    (200, {"ready": True}, True),
])
def test_readiness_requires_a_positive_contract(status, body, expected):
    assert AUDIT.readiness_ok(status, body) is expected


def test_isolated_tests_cannot_recover_production_credentials(monkeypatch, tmp_path):
    for name in ("OPENROUTER_API_KEY_1", "NOUS_API_KEY", "AUTH_PATH", "BEARER_TOKEN", "MODEL_STATE_DB"):
        monkeypatch.setenv(name, "production-value")
    env = AUDIT.isolated_test_environment(tmp_path)
    assert "OPENROUTER_API_KEY_1" not in env
    assert "NOUS_API_KEY" not in env
    assert env["BEARER_TOKEN"] == ""
    assert env["MODEL_STATE_DB"] == str(tmp_path / "model-state.db")
    assert env["METRICS_SNAPSHOT_FILE"] == str(tmp_path / "metrics-snapshot.json")
    assert env["AUTH_PATH"] == str(tmp_path / "missing-auth.json")
    runtime_env = ISOLATION.isolated_environment()
    assert "production-value" not in runtime_env.values()
    assert runtime_env["MODEL_REGISTRY_URL"] == ""
    assert not Path(runtime_env["AUTH_PATH"]).exists()
    assert ROOT not in Path(runtime_env["MODEL_STATE_DB"]).parents
    assert ROOT not in Path(runtime_env["METRICS_SNAPSHOT_FILE"]).parents


def test_port_conflict_does_not_touch_existing_listener():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(RuntimeError, match="already in use"):
            ISOLATION.assert_ports_free(port)
        assert listener.fileno() >= 0


def prepared_audit(monkeypatch, tmp_path, **options):
    (tmp_path / "wrappers.json").write_text((ROOT / "wrappers.json").read_text())
    installer = tmp_path / "install.sh"
    installer.write_text("#!/bin/sh\n")
    installer.chmod(0o755)
    (tmp_path / "runtime").mkdir()
    (tmp_path / "common").mkdir()
    commits = {}
    for index, name in enumerate(AUDIT.SERVICE_TARGETS):
        (tmp_path / name).mkdir()
        (tmp_path / name / ".env").write_text("")
        commits[name] = f"{index + 1:040x}"
        (tmp_path / "runtime" / f"{name}.commit").write_text(commits[name])
    args = argparse.Namespace(
        repo_dir=tmp_path, registry_url="http://registry.local", required_wrapper=list(AUDIT.SERVICE_TARGETS),
        require_registry=False, api_key_env="LOCAL_TEST_TOKEN", run_tests=False, run_smoke=False,
        run_load=False, smoke_all=False, wrapper_url=None, model=None, surface=None,
        requests=1, concurrency=1, report_dir=tmp_path / "reports")
    for key, value in options.items():
        setattr(args, key, value)
    monkeypatch.setattr(AUDIT, "parse_args", lambda: args)
    monkeypatch.setattr(AUDIT.shutil, "which", lambda command: "/bin/systemctl")

    def command(self, cmd, **kwargs):
        if cmd[:2] == ["systemctl", "--user"]:
            return 0, "active"
        if cmd[1:3] == ["rev-parse", "HEAD"]:
            return 0, commits["model-registry"]
        if cmd[1:3] == ["branch", "--show-current"]:
            return 0, "main"
        if cmd[1:3] == ["remote", "get-url"]:
            return 0, "configured-origin"
        return 0, ""

    requests = []

    def http(self, url, **kwargs):
        requests.append((url, kwargs))
        if kwargs.get("method") == "POST":
            return 200, 1.0, {"model": kwargs["payload"]["model"]}, ""
        if url.endswith("/ready"):
            return 200, 1.0, {"ready": True}, ""
        name = "model-registry" if "registry.local" in url else next(
            name for name, port in AUDIT.SERVICE_TARGETS.items() if f":{port}/" in url)
        return 200, 1.0, {"status": "ok", "git_commit": commits[name]}, ""

    monkeypatch.setattr(AUDIT.Audit, "command", command)
    monkeypatch.setattr(AUDIT.Audit, "http", http)
    return args, requests


def test_preflight_checks_production_services_with_their_own_commit(monkeypatch, tmp_path):
    _, requests = prepared_audit(monkeypatch, tmp_path)
    assert AUDIT.main() == 2  # Different deployed revisions must be reviewed.
    report = next((tmp_path / "reports").glob("*.md")).read_text()
    assert "**FAIL**" not in report
    assert "systemd wrapper-nvidia-python.service" in report
    assert "systemd wrapper-openrouter.service" in report
    assert "readiness openrouter" in report
    assert len([url for url, _ in requests if url.endswith("/ready")]) == 3
    metadata = json.loads((tmp_path / "wrappers.json").read_text())["wrappers"]
    for name, config in metadata.items():
        if config["status"] == "pending":
            assert f"provider {name}" in report
            assert not any(f":{config['port']}/" in url for url, _ in requests)
            assert f"systemd wrapper-{name}.service" not in report


def test_explicit_single_wrapper_smoke_uses_only_operator_model(monkeypatch, tmp_path):
    _, requests = prepared_audit(monkeypatch, tmp_path, run_smoke=True,
                                 wrapper_url="http://staging/v1", model="requested/exact")
    monkeypatch.setenv("LOCAL_TEST_TOKEN", "fake-local-token")
    AUDIT.main()
    posts = [(url, kwargs) for url, kwargs in requests if kwargs.get("method") == "POST"]
    assert len(posts) == 1
    assert posts[0][0] == "http://staging/v1/chat/completions"
    assert posts[0][1]["payload"]["model"] == "requested/exact"


@pytest.mark.parametrize("missing", ["model", "token"])
def test_missing_smoke_input_prevents_any_inference(monkeypatch, tmp_path, missing):
    _, requests = prepared_audit(monkeypatch, tmp_path, run_smoke=True, smoke_all=True,
                                 model=None if missing == "model" else "requested/exact")
    if missing == "model":
        monkeypatch.setenv("LOCAL_TEST_TOKEN", "fake-local-token")
    else:
        monkeypatch.delenv("LOCAL_TEST_TOKEN", raising=False)
    AUDIT.main()
    assert not any(kwargs.get("method") == "POST" for _, kwargs in requests)


def test_load_exit_failure_cannot_pass_or_publish_response_bodies(monkeypatch, tmp_path):
    monkeypatch.setattr(AUDIT.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=1, stdout="ok=1/1 error=0\nsample_err=private-provider-response\n"))
    audit = AUDIT.Audit(tmp_path)
    AUDIT._run_load(audit, tmp_path, "http://staging/v1", "requested/exact", "fake", 1, 1)
    assert audit.failures == 1
    assert "private-provider-response" not in "\n".join(audit.lines)
