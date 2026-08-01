#!/usr/bin/env python3
"""Shared authentication primitives for all wrappers.

Extracted during the 2026-08-01 audit (findings B-28/B-29/B-30/B-31) to
eliminate five divergent auth implementations that each failed differently:

  * B-28 — three wrappers failed **open** when BEARER_TOKEN was unset, turning
    a protected inference proxy into an open relay on a truncated .env.
  * B-29 — nous compared against a module-level constant captured at import,
    so token rotation (and revocation!) required a full restart.
  * B-30 — `hmac.compare_digest` with two `str` arguments raises TypeError on
    non-ASCII input, surfacing as an unhandled 500 instead of a clean 401.

All wrappers now delegate here. Behaviour is identical across the fleet; only
the error envelope shape differs (OpenAI vs Anthropic), which callers supply.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Optional

logger = logging.getLogger('wrapper-auth')

_TRUE = ('1', 'true', 'yes', 'on', 'y')
_FALSE = ('0', 'false', 'no', 'off', 'n')


def bearer_token(env_var: str = 'BEARER_TOKEN') -> str:
    """Read the configured bearer token.

    B-29 fix: read from os.environ on EVERY call so .env hot-reload (watchdog)
    and credential rotation take effect without a restart, and so a revoked
    token stops working immediately.
    """
    return (os.environ.get(env_var) or '').strip()


def auth_disabled() -> bool:
    """True when the operator explicitly disabled auth (open LAN mode)."""
    return (os.environ.get('DISABLE_AUTH') or '').strip().lower() in _TRUE


def require_auth() -> bool:
    """B-28 fix: default to failing CLOSED when no token is configured.

    Set REQUIRE_AUTH=false to opt into the legacy fail-open behaviour for
    deliberately open deployments.
    """
    raw = (os.environ.get('REQUIRE_AUTH') or '').strip().lower()
    if raw in _FALSE:
        return False
    return True


def extract_client_token(headers) -> str:
    """Pull the caller's credential from either SDK's header.

    OpenAI SDKs send `Authorization: Bearer <t>`; Anthropic SDKs send
    `x-api-key: <t>`. A malformed Authorization header must not mask a valid
    x-api-key, so both are considered independently.
    """
    try:
        auth = (headers.get('authorization') or '').strip()
    except Exception:
        auth = ''
    try:
        api_key = (headers.get('x-api-key') or '').strip()
    except Exception:
        api_key = ''

    if auth:
        if auth.lower().startswith('bearer '):
            return auth[7:].strip()
        return auth
    return api_key


def tokens_match(client_token: str, server_token: str) -> bool:
    """Constant-time comparison that never raises.

    B-30 fix: encode both operands to bytes. `hmac.compare_digest` raises
    TypeError when given `str` containing non-ASCII, which previously escaped
    the handler as a 500 instead of a 401.
    """
    if not client_token or not server_token:
        return False
    try:
        return hmac.compare_digest(
            client_token.encode('utf-8', errors='strict'),
            server_token.encode('utf-8', errors='strict'),
        )
    except (UnicodeError, TypeError, AttributeError):
        return False


class AuthResult:
    """Outcome of an auth check.

    `ok`      — allow the request.
    `status`  — HTTP status to return when not ok (401 or 503).
    `message` — human-readable reason.
    """

    __slots__ = ('ok', 'status', 'message')

    def __init__(self, ok: bool, status: int = 401, message: str = 'Unauthorized'):
        self.ok = ok
        self.status = status
        self.message = message

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.ok


_OK = AuthResult(True)


def check_auth(headers, *, env_var: str = 'BEARER_TOKEN',
               surface: str = '') -> AuthResult:
    """Canonical auth decision shared by every wrapper.

    Returns AuthResult(ok=True) to allow; otherwise the caller renders its own
    provider-shaped error envelope with the supplied status/message.
    """
    if auth_disabled():
        return _OK

    server_token = bearer_token(env_var)

    if not server_token:
        # B-28: no credential configured.
        if require_auth():
            logger.error(
                '[auth] %s is unset and REQUIRE_AUTH=true — refusing request%s. '
                'Configure %s or set REQUIRE_AUTH=false to serve open.',
                env_var, f' to {surface}' if surface else '', env_var,
            )
            return AuthResult(False, 503, 'Server auth not configured')
        logger.warning(
            '[auth] %s unset and REQUIRE_AUTH=false — serving%s OPEN (insecure)',
            env_var, f' {surface}' if surface else '',
        )
        return _OK

    client_token = extract_client_token(headers)
    if tokens_match(client_token, server_token):
        return _OK
    return AuthResult(False, 401, 'Unauthorized')


def is_public_path(path: str, method: str,
                   public_any: frozenset, public_get: frozenset,
                   get_prefixes: tuple = ()) -> bool:
    """Exact-match public-path test (B-27).

    Prefix matching (`path.startswith('/v1/models')`) wrongly matched
    '/v1/models-anything' and ignored the HTTP method, so a POST to a
    discovery-only route bypassed auth. Exact match + method gating fixes both.
    """
    if path in public_any:
        return True
    if method == 'GET':
        if path in public_get:
            return True
        for prefix in get_prefixes:
            if path.startswith(prefix):
                return True
    return False
