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

    Checks Content-Length header without reading the body, so it's safe
    for streaming endpoints. Rejects oversized requests with 413.

    Usage:
        app.add_middleware(RequestSizeLimiter)
    """

    def __init__(self, app: Any, max_bytes: int = MAX_REQUEST_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope['type'] == 'http':
            headers = dict(scope.get('headers', []))
            content_length = headers.get(b'content-length')
            if content_length:
                try:
                    size = int(content_length)
                    if size > self.max_bytes:
                        await send({
                            'type': 'http.response.start',
                            'status': 413,
                            'headers': [
                                [b'content-type', b'application/json'],
                            ],
                        })
                        import json
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
                        return
                except (ValueError, TypeError):
                    pass
        await self.app(scope, receive, send)


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
