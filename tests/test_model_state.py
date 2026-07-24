from __future__ import annotations

import json
from pathlib import Path

from common.model_state import ModelStateStore, classify_upstream_error, credential_fingerprint


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
    assert classify_upstream_error(410, "model reached its end of life")['state'] == 'globally_retired'
    assert classify_upstream_error(410, "temporary unavailable")['state'] != 'globally_retired'
    assert classify_upstream_error(429, "model capacity")['state'] == 'rate_limited'
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
    assert state["account_scope"] == credential_fingerprint("secret-key-a")


def test_catalog_does_not_delete_last_good_snapshot_on_empty_refresh(tmp_path: Path):
    db = tmp_path / "model-state.db"
    store = ModelStateStore("test-provider", db)
    store.upsert_catalog([{"id": "vendor/model-a"}], source="first")
    store.upsert_catalog([], source="empty-upstream")
    assert store.get_ids() == ["vendor/model-a"]
