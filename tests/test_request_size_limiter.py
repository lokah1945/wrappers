"""Transport-independent body caps and response-streaming regressions."""

import asyncio

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from common.middleware import RequestSizeLimiter


async def invoke(app, chunks, headers=(), method="POST"):
    messages = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]
    sent = []
    disconnected = asyncio.Event()

    async def receive():
        if messages:
            return messages.pop(0)
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"},
        "method": method, "path": "/", "raw_path": b"/", "query_string": b"",
        "headers": list(headers), "http_version": "1.1", "scheme": "http",
        "server": ("test", 80), "client": ("test", 1234),
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=1)
    return sent


@pytest.mark.parametrize("headers", [
    [], [(b"transfer-encoding", b"chunked")], [(b"content-length", b"1")],
    [(b"content-length", b"bad")], [(b"content-length", b"-1")],
])
async def test_actual_bytes_cannot_bypass_cap(headers):
    async def downstream(scope, receive, send):
        while (await receive()).get("more_body", False):
            pass
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"accepted"})

    sent = await invoke(RequestSizeLimiter(downstream, max_bytes=4), [b"abc", b"de"], headers)
    assert [message["status"] for message in sent if message["type"] == "http.response.start"] == [413]


async def test_declared_oversize_rejected_without_reading_body():
    async def unexpected(*args):
        raise AssertionError("oversized Content-Length must reject before reading/dispatch")

    sent = []

    async def send(message):
        sent.append(message)

    await RequestSizeLimiter(unexpected, max_bytes=4)(
        {"type": "http", "headers": [(b"content-length", b"5")]}, unexpected, send,
    )
    assert sent[0]["status"] == 413


async def test_exact_limit_binary_body_survives_base_http_middleware_and_streams():
    app = FastAPI()

    @app.middleware("http")
    async def relay(request, call_next):
        return await call_next(request)

    @app.post("/")
    async def echo(request: Request):
        body = await request.body()

        async def stream():
            yield body[:2]
            await asyncio.sleep(0)
            yield body[2:]

        return StreamingResponse(stream(), media_type="application/octet-stream")

    app.add_middleware(RequestSizeLimiter, max_bytes=4)
    sent = await invoke(app, [b"\x00\x01", b"\xff\xfe"], [(b"content-type", b"application/octet-stream")])
    assert sent[0]["status"] == 200
    assert b"".join(message.get("body", b"") for message in sent) == b"\x00\x01\xff\xfe"
    assert sent[-1].get("more_body", False) is False


async def test_oversize_does_not_escape_base_http_middleware_or_route_error_handler():
    app = FastAPI()

    @app.middleware("http")
    async def relay(request, call_next):
        return await call_next(request)

    @app.post("/")
    async def echo(request: Request):
        try:
            return Response(await request.body())
        except Exception:
            return Response("route swallowed error", status_code=500)

    sent = await invoke(RequestSizeLimiter(app, max_bytes=4), [b"abc", b"de"], [(b"transfer-encoding", b"chunked")])
    assert sent[0]["status"] == 413
    assert len([message for message in sent if message["type"] == "http.response.start"]) == 1


async def test_request_disconnect_before_complete_does_not_dispatch():
    called = False
    received = iter([
        {"type": "http.request", "body": b"ab", "more_body": True},
        {"type": "http.disconnect"},
    ])

    async def receive():
        return next(received)

    async def downstream(*args):
        nonlocal called
        called = True

    async def send(message):
        raise AssertionError("do not respond after request disconnect")

    await RequestSizeLimiter(downstream, max_bytes=4)({"type": "http"}, receive, send)
    assert called is False


async def test_non_http_scope_passes_through():
    seen = []

    async def downstream(scope, receive, send):
        seen.append(scope)

    scope = {"type": "lifespan"}
    await RequestSizeLimiter(downstream)(scope, None, None)
    assert seen == [scope]
