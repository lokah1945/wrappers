from __future__ import annotations

import json
from pathlib import Path

from common.model_state import ModelStateStore, classify_upstream_error, credential_fingerprint
from common.model.errors import classify_provider_error, load_provider_error_manifest


def test_account_hint_precedes_credential_scope(tmp_path: Path):
    store = ModelStateStore("nvidia", tmp_path / "state.db")
    state = store.record_error(
        "provider/model-a", "key-a", 404,
        {"detail": "Function fn: Not found for account acct-123"},
    )
    assert state["account_scope"] == credential_fingerprint("acct-123")
    assert state["account_scope"] != credential_fingerprint("key-a")


def test_provider_manifest_rule_is_used_for_account_scoped_error():
    manifest = load_provider_error_manifest("nvidia")
    result = classify_provider_error("nvidia", 404, "Function fn not found for account acct", manifest)
    assert result["state"] == "account_unavailable"
    assert result["account_scoped"] is True


def test_account_scoped_404_is_not_global_retirement():
    payload = {
        "status": 404,
        "title": "Not Found",
        "detail": "Function 'fn-1': Not found for account 'acct-1'",
    }
    result = classify_upstream_error(404, payload)
    assert result["state"] == "account_unavailable"
    assert result["reason_code"] == "NOT_DEPLOYED_FOR_ACCOUNT"


def test_only_explicit_provider_eol_is_global_retirement():
    assert classify_upstream_error(302, "redirect")['state'] != 'available'
    assert classify_upstream_error(410, "model reached its end of life")['state'] == 'globally_retired'
    assert classify_upstream_error(410, "temporary unavailable")['state'] != 'globally_retired'
    assert classify_upstream_error(429, "model capacity")['state'] == 'model_rate_limited'
    assert classify_upstream_error(503, "gateway timeout")['state'] == 'transient_failure'


def test_catalog_and_account_status_persist_across_store_instances(tmp_path: Path):
    db = tmp_path / "model-state.db"
    first = ModelStateStore("test-provider", db, catalog_ttl_sec=3600)
    first.upsert_catalog([
        {"id": "vendor/model-a", "owned_by": "vendor", "context_window": 128000},
        "vendor/model-b",
    ], source="test")
    first.record_error(
        "vendor/model-a", "secret-key-a", 404,
        {"detail": "Function not found for account 'acct-a'"},
        endpoint="/v1/chat/completions",
    )

    second = ModelStateStore("test-provider", db, catalog_ttl_sec=3600)
    ids = second.get_ids()
    assert ids == ["vendor/model-a", "vendor/model-b"]
    state = second.status_for("vendor/model-a")
    assert state["state"] == "account_unavailable"
    assert state["reason_code"] == "NOT_DEPLOYED_FOR_ACCOUNT"
    assert state["account_scope"] == credential_fingerprint("acct-a")


def test_catalog_does_not_delete_last_good_snapshot_on_empty_refresh(tmp_path: Path):
    db = tmp_path / "model-state.db"
    store = ModelStateStore("test-provider", db)
    store.upsert_catalog([{"id": "vendor/model-a"}], source="first")
    store.upsert_catalog([], source="empty-upstream")
    assert store.get_ids() == ["vendor/model-a"]


def test_unscoped_status_does_not_collapse_conflicting_accounts(tmp_path: Path):
    db = tmp_path / "model-state.db"
    store = ModelStateStore("test-provider", db)
    store.record_status("vendor/model-a", "account-a", "available", 200, "OK")
    store.record_status("vendor/model-a", "account-b", "account_unavailable", 404, "NOT_DEPLOYED_FOR_ACCOUNT")
    state = store.status_for("vendor/model-a")
    assert state["state"] == "mixed"
    assert state["reason_code"] == "MULTIPLE_ACCOUNT_OR_ENDPOINT_STATES"
    assert store.status_for("vendor/model-a", "account-a")["state"] == "available"
    assert store.status_for("vendor/model-a", "account-b")["state"] == "account_unavailable"


def test_effective_status_is_available_when_one_account_can_serve(tmp_path: Path):
    db = tmp_path / "model-state.db"
    store = ModelStateStore("test-provider", db)
    store.record_status("vendor/model-a", "account-a", "available", 200, "OK", endpoint="/v1/chat/completions")
    store.record_status("vendor/model-a", "account-b", "account_unavailable", 404, "NOT_DEPLOYED_FOR_ACCOUNT", endpoint="/v1/chat/completions")
    state = store.effective_status_for("vendor/model-a")
    assert state["state"] == "available"
    assert state["http_status"] == 200
    assert state["endpoint"] == "/v1/chat/completions"
    assert state["observed_state"] == "mixed"
    assert len(state["scoped_states"]) == 2


def test_effective_status_keeps_route_failure_unavailable_without_success(tmp_path: Path):
    db = tmp_path / "model-state.db"
    store = ModelStateStore("test-provider", db)
    store.record_status("vendor/model-a", "account-a", "wrong_route", 404, "UPSTREAM_ROUTE_NOT_FOUND", endpoint="/v1/chat/completions")
    state = store.effective_status_for("vendor/model-a")
    assert state["state"] == "wrong_route"
    assert state["http_status"] == 404


def test_provider_manifest_loads_from_model_registry():
    """2026-10-17 regression: the loader path must actually find the manifests
    under <repo>/model-registry/manifests/errors/ (parents[1] pointed at
    common/ which never contained them — every manifest silently loaded {},
    so provider rules never ran)."""
    manifest = load_provider_error_manifest("nvidia")
    assert isinstance(manifest, dict)
    assert manifest.get("rules"), (
        "nvidia manifest failed to load from model-registry/manifests/errors/"
    )


def test_nvidia_function_not_found_404_is_account_scoped():
    """NVIDIA's real not-deployed 404 phrasing (observed 2026-10-17 on
    nvidia/nemotron-3-ultra-550b-a55b) must classify account-scoped and
    retriable across keys — not generic UNKNOWN with no rotation."""
    payload = {
        "status": 404,
        "title": "Not Found",
        "detail": "Function id '948fe171-ce7a-4332-8bc0-5e14e90259f9' version "
                  "'null': Specified function in account 'SVfYs4' is not found",
    }
    manifest = load_provider_error_manifest("nvidia")
    result = classify_provider_error("nvidia", 404, payload, manifest)
    assert result["state"] == "account_unavailable"
    assert result["reason_code"] == "NOT_DEPLOYED_FOR_ACCOUNT"
    assert result["account_scoped"] is True
    assert result["retry_same_model"] is True
