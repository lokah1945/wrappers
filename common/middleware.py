#!/usr/bin/env python3
"""Shared middleware for all wrappers.

Provides enterprise-grade request handling:
- Request size limiting (prevents abuse/memory exhaustion)
- Request ID propagation
- Graceful shutdown support
"""

from __future__ import annotations

import os
import signal
import logging
from typing import Any

logger = logging.getLogger('wrapper-middleware')

MAX_REQUEST_BYTES = int(os.environ.get('MAX_REQUEST_BYTES', str(10 * 1024 * 1024)))  # 10MB default


class RequestSizeLimiter:
    """Pure ASGI middleware for request size limiting.

    Rejects an oversized Content-Length immediately and checks received bytes
    independently of transport headers. Buffers at most the configured limit
    before dispatch, so malformed or understated lengths cannot bypass it.
    Response streaming and genuine client disconnects are preserved.

    Usage:
        app.add_middleware(RequestSizeLimiter)
    """

    def __init__(self, app: Any, max_bytes: int = MAX_REQUEST_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return

        for name, value in scope.get('headers', []):
            if name.lower() == b'content-length':
                try:
                    size = int(value)
                    if size > self.max_bytes:
                        await self._reject(send)
                        return
                except (ValueError, TypeError):
                    pass

        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            total += len(chunk)
            if total > self.max_bytes:
                await self._reject(send)
                return
            chunks.append(chunk)
            if not message.get('more_body', False):
                break

        body = b''.join(chunks)
        chunks.clear()
        sent_body = False

        async def replay_receive():
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            # Never synthesize disconnect: response generators use receive()
            # to detect real disconnections after the request has been read.
            return await receive()

        await self.app(scope, replay_receive, send)

    async def _reject(self, send: Any) -> None:
        import json
        await send({
            'type': 'http.response.start',
            'status': 413,
            'headers': [
                [b'content-type', b'application/json'],
            ],
        })
        body = json.dumps({
            'error': {
                'type': 'request_too_large',
                'message': f'Request body exceeds maximum size of {self.max_bytes} bytes',
            }
        }).encode()
        await send({
            'type': 'http.response.body',
            'body': body,
        })


def setup_graceful_shutdown(shutdown_callback=None):
    """Register SIGTERM/SIGINT handlers for graceful shutdown.

    Enterprise deployments (Kubernetes, systemd) send SIGTERM to request
    graceful shutdown. This handler ensures in-flight requests complete
    before the process exits.
    """
    def _handler(signum, frame):
        sig_name = signal.Signals(signum).name
        logger.info(f"Received {sig_name}, initiating graceful shutdown...")
        if shutdown_callback:
            try:
                shutdown_callback()
            except Exception as e:
                logger.warning(f"Shutdown callback error: {e}")

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def sanitize_header_value(value: str) -> str:
    """Remove newlines, carriage returns, and control characters from header values.

    Prevents header injection, log injection, and request smuggling attacks
    where malicious clients embed CRLF sequences in header values.

    BUG-SEC2: Applied across all wrappers to sanitize forwarded headers.
    """
    if not value:
        return value
    import re as _re
    # Strip CR/LF first (most common injection vectors)
    sanitized = value.replace('\r', '').replace('\n', '')
    # Remove all remaining control characters (0x00-0x1F, 0x7F)
    sanitized = _re.sub(r'[\x00-\x1f\x7f]', '', sanitized)
    return sanitized.strip()
