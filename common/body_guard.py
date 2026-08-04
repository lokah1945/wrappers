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
        # Round-4 audit: INFERENCE surfaces are always inspected regardless of
        # Content-Type — a client labelling a JSON body text/plain otherwise
        # bypassed the guard and detonated the same handler-side AttributeErrors
        # (Starlette's request.json() parses regardless of Content-Type).
        if ctype and 'json' not in ctype and not _is_inference_surface(path):
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
            except RecursionError:
                # B-25.1 closure: syntactically VALID JSON nested deeper than
                # the interpreter limit raises RecursionError, not ValueError —
                # it escaped this guard AND would crash the route's own
                # request.json() the same way, producing an unshaped 500.
                # No downstream parser can accept this body, so the guard is
                # the only place that CAN shape the rejection (CONTRACT §4:
                # malformed body ⇒ shaped 4xx, never 5xx).
                logger.warning('[body-guard] rejecting over-deep nested JSON body on %s', path)
                await _reject(send, path,
                              'Request body JSON is nested too deeply to be parsed safely. '
                              'Flatten the structure (e.g. fewer nested arrays/objects).')
                return
            except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
                # Malformed JSON: routes already return a shaped 400 for this,
                # and their message includes the parser detail. Pass through.
                parsed = None
            else:
                # Round-4 audit (2026-08-03): a parsed body of JSON `null`
                # decodes to None and used to take the same pass-through path
                # as unparseable JSON — the route then did `body.get(...)` on
                # None and 500'd on EVERY wrapper. Parse failure and JSON-null
                # must be distinguished.
                if parsed is None:
                    logger.warning('[body-guard] rejecting JSON null body on %s', path)
                    await _reject(send, path, 'Request body must be a JSON object, got null. '
                                             'Send an object like {"model": "...", "messages": [...]}.')
                    return
                if not isinstance(parsed, dict):
                    logger.warning(
                        '[body-guard] rejecting non-object JSON body on %s (%s)',
                        path, type(parsed).__name__)
                    await _reject(send, path,
                                  f'Request body must be a JSON object, got {type(parsed).__name__}. '
                                  f'Send an object like {{"model": "...", "messages": [...]}}.')
                    return
                # Round-4 audit: on the LLM inference surfaces, VALID JSON with
                # broken semantics (messages: ["hi"], tools: {"name": ...},
                # max_tokens: "lots", model: 42, content blocks that are bare
                # strings) escaped every handler's validation and detonated an
                # AttributeError mid-handler (500 — or a 502 blaming the
                # upstream). Validate the structural contract once, here, for
                # every wrapper (CONTRACT §4: malformed ⇒ shaped 4xx, never 5xx).
                if _is_inference_surface(path):
                    sem_err = _semantic_error(parsed, path)
                    if sem_err:
                        logger.warning('[body-guard] rejecting %s: %s', path, sem_err)
                        await _reject(send, path, sem_err)
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


# Surfaces whose request bodies follow the OpenAI/Anthropic message contract.
# Only these get SEMANTIC validation; management/catalog endpoints are left
# alone (they own their own body shapes).
_INFERENCE_SURFACES: tuple[str, ...] = (
    '/v1/chat/completions',
    '/v1/completions',
    '/v1/messages',          # covers /v1/messages/count_tokens
    '/v1/responses',
    '/v1/embeddings',
    '/v1/ranking',
    '/v1/images',
)

_MAX_TOKENS_CAP = 1_000_000  # CONTRACT §4


def _is_inference_surface(path: str) -> bool:
    return any(path.startswith(p) for p in _INFERENCE_SURFACES)


def _semantic_error(body: dict, path: str) -> 'str | None':
    """Structural contract for message-style inference bodies.

    Returns a human-readable error string, or None when the body is
    structurally sound. Only shapes that would otherwise detonate an
    AttributeError/TypeError inside a handler or translator are rejected —
    everything else is forwarded verbatim (transparent-proxy principle):
    unknown roles, odd parameter *values*, unrecognised block types are the
    upstream's business, not the wrapper's.
    """
    model = body.get('model')
    if model is not None and not isinstance(model, str):
        return f'model must be a string, got {type(model).__name__}'

    msgs = body.get('messages')
    if msgs is not None:
        if not isinstance(msgs, list):
            return f'messages must be an array, got {type(msgs).__name__}'
        for i, m in enumerate(msgs):
            if not isinstance(m, dict):
                return f'messages[{i}] must be an object, got {type(m).__name__}'
            c = m.get('content')
            if isinstance(c, list):
                for j, blk in enumerate(c):
                    if not isinstance(blk, dict):
                        return (f'messages[{i}].content[{j}] must be an object '
                                f'({{"type": ...}} block), got {type(blk).__name__}')
            tcs = m.get('tool_calls')
            if tcs is not None:
                if not isinstance(tcs, list):
                    return f'messages[{i}].tool_calls must be an array'
                for j, tc in enumerate(tcs):
                    if not isinstance(tc, dict):
                        return (f'messages[{i}].tool_calls[{j}] must be an object, '
                                f'got {type(tc).__name__}')

    tools = body.get('tools')
    if tools is not None:
        if not isinstance(tools, list):
            return f'tools must be an array, got {type(tools).__name__}'
        for i, t in enumerate(tools):
            if not isinstance(t, dict):
                return f'tools[{i}] must be an object, got {type(t).__name__}'

    for field in ('max_tokens', 'max_output_tokens', 'max_completion_tokens'):
        v = body.get(field)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, int):
            return f'{field} must be a positive integer, got {v!r}'
        if v <= 0:
            return f'{field} must be a positive integer'
        if v > _MAX_TOKENS_CAP:
            return f'{field} exceeds maximum allowed value of {_MAX_TOKENS_CAP}'

    if path.startswith('/v1/responses'):
        inp = body.get('input')
        if inp is not None:
            if isinstance(inp, list):
                for i, item in enumerate(inp):
                    if not isinstance(item, dict):
                        return f'input[{i}] must be an object, got {type(item).__name__}'
            elif not isinstance(inp, str):
                return f'input must be a string or an array, got {type(inp).__name__}'

    system = body.get('system')
    if system is not None and not isinstance(system, str):
        if isinstance(system, list):
            for i, blk in enumerate(system):
                if not isinstance(blk, dict):
                    return f'system[{i}] must be an object, got {type(blk).__name__}'
        else:
            return f'system must be a string or an array, got {type(system).__name__}'

    return None


async def _reject(send: Any, path: str, message: str) -> None:
    if _is_anthropic_surface(path):
        payload = {'type': 'error',
                   'error': {'type': 'invalid_request_error', 'message': message}}
    else:
        payload = {'error': {'type': 'invalid_request_error', 'message': message,
                             'code': 'invalid_request'}}
    body = json.dumps(payload).encode()
    await send({'type': 'http.response.start', 'status': 400,
                'headers': [(b'content-type', b'application/json'),
                            (b'content-length', str(len(body)).encode())]})
    await send({'type': 'http.response.body', 'body': body})
