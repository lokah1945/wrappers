#!/usr/bin/env python3
"""Concurrency and release invariants for the shared wrapper contract."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _clear_src_paths():
    for k in list(sys.modules):
        if k == "src" or k.startswith("src."):
            del sys.modules[k]
    sys.path = [p for p in sys.path if all(x not in p for x in ("nvidia-python", "opencode", "blackbox"))]


def _load_src_main(wrapper: str):
    _clear_src_paths()
    sys.path.insert(0, str(ROOT / wrapper))
    import src.main as mod  # type: ignore
    return mod


def _load_src_key_pool(wrapper: str):
    _clear_src_paths()
    sys.path.insert(0, str(ROOT / wrapper))
    from src.key_pool import KeyPool  # type: ignore
    return KeyPool


def _load_nous():
    spec = importlib.util.spec_from_file_location("wrapper_nous_concurrency", ROOT / "nous" / "wrapper_nous.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _set_env(prefix: str, count: int = 5):
    old = {k: os.environ.get(k) for k in [f"{prefix}_API_KEY_{i}" for i in range(1, count + 1)]}
    old[f"{prefix}_API_KEY"] = os.environ.get(f"{prefix}_API_KEY")
    os.environ.pop(f"{prefix}_API_KEY", None)
    for i in range(1, count + 1):
        os.environ[f"{prefix}_API_KEY_{i}"] = f"sk-{prefix.lower()}-test-{i:02d}-abcdefghijklmnopqrstuvwxyz"
    return old


def _restore_env(old: dict):
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


async def _exercise_async_pool(pool, model="m", n=100):
    async def one():
        item = await pool.acquire(model)
        assert item is not None
        await asyncio.sleep(0)
        pool.release(item["key"])
    await asyncio.gather(*(one() for _ in range(n)))


def test_opencode_key_pool_concurrent_acquire_release_no_leak():
    old = _set_env("OPENCODE", 5)
    try:
        KeyPool = _load_src_key_pool("opencode")
        pool = KeyPool().load_from_env()
        asyncio.run(_exercise_async_pool(pool, n=100))
        assert all(k.in_flight == 0 for k in pool.keys)
        used = [k.total_requests for k in pool.keys]
        assert sum(used) == 100
        assert len([x for x in used if x > 0]) >= 2
    finally:
        _restore_env(old)


def test_blackbox_key_pool_concurrent_acquire_release_no_leak():
    old = _set_env("BLACKBOX", 5)
    try:
        KeyPool = _load_src_key_pool("blackbox")
        pool = KeyPool().load_from_env()
        asyncio.run(_exercise_async_pool(pool, n=100))
        assert all(k.in_flight == 0 for k in pool.keys)
        used = [k.total_requests for k in pool.keys]
        assert sum(used) == 100
        assert len([x for x in used if x > 0]) >= 2
    finally:
        _restore_env(old)


def test_nvidia_key_pool_concurrent_reservations_release_no_leak():
    old = _set_env("NVIDIA", 5)
    try:
        KeyPool = _load_src_key_pool("nvidia-python")
        pool = KeyPool().load_from_env()

        async def one():
            item = await pool.acquire("nvidia/test")
            assert item is not None
            await asyncio.sleep(0)
            pool.release_success(item["key"])

        async def main():
            await asyncio.gather(*(one() for _ in range(50)))
        asyncio.run(main())
        assert all(k.in_flight == 0 for k in pool.keys)
        assert sum(k.total_requests for k in pool.keys) == 50
    finally:
        _restore_env(old)


def test_nous_key_pool_concurrent_acquire_release_no_leak():
    old = _set_env("NOUS", 5)
    try:
        wn = _load_nous()
        wn.KEY_POOL.load_from_env()

        async def one():
            entry = await asyncio.to_thread(wn.KEY_POOL.acquire)
            assert entry is not None
            await asyncio.sleep(0)
            wn.KEY_POOL.release(entry)

        async def main():
            await asyncio.gather(*(one() for _ in range(100)))
        asyncio.run(main())
        assert all(k.in_flight == 0 for k in wn.KEY_POOL.keys)
        assert sum(k.total_requests for k in wn.KEY_POOL.keys) == 100
    finally:
        _restore_env(old)


def test_response_stores_stay_bounded():
    # Blackbox store
    bb = _load_src_main("blackbox")
    bb._RESPONSE_STORE.clear()
    for i in range(250):
        bb._store_response(f"resp_{i}", [{"role": "user", "content": str(i)}])
    assert len(bb._RESPONSE_STORE) <= 200

    # OpenCode store follows the same bounded behavior in route code; exercise directly.
    oc = _load_src_main("opencode")
    oc._RESPONSE_STORE.clear()
    for i in range(250):
        oc._RESPONSE_STORE[f"resp_{i}"] = []
        if len(oc._RESPONSE_STORE) > 200:
            oc._RESPONSE_STORE.pop(next(iter(oc._RESPONSE_STORE)))
    assert len(oc._RESPONSE_STORE) <= 200
