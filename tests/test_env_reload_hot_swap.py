"""Shared .env reload service and state-preserving pool sync (final audit 2026-11-15).

Covers the hot-swap contract from docs/MASTER_PROMPT_WRAPPERS_FINAL_AUDIT.md §7:
ownership-based reconciliation, zero-key generations, state retention by
credential identity, retire-on-remove, thread-safe loop capture, watcher
event coverage and last-known-good on corrupt files.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

from common.env_reload import (
    EnvReloadService,
    parse_env_file,
    reconcile_snapshot,
    scan_pool_keys,
    resolve_env_path,
)

ROOT = Path(__file__).resolve().parents[1]


# ── parser ────────────────────────────────────────────────────────────────

def test_parse_env_file_quotes_comments_export_and_missing(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "PLAIN=value1\n"
        "QUOTED=\"value 2\"\n"
        "SINGLE='value3'\n"
        "INLINE=value4 # trailing comment\n"
        "export EXPORTED=value5\n"
        "\n"
        "NO_EQUALS_LINE\n"
    )
    snapshot, error = parse_env_file(env)
    assert error is None
    assert snapshot == {
        "PLAIN": "value1",
        "QUOTED": "value 2",
        "SINGLE": "value3",
        "INLINE": "value4",
        "EXPORTED": "value5",
    }
    # A missing file is a valid zero-variable snapshot.
    empty, err = parse_env_file(tmp_path / "absent.env")
    assert empty == {} and err is None
    # Binary corruption keeps last-known-good (snapshot None).
    corrupt = tmp_path / "corrupt.env"
    corrupt.write_bytes(b"NVIDIA_API_KEY=x\x00\xff\xfebinary")
    snapshot, error = parse_env_file(corrupt)
    assert snapshot is None and error


# ── ownership reconciliation ──────────────────────────────────────────────

def test_reconcile_snapshot_sets_changes_and_removes_owned_only():
    saved = os.environ.copy()
    try:
        os.environ.update({"PROVIDER_API_KEY": "old", "FOREIGN_VAR": "keepme"})
        previous = {"PROVIDER_API_KEY": "old", "OTHER_FILE_VAR": "fileval"}
        os.environ["OTHER_FILE_VAR"] = "fileval"
        snapshot = {"PROVIDER_API_KEY": "new", "PROVIDER_API_KEY_2": "added"}
        changes = reconcile_snapshot(snapshot, previous)
        assert os.environ["PROVIDER_API_KEY"] == "new"
        assert os.environ["PROVIDER_API_KEY_2"] == "added"
        # OTHER_FILE_VAR was file-owned last time and is gone from the file now.
        assert "OTHER_FILE_VAR" not in os.environ
        assert changes == {"set": {"PROVIDER_API_KEY": "new", "PROVIDER_API_KEY_2": "added"},
                           "removed": ["OTHER_FILE_VAR"]}
        # Foreign variables are never touched.
        assert os.environ["FOREIGN_VAR"] == "keepme"
        # A variable taken over by another source keeps the foreign value.
        os.environ["PROVIDER_API_KEY"] = "operator-override"
        changes = reconcile_snapshot({}, snapshot)
        assert os.environ["PROVIDER_API_KEY"] == "operator-override"
        assert "PROVIDER_API_KEY_2" not in os.environ
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_scan_pool_keys_pattern_dedupe_and_order():
    snapshot = {
        "NVIDIA_API_KEY_2": "keyvalue-bbbbbbbbbbbb",
        "NVIDIA_API_KEY": "keyvalue-aaaaaaaaaaaa",
        "NVIDIA_API_KEY_1": "keyvalue-aaaaaaaaaaaa",  # duplicate value
        "NVIDIA_API_KEY_SHORT": "abc",  # below min length
        "NVIDIA_API_KEY_EMPTY": "  ",
        "UNRELATED_API_KEY": "unrelated-key-value",
    }
    assert scan_pool_keys(snapshot, r"^NVIDIA_API_KEY(_\d+)?$") == ["keyvalue-aaaaaaaaaaaa", "keyvalue-bbbbbbbbbbbb"]
    assert scan_pool_keys({}, r"^NVIDIA_API_KEY(_\d+)?$") == []
    assert scan_pool_keys({"NVIDIA_API_KEY": "abcdefghij"}, r"^NVIDIA_API_KEY(_\d+)?$") == ["abcdefghij"]


def test_resolve_env_path_override(monkeypatch, tmp_path):
    monkeypatch.setenv("WRAPPER_ENV_FILE", str(tmp_path / "custom.env"))
    assert resolve_env_path(tmp_path / "default.env") == tmp_path / "custom.env"
    monkeypatch.delenv("WRAPPER_ENV_FILE")
    assert resolve_env_path(tmp_path / "default.env") == tmp_path / "default.env"


# ── service: reload lifecycle ─────────────────────────────────────────────

async def test_service_reload_applies_and_reports_generations(tmp_path):
    env = tmp_path / ".env"
    env.write_text("NVIDIA_API_KEY=fake-key-aaaaaaaaaaaaaaaaaa\nBEARER_TOKEN=tok1\n")
    applied = []

    async def apply(snapshot):
        applied.append(dict(snapshot))

    service = EnvReloadService(env_path=env, apply=apply)
    assert service.initialize() is True
    # A write coalesces through reload_now directly (the debounce path is
    # covered by the live acceptance harness with real processes).
    env.write_text("NVIDIA_API_KEY=fake-key-bbbbbbbbbbbbbbbbbb\nBEARER_TOKEN=tok1\n")
    assert await service.reload_now(trigger="test") is True
    assert service.generation == 1
    assert service.last_result == "ok"
    assert applied[-1]["NVIDIA_API_KEY"] == "fake-key-bbbbbbbbbbbbbbbbbb"
    # Zero-key file applies: valid empty state.
    env.write_text("BEARER_TOKEN=tok1\n")
    assert await service.reload_now(trigger="test") is True
    assert applied[-1].get("NVIDIA_API_KEY") is None
    # Corrupt file keeps last-known-good with a visible failure.
    env.write_bytes(b"\x00\x01garbage")
    assert await service.reload_now(trigger="test") is False
    assert service.last_result == "failed"
    assert service.last_error
    assert applied[-1].get("NVIDIA_API_KEY") is None  # LKG semantics kept
    status = service.status()
    assert status["generation"] == 2 and status["last_result"] == "failed"
    # Repair recovers automatically.
    env.write_text("NVIDIA_API_KEY=fake-key-cccccccccccccccccc\n")
    assert await service.reload_now(trigger="test") is True
    assert applied[-1]["NVIDIA_API_KEY"] == "fake-key-cccccccccccccccccc"


async def test_service_invalid_then_valid_file_via_ownership(tmp_path):
    env = tmp_path / ".env"
    env.write_text("NOUS_API_KEY=fake-nous-aaaaaaaaaaaaaaaaa\n")
    service = EnvReloadService(env_path=env, apply=lambda snapshot: asyncio.sleep(0))
    service.initialize()
    env.write_text("NOUS_API_KEY_2=fake-nous-bbbbbbbbbbbbbbbbbb\n")  # key line removed, new one added
    await service.reload_now(trigger="test")
    assert "NOUS_API_KEY" not in os.environ or os.environ.get("NOUS_API_KEY") != "fake-nous-aaaaaaaaaaaaaaaaa"
    assert os.environ.get("NOUS_API_KEY_2") == "fake-nous-bbbbbbbbbbbbbbbbbb"


# ── pools: state-preserving sync ──────────────────────────────────────────

def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    module = ModuleType(name)
    spec.loader.exec_module(module)
    return module


KEY_A = "fake-pool-key-aaaaaaaaaaaaaaaaaa"
KEY_B = "fake-pool-key-bbbbbbbbbbbbbbbbbb"
KEY_C = "fake-pool-key-cccccccccccccccccc"


def _with_env(env: dict, fn):
    saved = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update(env)
        return fn()
    finally:
        os.environ.clear()
        os.environ.update(saved)


@pytest.mark.parametrize("provider,path", [
    ("nvidia-python", ROOT / "nvidia-python" / "src" / "key_pool.py"),
    ("openrouter", ROOT / "openrouter" / "src" / "key_pool.py"),
    ("coarena", ROOT / "coarena" / "src" / "key_pool.py"),
])
async def test_pool_sync_keys_state_retention_zero_and_retire(provider, path):
    mod = _load(f"pool_{provider.replace('-', '_')}", path)
    prefix = "NVIDIA" if provider == "nvidia-python" else provider.upper()
    pool = mod.KeyPool()
    _with_env({f"{prefix}_API_KEY": KEY_A}, lambda: pool.load_from_env())
    entry = pool.keys[0]
    entry.in_flight = 2
    entry.total_requests = 7
    entry.hard_blocked_until = time.time() + 120.0
    entry.block_reason = "rate_limit"
    original = entry

    # Add a second key: retained credential keeps state on the SAME object.
    assert await pool.sync_keys([KEY_A, KEY_B]) is True
    retained = [k for k in pool.keys if k.api_key == KEY_A][0]
    assert retained is original
    assert retained.in_flight == 2 and retained.total_requests == 7
    assert retained.hard_blocked_until > time.time()
    assert len(pool.keys) == 2

    # Remove KEY_A: it must be retired from acquisition.
    assert await pool.sync_keys([KEY_B]) is True
    assert [k.api_key for k in pool.keys] == [KEY_B]

    # Zero-key generation is valid and applies.
    assert await pool.sync_keys([]) is True
    assert pool.keys == []

    # Re-add recovers without restart; unchanged sync is a no-op.
    assert await pool.sync_keys([KEY_C]) is True
    assert await pool.sync_keys([KEY_C]) is False

    # Reorder keeps identity and state (no reset).
    entry_c = pool.keys[0]
    entry_c.total_requests = 11
    assert await pool.sync_keys([KEY_A, KEY_C]) is True
    by_key = {k.api_key: k for k in pool.keys}
    assert by_key[KEY_C].total_requests == 11  # state survived the reorder


async def test_nous_pool_sync_keys_state_retention():
    # Nous KeyPool lives inline in main.py; load just the class pair.
    import ast
    src = (ROOT / "nous" / "src" / "main.py").read_text()
    tree = ast.parse(src)
    nodes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in {"KeyEntry", "KeyPool"}]
    mod = ModuleType("nous_pool_test")
    header = (
        "import os, time, asyncio, logging\n"
        "from typing import List, Dict, Optional, Set\n"
        "logger = logging.getLogger('nous-pool-test')\n"
    )
    exec(compile(header + "\n" + ast.unparse(ast.Module(body=nodes, type_ignores=[])),
                 "<nous-pool-test>", "exec"), mod.__dict__)
    pool = mod.KeyPool()
    _with_env({"NOUS_API_KEY": KEY_A}, lambda: pool.load_from_env())
    entry = pool.keys[0]
    entry.in_flight = 1
    entry.total_requests = 5
    entry.hard_blocked_until = time.time() + 90.0

    assert await pool.sync_keys([KEY_A, KEY_B]) is True
    retained = [k for k in pool.keys if k.api_key == KEY_A][0]
    assert retained is entry and retained.in_flight == 1 and retained.total_requests == 5
    assert await pool.sync_keys([]) is True
    assert pool.keys == []
    assert await pool.sync_keys([KEY_A]) is True
    # Exclusion is by api_key identity now.
    got = await pool.acquire(exclude={KEY_A})
    assert got is None
    got = await pool.acquire(exclude={KEY_B})
    assert got is not None and got.api_key == KEY_A


async def test_openrouter_release_after_retire_keeps_total_consistent():
    mod = _load("pool_or_release", ROOT / "openrouter" / "src" / "key_pool.py")
    pool = mod.KeyPool()
    _with_env({"OPENROUTER_API_KEY": KEY_A, "OPENROUTER_API_KEY_2": KEY_B},
              lambda: pool.load_from_env())
    acquired = await pool.acquire("model/x")
    entry = acquired["key"]
    assert pool._in_flight_total == 1
    # Retire the in-flight credential (key removed from file).
    await pool.sync_keys([KEY_B])
    assert pool._in_flight_total == 0  # retired reservation no longer counted
    # The in-flight holder finishes: releasing its own object must not drive
    # the counter negative.
    pool.release(entry)
    assert pool._in_flight_total == 0
    assert entry.in_flight == 0


# ── service + watcher integration (fast path) ─────────────────────────────

async def test_service_watchdog_reload_end_to_end(tmp_path, monkeypatch):
    pytest.importorskip("watchdog")
    # Other modules in the session set WRAPPER_SKIP_DOTENV at import time
    # without cleanup (e.g. test_metrics_parameters); this test needs the
    # watcher live.
    monkeypatch.delenv("WRAPPER_SKIP_DOTENV", raising=False)
    env = tmp_path / ".env"
    env.write_text("NVIDIA_API_KEY=fake-key-aaaaaaaaaaaaaaaaaa\n")
    seen = []

    async def apply(snapshot):
        seen.append(scan_pool_keys(snapshot, r"^NVIDIA_API_KEY(_\d+)?$"))

    service = EnvReloadService(env_path=env, apply=apply, debounce=0.05)
    assert service.initialize() is True
    assert service.start() is True
    try:
        assert service.watcher_mode == "watchdog"
        # In-place write.
        env.write_text("NVIDIA_API_KEY=fake-key-bbbbbbbbbbbbbbbbbb\n")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not seen:
            await asyncio.sleep(0.02)
        assert seen and seen[-1] == ["fake-key-bbbbbbbbbbbbbbbbbb"]
        # Atomic replace (rename onto the watched path).
        tmp = tmp_path / ".env.tmp"
        tmp.write_text("NVIDIA_API_KEY_2=fake-key-cccccccccccccccccc\n")
        tmp.replace(env)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and seen[-1] != ["fake-key-cccccccccccccccccc"]:
            await asyncio.sleep(0.02)
        assert seen[-1] == ["fake-key-cccccccccccccccccc"]
        assert service.generation >= 2
        # File deletion → valid zero-key generation.
        env.unlink()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and seen[-1] != []:
            await asyncio.sleep(0.02)
        assert seen[-1] == []
    finally:
        service.stop()
    assert service.watcher_mode == "stopped"


async def test_service_stop_is_idempotent_and_joins(tmp_path, monkeypatch):
    monkeypatch.delenv("WRAPPER_SKIP_DOTENV", raising=False)
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    service = EnvReloadService(env_path=env, apply=lambda snapshot: asyncio.sleep(0))
    service.initialize()
    service.start()
    thread = service._poll_thread
    service.stop()
    service.stop()  # idempotent
    if thread is not None:
        assert not thread.is_alive()
