"""Authoritative port-plan checks without importing any provider implementation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.scope_policy

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "nvidia-python": 9101,
    "nous": 9102,
    "openrouter": 9103,
    "opencode": 9104,
    "blackbox": 9105,
    "model-registry": 9200,
}


def test_metadata_has_sequential_active_and_reserved_ports():
    data = json.loads((ROOT / "wrappers.json").read_text())
    wrappers = data["wrappers"]
    assert {name: wrappers[name]["port"] for name in EXPECTED} == EXPECTED
    assert data["metadata"]["dashboard_url"].endswith(":9106")
    assert "9100" in data["metadata"]["port_allocation"]
    assert data["metadata"]["port_9100_owner"].startswith("model-fetcher MCP HTTP server")
    assert "model_fetcher/mcp_server.py" in data["metadata"]["port_9100_owner"]
    assert wrappers["model-registry"]["service_kind"] == "internal"


def test_units_match_metadata_without_touching_provider_source():
    # Only active-wrapper and dashboard units are inspected; frozen provider
    # implementations and their repository files remain outside this test.
    for name in ("nvidia-python", "nous", "openrouter"):
        unit = ROOT / name / "systemd" / f"wrapper-{name}.service"
        assert f"--port {EXPECTED[name]}" in unit.read_text()
    unit = ROOT / "dashboard" / "systemd" / "wrapper-dashboard.service"
    assert "--port 9106" in unit.read_text()
    dashboard = json.loads((ROOT / "wrappers.json").read_text())["dashboard"]
    assert dashboard["port"] == 9106
    assert "--port 9106" in dashboard["run_command"]
