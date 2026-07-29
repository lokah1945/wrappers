#!/usr/bin/env python3
"""
catalog_queries.py — SQLite-backed model catalog queries.

Provides search, get, list operations against the NVIDIA NIM + multi-provider catalog.
Used by all wrappers via common/catalog_integration.py
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

DEFAULT_DB = os.environ.get(
    "CATALOG_DB",
    str(Path(__file__).parent / "data" / "active_nvidia_nim.sqlite3")
)


def open_db(db_path: str = None):
    """Open SQLite database in read-only mode with row factory."""
    path = db_path or DEFAULT_DB
    if not os.path.exists(path):
        raise FileNotFoundError(f"Catalog DB not found: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert sqlite3.Row to dict."""
    return {k: row[k] for k in row.keys()}


def search_models(
    db,
    query: str = None,
    modality: str = None,
    tier: str = None,
    working_only: bool = False,
    free_only: bool = False,
    publisher: str = None,
    limit: int = 50,
) -> list[dict]:
    """Search models in catalog with filters."""
    sql = "SELECT * FROM models WHERE 1=1"
    params = []

    if query:
        sql += " AND (id LIKE ? OR name LIKE ? OR description LIKE ?)"
        q = f"%{query}%"
        params.extend([q, q, q])

    if modality:
        sql += " AND modality = ?"
        params.append(modality)

    if tier:
        sql += " AND tier = ?"
        params.append(tier)

    if working_only:
        sql += " AND availability_state = 'available'"

    if free_only:
        sql += " AND (id LIKE '%:free' OR id LIKE '%-free' OR pricing_prompt = 0)"

    if publisher:
        sql += " AND publisher = ?"
        params.append(publisher)

    sql += " ORDER BY CASE WHEN availability_state = 'available' THEN 0 ELSE 1 END, id LIMIT ?"
    params.append(limit)

    cur = db.execute(sql, params)
    return [_row_to_dict(row) for row in cur.fetchall()]


def get_model(db, catalog_id: str) -> Optional[dict]:
    """Get single model by catalog_id."""
    cur = db.execute("SELECT * FROM models WHERE id = ?", (catalog_id,))
    row = cur.fetchone()
    return _row_to_dict(row) if row else None


def get_provider_model(db, provider: str, model_id: str) -> Optional[dict]:
    """Get model by provider prefix and model id."""
    cur = db.execute(
        "SELECT * FROM models WHERE id = ? OR id = ?",
        (f"{provider}/{model_id}", model_id)
    )
    row = cur.fetchone()
    return _row_to_dict(row) if row else None


def list_providers(db) -> list[dict]:
    """List all unique providers in catalog."""
    cur = db.execute("""
        SELECT DISTINCT provider, COUNT(*) as model_count
        FROM models
        WHERE provider IS NOT NULL
        GROUP BY provider
        ORDER BY model_count DESC
    """)
    return [{"provider": row["provider"], "model_count": row["model_count"]} for row in cur.fetchall()]


def search_provider_models(
    db,
    provider: str = None,
    query: str = None,
    free_only: bool = False,
    limit: int = 50,
) -> list[dict]:
    """Search models within a specific provider."""
    sql = "SELECT * FROM models WHERE 1=1"
    params = []

    if provider:
        sql += " AND provider = ?"
        params.append(provider)

    if query:
        sql += " AND (id LIKE ? OR name LIKE ? OR description LIKE ?)"
        q = f"%{query}%"
        params.extend([q, q, q])

    if free_only:
        sql += " AND (id LIKE '%:free' OR id LIKE '%-free' OR pricing_prompt = 0)"

    sql += " ORDER BY CASE WHEN availability_state = 'available' THEN 0 ELSE 1 END, id LIMIT ?"
    params.append(limit)

    cur = db.execute(sql, params)
    return [_row_to_dict(row) for row in cur.fetchall()]


def stats(db) -> dict:
    """Get catalog statistics."""
    total = db.execute("SELECT COUNT(*) as c FROM models").fetchone()["c"]
    available = db.execute("SELECT COUNT(*) as c FROM models WHERE availability_state = 'available'").fetchone()["c"]
    free = db.execute("SELECT COUNT(*) as c FROM models WHERE id LIKE '%:free' OR id LIKE '%-free'").fetchone()["c"]
    providers = db.execute("SELECT COUNT(DISTINCT provider) as c FROM models WHERE provider IS NOT NULL").fetchone()["c"]
    return {
        "total_models": total,
        "available_models": available,
        "free_models": free,
        "providers": providers,
    }


