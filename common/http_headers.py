"""Preserve upstream response metadata without forwarding transport framing."""
from common.context_recovery import context_response_headers
from common.translations.shared import HOP_BY_HOP_HEADERS, sanitize_header_value


_CREDENTIAL_HEADERS = frozenset({
    'authorization', 'proxy-authorization', 'x-api-key', 'api-key',
    'x-goog-api-key', 'cookie', 'set-cookie',
})


def upstream_response_headers(response):
    """Return end-to-end headers for decoded JSON/SSE proxy responses.

    aiohttp decompresses the body and the ASGI response frames it again, so the
    upstream encoding/length must not describe the downstream body. Connection
    can nominate additional hop-by-hop fields, beyond the standard names.
    """
    raw = getattr(response, 'upstream_headers', getattr(response, 'headers', {}))
    headers = {str(key).lower(): sanitize_header_value(value)
               for key, value in raw.items()}
    excluded = set(HOP_BY_HOP_HEADERS) | _CREDENTIAL_HEADERS | {
        'content-length', 'content-encoding', 'trailer', 'proxy-connection',
    }
    excluded.update(value.strip().lower() for value in headers.get('connection', '').split(','))
    return {key: value for key, value in {
        **headers,
        **{key.lower(): sanitize_header_value(value)
           for key, value in context_response_headers(response).items()},
    }.items() if key not in excluded}
