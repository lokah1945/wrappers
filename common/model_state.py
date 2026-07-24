#!/usr/bin/env python3
"""Persistent model catalog and account-scoped availability state.

The wrappers intentionally keep catalog discovery separate from runtime
availability.  A provider's public /models response answers "does this model
exist in the provider catalog?"; a request/probe using a credential answers
"can this account use it?".  Conflating those two questions caused the
NVIDIA Kimi false-retirement incident.

This module is dependency-free and safe to import from all four wrappers.
SQLite is used only for small metadata/state writes; raw credentials are never
stored, only a SHA-256 fingerprint.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from .model.errors import classify_upstream_error, error_text
from .model.sanitize import sanitize_error_detail
from .model.validation import validate_catalog_entries, validate_observation

SCHEMA_VERSION = 1


def credential_fingerprint(value: str | None) -> str:
    """Return a non-reversible stable credential/account scope identifier."""
    if not value:
        return "unknown"
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:24]


class ModelStateStore:
    """Small per-wrapper SQLite store for catalog and scoped model state."""

    def __init__(self, provider: str, db_path: str | os.PathLike[str] | None = None,
                 catalog_ttl_sec: int | None = None):
        self.provider = str(provider)
        default_path = Path.cwd() / "model-state.db"
        self.db_path = Path(db_path or os.environ.get("MODEL_STATE_DB") or default_path)
        self.catalog_ttl_sec = int(catalog_ttl_sec or os.environ.get("MODEL_CATALOG_TTL_SEC", "21600"))
        self.status_write_interval_sec = int(os.environ.get("MODEL_STATUS_WRITE_INTERVAL_SEC", "60"))
        self._initialized = False
        self._last_status_write: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        if not self._initialized:
            self._init(conn)
        return conn

    def _init(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS model_catalog (
                provider       TEXT NOT NULL,
                model_id       TEXT NOT NULL,
                metadata_json  TEXT NOT NULL DEFAULT '{}',
                source         TEXT NOT NULL DEFAULT 'upstream',
                catalog_seen_at REAL NOT NULL,
                expires_at     REAL NOT NULL,
                is_listed      INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(provider, model_id)
            );
            CREATE INDEX IF NOT EXISTS idx_model_catalog_expiry
                ON model_catalog(provider, expires_at);
            CREATE TABLE IF NOT EXISTS model_account_status (
                provider              TEXT NOT NULL,
                account_scope         TEXT NOT NULL,
                model_id              TEXT NOT NULL,
                endpoint              TEXT NOT NULL DEFAULT '',
                state                 TEXT NOT NULL,
                reason_code           TEXT NOT NULL DEFAULT '',
                reason_detail         TEXT NOT NULL DEFAULT '',
                http_status           INTEGER NOT NULL DEFAULT 0,
                checked_at            REAL NOT NULL,
                consecutive_failures  INTEGER NOT NULL DEFAULT 0,
                consecutive_successes INTEGER NOT NULL DEFAULT 0,
                next_retry_at         REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(provider, account_scope, model_id, endpoint)
            );
            CREATE INDEX IF NOT EXISTS idx_model_status_model
                ON model_account_status(provider, model_id);
            CREATE TABLE IF NOT EXISTS model_state_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                provider      TEXT NOT NULL,
                account_scope TEXT NOT NULL,
                model_id      TEXT NOT NULL,
                state         TEXT NOT NULL,
                reason_code   TEXT NOT NULL DEFAULT '',
                http_status   INTEGER NOT NULL DEFAULT 0,
                detail        TEXT NOT NULL DEFAULT '',
                created_at    REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS model_state_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        conn.execute(
            "INSERT OR REPLACE INTO model_state_meta(key,value) VALUES('schema_version',?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
        self._initialized = True

    @staticmethod
    def _model_id(entry: Any) -> str:
        if isinstance(entry, str):
            return entry.strip()
        if isinstance(entry, dict):
            return str(entry.get("id") or entry.get("model") or "").strip()
        return ""

    def upsert_catalog(self, models: Iterable[Any], source: str = "upstream",
                       ttl_sec: int | None = None) -> list[str]:
        now = time.time()
        ttl = int(ttl_sec or self.catalog_ttl_sec)
        entries = validate_catalog_entries(models)
        conn = self._connect()
        ids: list[str] = []
        try:
            for entry in entries:
                mid = self._model_id(entry)
                if not mid:
                    continue
                metadata = entry if isinstance(entry, dict) else {"id": mid}
                conn.execute(
                    """INSERT INTO model_catalog
                       (provider,model_id,metadata_json,source,catalog_seen_at,expires_at,is_listed)
                       VALUES(?,?,?,?,?,?,1)
                       ON CONFLICT(provider,model_id) DO UPDATE SET
                         metadata_json=excluded.metadata_json,
                         source=excluded.source,
                         catalog_seen_at=excluded.catalog_seen_at,
                         expires_at=excluded.expires_at,
                         is_listed=1""",
                    (self.provider, mid, json.dumps(metadata, ensure_ascii=False, default=str),
                     source, now, now + ttl),
                )
                ids.append(mid)
            if ids:
                # Do not delete absent entries immediately: upstream outages and
                # transient catalog changes must not erase the last good snapshot.
                conn.commit()
        finally:
            conn.close()
        return sorted(set(ids))

    def get_catalog(self, fresh_only: bool = False) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            query = "SELECT * FROM model_catalog WHERE provider=? AND is_listed=1"
            params: list[Any] = [self.provider]
            if fresh_only:
                query += " AND expires_at >= ?"
                params.append(time.time())
            query += " ORDER BY model_id"
            rows = conn.execute(query, params).fetchall()
            result = []
            for row in rows:
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except Exception:
                    metadata = {"id": row["model_id"]}
                if not isinstance(metadata, dict):
                    metadata = {"id": row["model_id"]}
                metadata.setdefault("id", row["model_id"])
                # Internal timestamps remain in SQLite; never leak them as
                # provider model metadata unless a management endpoint asks
                # for them explicitly.
                result.append(metadata)
            return result
        finally:
            conn.close()

    def get_ids(self, fresh_only: bool = False) -> list[str]:
        return [str(item.get("id")) for item in self.get_catalog(fresh_only) if item.get("id")]

    def catalog_age_sec(self) -> Optional[float]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT MAX(catalog_seen_at) AS seen FROM model_catalog WHERE provider=? AND is_listed=1",
                (self.provider,),
            ).fetchone()
            if not row or row["seen"] is None:
                return None
            return max(0.0, time.time() - float(row["seen"]))
        finally:
            conn.close()

    def record_status(self, model_id: str, account_scope: str, state: str,
                      status_code: int = 0, reason_code: str = "",
                      reason_detail: str = "", endpoint: str = "",
                      retry_after_sec: int = 0) -> dict[str, Any]:
        validated = validate_observation(model_id, account_scope, state, reason_code, reason_detail, endpoint)
        model_id = validated["model_id"]
        account_scope = validated["account_scope"]
        state = validated["state"]
        reason_code = validated["reason_code"]
        endpoint = validated["endpoint"]
        now = time.time()
        reason_detail = sanitize_error_detail(validated["reason_detail"])
        write_key = (account_scope, model_id, endpoint)
        cached_write = self._last_status_write.get(write_key)
        if cached_write:
            previous_at, previous = cached_write
            if previous.get("state") == state and now - previous_at < self.status_write_interval_sec:
                return dict(previous)
        conn = self._connect()
        try:
            old = conn.execute(
                """SELECT * FROM model_account_status
                   WHERE provider=? AND account_scope=? AND model_id=? AND endpoint=?""",
                (self.provider, account_scope, model_id, endpoint),
            ).fetchone()
            good = state == "available"
            failures = 0 if good else (int(old["consecutive_failures"]) + 1 if old else 1)
            successes = (int(old["consecutive_successes"]) + 1 if old else 1) if good else 0
            next_retry = now + max(0, int(retry_after_sec or 0))
            conn.execute(
                """INSERT INTO model_account_status
                   (provider,account_scope,model_id,endpoint,state,reason_code,reason_detail,
                    http_status,checked_at,consecutive_failures,consecutive_successes,next_retry_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(provider,account_scope,model_id,endpoint) DO UPDATE SET
                    state=excluded.state,reason_code=excluded.reason_code,
                    reason_detail=excluded.reason_detail,http_status=excluded.http_status,
                    checked_at=excluded.checked_at,consecutive_failures=excluded.consecutive_failures,
                    consecutive_successes=excluded.consecutive_successes,next_retry_at=excluded.next_retry_at""",
                (self.provider, account_scope, model_id, endpoint, state, reason_code,
                 str(reason_detail or "")[:4000], int(status_code or 0), now, failures,
                 successes, next_retry),
            )
            conn.execute(
                """INSERT INTO model_state_events
                   (provider,account_scope,model_id,state,reason_code,http_status,detail,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (self.provider, account_scope, model_id, state, reason_code,
                 int(status_code or 0), str(reason_detail or "")[:4000], now),
            )
            conn.commit()
            result = {
                "state": state,
                "reason_code": reason_code,
                "reason_detail": str(reason_detail or "")[:4000],
                "last_status": int(status_code or 0),
                "checked_at": now,
                "consecutive_failures": failures,
                "consecutive_successes": successes,
                "account_scope": account_scope,
            }
            self._last_status_write[write_key] = (now, dict(result))
            return result
        finally:
            conn.close()

    async def record_status_async(self, *args, **kwargs) -> dict[str, Any]:
        """Run SQLite status persistence off the wrapper event loop."""
        return await asyncio.to_thread(self.record_status, *args, **kwargs)

    async def record_error_async(self, *args, **kwargs) -> dict[str, Any]:
        """Run SQLite error persistence off the wrapper event loop."""
        return await asyncio.to_thread(self.record_error, *args, **kwargs)

    async def upsert_catalog_async(self, *args, **kwargs) -> list[str]:
        """Run catalog persistence off the wrapper event loop."""
        return await asyncio.to_thread(self.upsert_catalog, *args, **kwargs)

    def status_map(self, account_scope: str | None = None,
                   endpoint: str | None = None) -> dict[str, dict[str, Any]]:
        """Return scoped status without collapsing conflicting accounts.

        Without a scope filter, a model observed as available for one account
        and unavailable for another is returned as ``mixed`` rather than being
        misrepresented by whichever row happened to be newest.
        """
        conn = self._connect()
        try:
            query = """SELECT * FROM model_account_status
                       WHERE provider=?"""
            params: list[Any] = [self.provider]
            if account_scope:
                query += " AND account_scope=?"
                params.append(account_scope)
            if endpoint:
                query += " AND endpoint=?"
                params.append(endpoint)
            query += " ORDER BY checked_at DESC"
            rows = conn.execute(query, params).fetchall()
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                grouped.setdefault(row["model_id"], []).append(dict(row))

            result: dict[str, dict[str, Any]] = {}
            for mid, entries in grouped.items():
                states = {entry.get("state") for entry in entries}
                scopes = {entry.get("account_scope") for entry in entries}
                endpoints = {entry.get("endpoint") for entry in entries}
                newest = max(entries, key=lambda entry: entry.get("checked_at", 0))
                if len(states) > 1:
                    result[mid] = {
                        "model_id": mid,
                        "state": "mixed",
                        "reason_code": "MULTIPLE_ACCOUNT_OR_ENDPOINT_STATES",
                        "reason_detail": "Availability differs by account or endpoint",
                        "http_status": 0,
                        "checked_at": newest.get("checked_at", 0),
                        "scope_count": len(scopes),
                        "endpoint_count": len(endpoints),
                        "scoped_states": [
                            {
                                "account_scope": entry.get("account_scope"),
                                "endpoint": entry.get("endpoint"),
                                "state": entry.get("state"),
                                "checked_at": entry.get("checked_at"),
                            }
                            for entry in entries
                        ],
                    }
                else:
                    result[mid] = dict(newest)
                    if len(scopes) > 1:
                        result[mid]["account_scope"] = "multiple"
                    if len(endpoints) > 1:
                        result[mid]["endpoint"] = "multiple"
            return result
        finally:
            conn.close()

    def status_for(self, model_id: str, account_scope: str | None = None) -> Optional[dict[str, Any]]:
        return self.status_map(account_scope).get(model_id)

    def record_error(self, model_id: str, account_credential: str | None,
                     status_code: int, payload: Any, endpoint: str = "") -> dict[str, Any]:
        classification = classify_upstream_error(status_code, payload)
        detail = sanitize_error_detail(payload)
        scope = credential_fingerprint(account_credential)
        return self.record_status(
            model_id=model_id,
            account_scope=scope,
            state=classification["state"],
            status_code=status_code,
            reason_code=classification["reason_code"],
            reason_detail=detail,
            endpoint=endpoint,
        )



__all__ = [
    "ModelStateStore",
    "classify_upstream_error",
    "credential_fingerprint",
    "error_text",
]
