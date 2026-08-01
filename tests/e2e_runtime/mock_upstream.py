#!/usr/bin/env python3
"""Mock upstream provider used by the runtime E2E harness.

Speaks the OpenAI Chat Completions API (JSON + SSE) and the Anthropic Messages
API, and can be told — per request, via the model id — to emit pathological
but *legal* upstream behaviour that real providers actually produce:

  normal            well-formed stream
  nospace           `data:{...}` with no space after the colon
  keepalive         interleaved bare `data:` keep-alive lines
  crlf              CRLF (\r\n) SSE framing
  tools             two parallel tool calls split across chunks
  reasoning         reasoning_content deltas before the answer
  nofinish          stream ends with no finish_reason and no [DONE]
  noterminator      final frame has no trailing blank line
  midstream_error   an {"error": ...} frame mid-stream
  abrupt            connection closes mid-frame
  slow              long idle gap (exercises heartbeats)
  usage_after       usage-only chunk after finish_reason
  empty             finishes with no content at all
  unicode           multibyte content split across chunk boundaries
"""

from __future__ import annotations

import asyncio
import json
import time

from aiohttp import web

MODES = (
    'normal', 'nospace', 'keepalive', 'crlf', 'tools', 'reasoning', 'reasoning_only',
    'nofinish', 'noterminator', 'midstream_error', 'abrupt', 'slow', 'usage_after',
    'empty', 'unicode',
    # Round-2 adversarial modes
    'bigchunk',      # one huge multi-line write (many events in a single TCP read)
    'bytesplit',     # every frame split byte-by-byte across writes
    'comments',      # SSE comment lines and retry: directives interleaved
    'dupfinish',     # two finish_reason chunks (upstream double-terminates)
    'nullcontent',   # delta.content is JSON null
    'emptychoices',  # choices: [] on some frames
    'toolnoid',      # tool_call without an id (wrapper must synthesise one)
    'longtool',      # tool arguments split across 8 fragments
    'http500',       # upstream returns 500 before streaming
    'http429',       # upstream returns 429 (retry/cooldown path)
)


def _mode(model: str) -> str:
    for m in MODES:
        if model.endswith(f'/{m}') or model == m or f'-{m}' in model:
            return m
    return 'normal'


def _chunk(delta=None, finish=None, usage=None):
    body = {
        'id': 'chatcmpl-mock', 'object': 'chat.completion.chunk',
        'created': int(time.time()), 'model': 'mock',
        'choices': [{'index': 0, 'delta': delta or {}, 'finish_reason': finish}],
    }
    if usage:
        body['usage'] = usage
    return json.dumps(body)


async def _write(resp, payload: str, *, space=True, crlf=False):
    nl = '\r\n' if crlf else '\n'
    sep = 'data: ' if space else 'data:'
    await resp.write(f'{sep}{payload}{nl}{nl}'.encode())


