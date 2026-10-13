"""Release fixtures must actually emit the HTTP/SSE failures they claim."""
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tests.e2e_runtime import mock_upstream


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mode,status", [("http500", 500), ("http429", 429)])
async def test_pre_stream_rejection_sends_real_http_error(stream, mode, status):
    app = web.Application()
    app.router.add_post("/v1/chat/completions", mock_upstream.chat_completions)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/v1/chat/completions", json={
            "model": f"mock/{mode}", "stream": stream,
            "messages": [{"role": "user", "content": "fixture"}],
        })
        assert response.status == status
        assert response.content_type == "application/json"
        assert "error" in await response.json()
        if status == 429:
            assert response.headers["Retry-After"] == "1"


@pytest.mark.parametrize("mode", ["nofinish", "noterminator", "midstream_error"])
async def test_native_eof_fixture_preserves_its_declared_wire_shape(mode):
    app = web.Application()
    app.router.add_post("/v1/chat/completions", mock_upstream.chat_completions)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/v1/chat/completions", json={
            "model": f"mock/{mode}", "stream": True,
            "messages": [{"role": "user", "content": "fixture"}],
        })
        assert response.status == 200
        raw = await response.read()
        if mode == "noterminator":
            assert raw.endswith(b"data: [DONE]")
            assert not raw.endswith(b"\n")
        elif mode == "nofinish":
            assert b"[DONE]" not in raw
            assert b'"finish_reason": "stop"' not in raw
            assert b"from mock upstream." in raw
        else:
            assert b"[DONE]" not in raw
            assert b"upstream exploded" in raw
