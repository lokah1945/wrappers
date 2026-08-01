#!/usr/bin/env python3
"""JSON body shape guard — shared across all wrappers.

RUNTIME FINDING R-01 (2026-08-01 runtime E2E):
Every wrapper returned **HTTP 500 Internal Server Error** when a client POSTed
a syntactically valid JSON body that was not an object, e.g. `[1,2,3]` or
`"str"` or `42`. The handlers immediately call `body.get(...)`, and
`list.get` / `str.get` raise AttributeError, which escapes as a 500 with an
"Exception in ASGI application" traceback in the logs.

Real agents hit this: a client bug, a retried/truncated payload, or a proxy
that re-encodes the body can all produce a non-object. A 500 tells the SDK the
*server* is broken (many SDKs then retry, amplifying load) instead of telling
it the *request* was malformed.

opencode already guarded 3 of its routes inline (its `F6` fix) but the other
four wrappers had no guard at all, and even opencode left several POST routes
uncovered — a textbook cross-wrapper parity gap.

Fixing this at 31 individual `await request.json()` call sites would be
fragile and would regress the moment someone adds a route. This middleware
enforces it once, for every current and future JSON POST/PUT/PATCH route.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger('wrapper-body-guard')

# Surfaces that legitimately accept a non-object JSON body (none today, but
# keep the hook so a future batch endpoint can opt out explicitly).
_ALLOW_NON_OBJECT_PATHS: frozenset[str] = frozenset()

_METHODS_WITH_BODY = (b'POST', b'PUT', b'PATCH')


class JSONBodyGuard:
    """Pure-ASGI middleware rejecting non-object JSON bodies with a 400.

    Buffers the request body once, validates its shape, and replays it to the
    downstream app so handlers still read it normally. Streaming *responses*
    are unaffected — only the request body is buffered, and only for methods
    that carry one.

    The error envelope shape differs per provider surface, so it is chosen
    from the request path: Anthropic surfaces get {"type":"error","error":{…}},
    everything else gets the OpenAI {"error":{…}} shape.
    """

    def __init__(self, app: Any, max_buffer: int = 64 * 1024 * 1024):
        self.app = app
        self.max_buffer = max_buffer

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return

        method = scope.get('method', '').encode() if isinstance(scope.get('method'), str) \
            else scope.get('method', b'')
        if method not in _METHODS_WITH_BODY:
            await self.app(scope, receive, send)
            return

        path = scope.get('path', '')
        if path in _ALLOW_NON_OBJECT_PATHS:
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get('headers', [])}
        ctype = headers.get(b'content-type', b'').decode('latin-1', 'replace').lower()
        # Only inspect JSON payloads; leave multipart/binary uploads alone.
        if ctype and 'json' not in ctype:
            await self.app(scope, receive, send)
            return

        # Buffer the body.
        chunks: list[bytes] = []
        total = 0
        more = True
        while more:
            msg = await receive()
            if msg['type'] == 'http.disconnect':
                # Client vanished before we saw the whole body; let the app
                # observe the disconnect normally.
                await self.app(scope, _replay([], receive, disconnect=True), send)
                return
            body = msg.get('body', b'') or b''
            total += len(body)
            if total > self.max_buffer:
                # Too large to inspect; hand off untouched (the size limiter
                # middleware is responsible for rejecting oversized requests).
                chunks.append(body)
                more = msg.get('more_body', False)
                while more:
                    m2 = await receive()
                    chunks.append(m2.get('body', b'') or b'')
                    more = m2.get('more_body', False)
                await self.app(scope, _replay(chunks, receive), send)
                return
            chunks.append(body)
            more = msg.get('more_body', False)

        raw = b''.join(chunks)

        # An empty body is handled by the route's own validation (some routes
        # legitimately accept no body, e.g. the openrouter key-management ones).
        if raw.strip():
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
                # Malformed JSON: routes already return a shaped 400 for this,
                # and their message includes the parser detail. Pass through.
                parsed = None
            if parsed is not None and not isinstance(parsed, dict):
                logger.warning(
                    '[body-guard] rejecting non-object JSON body on %s (%s)',
                    path, type(parsed).__name__)
                await _reject(send, path, type(parsed).__name__)
                return

        await self.app(scope, _replay(chunks, receive), send)


def _replay(chunks: list[bytes], original_receive: Any, disconnect: bool = False):
    """Build a receive() callable that replays the buffered body.

    Emits the whole body as ONE `http.request` message with `more_body: False`,
    then DELEGATES to the original receive() for everything after.

    Two subtleties, both discovered the hard way during the runtime E2E run:

    1. Replaying chunk-by-chunk breaks Starlette's BaseHTTPMiddleware, which
       raises `RuntimeError: Unexpected message received: http.request` when it
       sees a second http.request after the body is complete.

    2. Synthesising `http.disconnect` once the body is exhausted looks correct
       but silently BREAKS EVERY STREAMING RESPONSE: StreamingResponse runs a
       disconnect-watcher task that calls receive(), and an immediate
       http.disconnect makes it conclude the client went away and cancel the
       stream after the first event. Delegating to the real receive() keeps
       genuine disconnect detection working (which the pool relies on to
       release keys) while never fabricating one.
    """
    body = b''.join(chunks)
    state = {'sent_body': False}

    async def receive():
        if disconnect:
            return {'type': 'http.disconnect'}
        if not state['sent_body']:
            state['sent_body'] = True
            return {'type': 'http.request', 'body': body, 'more_body': False}
        # Body fully delivered — hand back to the server so real disconnects
        # (and nothing else) reach the application.
        return await original_receive()

    return receive


def _is_anthropic_surface(path: str) -> bool:
    return path.startswith('/v1/messages') or path.startswith('/v1/complete')


async def _reject(send: Any, path: str, got: str) -> None:
    msg = (f'Request body must be a JSON object, got {got}. '
           f'Send an object like {{"model": "...", "messages": [...]}}.')
    if _is_anthropic_surface(path):
        payload = {'type': 'error',
                   'error': {'type': 'invalid_request_error', 'message': msg}}
    else:
        payload = {'error': {'type': 'invalid_request_error', 'message': msg,
                             'code': 'invalid_body_shape'}}
    body = json.dumps(payload).encode()
    await send({'type': 'http.response.start', 'status': 400,
                'headers': [(b'content-type', b'application/json'),
                            (b'content-length', str(len(body)).encode())]})
    await send({'type': 'http.response.body', 'body': body})
