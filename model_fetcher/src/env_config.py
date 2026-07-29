#!/usr/bin/env python3
"""
env_config.py — Shared environment configuration for catalog.

Centralizes FREE_ONLY and related settings so all wrappers use the same logic.
"""

import os


def free_only() -> bool:
    """
    Check if FREE_ONLY mode is enabled.
    
    Reads from:
    - FREE_ONLY env var (yes/true/1/on/y)
    - Catalog DB config if available
    """
    v = (os.environ.get("FREE_ONLY") or "no").strip().lower()
    if v in ("yes", "true", "1", "on", "y"):
        return True
    
    # Could also check catalog DB config here if needed
    return False


def free_model_allowlist() -> set:
    """Get allowlisted free model IDs from env."""
    allow = (os.environ.get("FREE_MODEL_ALLOWLIST") or "").strip()
    if not allow:
        return set()
    return {x.strip().lower() for x in allow.split(",") if x.strip()}


def is_free_model(model_id: str) -> bool:
    """
    Check if a model ID qualifies as free.
    
    Criteria:
    - Ends with :free or -free
    - In FREE_MODEL_ALLOWLIST
    - Pricing is $0 (checked at catalog level)
    """
    if not model_id:
        return False
    mid = str(model_id).lower().strip()
    
    if mid.endswith(":free") or mid.endswith("-free"):
        return True
    
    allowlist = free_model_allowlist()
    if mid in allowlist:
        return True
    
    # Could also check catalog pricing here if needed
    return False


# Other shared config
CATALOG_DB = os.environ.get("CATALOG_DB", "/root/wrapper/model_fetcher/data/active_nvidia_nim.sqlite3")
MODEL_REGISTRY_URL = os.environ.get("MODEL_REGISTRY_URL", "http://127.0.0.1:9200")
MODEL_REGISTRY_ADMIN_TOKEN = os.environ.get("MODEL_REGISTRY_ADMIN_TOKEN", "model-registry-local-key")


if __name__ == "__main__":
    print(f"FREE_ONLY: {free_only()}")
    print(f"ALLOWLIST: {free_model_allowlist()}")
    print(f"CATALOG_DB: {CATALOG_DB}")