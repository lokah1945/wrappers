#!/usr/bin/env python3
"""
Refresh Model Fetcher Catalog from upstream providers.

Currently supports:
- OpenRouter API (most comprehensive model catalog)
- NVIDIA NIM API (requires NVIDIA_API_KEY)

Updates the central SQLite catalog database.
"""

import json
import os
import sys
import time
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "src"))

from catalog_queries import upsert_catalog, init_db, DEFAULT_DB

logger = logging.getLogger("catalog-refresh")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

try:
    import aiohttp
    import asyncio
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False
    logger.warning("aiohttp not installed, async fetch unavailable")


# ─── OpenRouter ──────────────────────────────────────────────────────────

async def fetch_openrouter_models():
    """Fetch models from OpenRouter API."""
    url = "https://openrouter.ai/api/v1/models"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=30) as resp:
                if resp.status != 200:
                    logger.error(f"OpenRouter API error: {resp.status}")
                    return []
                data = await resp.json()
                return data.get("data", [])
        except Exception as e:
            logger.error(f"OpenRouter fetch failed: {e}")
            return []


def transform_openrouter_model(m):
    """Transform OpenRouter model to catalog schema."""
    pricing = m.get("pricing", {}) or {}
    top_provider = m.get("top_provider", {}) or {}
    architecture = m.get("architecture", {}) or {}
    
    model_id = m.get("id", "")
    is_free = ":free" in model_id or "-free" in model_id
    
    return {
        "id": model_id,
        "canonical_slug": model_id,
        "hugging_face_id": model_id,
        "name": m.get("name", model_id),
        "created": int(time.time()),
        "description": m.get("description", ""),
        "context_length": m.get("context_length", 4096),
        "modality": architecture.get("modality", "text"),
        "input_modalities": architecture.get("input_modalities", ["text"]),
        "output_modalities": architecture.get("output_modalities", ["text"]),
        "tokenizer": architecture.get("tokenizer", "unknown"),
        "instruct_type": architecture.get("instruct_type", "chat"),
        "pricing_prompt": float(pricing.get("prompt", 0)) if pricing else 0.0,
        "pricing_completion": float(pricing.get("completion", 0)) if pricing else 0.0,
        "top_provider_context_length": top_provider.get("context_length", 4096),
        "top_provider_max_completion_tokens": top_provider.get("max_completion_tokens", 4096),
        "top_provider_is_moderated": 1 if top_provider.get("is_moderated") else 0,
        "supported_parameters": json.dumps(m.get("supported_parameters", ["temperature", "top_p", "max_tokens"])),
        "default_parameters": json.dumps(m.get("default_parameters", {"temperature": 0.7, "top_p": 1.0, "max_tokens": 4096})),
        "supported_voices": json.dumps(m.get("supported_voices")) if m.get("supported_voices") else None,
        "knowledge_cutoff": m.get("knowledge_cutoff", "2024-01"),
        "expiration_date": m.get("expiration_date"),
        "provider": "openrouter",
        "publisher": m.get("owned_by", "openrouter"),
        "tier": "free" if is_free else "paid",
        "architecture": json.dumps(architecture),
        "availability_state": "available",
        "reason_code": "OK",
        "checked_at": time.time(),
        "source": "openrouter_api",
    }


# ─── NVIDIA NIM ──────────────────────────────────────────────────────────

async def fetch_nvidia_models():
    """Fetch models from NVIDIA NIM API."""
    api_key = os.environ.get('NVIDIA_API_KEY')
    if not api_key:
        logger.warning("NVIDIA_API_KEY not set, skipping NVIDIA fetch")
        return []
    
    url = "https://integrate.api.nvidia.com/v1/models"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=30) as resp:
                if resp.status != 200:
                    logger.error(f"NVIDIA API error: {resp.status}")
                    return []
                data = await resp.json()
                return data.get("data", [])
        except Exception as e:
            logger.error(f"NVIDIA fetch failed: {e}")
            return []


def transform_nvidia_model(m):
    """Transform NVIDIA model to catalog schema."""
    model_id = m.get("id", "")
    is_free = ":free" in model_id or "-free" in model_id
    
    return {
        "id": model_id,
        "canonical_slug": model_id,
        "hugging_face_id": model_id,
        "name": model_id.split("/")[-1] if "/" in model_id else model_id,
        "created": int(time.time()),
        "description": f"{model_id} via NVIDIA NIM",
        "context_length": m.get("max_context_length", 128000),
        "modality": "text",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "tokenizer": "unknown",
        "instruct_type": "chat",
        "pricing_prompt": 0.0,
        "pricing_completion": 0.0,
        "top_provider_context_length": m.get("max_context_length", 128000),
        "top_provider_max_completion_tokens": m.get("max_output_tokens", 4096),
        "top_provider_is_moderated": 0,
        "supported_parameters": json.dumps(["temperature", "top_p", "max_tokens"]),
        "default_parameters": json.dumps({"temperature": 0.7, "top_p": 1.0, "max_tokens": 4096}),
        "supported_voices": None,
        "knowledge_cutoff": "2024-01",
        "expiration_date": None,
        "provider": "nvidia",
        "publisher": m.get("owned_by", "nvidia"),
        "tier": "free" if is_free else "paid",
        "architecture": json.dumps({"input_modalities": ["text"], "output_modalities": ["text"], "tokenizer": "unknown", "instruct_type": "chat"}),
        "availability_state": "available",
        "reason_code": "OK",
        "checked_at": time.time(),
        "source": "nvidia_nim_api",
    }


# ─── Main Refresh ────────────────────────────────────────────────────────

async def refresh_all():
    """Refresh catalog from all available providers."""
    logger.info("Starting catalog refresh...")
    
    # Initialize DB
    init_db(DEFAULT_DB)
    
    all_models = []
    
    if not HAS_AIOHTTP:
        logger.error("aiohttp not installed, cannot fetch models")
        return
    
    # OpenRouter
    or_models = await fetch_openrouter_models()
    for m in or_models:
        all_models.append(transform_openrouter_model(m))
    logger.info(f"Fetched {len(or_models)} OpenRouter models")
    
    # NVIDIA
    nvidia_models = await fetch_nvidia_models()
    for m in nvidia_models:
        all_models.append(transform_nvidia_model(m))
    logger.info(f"Fetched {len(nvidia_models)} NVIDIA models")
    
    # Upsert all models
    if all_models:
        upsert_catalog(DEFAULT_DB, all_models, source="refresh_catalog")
        logger.info(f"Upserted {len(all_models)} total models to catalog")
    else:
        logger.warning("No models fetched from any provider")


if __name__ == "__main__":
    if not HAS_AIOHTTP:
        logger.error("aiohttp required. Install: pip install aiohttp")
        sys.exit(1)
    asyncio.run(refresh_all())