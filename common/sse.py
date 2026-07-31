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
import logging
from typing import Any, AsyncIterator

logger = logging.getLogger('wrapper-sse')

# Sentinel yielded when the upstream has been silent for the idle window.
IDLE = object()


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
    try:
        while True:
            if chunk_task is None:
                chunk_task = asyncio.ensure_future(chunk_iter.__anext__())
            done_set, _pending = await asyncio.wait({chunk_task}, timeout=idle_sec)
            if not done_set:
                # Task still running → upstream is genuinely idle, not dead.
                yield IDLE
                continue
            finished, chunk_task = chunk_task, None
            try:
                chunk = finished.result()
            except StopAsyncIteration:
                return
            yield chunk
    finally:
        if chunk_task is not None:
            chunk_task.cancel()
            try:
                await chunk_task
            except (asyncio.CancelledError, StopAsyncIteration, Exception):
                pass


def normalize_sse_newlines(buffer: bytes) -> bytes:
    """Normalize CRLF SSE framing to LF.

    Parity fix (nous N-08): upstreams that frame with ``\\r\\n`` otherwise never
    match a ``\\n\\n`` split, so the whole response accumulates in the buffer
    until EOF instead of streaming incrementally.
    """
    if b'\r' in buffer:
        return buffer.replace(b'\r\n', b'\n')
    return buffer
