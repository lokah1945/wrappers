#!/usr/bin/env python3
"""Upstream Compatibility Layer — operator-declared upstream dialect.

Every wrapper used to HARDCODE the assumption that its upstream speaks the
OpenAI Chat Completions protocol (`POST {base}/chat/completions`). That is why
the Anthropic surface (Claude Code) works against OpenAI-compatible upstreams
(NVIDIA NIM, Nous, OpenCode Zen, BLACKBOX, OpenRouter) via translation.

COMPATIBILITY_LAYER lets the operator declare what the UPSTREAM actually
speaks, so the wrapper picks the exact translation path instead of guessing:

    1 = OpenAI Compatible  (chat completions)   [default — current behaviour]
    2 = Anthropic Compatible (messages API)
    3 = Auto Discovery      (probe upstream once, cache, fall back to 1)

The operator is the source of truth: "user harus dengan sadar menentukan
upstream menggunakan Compatibility Layer apa, sehingga translation dari
agent/client akan lebih presisi daripada wrapper menebak".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger('wrapper-compat')

LAYER_OPENAI = '1'
LAYER_ANTHROPIC = '2'
LAYER_AUTO = '3'

_VALID = {LAYER_OPENAI, LAYER_ANTHROPIC, LAYER_AUTO}
_DEFAULT = LAYER_OPENAI

# probe cache: base_url -> (timestamp, layer)
_probe_cache: dict[str, tuple[float, str]] = {}
_probe_lock = asyncio.Lock()
_PROBE_TTL_SEC = float(os.environ.get('COMPATIBILITY_PROBE_TTL_SEC', '300'))


def compat_layer_raw() -> str:
    """Raw (unvalidated) value of COMPATIBILITY_LAYER, stripped."""
    return (os.environ.get('COMPATIBILITY_LAYER') or '').strip()


def validate_compat_layer() -> str:
    """Return the validated layer; raise ValueError on an invalid value.

    Called from each wrapper's validate_config() so an invalid
    COMPATIBILITY_LAYER fails fast at startup instead of misbehaving silently.
    """
    raw = compat_layer_raw()
    if not raw:
        return _DEFAULT
    if raw not in _VALID:
        raise ValueError(
            f"COMPATIBILITY_LAYER must be 1 (OpenAI), 2 (Anthropic) or 3 (Auto), "
            f"got {raw!r}. Set it explicitly or remove it (defaults to 1)."
        )
    return raw


def compat_layer() -> str:
    """Validated layer; defaults to 1 (OpenAI) so existing deployments are
    byte-for-byte unchanged."""
    try:
        return validate_compat_layer()
    except ValueError:
        logger.error('invalid COMPATIBILITY_LAYER; falling back to 1 (OpenAI)')
        return _DEFAULT


def is_openai_upstream() -> bool:
    return compat_layer() == LAYER_OPENAI


def is_anthropic_upstream() -> bool:
    return compat_layer() == LAYER_ANTHROPIC


def is_auto_discovery() -> bool:
    return compat_layer() == LAYER_AUTO


async def probe_upstream_compatibility(session: Any, base_url: str) -> str:
    """Best-effort probe of the upstream dialect (COMPATIBILITY_LAYER=3).

    Order:
      1. GET {base}/v1/models or {base}/models with a model list  -> OpenAI
      2. POST {base}/v1/messages (minimal) status != 404           -> Anthropic
      3. POST {base}/v1/chat/completions (minimal) status != 404   -> OpenAI
      4. otherwise                                                 -> OpenAI (default)

    Result is cached per base_url with COMPATIBILITY_PROBE_TTL_SEC (default
    300s) so the probe runs once. The probe never sends credentials and uses
    minimal bodies, so it never burns upstream quota.
    """
    base = (base_url or '').rstrip('/')
    now = time.time()
    cached = _probe_cache.get(base)
    if cached and (now - cached[0]) < _PROBE_TTL_SEC:
        return cached[1]

    async with _probe_lock:
        # double-check under the lock
        cached = _probe_cache.get(base)
        if cached and (time.time() - cached[0]) < _PROBE_TTL_SEC:
            return cached[1]

        layer = await _probe(session, base)
        _probe_cache[base] = (time.time(), layer)
        logger.info('[compat] upstream %s probed as COMPATIBILITY_LAYER=%s', base, layer)
        return layer


def _probe_base(base: str) -> str:
    """R17 (B-17.1): normalize an operator base URL for probing.

    `<PREFIX>_BASE_URL` may itself end in `/v1` (OpenRouter/OpenCode style).
    The probe paths below are OpenAI/Anthropic-native absolute paths; probing
    `{base}/v1/messages` against a base that already contains `/v1` produced
    `/v1/v1/messages` → 404 at every step → auto-discovery ALWAYS fell
    through to the OpenAI default for v1-style bases. Strip one trailing
    `/v1` so both "root" and "v1" base styles probe identically."""
    b = (base or '').rstrip('/')
    if b.lower().endswith('/v1'):
        b = b[:-3]
    return b


async def _probe(session: Any, base: str) -> str:
    base = _probe_base(base)
    # 1. model listing -> OpenAI
    for models_path in ('/v1/models', '/models'):
        try:
            async with session.get(f'{base}{models_path}', timeout=5) as resp:
                if resp.status == 200:
                    try:
                        body = await resp.json()
                    except Exception:
                        body = {}
                    if isinstance(body, dict) and ('data' in body or 'models' in body):
                        return LAYER_OPENAI
        except Exception:
            continue

    # 2. Anthropic messages endpoint
    try:
        async with session.post(
            f'{base}/v1/messages',
            json={'model': 'probe', 'max_tokens': 1,
                  'messages': [{'role': 'user', 'content': 'hi'}]},
            timeout=5,
        ) as resp:
            if resp.status not in (404, 405):
                return LAYER_ANTHROPIC
    except Exception:
        pass

    # 3. OpenAI chat completions endpoint
    try:
        async with session.post(
            f'{base}/v1/chat/completions',
            json={'model': 'probe',
                  'messages': [{'role': 'user', 'content': 'hi'}]},
            timeout=5,
        ) as resp:
            if resp.status not in (404, 405):
                return LAYER_OPENAI
    except Exception:
        pass

    # 4. default: OpenAI (historical assumption)
    return LAYER_OPENAI


async def resolve_upstream_is_anthropic(session_or_getter: Any, base_url: str) -> bool:
    """R17 (B-17.1): the effective upstream-dialect decision for REQUEST
    ROUTING (CONTRACT §9.2).

    Every wrapper previously branched on `is_anthropic_upstream()`, which
    reads ONLY the env var — under COMPATIBILITY_LAYER=3 (auto-discovery)
    that is always False, so the probe result was never consumed and every
    wrapper silently behaved as layer 1 (OpenAI dialect forwarding to an
    Anthropic-native upstream → upstream 404 on every call).

    Semantics per contract:
      layer 1 → False (OpenAI), layer 2 → True (Anthropic), never network;
      layer 3 → probe once per base URL (TTL-cached by
      probe_upstream_compatibility), inconclusive → False (fall back to 1).

    `session_or_getter` may be an aiohttp session or a zero-arg (async)
    callable returning one."""
    layer = compat_layer()
    if layer == LAYER_ANTHROPIC:
        return True
    if layer == LAYER_OPENAI:
        return False
    session = session_or_getter
    if callable(session):
        session = session()
    if asyncio.iscoroutine(session):
        session = await session
    try:
        probed = await probe_upstream_compatibility(session, base_url)
    except Exception as exc:  # probe must never break a request path
        logger.warning('[compat] auto-discovery probe failed (%s); falling back to OpenAI', exc)
        return False
    return probed == LAYER_ANTHROPIC


def upstream_messages_path(style: str) -> str:
    """Upstream path for the Anthropic Messages endpoint given the wrapper's
    base-URL style:
      'full_v1'  -> base already includes /v1            -> /messages
      'no_v1'    -> base is the root                      -> /v1/messages
    """
    if style == 'full_v1':
        return 'messages'
    return 'v1/messages'


def anthropic_error_surface(content: dict, status_code: int):
    """Shape an Anthropic-surface error envelope (used by layer-2 paths)."""
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=status_code,
                        content={'type': 'error', 'error': content})


def json_dumps_sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ── Layer-2 stream adapters (Anthropic upstream) ──────────────────────────
# All three take an upstream byte/chunk source and yield SSE frames as str.
# They NEVER release the upstream response or pool key — the caller wraps with
# try/finally, consistent with every existing wrapper stream path.

async def _chunks_with_heartbeat(src, hb_interval: float):
    """Yield upstream chunks plus ': heartbeat' comments during idle gaps.

    `src` is any async iterable of bytes/str (aiohttp ClientResponse or an
    existing heartbeat-wrapped generator). Sentinel-idle via common.sse.
    """
    from common.sse import IDLE, iter_chunks_with_idle
    last = time.time()
    async for raw in iter_chunks_with_idle(src, hb_interval):
        if raw is IDLE:
            now = time.time()
            if now - last >= hb_interval:
                yield b': heartbeat\n\n'
                last = now
            continue
        last = time.time()
        yield raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode()


async def passthrough_anthropic_sse(src, hb_interval: float):
    """Forward Anthropic Messages SSE with heartbeats — used by the layer-2
    `/v1/messages` surface.

    Audit 2026-08-03: now re-serialised via the shared
    PassthroughBlockRewriter (terminal_done=False — Anthropic ends with
    message_stop, never [DONE]):
      * P0-4 — special tokens are scrubbed even on this verbatim path
        (user report '\"><unk><unk>…'), catching tokens fragmented across
        chunks,
      * P0-1 / CONTRACT §3.3 — upstream EOF without message_stop/message_delta
        surfaces an Anthropic `error` event instead of a fabricated success
        framing, and
      * heartbeat comments are block-aligned (never split a data: line).
    """
    from common.sanitize_tokens import PassthroughBlockRewriter
    from common.sse import IDLE, iter_chunks_with_idle
    rw = PassthroughBlockRewriter()
    last = time.time()
    async for raw in iter_chunks_with_idle(src, hb_interval):
        if raw is IDLE:
            now = time.time()
            if rw.at_block_boundary() and now - last >= hb_interval:
                yield b': heartbeat\n\n'
                last = now
            continue
        last = time.time()
        for fr in rw.feed(raw):
            yield fr
    for fr in rw.finish(terminal_done=False):
        yield fr


async def translate_anthropic_stream_to_openai_chat(src, model: str, hb_interval: float):
    """Translate an Anthropic Messages SSE upstream stream -> OpenAI chat SSE
    frames — used by the layer-2 `/v1/chat/completions` and `/v1/responses`
    surfaces. Comments/heartbeats pass through harmlessly."""
    from common.translations.shared import stream_anthropic_to_openai
    async for frame in stream_anthropic_to_openai(
            _chunks_with_heartbeat(src, hb_interval), model):
        yield frame


async def translate_openai_chat_sse_to_responses(chat_sse_gen, model: str):
    """Translate an OpenAI chat SSE frame generator -> Responses SSE frames.

    Used by the layer-2 `/v1/responses` surface after
    translate_anthropic_stream_to_openai_chat. Mirrors the proven openrouter
    Responses translator (CODEX-RESP-01/02 shapes): full response.created,
    eager item add, standard function_call_arguments events, unconditional
    completion, single [DONE].

    Audit 2026-08-03:
      * P0-4 — content/reasoning deltas are scrubbed through stateful
        special-token filters (tokens fragmented across chunks included),
        flushed into their channel before the .done events;
      * P0-1 / CONTRACT §3.3 — an upstream EOF without finish_reason and
        without an error frame is surfaced as response.failed
        (code=upstream_premature_eof) instead of a fabricated success;
      * P1-3 — usage is accumulated from upstream usage chunks and emitted
        as a full Responses usage object (input/output details scaffolding;
        strict Response.model_validate_json passes).
    """
    from common.translations.shared import (
        responses_usage as _responses_usage,
        tokens_from_chat_usage as _tokens_from_chat_usage,
    )
    try:
        from common.sanitize_tokens import (
            SpecialTokenFilter as _STF,
            PREMATURE_EOF_MSG as _PREMATURE_EOF_MSG,
        )
    except ImportError:  # pragma: no cover - standalone fallback
        class _STF:  # type: ignore[no-redef]
            def feed(self, t):
                return t

            def flush(self):
                return ''

        _PREMATURE_EOF_MSG = (
            'upstream stream ended prematurely: EOF without a terminal '
            'signal; the response may be truncated — client may retry')

    resp_id = f"resp_{int(time.time()*1000)}"
    created_at = int(time.time())
    msg_id = f"msg_{int(time.time()*1000)}"
    full_text = ''
    reasoning_started = False
    acc_reason = ''
    rsn_index = 1
    rsn_id = f"rsn_{int(time.time()*1000)}"
    tool_accs: list = []
    next_output_index = 1
    msg_open = False
    upstream_error = None
    error_code = 'upstream_error'
    saw_finish = False  # P0-1: finish_reason seen in any choice chunk
    acc_usage = (0, 0, 0, 0)  # (input, output, cached, reasoning)
    _ftext = _STF()  # P0-4: content channel
    _freason = _STF()  # P0-4: reasoning channel
    seq = 0

    def _sse(event_type: str, payload: dict) -> str:
        nonlocal seq
        seq += 1
        return f'event: {event_type}\ndata: {json.dumps({"type": event_type, "sequence_number": seq, **payload}, ensure_ascii=False)}\n\n'

    yield _sse('response.created', {'response': {
        'id': resp_id, 'object': 'response', 'created_at': created_at,
        'model': model, 'status': 'in_progress', 'output': [],
        'usage': _responses_usage(0, 0, 0, 0),
    }})
    yield _sse('response.in_progress', {'response': {'id': resp_id, 'status': 'in_progress'}})
    yield _sse('response.output_item.added', {
        'output_index': 0,
        'item': {'id': msg_id, 'type': 'message', 'status': 'in_progress',
                 'role': 'assistant', 'content': []},
    })
    yield _sse('response.content_part.added', {
        'item_id': msg_id, 'output_index': 0, 'content_index': 0,
        'part': {'type': 'output_text', 'text': '', 'annotations': []},
    })
    msg_open = True

    def _get_tool_acc(tc: dict) -> dict:
        nonlocal next_output_index
        idx = tc.get('index') if isinstance(tc.get('index'), int) else len(tool_accs)
        acc = tool_accs[idx] if idx < len(tool_accs) else None
        if acc is None:
            acc = {'call_id': tc.get('id') or f'call_{idx}_{int(time.time()*1000)}',
                   'name': '', 'args': '', 'output_index': next_output_index, 'added': False}
            next_output_index += 1
            while len(tool_accs) <= idx:
                tool_accs.append(None)
            tool_accs[idx] = acc
        if tc.get('id'):
            acc['call_id'] = tc['id']
        return acc

    async def _process_frame(frame: str):
        nonlocal full_text, upstream_error, error_code, reasoning_started, acc_reason, rsn_index, next_output_index, saw_finish, acc_usage
        for line in frame.split('\n'):
            line = line.strip()
            if not line.startswith('data:'):
                continue
            data_str = line[5:].strip()
            if not data_str or data_str == '[DONE]':
                continue
            try:
                c = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            if isinstance(c, dict) and c.get('type') == 'error':
                e = c.get('error') or {}
                upstream_error = e.get('message') if isinstance(e, dict) else str(e)
                continue
            if isinstance(c, dict) and c.get('error') is not None and 'choices' not in c:
                e = c['error']
                upstream_error = (e.get('message') if isinstance(e, dict) else str(e)) or 'upstream error'
                if isinstance(e, dict) and e.get('code'):
                    error_code = str(e['code'])
                continue
            if isinstance(c, dict) and isinstance(c.get('usage'), dict):
                acc_usage = _tokens_from_chat_usage(c['usage'])
            if not isinstance(c, dict) or 'choices' not in c:
                continue
            choice = (c.get('choices') or [{}])[0] or {}
            if choice.get('finish_reason'):
                saw_finish = True
            delta = choice.get('delta') or {}
            content = delta.get('content')
            if isinstance(content, str) and content:
                content = _ftext.feed(content)  # P0-4
                if content:
                    full_text += content
                    yield _sse('response.output_text.delta', {
                        'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'delta': content})
            reason = (delta.get('reasoning_content')
                      if isinstance(delta.get('reasoning_content'), str)
                      else (delta.get('reasoning') if isinstance(delta.get('reasoning'), str) else ''))
            if reason:
                reason = _freason.feed(reason)  # P0-4
            if reason:
                if not reasoning_started:
                    reasoning_started = True
                    rsn_index = next_output_index
                    next_output_index += 1
                    yield _sse('response.output_item.added', {
                        'output_index': rsn_index,
                        'item': {'id': rsn_id, 'type': 'reasoning', 'status': 'in_progress',
                                 'summary': [], 'content': []}})
                acc_reason += reason
                yield _sse('response.reasoning_text.delta', {
                    'item_id': rsn_id, 'output_index': rsn_index, 'content_index': 0, 'delta': reason})
            for tc in delta.get('tool_calls') or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get('function') or {}
                acc = _get_tool_acc(tc)
                if not acc['added']:
                    acc['added'] = True
                    yield _sse('response.output_item.added', {
                        'output_index': acc['output_index'],
                        'item': {'id': acc['call_id'], 'type': 'function_call', 'status': 'in_progress',
                                 'call_id': acc['call_id'], 'name': acc['name'], 'arguments': ''}})
                if isinstance(fn.get('name'), str) and fn['name']:
                    # P0-3 fix: never emit the NAME as an arguments delta —
                    # delta-accumulating clients collected `name{...}` (invalid
                    # JSON). Name rides output_item.added.item.name instead.
                    acc['name'] += fn['name']
                if isinstance(fn.get('arguments'), str) and fn['arguments']:
                    acc['args'] += fn['arguments']
                    yield _sse('response.function_call_arguments.delta', {
                        'item_id': acc['call_id'], 'output_index': acc['output_index'],
                        'delta': fn['arguments']})

    try:
        async for frame in chat_sse_gen:
            if isinstance(frame, (bytes, bytearray)):
                frame = frame.decode('utf-8', errors='replace')
            if isinstance(frame, str):
                async for out in _process_frame(frame):
                    yield out
            else:
                async for out in _process_frame(str(frame)):
                    yield out
    finally:
        pass  # upstream cleanup owned by caller

    # P0-1 / CONTRACT §3.3: EOF with NO finish_reason AND NO error frame is a
    # truncated turn — it must surface as failed, not completed.
    if not upstream_error and not saw_finish:
        upstream_error = _PREMATURE_EOF_MSG
        error_code = 'upstream_premature_eof'

    # P0-4: release filter-withheld text into its own channel BEFORE the
    # .done events so the totals and the delta stream agree.
    rest_reason = _freason.flush()
    if rest_reason:
        if not reasoning_started:
            reasoning_started = True
            rsn_index = next_output_index
            next_output_index += 1
            yield _sse('response.output_item.added', {
                'output_index': rsn_index,
                'item': {'id': rsn_id, 'type': 'reasoning', 'status': 'in_progress',
                         'summary': [], 'content': []}})
        acc_reason += rest_reason
        yield _sse('response.reasoning_text.delta', {
            'item_id': rsn_id, 'output_index': rsn_index, 'content_index': 0, 'delta': rest_reason})
    rest_text = _ftext.flush()
    if rest_text:
        full_text += rest_text
        yield _sse('response.output_text.delta', {
            'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'delta': rest_text})

    if reasoning_started:
        yield _sse('response.reasoning_text.done', {
            'item_id': rsn_id, 'output_index': rsn_index, 'content_index': 0, 'text': acc_reason})
        yield _sse('response.output_item.done', {
            'output_index': rsn_index,
            'item': {'id': rsn_id, 'type': 'reasoning', 'status': 'completed',
                     'summary': [], 'content': [{'type': 'reasoning_text', 'text': acc_reason}]}})
    yield _sse('response.output_text.done', {
        'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'text': full_text})
    yield _sse('response.content_part.done', {
        'item_id': msg_id, 'output_index': 0, 'content_index': 0,
        'part': {'type': 'output_text', 'text': full_text, 'annotations': []}})
    yield _sse('response.output_item.done', {
        'output_index': 0,
        'item': {'id': msg_id, 'type': 'message', 'status': 'completed', 'role': 'assistant',
                 'content': [{'type': 'output_text', 'text': full_text, 'annotations': []}]}})
    for acc in tool_accs:
        if not acc:
            continue
        yield _sse('response.function_call_arguments.done', {
            'item_id': acc['call_id'], 'output_index': acc['output_index'],
            'name': acc['name'], 'arguments': acc['args']})
        yield _sse('response.output_item.done', {
            'output_index': acc['output_index'],
            'item': {'id': acc['call_id'], 'type': 'function_call', 'status': 'completed',
                     'call_id': acc['call_id'], 'name': acc['name'], 'arguments': acc['args']}})
    if upstream_error:
        yield _sse('response.failed', {'response': {
            'id': resp_id, 'object': 'response', 'created_at': created_at, 'model': model,
            'status': 'failed', 'usage': _responses_usage(*acc_usage),
            'error': {'code': error_code, 'message': str(upstream_error)[:2000]}}})
        yield 'data: [DONE]\n\n'
        return
    outputs_by_index = {
        0: {'id': msg_id, 'type': 'message', 'status': 'completed', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': full_text, 'annotations': []}]},
    }
    if reasoning_started:
        outputs_by_index[rsn_index] = {'id': rsn_id, 'type': 'reasoning', 'status': 'completed',
                                       'summary': [], 'content': [{'type': 'reasoning_text', 'text': acc_reason}]}
    for acc in tool_accs:
        if not acc:
            continue
        outputs_by_index[acc['output_index']] = {'id': acc['call_id'], 'type': 'function_call',
                                                 'status': 'completed', 'call_id': acc['call_id'],
                                                 'name': acc['name'], 'arguments': acc['args']}
    output = [outputs_by_index[i] for i in sorted(outputs_by_index)]
    yield _sse('response.completed', {'response': {
        'id': resp_id, 'object': 'response', 'created_at': created_at, 'model': model,
        'status': 'completed', 'output': output,
        'parallel_tool_calls': True, 'tool_choice': 'auto', 'tools': [],
        'usage': _responses_usage(*acc_usage),
    }})
    yield 'data: [DONE]\n\n'