async def chat_completions(request: web.Request):
    body = await request.json()
    model = body.get('model', '')
    mode = _mode(model)
    stream = bool(body.get('stream'))

    if not stream:
        if mode == 'empty':
            msg = {'role': 'assistant', 'content': None}
        elif mode == 'tools':
            msg = {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'call_a', 'type': 'function',
                 'function': {'name': 'alpha', 'arguments': '{"x":1}'}},
                {'id': 'call_b', 'type': 'function',
                 'function': {'name': 'beta', 'arguments': '{"y":2}'}}]}
        else:
            msg = {'role': 'assistant', 'content': 'Hello from mock upstream.'}
        return web.json_response({
            'id': 'chatcmpl-mock', 'object': 'chat.completion',
            'created': int(time.time()), 'model': model,
            'choices': [{'index': 0, 'message': msg,
                         'finish_reason': 'tool_calls' if mode == 'tools' else 'stop'}],
            'usage': {'prompt_tokens': 11, 'completion_tokens': 7, 'total_tokens': 18},
        })

    # HARNESS NOTE: force `Connection: close` on SSE responses. With HTTP
    # keep-alive, aiohttp holds the socket open after write_eof(), so a wrapper
    # that (correctly) waits for EOF on an upstream that sent no [DONE] appears
    # to "hang" — a harness artifact, not a wrapper bug. Real SSE providers
    # close the connection at end of stream.
    resp = web.StreamResponse(status=200, headers={
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Connection': 'close',
    })
    resp.force_close()
    await resp.prepare(request)
    crlf = (mode == 'crlf')
    space = (mode != 'nospace')

    try:
        if mode == 'abrupt':
            await resp.write(b'data: {"id":"x","object":"chat.completion.chunk","choi')
            await resp.write_eof()
            return resp

        if mode == 'slow':
            await _write(resp, _chunk({'role': 'assistant', 'content': ''}), space=space, crlf=crlf)
            await asyncio.sleep(1.2)  # force the wrapper to heartbeat
            await _write(resp, _chunk({'content': 'after a long think'}), space=space, crlf=crlf)
            await _write(resp, _chunk(finish='stop'), space=space, crlf=crlf)
            await _write(resp, '[DONE]', space=space, crlf=crlf)
            return resp

        if mode == 'tools':
            await _write(resp, _chunk({'role': 'assistant', 'content': None}), space=space, crlf=crlf)
            await _write(resp, _chunk({'tool_calls': [
                {'index': 0, 'id': 'call_a', 'type': 'function',
                 'function': {'name': 'alpha', 'arguments': '{"x'}},
                {'index': 1, 'id': 'call_b', 'type': 'function',
                 'function': {'name': 'beta', 'arguments': '{"y'}},
            ]}), space=space, crlf=crlf)
            await _write(resp, _chunk({'tool_calls': [
                {'index': 0, 'function': {'arguments': '":1}'}},
                {'index': 1, 'function': {'arguments': '":2}'}},
            ]}), space=space, crlf=crlf)
            await _write(resp, _chunk(finish='tool_calls'), space=space, crlf=crlf)
            await _write(resp, '[DONE]', space=space, crlf=crlf)
            return resp

        if mode == 'reasoning':
            await _write(resp, _chunk({'role': 'assistant', 'content': ''}), space=space, crlf=crlf)
            await _write(resp, _chunk({'reasoning_content': 'Let me think... '}), space=space, crlf=crlf)
            await _write(resp, _chunk({'reasoning_content': 'still thinking.'}), space=space, crlf=crlf)
            await _write(resp, _chunk({'content': 'The answer is 42.'}), space=space, crlf=crlf)
            await _write(resp, _chunk(finish='stop'), space=space, crlf=crlf)
            await _write(resp, '[DONE]', space=space, crlf=crlf)
            return resp

        if mode == 'reasoning_only':
            # CODEX-RESP-01 regression: a model that emits ONLY reasoning
            # (reasoning_content deltas, NO text content) must still produce a
            # terminal response.completed. The old openrouter translator
            # skipped the completion events for such streams (guarded on
            # `if text_started:`), so Codex hung waiting for them.
            await _write(resp, _chunk({'role': 'assistant', 'content': ''}), space=space, crlf=crlf)
            await _write(resp, _chunk({'reasoning_content': 'Let me think... '}), space=space, crlf=crlf)
            await _write(resp, _chunk({'reasoning_content': 'still thinking.'}), space=space, crlf=crlf)
            await _write(resp, _chunk(finish='stop'), space=space, crlf=crlf)
            await _write(resp, '[DONE]', space=space, crlf=crlf)
            return resp

        if mode == 'midstream_error':
            await _write(resp, _chunk({'role': 'assistant', 'content': 'partial'}), space=space, crlf=crlf)
            await _write(resp, json.dumps({'error': {
                'message': 'upstream exploded', 'type': 'server_error'}}), space=space, crlf=crlf)
            return resp

        if mode == 'empty':
            await _write(resp, _chunk({'role': 'assistant', 'content': ''}), space=space, crlf=crlf)
            await _write(resp, _chunk(finish='stop'), space=space, crlf=crlf)
            await _write(resp, '[DONE]', space=space, crlf=crlf)
            return resp

        if mode == 'http500':
            return web.json_response(
                {'error': {'message': 'internal upstream failure', 'type': 'server_error'}},
                status=500)

        if mode == 'http429':
            return web.json_response(
                {'error': {'message': 'rate limited', 'type': 'rate_limit_error'}},
                status=429, headers={'Retry-After': '1'})

        if mode == 'bigchunk':
            # Many complete events delivered in ONE TCP write: exercises
            # buffer splitting when a single read contains N frames.
            blob = ''.join(
                'data: ' + _chunk({'content': f'part{i} '}) + '\n\n' for i in range(20))
            blob = 'data: ' + _chunk({'role': 'assistant', 'content': ''}) + '\n\n' + blob
            blob += 'data: ' + _chunk(finish='stop') + '\n\ndata: [DONE]\n\n'
            await resp.write(blob.encode())
            return resp

        if mode == 'bytesplit':
            # Every byte in its own write: the harshest possible framing test.
            blob = ('data: ' + _chunk({'role': 'assistant', 'content': ''}) + '\n\n'
                    + 'data: ' + _chunk({'content': 'split bytes ok'}) + '\n\n'
                    + 'data: ' + _chunk(finish='stop') + '\n\n'
                    + 'data: [DONE]\n\n').encode()
            for i in range(0, len(blob), 1):
                await resp.write(blob[i:i + 1])
            return resp

        if mode == 'comments':
            await _write(resp, _chunk({'role': 'assistant', 'content': ''}), space=space, crlf=crlf)
            await resp.write(b': this is an SSE comment\n\n')
            await resp.write(b'retry: 3000\n\n')
            await resp.write(b'id: evt-1\n\n')
            await _write(resp, _chunk({'content': 'after comments'}), space=space, crlf=crlf)
            await _write(resp, _chunk(finish='stop'), space=space, crlf=crlf)
            await _write(resp, '[DONE]', space=space, crlf=crlf)
            return resp

        if mode == 'dupfinish':
            await _write(resp, _chunk({'role': 'assistant', 'content': 'hi'}), space=space, crlf=crlf)
            await _write(resp, _chunk(finish='stop'), space=space, crlf=crlf)
            await _write(resp, _chunk(finish='stop'), space=space, crlf=crlf)
            await _write(resp, '[DONE]', space=space, crlf=crlf)
            return resp

        if mode == 'nullcontent':
            await _write(resp, _chunk({'role': 'assistant', 'content': None}), space=space, crlf=crlf)
            await _write(resp, _chunk({'content': None}), space=space, crlf=crlf)
            await _write(resp, _chunk({'content': 'real text'}), space=space, crlf=crlf)
            await _write(resp, _chunk(finish='stop'), space=space, crlf=crlf)
            await _write(resp, '[DONE]', space=space, crlf=crlf)
            return resp

        if mode == 'emptychoices':
            await _write(resp, _chunk({'role': 'assistant', 'content': ''}), space=space, crlf=crlf)
            await resp.write(('data: ' + json.dumps({
                'id': 'x', 'object': 'chat.completion.chunk', 'choices': []}) + '\n\n').encode())
            await _write(resp, _chunk({'content': 'survived empty choices'}), space=space, crlf=crlf)
            await _write(resp, _chunk(finish='stop'), space=space, crlf=crlf)
            await _write(resp, '[DONE]', space=space, crlf=crlf)
            return resp

        if mode == 'toolnoid':
            await _write(resp, _chunk({'tool_calls': [
                {'index': 0, 'function': {'name': 'noid', 'arguments': '{"a":1}'}}]}),
                space=space, crlf=crlf)
            await _write(resp, _chunk(finish='tool_calls'), space=space, crlf=crlf)
            await _write(resp, '[DONE]', space=space, crlf=crlf)
            return resp

        if mode == 'longtool':
            frag = ['{"que', 'ry":"', 'hello', ' wor', 'ld","', 'n":', '42', '}']
            await _write(resp, _chunk({'tool_calls': [
                {'index': 0, 'id': 'call_long', 'function': {'name': 'search', 'arguments': ''}}]}),
                space=space, crlf=crlf)
            for f in frag:
                await _write(resp, _chunk({'tool_calls': [
                    {'index': 0, 'function': {'arguments': f}}]}), space=space, crlf=crlf)
            await _write(resp, _chunk(finish='tool_calls'), space=space, crlf=crlf)
            await _write(resp, '[DONE]', space=space, crlf=crlf)
            return resp

        if mode == 'unicode':
            await _write(resp, _chunk({'role': 'assistant', 'content': ''}), space=space, crlf=crlf)
            # Split a 4-byte emoji across two writes at the byte level.
            blob = ('data: ' + _chunk({'content': 'halo 👋 dunia'}) + '\n\n').encode()
            await resp.write(blob[:len(blob) // 2])
            await asyncio.sleep(0.02)
            await resp.write(blob[len(blob) // 2:])
            await _write(resp, _chunk(finish='stop'), space=space, crlf=crlf)
            await _write(resp, '[DONE]', space=space, crlf=crlf)
            return resp

        # normal / nospace / keepalive / crlf / nofinish / noterminator / usage_after
        await _write(resp, _chunk({'role': 'assistant', 'content': ''}), space=space, crlf=crlf)
        if mode == 'keepalive':
            await resp.write(b'data:\n\n')       # bare keep-alive (B-01)
        await _write(resp, _chunk({'content': 'Hello '}), space=space, crlf=crlf)
        if mode == 'keepalive':
            await resp.write(b'data:\n\n')
            await resp.write(b': ping\n\n')      # comment keep-alive
        await _write(resp, _chunk({'content': 'from mock upstream.'}), space=space, crlf=crlf)

        if mode == 'nofinish':
            return resp                          # no finish_reason, no [DONE]

        await _write(resp, _chunk(finish='stop',
                                  usage={'prompt_tokens': 11, 'completion_tokens': 7,
                                         'total_tokens': 18}), space=space, crlf=crlf)
        if mode == 'usage_after':
            await _write(resp, _chunk(usage={'prompt_tokens': 11, 'completion_tokens': 9,
                                             'total_tokens': 20}), space=space, crlf=crlf)
        if mode == 'noterminator':
            await resp.write(b'data: [DONE]')    # no trailing blank line
            return resp
        await _write(resp, '[DONE]', space=space, crlf=crlf)
        return resp
    finally:
        try:
            await resp.write_eof()
        except Exception:
            pass


async def anthropic_messages(request: web.Request):
    """Native Anthropic surface (opencode routes some models here)."""
    body = await request.json()
    if not body.get('stream'):
        return web.json_response({
            'id': 'msg_mock', 'type': 'message', 'role': 'assistant',
            'model': body.get('model', ''),
            'content': [{'type': 'text', 'text': 'Hello from mock upstream.'}],
            'stop_reason': 'end_turn', 'stop_sequence': None,
            'usage': {'input_tokens': 11, 'output_tokens': 7},
        })
    resp = web.StreamResponse(status=200, headers={'Content-Type': 'text/event-stream'})
    await resp.prepare(request)

    def ev(t, d):
        return f'event: {t}\ndata: {json.dumps(d)}\n\n'.encode()

    await resp.write(ev('message_start', {'type': 'message_start', 'message': {
        'id': 'msg_mock', 'type': 'message', 'role': 'assistant',
        'model': body.get('model', ''), 'content': [], 'stop_reason': None,
        'usage': {'input_tokens': 11, 'output_tokens': 0}}}))
    await resp.write(ev('content_block_start', {'type': 'content_block_start', 'index': 0,
                                                'content_block': {'type': 'text', 'text': ''}}))
    await resp.write(ev('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                                                'delta': {'type': 'text_delta', 'text': 'Hello'}}))
    await resp.write(ev('content_block_stop', {'type': 'content_block_stop', 'index': 0}))
    await resp.write(ev('message_delta', {'type': 'message_delta',
                                          'delta': {'stop_reason': 'end_turn'},
                                          'usage': {'output_tokens': 7}}))
    await resp.write(ev('message_stop', {'type': 'message_stop'}))
    await resp.write_eof()
    return resp


async def models(request: web.Request):
    data = [{'id': f'mock/{m}', 'object': 'model', 'created': 0, 'owned_by': 'mock'}
            for m in MODES]
    data.append({'id': 'mock/default', 'object': 'model', 'created': 0, 'owned_by': 'mock'})
    return web.json_response({'object': 'list', 'data': data})


async def embeddings(request: web.Request):
    body = await request.json()
    inp = body.get('input')
    n = len(inp) if isinstance(inp, list) else 1
    return web.json_response({
        'object': 'list', 'model': body.get('model', ''),
        'data': [{'object': 'embedding', 'index': i, 'embedding': [0.1, 0.2, 0.3]}
                 for i in range(n)],
        'usage': {'prompt_tokens': 5, 'total_tokens': 5},
    })


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_post('/v1/chat/completions', chat_completions)
    app.router.add_post('/chat/completions', chat_completions)
    app.router.add_post('/v1/messages', anthropic_messages)
    app.router.add_post('/messages', anthropic_messages)
    app.router.add_get('/v1/models', models)
    app.router.add_get('/models', models)
    app.router.add_post('/v1/embeddings', embeddings)
    app.router.add_post('/embeddings', embeddings)
    return app



# ── Anthropic-native upstream (COMPATIBILITY_LAYER=2 / =3 auto) ───────────
# Serves ONLY the Anthropic Messages API (+ /messages alias for base styles
# that already include /v1). No /chat/completions — auto-discovery must detect
# Anthropic.

async def _anthropic_chunk(text, delta_kind='text_delta', key='text'):
    return json.dumps({'type': 'content_block_delta', 'index': 0,
                       'delta': {'type': delta_kind, key: text}})


async def anthropic_messages(request: web.Request):
    body = await request.json()
    model = body.get('model', '')
    mode = _mode(model)
    stream = bool(body.get('stream'))
    msg_id = f"msg_anthropic_{int(time.time()*1000)}"

    if not stream:
        content = []
        if mode in ('reasoning', 'reasoning_only'):
            content.append({'type': 'thinking', 'thinking': 'Let me think...'})
        if mode == 'tools':
            content.append({'type': 'text', 'text': ''})
            content.append({'type': 'tool_use', 'id': 'toolu_a', 'name': 'alpha',
                            'input': {'x': 1}})
            content.append({'type': 'tool_use', 'id': 'toolu_b', 'name': 'beta',
                            'input': {'y': 2}})
            stop_reason = 'tool_use'
        else:
            if mode != 'reasoning_only':
                content.append({'type': 'text', 'text': 'Hello from anthropic mock.'})
            stop_reason = 'end_turn'
        return web.json_response({
            'id': msg_id, 'type': 'message', 'role': 'assistant', 'model': model,
            'content': content, 'stop_reason': stop_reason, 'stop_sequence': None,
            'usage': {'input_tokens': 12, 'output_tokens': 8},
        })

    resp = web.StreamResponse(status=200, headers={
        'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache',
        'Connection': 'close'})
    resp.force_close()
    await resp.prepare(request)

    async def _write(obj):
        await resp.write(f"event: {obj['type']}\ndata: {json.dumps(obj)}\n\n".encode())

    await _write({'type': 'message_start', 'message': {
        'id': msg_id, 'type': 'message', 'role': 'assistant', 'model': model,
        'content': [], 'stop_reason': None, 'stop_sequence': None,
        'usage': {'input_tokens': 12, 'output_tokens': 0}}})
    idx = 0
    if mode in ('reasoning', 'reasoning_only'):
        await _write({'type': 'content_block_start', 'index': idx,
                      'content_block': {'type': 'thinking', 'thinking': ''}})
        await _write({'type': 'content_block_delta', 'index': idx,
                      'delta': {'type': 'thinking_delta', 'thinking': 'Let me think...'}})
        await _write({'type': 'content_block_stop', 'index': idx})
        idx += 1
    if mode == 'tools':
        await _write({'type': 'content_block_start', 'index': idx,
                      'content_block': {'type': 'tool_use', 'id': 'toolu_a', 'name': 'alpha', 'input': {}}})
        await _write({'type': 'content_block_delta', 'index': idx,
                      'delta': {'type': 'input_json_delta', 'partial_json': '{"x":1}'}})
        await _write({'type': 'content_block_stop', 'index': idx})
        idx += 1
        await _write({'type': 'content_block_start', 'index': idx,
                      'content_block': {'type': 'tool_use', 'id': 'toolu_b', 'name': 'beta', 'input': {}}})
        await _write({'type': 'content_block_delta', 'index': idx,
                      'delta': {'type': 'input_json_delta', 'partial_json': '{"y":2}'}})
        await _write({'type': 'content_block_stop', 'index': idx})
        idx += 1
    if mode != 'reasoning_only':
        await _write({'type': 'content_block_start', 'index': idx,
                      'content_block': {'type': 'text', 'text': ''}})
        await _write({'type': 'content_block_delta', 'index': idx,
                      'delta': {'type': 'text_delta', 'text': 'Hello from anthropic mock.'}})
        await _write({'type': 'content_block_stop', 'index': idx})
    stop_reason = 'tool_use' if mode == 'tools' else 'end_turn'
    await _write({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None},
                  'usage': {'input_tokens': 12, 'output_tokens': 8}})
    await _write({'type': 'message_stop'})
    await resp.write_eof()
    return resp


def build_anthropic_app():
    app = web.Application()
    app.router.add_post('/v1/messages', anthropic_messages)
    app.router.add_post('/messages', anthropic_messages)
    return app

if __name__ == '__main__':
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
    mode = sys.argv[2] if len(sys.argv) > 2 else 'openai'
    app = build_anthropic_app() if mode == 'anthropic' else build_app()
    web.run_app(app, host='127.0.0.1', port=port, print=None)
