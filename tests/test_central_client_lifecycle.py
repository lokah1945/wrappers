"""Best-effort registry writes drain or are counted when shutdown expires."""

import asyncio

from common.model.central_client import ModelRegistryClient


async def test_stop_drains_catalog_and_observations_even_before_worker_started(monkeypatch):
    client = ModelRegistryClient("http://registry.test")
    posted = []

    async def post(path, payload):
        await asyncio.sleep(0)
        posted.append((path, payload))
        return True

    monkeypatch.setattr(client, "_post", post)
    client.schedule_catalog("nvidia", [{"id": "vendor/model"}], "r1")
    client.schedule_observation("nvidia", "vendor/model", "scope", "available", 200, "OK", "", "chat")
    await client.stop()
    assert [path for path, _ in posted] == ["/internal/catalog", "/internal/observations"]
    assert client._queue.qsize() == 0
    assert client.dropped_observations == 0
    assert client._worker is None
    assert client._start_task is None
    await asyncio.wait_for(client._queue.join(), timeout=0.1)


async def test_stop_bounds_hung_write_counts_drops_and_closes_session(monkeypatch):
    monkeypatch.setenv("MODEL_REGISTRY_DRAIN_TIMEOUT_SEC", "0.02")
    client = ModelRegistryClient("http://registry.test")
    started = asyncio.Event()

    async def post(path, payload):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(client, "_post", post)
    await client.start()
    session = client._session
    client.schedule_catalog("nvidia", [{"id": "vendor/a"}], "r1")
    client.schedule_catalog("nvidia", [{"id": "vendor/b"}], "r1")
    await started.wait()
    await asyncio.wait_for(client.stop(), timeout=0.3)
    assert session.closed
    assert client.dropped_observations == 2
    assert client._worker is None
    assert client._session is None
    assert client._queue.qsize() == 0
    await asyncio.wait_for(client._queue.join(), timeout=0.1)


async def test_closed_client_does_not_restart_implicitly(monkeypatch):
    client = ModelRegistryClient("http://registry.test")
    await client.stop()
    client.schedule_catalog("nvidia", [{"id": "vendor/model"}], "r1")
    assert client.dropped_observations == 1
    assert client._queue.empty()
    assert client._start_task is None
    assert await client._ensure_session() is None
    # An explicit lifecycle restart is supported for app reloads/test clients.
    await client.start()
    assert client._session is not None
    await client.stop()
    await client.stop()


async def test_session_creation_failure_is_best_effort(monkeypatch):
    client = ModelRegistryClient("http://registry.test")

    async def unavailable_session():
        raise OSError("session setup unavailable")

    monkeypatch.setattr(client, "_ensure_session", unavailable_session)
    assert await client.ingest_catalog("nvidia", [{"id": "vendor/model"}], "r1") is False
    assert client.failed_posts == 1
    assert client.consecutive_failures == 1
    await client.stop()
