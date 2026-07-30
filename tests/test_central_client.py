import asyncio

from common.model.central_client import ModelRegistryClient


def test_observation_queue_is_bounded_and_drops_without_blocking(monkeypatch):
    monkeypatch.setenv("MODEL_REGISTRY_OBSERVATION_QUEUE", "1")

    async def run():
        client = ModelRegistryClient("http://registry.test")
        client.schedule_observation("nvidia", "provider/model-a", "scope", "available", 200, "OK", "", "chat")
        client.schedule_observation("nvidia", "provider/model-b", "scope", "available", 200, "OK", "", "chat")
        assert client._queue.qsize() == 1
        assert client.dropped_observations == 1
        await client.stop()

    asyncio.run(run())


def test_disabled_client_does_not_enqueue_or_open_connections():
    client = ModelRegistryClient("")
    client.schedule_observation("nvidia", "provider/model-a", "scope", "available", 200, "OK", "", "chat")
    assert client._queue.qsize() == 0
    assert client.enabled is False
