#!/usr/bin/env python3
"""
registry.py — Dynamic model registry for wrapper-nvidia.
Migrated from registry.js — functionally identical.

Authoritative context/max-output numbers come from NVIDIA's machine-readable
"featured-models" catalog (NGC assets), which is the same source OpenClaw uses.
We fetch it periodically, cache it locally, and fall back to the last good
cache (or a tiny static seed) if the network/endpoint is unavailable —
NEVER to silent hardcoded guesses.

Usage:
  from src.registry import Registry
  registry = Registry()
  await registry.refresh(True)          # force first load
  registry.start()                       # background periodic refresh
  registry.get_official_context('deepseek-ai/deepseek-v4-pro')  # -> {context, maxOutput}
"""

import os
import json
import time
import asyncio
import logging
from typing import Dict, Optional

import aiohttp

logger = logging.getLogger('wrapper-nvidia')

NGC_FEATURED_URL = (os.environ.get('NGC_FEATURED_MODELS_URL') or
    'https://assets.ngc.nvidia.com/products/api-catalog/featured-models.json').rstrip('/')

REGISTRY_REFRESH_SEC = int(os.environ.get('REGISTRY_REFRESH_SEC', '3600'))

CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'nvidia', 'ngc-featured-cache.json')

STATIC_SEED = {
    'nvidia/nemotron-3-ultra-550b-a55b': {'context': 1048576, 'maxOutput': 8192},
    'nemotron-3-super-120b-a12b': {'context': 1000000, 'maxOutput': 8192},
    'z-ai/glm-5.2': {'context': 202752, 'maxOutput': 8192},
    'minimaxai/minimax-m3': {'context': 196608, 'maxOutput': 8192},
    'deepseek-ai/deepseek-v4-pro': {'context': 262144, 'maxOutput': 16384},
}


class Registry:
    def __init__(self):
        self._map: Dict[str, dict] = {}
        self._source: str = 'empty'
        self._last_sync: float = 0
        self._last_error: Optional[str] = None
        self._agent: Optional[aiohttp.ClientSession] = None
        self._timer: Optional[asyncio.Task] = None

    def set_external_agent(self, agent):
        self._agent = agent

    async def _fetch_live(self) -> Dict[str, dict]:
        timeout = aiohttp.ClientTimeout(total=20)
        session = self._agent or aiohttp.ClientSession(timeout=timeout)
        owns_session = self._agent is None

        try:
            async with session.get(NGC_FEATURED_URL,
                                   headers={'Accept': 'application/json'}) as resp:
                if resp.status != 200:
                    raise Exception(f'NGC featured-models HTTP {resp.status}')
                body = await resp.json()
        finally:
            if owns_session:
                await session.close()

        arr = body.get('featured-models', body.get('models', body.get('data', [])))
        map_result = {}
        for m in arr:
            mid = m.get('model', m.get('id'))
            if not mid:
                continue
            context = int(m.get('context', 0) or 0)
            max_output = int(m.get('max-output', m.get('max_output', m.get('maxOutput', 0))) or 0)
            if not context or context <= 0:
                continue
            map_result[mid] = {
                'context': context,
                'maxOutput': max_output if max_output and max_output > 0 else 4096,
            }

        if not map_result:
            raise Exception('NGC featured-models returned no usable entries')
        return map_result

    def _load_cache_file(self) -> Optional[Dict[str, dict]]:
        try:
            if not os.path.exists(CACHE_FILE):
                return None
            with open(CACHE_FILE, 'r') as f:
                raw = json.load(f)
            if raw and raw.get('map') and raw['map']:
                return raw['map']
        except Exception as e:
            logger.warning(f'[registry] cache read failed: {e}')
        return None

    def _save_cache_file(self):
        try:
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            with open(CACHE_FILE, 'w') as f:
                json.dump({
                    'source': 'live',
                    'syncedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                    'map': self._map,
                }, f, indent=2)
        except Exception as e:
            logger.warning(f'[registry] cache write failed: {e}')

    async def refresh(self, force: bool = False) -> Dict[str, dict]:
        now = time.time()
        # time.time() is in seconds; do not multiply the TTL by milliseconds.
        if not force and self._last_sync and (now - self._last_sync) < REGISTRY_REFRESH_SEC:
            return self._map

        try:
            live = await self._fetch_live()
            self._map = live
            self._source = 'live'
            self._last_sync = now
            self._last_error = None
            self._save_cache_file()
            logger.info(f'[registry] Synced NGC featured-models: {len(live)} models (live)')
        except Exception as e:
            self._last_error = str(e)
            cached = self._load_cache_file()
            if cached and cached:
                self._map = cached
                self._source = 'cache'
                self._last_sync = now
                logger.warning(f'[registry] Live NGC fetch failed ({e}); using on-disk cache ({len(cached)} models)')
            elif not self._map:
                self._map = dict(STATIC_SEED)
                self._source = 'seed'
                self._last_sync = now
                logger.warning(f'[registry] Live NGC fetch failed ({e}) and no cache; using static seed ({len(STATIC_SEED)} models)')
            else:
                logger.warning(f'[registry] Live NGC fetch failed ({e}); keeping existing map ({len(self._map)} models)')
        return self._map

    def start(self):
        async def _tick():
            while True:
                await asyncio.sleep(REGISTRY_REFRESH_SEC)
                try:
                    await self.refresh()
                except Exception:
                    pass

        self._timer = asyncio.create_task(_tick())

    def stop(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def get_official_context(self, model_id: str) -> Optional[dict]:
        if not model_id:
            return None
        candidates = {model_id}
        slash = model_id.find('/')
        if slash >= 0:
            candidates.add(model_id[slash + 1:])
        for id in candidates:
            if id in self._map:
                return self._map[id]
        return None

    def has_official_context(self, model_id: str) -> bool:
        return self.get_official_context(model_id) is not None

    def all(self) -> dict:
        return dict(self._map)

    def status(self) -> dict:
        return {
            'source': self._source,
            'models': len(self._map),
            'lastSync': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self._last_sync)) if self._last_sync else None,
            'lastError': self._last_error,
            'url': NGC_FEATURED_URL,
        }