def upsert_catalog(db_path: str, models: list[dict], source: str = "unknown"):
    """Insert or update catalog entries (for write mode)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for m in models:
            # Flatten known fields
            conn.execute("""
                INSERT INTO models (
                    id, canonical_slug, hugging_face_id, name, created, description,
                    context_length, modality, input_modalities, output_modalities,
                    tokenizer, instruct_type, pricing_prompt, pricing_completion,
                    top_provider_context_length, top_provider_max_completion_tokens,
                    top_provider_is_moderated, supported_parameters, default_parameters,
                    supported_voices, knowledge_cutoff, expiration_date,
                    provider, publisher, tier, architecture, availability_state,
                    reason_code, checked_at, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    canonical_slug=excluded.canonical_slug,
                    hugging_face_id=excluded.hugging_face_id,
                    name=excluded.name,
                    created=excluded.created,
                    description=excluded.description,
                    context_length=excluded.context_length,
                    modality=excluded.modality,
                    input_modalities=excluded.input_modalities,
                    output_modalities=excluded.output_modalities,
                    tokenizer=excluded.tokenizer,
                    instruct_type=excluded.instruct_type,
                    pricing_prompt=excluded.pricing_prompt,
                    pricing_completion=excluded.pricing_completion,
                    top_provider_context_length=excluded.top_provider_context_length,
                    top_provider_max_completion_tokens=excluded.top_provider_max_completion_tokens,
                    top_provider_is_moderated=excluded.top_provider_is_moderated,
                    supported_parameters=excluded.supported_parameters,
                    default_parameters=excluded.default_parameters,
                    supported_voices=excluded.supported_voices,
                    knowledge_cutoff=excluded.knowledge_cutoff,
                    expiration_date=excluded.expiration_date,
                    provider=excluded.provider,
                    publisher=excluded.publisher,
                    tier=excluded.tier,
                    architecture=excluded.architecture,
                    availability_state=excluded.availability_state,
                    reason_code=excluded.reason_code,
                    checked_at=excluded.checked_at,
                    source=excluded.source
            """, (
                m.get("id"), m.get("canonical_slug"), m.get("hugging_face_id"),
                m.get("name"), m.get("created"), m.get("description"),
                m.get("context_length"), m.get("modality"),
                json.dumps(m.get("architecture", {}).get("input_modalities")) if m.get("architecture", {}).get("input_modalities") else None,
                json.dumps(m.get("architecture", {}).get("output_modalities")) if m.get("architecture", {}).get("output_modalities") else None,
                m.get("architecture", {}).get("tokenizer"),
                m.get("architecture", {}).get("instruct_type"),
                m.get("pricing", {}).get("prompt") if m.get("pricing") else None,
                m.get("pricing", {}).get("completion") if m.get("pricing") else None,
                m.get("top_provider", {}).get("context_length") if m.get("top_provider") else None,
                m.get("top_provider", {}).get("max_completion_tokens") if m.get("top_provider") else None,
                m.get("top_provider", {}).get("is_moderated") if m.get("top_provider") else None,
                json.dumps(m.get("supported_parameters")) if m.get("supported_parameters") else None,
                json.dumps(m.get("default_parameters")) if m.get("default_parameters") else None,
                json.dumps(m.get("supported_voices")) if m.get("supported_voices") else None,
                m.get("knowledge_cutoff"), m.get("expiration_date"),
                m.get("provider"), m.get("owned_by"),  # publisher = owned_by
                m.get("tier"), json.dumps(m.get("architecture")) if m.get("architecture") else None,
                m.get("availability_state", "unknown"),
                m.get("reason_code", ""),
                m.get("checked_at"),
                source,
            ))
        conn.commit()
    finally:
        conn.close()


def get_catalog(db_path: str, fresh_only: bool = False) -> list[dict]:
    """Get all models from catalog."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if fresh_only:
            # Only models checked within last 6 hours
            import time
            cutoff = time.time() - 21600
            cur = conn.execute("SELECT * FROM models WHERE checked_at > ? ORDER BY id", (cutoff,))
        else:
            cur = conn.execute("SELECT * FROM models ORDER BY id")
        return [_row_to_dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def status_map(db_path: str) -> dict:
    """Get model_id -> status mapping."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("SELECT id, availability_state, reason_code, checked_at FROM models")
        return {row["id"]: {"state": row["availability_state"], "reason_code": row["reason_code"], "checked_at": row["checked_at"]} for row in cur.fetchall()}
    finally:
        conn.close()


def catalog_age_sec(db_path: str) -> float:
    """Get age of catalog in seconds (since newest check)."""
    import time
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT MAX(checked_at) as max_ts FROM models").fetchone()
        if row and row["max_ts"]:
            return time.time() - row["max_ts"]
    finally:
        conn.close()
    return float("inf")


def init_db(db_path: str):
    """Initialize catalog database schema."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS models (
                id TEXT PRIMARY KEY,
                canonical_slug TEXT,
                hugging_face_id TEXT,
                name TEXT,
                created INTEGER,
                description TEXT,
                context_length INTEGER,
                modality TEXT,
                input_modalities TEXT,
                output_modalities TEXT,
                tokenizer TEXT,
                instruct_type TEXT,
                pricing_prompt REAL,
                pricing_completion REAL,
                top_provider_context_length INTEGER,
                top_provider_max_completion_tokens INTEGER,
                top_provider_is_moderated INTEGER,
                supported_parameters TEXT,
                default_parameters TEXT,
                supported_voices TEXT,
                knowledge_cutoff TEXT,
                expiration_date TEXT,
                provider TEXT,
                publisher TEXT,
                tier TEXT,
                architecture TEXT,
                availability_state TEXT DEFAULT 'unknown',
                reason_code TEXT DEFAULT '',
                checked_at REAL,
                source TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_models_provider ON models(provider)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_models_availability ON models(availability_state)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_models_checked ON models(checked_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_models_free ON models(id) WHERE id LIKE '%:free' OR id LIKE '%-free'")
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        init_db(DEFAULT_DB)
        print(f"Initialized catalog DB at {DEFAULT_DB}")