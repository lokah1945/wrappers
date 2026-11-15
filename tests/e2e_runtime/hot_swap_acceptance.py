#!/usr/bin/env python3
"""Live API-key hot-swap acceptance over real sockets and live watchers.

Final audit 2026-11-15 (docs/MASTER_PROMPT_WRAPPERS_FINAL_AUDIT.md §7.4).

For every active wrapper this harness boots a REAL uvicorn process with the
shared .env reload service watching an ISOLATED temporary env file
(WRAPPER_ENV_FILE; WRAPPER_SKIP_DOTENV is deliberately NOT set), then drives
the operator scenario table end to end:

  1→2→many keys, remove one key, replace/reorder/duplicate, remove the last
  key then re-add, state retention across reloads, active stream during key
  removal, atomic replace, corrupt file then repair, and repeated
  shutdown/restart — with the PID pinned throughout and a documented reload
  bound (5 s on the local filesystem).

No production credential, port or service is touched: the upstream is a
loopback mock that records a SAFE fingerprint (sha256 prefix) of the
Authorization header it received.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from process_isolation import assert_ports_free, isolated_environment, stop_process

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from common.wrapper_scope import require_production, select_wrappers

WRAPPERS = {
    "nvidia-python": (19501, "NVIDIA", False),
    "nous": (19502, "NOUS", False),
    "openrouter": (19506, "OPENROUTER", True),
    "coarena": (19507, "COARENA", True),
}
WRAPPERS = {name: config for name, config in WRAPPERS.items()
            if name in select_wrappers(repo=ROOT, include_registry=False)}
UPSTREAM_PORT = 19599
TOKEN = "hot-swap-client-token"
RELOAD_BOUND_SEC = 5.0  # documented bound on the local filesystem
KEYS = {name: f"hotswap-fake-{name}-key-{index:012d}" for index, name in enumerate("ABCDE")}


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


class Upstream:
    def __init__(self):
        self.records: list[dict] = []
        self.reject_once: set[str] = set()  # fingerprints that get one 401

    async def models(self, request):
        return web.json_response({"object": "list", "data": [
            {"id": "mock/hot-swap", "object": "model", "context_length": 8192}]})

    async def inference(self, request):
        body = await request.json()
        auth = request.headers.get("Authorization", "").removeprefix("Bearer ") or \
            request.headers.get("x-api-key", "")
        fp = fingerprint(auth)
        self.records.append({"path": request.path, "key_fp": fp, "model": body.get("model")})
        if fp in self.reject_once:
            self.reject_once.discard(fp)
            return web.json_response({"error": {"type": "authentication_error",
                                                "message": "one-shot rejection"}},
                                     status=401)
        if "stream-slow" in str(body.get("model")):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            try:
                for index in range(6):
                    await asyncio.sleep(0.15)
                    chunk = {"id": "hs", "object": "chat.completion.chunk", "model": body["model"],
                             "choices": [{"index": 0, "delta": {"content": f"part{index}"}, "finish_reason": None}]}
                    await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
                await response.write(b"data: [DONE]\n\n")
                await response.write_eof()
            except (ConnectionResetError, asyncio.CancelledError):
                pass
            return response
        reply = {"id": "hs-reply", "object": "chat.completion", "model": body.get("model"),
                 "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}
        return web.json_response(reply)


def start_wrapper(name: str, env_file: Path, logs: Path):
    require_production(name, ROOT)
    port, prefix, versioned = WRAPPERS[name]
    env = isolated_environment()
    # The live watcher must run: isolation comes from WRAPPER_ENV_FILE, not
    # from disabling dotenv (that would void the hot-swap evidence).
    env.pop("WRAPPER_SKIP_DOTENV", None)
    env.update({
        "WRAPPER_ENV_FILE": str(env_file),
        "BEARER_TOKEN": TOKEN,
        "LISTEN_PORT": str(port),
        "LISTEN_HOST": "127.0.0.1",
        "RATE_LIMIT_RPM": "0", "SOFT_LIMIT_RPM": "100000", "HARD_LIMIT_RPM": "100000",
        "NOUS_HARD_LIMIT_RPM": "100000", "RETRY_BACKOFF_BASE_SEC": "0",
        "PACING_ENABLED": "false", "VERIFY_ON_BOOT": "false",
        "RATE_LIMIT_COOLDOWN_SEC": "1", "AUTH_KEY_COOLDOWN_SEC": "1",
        "TRANSIENT_KEY_COOLDOWN_SEC": "1", "KEY_COOLDOWN_MAX_SEC": "1",
        "HEARTBEAT_INTERVAL_MS": "100", "MODEL_CATALOG_TTL_SEC": "3600",
        "PYTHONPATH": f"{ROOT}:{ROOT / name}",
        f"{prefix}_BASE_URL": f"http://127.0.0.1:{UPSTREAM_PORT}" + ("/v1" if versioned else ""),
    })
    log = (logs / f"{name}-hotswap.log").open("w")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT / name, env=env, stdout=log, stderr=subprocess.STDOUT)
    return process, log


class Harness:
    def __init__(self, client: aiohttp.ClientSession, upstream: Upstream):
        self.client, self.upstream = client, upstream
        self.checks: list[dict] = []
        self.pid: int | None = None

    async def check(self, label, action):
        try:
            await action()
        except Exception as exc:
            self.checks.append({"check": label, "passed": False,
                                "error": f"{type(exc).__name__}: {exc}"[:1500]})
            print(f"FAIL {label}: {exc}", flush=True)
        else:
            self.checks.append({"check": label, "passed": True})
            print(f"PASS {label}", flush=True)

    async def get_json(self, port: int, path: str):
        async with self.client.get(f"http://127.0.0.1:{port}{path}",
                                   headers={"Authorization": f"Bearer {TOKEN}"},
                                   timeout=aiohttp.ClientTimeout(total=10)) as reply:
            assert reply.status == 200, f"{path} -> {reply.status}"
            return await reply.json()

    async def wait_health(self, port: int, process):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"wrapper exited before readiness ({process.returncode})")
            try:
                async with self.client.get(f"http://127.0.0.1:{port}/health",
                                           timeout=aiohttp.ClientTimeout(total=2)) as reply:
                    if reply.status == 200:
                        data = await reply.json()
                        self.pid = data.get("pid")
                        return
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(0.2)
        raise AssertionError("wrapper health did not become available")

    async def pool_keys(self, name: str) -> list[dict]:
        port = WRAPPERS[name][0]
        path = "/health" if name == "nous" else "/stats"
        data = await self.get_json(port, path)
        keys = data.get("live_keys") or data.get("key_pool", {}).get("keys", [])
        if isinstance(keys, dict):
            keys = list(keys.values())
        return keys

    async def keys_equal(self, name: str, count: int):
        keys = await self.pool_keys(name)
        return keys if len(keys) == count else None

    async def wait_for(self, predicate, timeout: float = RELOAD_BOUND_SEC, what: str = "condition"):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = await predicate()
            if last is not None:
                return last
            await asyncio.sleep(0.1)
        raise AssertionError(f"timed out after {timeout}s waiting for {what} (last={last!r})")

    async def chat(self, name: str, model: str = "mock/hot-swap"):
        port = WRAPPERS[name][0]
        async with self.client.post(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": f"Bearer {TOKEN}"},
                timeout=aiohttp.ClientTimeout(total=20)) as reply:
            return reply.status, await reply.text()

    async def assert_pid_unchanged(self, name: str):
        port = WRAPPERS[name][0]
        data = await self.get_json(port, "/health")
        assert data.get("pid") == self.pid, \
            f"PID changed during hot swap: {self.pid} -> {data.get('pid')}"


def write_env(env_file: Path, provider: str, key_names: list[str], *, corrupt: bool = False):
    prefix = "NVIDIA" if provider == "nvidia-python" else provider.upper()
    if corrupt:
        env_file.write_bytes(b"\x00\x01\x02 corrupt binary payload")
        return
    lines = [f"# hot-swap acceptance ({provider})"]
    for index, key_name in enumerate(key_names):
        var = f"{prefix}_API_KEY" if index == 0 else f"{prefix}_API_KEY_{index + 1}"
        lines.append(f"{var}={KEYS[key_name]}")
    env_file.write_text("\n".join(lines) + "\n")


async def run_provider(name: str, upstream: Upstream, client: aiohttp.ClientSession, log_dir: Path):
    port = WRAPPERS[name][0]
    harness = Harness(client, upstream)
    with tempfile.TemporaryDirectory(prefix=f"hotswap-{name}-") as state_dir:
        state = Path(state_dir)
        env_file = state / ".env"
        write_env(env_file, name, ["A"])
        process, log = start_wrapper(name, env_file, Path(log_dir))
        try:
            await harness.wait_health(port, process)
            await harness.check(f"{name}/boot-single-key", lambda: _boot(harness, name))
            await harness.check(f"{name}/add-1-to-2-to-many", lambda: _add_keys(harness, name, env_file))
            await harness.check(f"{name}/remove-one-key", lambda: _remove_one(harness, name, env_file, upstream))
            await harness.check(f"{name}/replace-reorder-duplicate", lambda: _reorder(harness, name, env_file))
            await harness.check(f"{name}/state-retained-across-reload", lambda: _state_retained(harness, name, env_file))
            await harness.check(f"{name}/remove-last-key-then-recover", lambda: _zero_and_recover(harness, name, env_file))
            await harness.check(f"{name}/retired-key-not-reselected", lambda: _retired_not_reselected(harness, name, env_file, upstream))
            await harness.check(f"{name}/atomic-replace-applied", lambda: _atomic_replace(harness, name, env_file))
            await harness.check(f"{name}/corrupt-file-keeps-last-known-good", lambda: _corrupt_then_repair(harness, name, env_file))
            await harness.check(f"{name}/stream-survives-key-removal", lambda: _stream_during_removal(harness, name, env_file))
        finally:
            await asyncio.to_thread(stop_process, process)
            log.close()
        # Shutdown/restart repeatability: boot once more with a known file,
        # verify clean start.
        write_env(env_file, name, ["A"])
        process2, log2 = start_wrapper(name, env_file, Path(log_dir))
        try:
            second = Harness(client, upstream)
            await second.wait_health(port, process2)
            await second.check(f"{name}/restart-clean-boot", lambda: _boot(second, name))
        finally:
            await asyncio.to_thread(stop_process, process2)
            log2.close()
    return harness.checks, [c for c in harness.checks if not c["passed"]]


# ── scenario bodies ───────────────────────────────────────────────────────

async def _boot(h: Harness, name: str):
    keys = await h.pool_keys(name)
    assert len(keys) == 1, f"expected 1 key at boot, stats show {len(keys)}"
    status, _ = await h.chat(name)
    assert status == 200
    record = h.upstream.records[-1]
    assert record["key_fp"] == fingerprint(KEYS["A"]), f"boot request used {record['key_fp']}"
    h.upstream.records.clear()


async def _add_keys(h: Harness, name: str, env_file: Path):
    write_env(env_file, name, ["A", "B"])
    await h.wait_for(lambda: h.keys_equal(name, 2), what="2 eligible keys after add")
    await h.assert_pid_unchanged(name)
    seen = set()
    for _ in range(8):
        status, _ = await h.chat(name)
        assert status == 200
        seen.add(h.upstream.records[-1]["key_fp"])
    assert seen == {fingerprint(KEYS["A"]), fingerprint(KEYS["B"])}, \
        f"two-key distribution expected, saw {seen}"
    write_env(env_file, name, ["A", "B", "C", "D"])
    await h.wait_for(lambda: h.keys_equal(name, 4), what="4 eligible keys after scaling")
    await h.assert_pid_unchanged(name)
    h.upstream.records.clear()


async def _remove_one(h: Harness, name: str, env_file: Path, upstream: Upstream):
    write_env(env_file, name, ["B", "C", "D"])  # A removed
    await h.wait_for(lambda: h.keys_equal(name, 3), what="3 eligible keys after removing A")
    for _ in range(6):
        status, _ = await h.chat(name)
        assert status == 200
    used = {record["key_fp"] for record in upstream.records}
    assert fingerprint(KEYS["A"]) not in used, f"retired key A still served requests: {used}"
    await h.assert_pid_unchanged(name)
    upstream.records.clear()


async def _reorder(h: Harness, name: str, env_file: Path):
    before = await h.pool_keys(name)
    assert len(before) == 3
    total_before = sorted(int(k.get("total_requests", 0)) for k in before)
    write_env(env_file, name, ["D", "C", "B", "B"])  # reorder + duplicate value
    await h.wait_for(lambda: h.keys_equal(name, 3), what="3 keys after reorder+dedupe")
    after = await h.pool_keys(name)
    total_after = sorted(int(k.get("total_requests", 0)) for k in after)
    assert total_after == total_before, \
        f"reorder/duplicate reset per-key counters: {total_before} -> {total_after}"
    for key in after:
        assert int(key.get("in_flight", 0)) == 0, "reorder leaked in-flight reservations"
    await h.assert_pid_unchanged(name)


async def _state_retained(h: Harness, name: str, env_file: Path):
    before = await h.pool_keys(name)
    served_before = sum(int(k.get("total_requests", 0)) for k in before)
    assert served_before > 0, "expected request counters before reload"
    write_env(env_file, name, ["C", "B", "D", "E"])  # add E, keep B/C/D
    await h.wait_for(lambda: h.keys_equal(name, 4), what="4 keys after adding E")
    after = await h.pool_keys(name)
    served_after = sum(int(k.get("total_requests", 0)) for k in after)
    assert served_after >= served_before, \
        f"retained credential counters lost across reload: {served_before} -> {served_after}"
    await h.assert_pid_unchanged(name)


async def _zero_and_recover(h: Harness, name: str, env_file: Path):
    write_env(env_file, name, [])  # valid zero-key file
    await h.wait_for(lambda: h.keys_equal(name, 0), what="0 eligible keys after removing the last key")
    status, raw = await h.chat(name)
    assert status in (401, 429, 503), f"zero-key state must fail honestly, got {status}"
    body = json.loads(raw)
    assert "error" in body, f"zero-key response must carry an error body: {raw[:200]}"
    await h.assert_pid_unchanged(name)
    write_env(env_file, name, ["C"])
    await h.wait_for(lambda: h.keys_equal(name, 1), what="recovery to 1 key without restart")
    status, _ = await h.chat(name)
    assert status == 200
    assert h.upstream.records[-1]["key_fp"] == fingerprint(KEYS["C"])
    await h.assert_pid_unchanged(name)
    h.upstream.records.clear()


async def _retired_not_reselected(h: Harness, name: str, env_file: Path, upstream: Upstream):
    # One-shot 401 for the key that serves the first request, then removal of
    # that key while retries continue: the retry must land on a live key, never
    # back on the retired credential.
    write_env(env_file, name, ["C", "E"])
    await h.wait_for(lambda: h.keys_equal(name, 2), what="2 keys before retry race")
    first = upstream.records[-1]["key_fp"] if upstream.records else fingerprint(KEYS["C"])
    upstream.reject_once.add(first)
    write_env(env_file, name, ["E"])  # retire the other key during the burst
    status, _ = await h.chat(name)
    assert status == 200, "retry during reload must still succeed"
    for record in upstream.records:
        assert record["key_fp"] in {fingerprint(KEYS["C"]), fingerprint(KEYS["E"])}, \
            f"unexpected credential during concurrent retry: {record['key_fp']}"
    used = {record["key_fp"] for record in upstream.records}
    assert fingerprint(KEYS["E"]) in used, "no live key was used after retirement"
    upstream.records.clear()
    upstream.reject_once.clear()  # an unconsumed one-shot must not poison later scenarios


async def _atomic_replace(h: Harness, name: str, env_file: Path):
    tmp = env_file.parent / ".env.incoming"
    write_env(tmp, name, ["E", "C"])
    tmp.replace(env_file)  # atomic rename onto the watched path
    await h.wait_for(lambda: h.keys_equal(name, 2), what="atomic replace applied within bound")
    await h.assert_pid_unchanged(name)


async def _corrupt_then_repair(h: Harness, name: str, env_file: Path):
    before = await h.pool_keys(name)
    assert len(before) == 2
    write_env(env_file, name, [], corrupt=True)
    await asyncio.sleep(RELOAD_BOUND_SEC)  # beyond debounce + watcher latency
    after = await h.pool_keys(name)
    assert len(after) == 2, f"corrupt file must keep last-known-good, saw {len(after)} keys"
    status, _ = await h.chat(name)
    assert status == 200, "last-known-good credentials must keep serving during corruption"
    write_env(env_file, name, ["C"])
    await h.wait_for(lambda: h.keys_equal(name, 1), what="recovery after repair")
    status, raw = await h.chat(name)
    assert status == 200, f"recovered credential must serve after repair (HTTP {status}): {raw[:200]}"
    h.upstream.records.clear()


async def _stream_during_removal(h: Harness, name: str, env_file: Path):
    port = WRAPPERS[name][0]
    write_env(env_file, name, ["C", "E"])
    await h.wait_for(lambda: h.keys_equal(name, 2), what="2 keys before stream scenario")
    # Open a slow stream, then remove every key except one mid-flight.
    async def consume():
        chunks = []
        async with h.client.post(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                json={"model": "mock/hot-swap-stream-slow", "stream": True,
                      "messages": [{"role": "user", "content": "stream"}]},
                headers={"Authorization": f"Bearer {TOKEN}"},
                timeout=aiohttp.ClientTimeout(total=30)) as reply:
            assert reply.status == 200
            async for raw in reply.content:
                chunks.append(raw)
        return chunks

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.25)  # first frame received; stream is owned upstream
    write_env(env_file, name, ["E"])
    chunks = await task
    assert b"[DONE]" in b"".join(chunks), "active stream did not complete after key removal"
    # Reservation released exactly once: pool back to zero in-flight.
    async def _drained():
        keys = await h.pool_keys(name)
        return keys if keys and all(int(k.get("in_flight", 0)) == 0 for k in keys) else None
    await h.wait_for(_drained, what="in-flight reservations drained after stream")
    await h.assert_pid_unchanged(name)
    h.upstream.records.clear()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wrapper", choices=WRAPPERS, action="append")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    names = args.wrapper or list(WRAPPERS)
    assert_ports_free(UPSTREAM_PORT, *(WRAPPERS[name][0] for name in names))
    upstream = Upstream()
    app = web.Application()
    app.router.add_get("/v1/models", upstream.models)
    app.router.add_post("/v1/chat/completions", upstream.inference)
    app.router.add_post("/v1/messages", upstream.inference)
    app.router.add_post("/v1/responses", upstream.inference)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", UPSTREAM_PORT).start()
    checks = []
    try:
        with tempfile.TemporaryDirectory(prefix="hotswap-logs-") as log_dir:
            async with aiohttp.ClientSession() as client:
                for name in names:
                    provider_checks, _ = await run_provider(name, upstream, client, Path(log_dir))
                    checks.extend(provider_checks)
    finally:
        await runner.cleanup()
    result = {"scope": names, "upstream": "isolated-local-mock", "reload_bound_sec": RELOAD_BOUND_SEC,
              "checks": checks,
              "passed": sum(c["passed"] for c in checks),
              "failed": sum(not c["passed"] for c in checks)}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Hot-swap acceptance: {result['passed']} passed, {result['failed']} failed", flush=True)
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
