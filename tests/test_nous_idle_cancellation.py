"""Idle disconnects must close actual Nous route readers and credential owners.

All upstreams, credentials and ASGI transports are controlled fixtures. The
production functions are loaded through the active-scope route fixture without
starting a wrapper, reading its environment, or performing inference.
"""
from __future__ import annotations

import asyncio

import anyio
import pytest

from test_active_native_passthrough import _request
from test_nous_stream_transport_cleanup import MODES, route_fixture


def first_upstream_chunk(mode):
    if mode in {'chat_cross', 'responses_cross'}:
        return (
            b'event: message_start\n'
            b'data: {"type":"message_start","message":{"id":"fixture",'
            b'"type":"message","role":"assistant","model":"exact/model",'
            b'"content":[],"usage":{"input_tokens":1,"output_tokens":0}}}\n\n'
            b'event: content_block_start\n'
            b'data: {"type":"content_block_start","index":0,'
            b'"content_block":{"type":"text","text":""}}\n\n'
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"fixture"}}\n\n'
        )
    if mode in {'responses_chat', 'messages_cross'}:
        return (
            b'data: {"id":"fixture","model":"exact/model","choices":'
            b'[{"index":0,"delta":{"role":"assistant","content":"fixture"},'
            b'"finish_reason":null}]}\n\n'
        )
    return b'event: future_native_event\ndata: {"future":true}\n\n'


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('disconnect', ['asyncio_cancel', 'anyio_disconnect'])
async def test_idle_cancellation_closes_reader_and_releases_nous_key_once(mode, disconnect):
    namespace, upstream, key, releases = route_fixture(mode)
    first_chunk, reading, reader_closed, first_body = (
        asyncio.Event() for _ in range(4)
    )

    async def chunks():
        try:
            first_chunk.set()
            yield first_upstream_chunk(mode)
            reading.set()
            await asyncio.Future()
        finally:
            reader_closed.set()

    async def release(entry):
        # Model a contended key-pool lock: cancellation must not skip the
        # checkpoint after the owner has marked itself released.
        await anyio.sleep(0)
        if entry is not None:
            releases.append(entry)

    upstream.iter_any = chunks
    namespace['KEY_POOL'].release = release
    surface = mode.split('_', 1)[0]
    route = 'chat_completions' if surface == 'chat' else surface
    response = await namespace[route](_request({
        'model': 'exact/model', 'stream': True, 'max_tokens': 128,
        'messages': [{'role': 'user', 'content': 'fixture'}],
    }))

    async def receive():
        if disconnect == 'anyio_disconnect':
            await reading.wait()
            await first_body.wait()
            return {'type': 'http.disconnect'}
        await asyncio.Future()

    async def send(message):
        if message['type'] == 'http.response.body' and message.get('body'):
            first_body.set()

    spec_version = '2.4' if disconnect == 'asyncio_cancel' else '2.0'
    task = asyncio.create_task(response(
        {'type': 'http', 'asgi': {'spec_version': spec_version}}, receive, send,
    ))
    try:
        await asyncio.wait_for(asyncio.gather(reading.wait(), first_body.wait()), 2)
        assert first_chunk.is_set()
        if disconnect == 'asyncio_cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        else:
            await asyncio.wait_for(task, 2)

        # Verify cleanup completed with the request, before any explicit close
        # could repair a leaked reader or reservation.
        assert reader_closed.is_set()
        assert not response._read_tasks
        assert upstream.release_count == 1
        assert releases == [key]
        await response.aclose()
        assert upstream.release_count == 1 and releases == [key]
    finally:
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        await response.aclose()
