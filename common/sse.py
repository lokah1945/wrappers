#!/usr/bin/env python3
"""Shared SSE streaming primitives for all wrappers.

Extracted during the 2026-08-01 audit (finding B-08). Three wrappers had
independently converged on the correct "sentinel task + asyncio.wait" pattern
(nous N-05, blackbox BB-5/DR-1, opencode OC-4/DR-1) while openrouter and
common/base_wrapper.py still used `asyncio.wait_for`, which is subtly wrong:

  `asyncio.wait_for(it.__anext__(), timeout=hb)` CANCELS the pending read on
  timeout, and its asyncio.TimeoutError is indistinguishable from a genuine
  aiohttp socket read timeout. The result is that a DEAD upstream gets
  heartbeated forever and the client hangs until its own timeout expires,
  rather than the stream being finalized with a visible error.

`iter_chunks_with_idle` waits on a *retained* task instead: an unfinished task
after the wait window means "idle → heartbeat", while a real upstream error
surfaces from task.result() and is raised to the caller.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from contextvars import ContextVar
from typing import Any, AsyncIterator

import anyio
from starlette.responses import StreamingResponse

logger = logging.getLogger('wrapper-sse')

# Sentinel yielded when the upstream has been silent for the idle window.
IDLE = object()

# Read tasks inherit the response's context, including nested translators.
# Keeping ownership here lets disconnect cleanup await those reads before
# closing generators that would otherwise still be running.
_response_reads: ContextVar[dict | None] = ContextVar('wrapper_response_reads', default=None)


async def close_stream(stream: Any, on_close=None) -> None:
    """Close an iterator and its explicitly transferred resource ownership."""
    with anyio.CancelScope(shield=True):
        try:
            close = getattr(stream, 'aclose', None)
            if close is not None:
                await close()
        finally:
            if on_close is not None:
                result = on_close()
                if inspect.isawaitable(result):
                    await result


class ClosingStreamingResponse(StreamingResponse):
    """Release streams on transport failure, cancellation and normal EOF.

    Starlette does not close the body iterator when ASGI 2.4 ``send`` fails.
    ``on_close`` covers resources acquired before the first generator step;
    closing an unstarted async generator alone cannot run its finally block.
    """

    def __init__(self, *args, on_close=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_close = on_close
        self._closed = False
        self._read_tasks: dict = {}

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        with anyio.CancelScope(shield=True):
            # In insertion order, outer reads finish their own nested cleanup
            # before any remaining inner task is cancelled independently.
            while self._read_tasks:
                task = next(iter(self._read_tasks))
                self._read_tasks.pop(task, None)
                if not task.done():
                    task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, StopAsyncIteration, Exception):
                    pass
            await close_stream(self.body_iterator, self.on_close)

    async def __call__(self, scope, receive, send) -> None:
        token = _response_reads.set(self._read_tasks)
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await self.aclose()
            finally:
                _response_reads.reset(token)


async def iter_chunks_with_idle(resp: Any, idle_sec: float) -> AsyncIterator[Any]:
    """Yield upstream chunks, plus the IDLE sentinel during silent gaps.

    B-08 fix. Callers emit heartbeats on IDLE so reasoning models that think
    for 30+ seconds don't trip client/LB idle timeouts, while a genuine
    upstream failure still propagates as an exception instead of being
    swallowed as an idle tick.

    Args:
        resp: an aiohttp ClientResponse (uses ``resp.content.iter_any()``) or
            any object exposing an async iterator via ``__aiter__``.
        idle_sec: seconds of silence before yielding IDLE.

    Raises:
        Whatever the upstream iterator raises (aiohttp.ClientError,
        asyncio.TimeoutError from a real sock_read timeout, ...).
    """
    content = getattr(resp, 'content', None)
    if content is not None and hasattr(content, 'iter_any'):
        chunk_iter = content.iter_any().__aiter__()
    else:
        chunk_iter = resp.__aiter__()

    chunk_task = None
    owned_reads = _response_reads.get()
    try:
        while True:
            if chunk_task is None:
                chunk_task = asyncio.ensure_future(chunk_iter.__anext__())
                if owned_reads is not None:
                    owned_reads[chunk_task] = None
            done_set, _pending = await asyncio.wait({chunk_task}, timeout=idle_sec)
            if not done_set:
                # Task still running → upstream is genuinely idle, not dead.
                yield IDLE
                continue
            finished, chunk_task = chunk_task, None
            if owned_reads is not None:
                owned_reads.pop(finished, None)
            try:
                chunk = finished.result()
            except StopAsyncIteration:
                return
            yield chunk
    finally:
        if chunk_task is not None:
            if owned_reads is not None:
                owned_reads.pop(chunk_task, None)
            chunk_task.cancel()
            try:
                await chunk_task
            except (asyncio.CancelledError, StopAsyncIteration, Exception):
                pass
        await close_stream(chunk_iter)


def normalize_sse_newlines(buffer: bytes) -> bytes:
    """Normalize CRLF SSE framing to LF.

    Parity fix (nous N-08): upstreams that frame with ``\\r\\n`` otherwise never
    match a ``\\n\\n`` split, so the whole response accumulates in the buffer
    until EOF instead of streaming incrementally.
    """
    if b'\r' in buffer:
        return buffer.replace(b'\r\n', b'\n')
    return buffer
