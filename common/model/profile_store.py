"""Persistent authoritative model profile store."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from .contracts import AliasBinding, CapabilityProfile, LimitProfile, ModelProfile, ProtocolProfile


class ModelProfileStore:
    SCHEMA_VERSION = 1

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._initialized = False
        self._write_lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        if not self._initialized:
            self._init(conn)
        return conn

    def _init(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS model_profiles (
                provider       TEXT NOT NULL,
                canonical_id   TEXT NOT NULL,
                profile_json   TEXT NOT NULL,
                profile_revision TEXT NOT NULL DEFAULT '',
                updated_at     REAL NOT NULL,
                PRIMARY KEY(provider, canonical_id)
            );
            CREATE TABLE IF NOT EXISTS model_profile_schema (
                version INTEGER PRIMARY KEY,
                applied_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS model_alias_bindings (
                provider TEXT NOT NULL,
                scope_type TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                alias TEXT NOT NULL,
                canonical_target TEXT NOT NULL,
                revision TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'manifest',
                updated_at REAL NOT NULL,
                PRIMARY KEY(provider, scope_type, scope_id, alias)
            );
        """)
        conn.execute(
            "INSERT OR IGNORE INTO model_profile_schema(version, applied_at) VALUES(?,?)",
            (self.SCHEMA_VERSION, time.time()),
        )
        conn.commit()
        self._initialized = True

    def save(self, profile: ModelProfile) -> None:
        self.save_many([profile])

    def save_many(self, profiles: list[ModelProfile]) -> None:
        if not profiles:
            return
        with self._write_lock:
            conn = self._connect()
            try:
                for profile in profiles:
                    conn.execute(
                        """INSERT INTO model_profiles(provider,canonical_id,profile_json,profile_revision,updated_at)
                           VALUES(?,?,?,?,?)
                           ON CONFLICT(provider,canonical_id) DO UPDATE SET
                             profile_json=excluded.profile_json,
                             profile_revision=excluded.profile_revision,
                             updated_at=excluded.updated_at""",
                        (
                            profile.provider,
                            profile.canonical_id,
                            json.dumps(profile.to_dict(), ensure_ascii=False, default=str),
                            profile.profile_revision,
                            time.time(),
                        ),
                    )
                conn.commit()
            finally:
                conn.close()

    def save_alias(self, binding: AliasBinding, provider: str) -> None:
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT INTO model_alias_bindings
                       (provider,scope_type,scope_id,alias,canonical_target,revision,source,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(provider,scope_type,scope_id,alias) DO UPDATE SET
                         canonical_target=excluded.canonical_target,
                         revision=excluded.revision,
                         source=excluded.source,
                         updated_at=excluded.updated_at""",
                    (provider, binding.scope_type, binding.scope_id, binding.alias,
                     binding.canonical_target, binding.revision, binding.source, time.time()),
                )
                conn.commit()
            finally:
                conn.close()

    def load_aliases(self, provider: str) -> list[AliasBinding]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT scope_type,scope_id,alias,canonical_target,revision,source
                   FROM model_alias_bindings WHERE provider=?""",
                (provider,),
            ).fetchall()
            return [AliasBinding(
                scope_type=row["scope_type"], scope_id=row["scope_id"],
                alias=row["alias"], canonical_target=row["canonical_target"],
                revision=row["revision"], source=row["source"],
            ) for row in rows]
        finally:
            conn.close()

    def load(self, provider: str) -> list[ModelProfile]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT profile_json FROM model_profiles WHERE provider=? ORDER BY canonical_id",
                (provider,),
            ).fetchall()
            profiles = []
            for row in rows:
                try:
                    data = json.loads(row["profile_json"])
                    caps = CapabilityProfile(**(data.get("capabilities") or {}))
                    limits = LimitProfile(**(data.get("limits") or {}))
                    protocols = tuple(ProtocolProfile(**item) for item in (data.get("protocols") or []))
                    profiles.append(ModelProfile(
                        canonical_id=data["canonical_id"],
                        provider=data["provider"],
                        provider_model_id=data["provider_model_id"],
                        lifecycle_state=data.get("lifecycle_state", "active"),
                        profile_revision=data.get("profile_revision", ""),
                        catalog_revision=data.get("catalog_revision", ""),
                        capabilities=caps,
                        limits=limits,
                        protocols=protocols,
                        request_rules=data.get("request_rules") or {},
                        policy=data.get("policy") or {},
                        provenance=data.get("provenance") or {},
                    ))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
            return profiles
        finally:
            conn.close()
