#!/usr/bin/env python3
"""
anthropic_compat.py — Anthropic Messages API <-> OpenAI Chat Completions translation.
Migrated from anthropic_compat.js — functionally identical.

Three translators:
  - anthropicToOpenai(body, official_context)  -> request  A->O
  - openaiToAnthropic(resp, model, ...)        -> response O->A  (non-streaming)
  - streamOpenaiToAnthropic(stream, ...)       -> response O->A  (SSE async generator)
"""

import re
import json
import time
import asyncio
from typing import Dict, List, Optional, Any, AsyncGenerator

from .capabilities import MODEL_CONTEXT_WINDOWS, DEFAULT_CONTEXT_WINDOW, get_context_window


_FINISH_TO_STOP = {
    'stop': 'end_turn',
    'length': 'max_tokens',
    'tool_calls': 'tool_use',
    'content_filter': 'refusal',
    None: 'end_turn',
}


def anthropic_error(etype: str, message: str) -> dict:
    return {'type': 'error', 'error': {'type': etype, 'message': message}}


def _sse(event: str, data: dict) -> str:
    return f'event: {event}\ndata: {json.dumps(data)}\n\n'


def _flatten_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(
            b.get('text', '') for b in content
            if isinstance(b, dict) and b.get('type') == 'text'
        )
    return ''


def extract_internal_reasoning(msg: dict) -> dict:
    """Normalize reasoning from upstream into a single internal representation."""
    m = msg or {}
    raw_content = m.get('content', '')
    if isinstance(raw_content, str):
        pass
    elif isinstance(raw_content, list):
        t = ''.join(b.get('text', '') for b in raw_content if isinstance(b, dict) and b.get('type') == 'text')
        if t:
            raw_content = t
    else:
        raw_content = str(raw_content) if raw_content is not None else ''

    reasoning = ''
    rc = m.get('reasoning_content') or m.get('reasoning')
    if isinstance(rc, str) and rc:
        reasoning = rc

    content = raw_content
    if not reasoning:
        trimmed = (content or '').strip()
        end = -1
        start = -1
        if trimmed.startswith('<think>'):
            start = 7
            end = trimmed.find('</think>')
        elif trimmed.startswith('<thinking>'):
            start = 10
            end = trimmed.find('</thinking>')
        if end != -1 and start != -1:
            reasoning = trimmed[start:end].strip()
            tag_len = 11 if trimmed.startswith('<thinking>') else 8
            content = trimmed[end + tag_len:].strip()

    return {'reasoning': reasoning or '', 'content': content}


def is_anthropic_message_order_valid(messages: list) -> bool:
    has_tool_result = False
    for msg in messages:
        if not msg or not msg.get('role'):
            continue
        if msg['role'] == 'system':
            continue
        if msg['role'] == 'tool':
            has_tool_result = True
        elif has_tool_result and msg['role'] not in ('assistant', 'tool'):
            return False
    return True


def get_compat_context_window(model_id: str, official_context: Optional[dict]) -> int:
    if official_context and official_context.get('context', 0) > 0:
        return official_context['context']
    return get_context_window(model_id)


def has_tool_result_block(msg: dict) -> bool:
    if not msg or not msg.get('content'):
        return False
    content = msg['content']
    if isinstance(content, str):
        return False
    if isinstance(content, list):
        return any(blk and blk.get('type') == 'tool_result' for blk in content)
    return False


def format_tool_calls_as_dsml(tool_uses: list) -> str:
    if not tool_uses:
        return ''
    invokes = []
    for blk in tool_uses:
        name = blk.get('name', '')
        params = []
        input_data = blk.get('input', {})
        for k, v in input_data.items():
            val_str = v if isinstance(v, str) else json.dumps(v)
            params.append(f'<｜DSML｜parameter name="{k}" string="true">{val_str}</｜DSML｜parameter>')
        invokes.append(f'<｜DSML｜invoke name="{name}">\n{chr(10).join(params)}\n</｜DSML｜invoke>')
    return f'<｜DSML｜tool_calls>\n{chr(10).join(invokes)}\n</｜DSML｜tool_calls>'


def strip_cache_control(node):
    if isinstance(node, list):
        for item in node:
            strip_cache_control(item)
        return node
    if isinstance(node, dict):
        node.pop('cache_control', None)
        for k in list(node.keys()):
            v = node[k]
            if isinstance(v, (dict, list)):
                strip_cache_control(v)
    return node


def sanitize_anthropic_tools(tools: list) -> tuple:
    if not isinstance(tools) or not tools:
        return (tools or [], False)
    dropped_search_tool = False
    out = []
    for t in tools:
        type_val = t.get('type') if isinstance(t, dict) else None
        if isinstance(type_val, str) and type_val.startswith('tool_search_tool_'):
            dropped_search_tool = True
            continue
        if isinstance(t, dict):
            t.pop('defer_loading', None)
        out.append(t)
    return (out, dropped_search_tool)


def anthropic_to_openai(a: dict, official_context: Optional[dict] = None) -> dict:
    """Translate Anthropic Messages request -> OpenAI Chat Completions body."""
    if not a or not isinstance(a, dict):
        return {'model': '', 'messages': []}
    if not isinstance(a.get('messages'), list):
        return {'model': '', 'messages': []}

    strip_cache_control(a)

    context_limit = get_compat_context_window(a.get('model', ''), official_context)
    max_allowed_tokens = max(4000, context_limit - (a.get('max_tokens', 4096) or 4096) - 2000)
    current_tokens = estimate_input_tokens(a)

    if current_tokens > max_allowed_tokens:
        while len(a['messages']) > 1 and current_tokens > max_allowed_tokens:
            if a['messages'][0] and a['messages'][0].get('role') == 'system':
                break
            remaining_users = sum(1 for m in a['messages'] if m and m.get('role') == 'user')
            if remaining_users <= 1:
                break
            if a['messages'][0].get('role') != 'user':
                a['messages'].pop(0)
            else:
                a['messages'].pop(0)
                while a['messages'] and a['messages'][0] and a['messages'][0].get('role') != 'user':
                    a['messages'].pop(0)
            while a['messages'] and a['messages'][0] and a['messages'][0].get('role') not in ('user', 'system'):
                a['messages'].pop(0)
            if not a['messages']:
                break
            current_tokens = estimate_input_tokens(a)

    if not is_anthropic_message_order_valid(a['messages']):
        return {
            'error': {
                'type': 'invalid_request_error',
                'message': 'Invalid message order: after a "tool" message, only "assistant" messages are allowed.',
            }
        }

    oai = {'model': a.get('model', '')}
    msgs = []

    system_texts = []
    sys_val = a.get('system')
    sys_text = sys_val if isinstance(sys_val, str) else _flatten_text(sys_val)
    if sys_text:
        system_texts.append(sys_text)

    for m in a['messages']:
        if m and m.get('role') == 'system':
            m_text = m['content'] if isinstance(m['content'], str) else _flatten_text(m['content'])
            if m_text:
                system_texts.append(m_text)
        if m and m.get('role') == 'developer':
            d_text = m['content'] if isinstance(m['content'], str) else _flatten_text(m['content'])
            if d_text:
                system_texts.append(d_text)

    if system_texts:
        msgs.append({'role': 'system', 'content': '\n\n'.join(system_texts)})

    for m in a['messages']:
        if not m or not isinstance(m, dict):
            continue
        role = m.get('role')
        content = m.get('content')

        if role in ('system', 'developer'):
            continue

        if role == 'tool':
            tool_result_content = content if isinstance(content, list) else []
            text_parts = []
            for blk in tool_result_content:
                if isinstance(blk, dict) and blk.get('type') == 'tool_result':
                    c = blk.get('content', '')
                    c = _flatten_text(c) if isinstance(c, (list, dict)) else c
                    text_content = c if isinstance(c, str) else json.dumps(c)
                    text_parts.append(f'<tool_result id="{blk.get("tool_use_id", "")}">\n{text_content}\n</tool_result>')
            if text_parts:
                msgs.append({'role': 'user', 'content': '\n\n'.join(text_parts)})
            continue

        if isinstance(content, str):
            msgs.append({'role': role, 'content': content})
            continue

        parts = []
        tool_uses = []
        raw_content = content if isinstance(content, list) else []

        for blk in raw_content:
            if not isinstance(blk, dict):
                continue
            t = blk.get('type')
            if t == 'text':
                parts.append({'type': 'text', 'text': blk.get('text', '')})
            elif t == 'thinking':
                parts.append({'type': 'text', 'text': f'  thinking\n{blk.get("thinking", "")}\n  response\n'})
            elif t == 'image':
                src = blk.get('source', {})
                url = ''
                if src.get('type') == 'base64':
                    url = f'data:{src.get("media_type", "image/png")};base64,{src.get("data", "")}'
                else:
                    url = src.get('url', '')
                parts.append({'type': 'image_url', 'image_url': {'url': url}})
            elif t == 'tool_use':
                tool_uses.append(blk)
            elif t == 'tool_result':
                c = blk.get('content', '')
                c = _flatten_text(c) if isinstance(c, (list, dict)) else c
                text_content = c if isinstance(c, str) else json.dumps(c)
                parts.append({'type': 'text', 'text': f'<tool_result id="{blk.get("tool_use_id", "")}">\n{text_content}\n</tool_result>'})

        if tool_uses:
            dsml = format_tool_calls_as_dsml(tool_uses)
            parts.append({'type': 'text', 'text': dsml})

        if role == 'user':
            if parts:
                if all(p['type'] == 'text' for p in parts):
                    msgs.append({'role': 'user', 'content': '\n\n'.join(p['text'] for p in parts)})
                else:
                    msgs.append({'role': 'user', 'content': parts})
        elif role == 'assistant':
            am = {'role': 'assistant'}
            txt = '\n\n'.join(p['text'] for p in parts if p['type'] == 'text')
            am['content'] = txt or ''
            msgs.append(am)

    oai['messages'] = msgs
    oai['max_tokens'] = a.get('max_tokens') if a.get('max_tokens') is not None else 8192

    param_map = [
        ('temperature', 'temperature'),
        ('top_p', 'top_p'),
        ('top_k', 'top_k'),
        ('stop_sequences', 'stop'),
    ]
    for src, dst in param_map:
        if a.get(src) is not None:
            oai[dst] = a[src]

    if a.get('stream'):
        oai['stream'] = True

    if a.get('tools') and isinstance(a['tools'], list) and len(a['tools']) > 0:
        cleaned, dropped = sanitize_anthropic_tools(a['tools'])
        if dropped:
            pass
        if cleaned:
            oai['tools'] = [
                {
                    'type': 'function',
                    'function': {
                        'name': (t.get('function', t) if isinstance(t, dict) else {}).get('name', ''),
                        'description': (t.get('function', t) if isinstance(t, dict) else {}).get('description', ''),
                        'parameters': (t.get('function', t) if isinstance(t, dict) else {}).get('parameters', t.get('input_schema', {})),
                    },
                }
                for t in cleaned
            ]

    tc = a.get('tool_choice')
    if tc:
        if isinstance(tc, str):
            if tc == 'auto':
                oai['tool_choice'] = 'auto'
            elif tc == 'any':
                oai['tool_choice'] = 'required'
            elif tc == 'none':
                oai['tool_choice'] = 'none'
        elif isinstance(tc, dict):
            tt = tc.get('type')
            if tt == 'auto':
                oai['tool_choice'] = 'auto'
            elif tt == 'any':
                oai['tool_choice'] = 'required'
            elif tt == 'none':
                oai['tool_choice'] = 'none'
            elif tt == 'tool':
                oai['tool_choice'] = {'type': 'function', 'function': {'name': tc.get('name', '')}}

    if a.get('extra_body') and isinstance(a['extra_body'], dict):
        oai['extra_body'] = dict(a['extra_body'])
    if a.get('nvext') and isinstance(a['nvext'], dict):
        oai['nvext'] = dict(a['nvext'])

    return oai


def estimate_input_tokens(a: dict) -> int:
    """Approximate token count for an Anthropic request body."""
    if not a or not isinstance(a, dict):
        return 1
    chars = 0
    sys_val = a.get('system')
    chars += len(sys_val if isinstance(sys_val, str) else _flatten_text(sys_val))

    for m in a.get('messages', []):
        if not m or not isinstance(m, dict):
            continue
        c = m.get('content')
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            for blk in c:
                if not isinstance(blk, dict):
                    continue
                t = blk.get('type')
                if t == 'text':
                    chars += len(blk.get('text', ''))
                elif t == 'thinking':
                    chars += len(blk.get('thinking', ''))
                elif t == 'tool_use':
                    chars += len(blk.get('name', '')) + len(json.dumps(blk.get('input', {})))
                elif t == 'tool_result':
                    rc = blk.get('content', '')
                    chars += len(rc if isinstance(rc, str) else json.dumps(rc))
                elif t == 'image':
                    chars += 1600 * 4

    for t in a.get('tools', []):
        if not isinstance(t, dict):
            continue
        chars += len(t.get('name', '')) + len(t.get('description', '')) + len(json.dumps(t.get('input_schema', {})))

    return max(1, (chars + 3) // 4)


def openai_to_anthropic(o: dict, model: str, request_id: str = None,
                        expect_thinking: bool = False, estimated_input: int = None) -> dict:
    """Translate OpenAI chat completion response -> Anthropic message."""
    choice = (o.get('choices') or [{}])[0] if o.get('choices') else {}
    msg = choice.get('message', {})
    content = []

    _nr = extract_internal_reasoning(msg)
    reasoning = _nr['reasoning']
    raw_content = _nr['content'] or ''

    if reasoning:
        content.append({'type': 'thinking', 'thinking': reasoning})

    # Parse DSML tool calls from content
    normalized_raw = raw_content.replace('\uff5c', '|').replace('<|DSML|', '|DSML|')
    if '|DSML|tool_calls>' in normalized_raw:
        normalized = normalized_raw
        OPEN = '|DSML|tool_calls>'
        CLOSE = '</|DSML|tool_calls>'
        segments = []
        cursor = 0
        while True:
            s_idx = normalized.find(OPEN, cursor)
            if s_idx == -1:
                segments.append({'type': 'text', 'text': normalized[cursor:]})
                break
            if s_idx > cursor:
                segments.append({'type': 'text', 'text': normalized[cursor:s_idx]})
            e_idx = normalized.find(CLOSE, s_idx)
            if e_idx == -1:
                segments.append({'type': 'text', 'text': normalized[s_idx:]})
                break
            segments.append({'type': 'dsml', 'text': normalized[s_idx:e_idx + len(CLOSE)]})
            cursor = e_idx + len(CLOSE)

        for seg in segments:
            if seg['type'] == 'text':
                t = seg['text'].strip()
                if t:
                    content.append({'type': 'text', 'text': t})
                continue
            invoke_regex = re.compile(r'\|DSML\|invoke\s+name="([^"]+)"[^>]*>([\s\S]*?)<\/\|DSML\|invoke>')
            for invoke_match in invoke_regex.finditer(seg['text']):
                name = invoke_match[1]
                inner = invoke_match[2]
                params = {}
                param_regex = re.compile(r'\|DSML\|parameter\s+name="([^"]+)"[^>]*>([\s\S]*?)<\/\|DSML\|parameter>')
                for param_match in param_regex.finditer(inner):
                    params[param_match[1]] = param_match[2]
                tool_call_id = f'call_dsml_{int(time.time() * 1000)}_{hash(name) % 10000:04x}'
                content.append({'type': 'tool_use', 'id': tool_call_id, 'name': name, 'input': params})
        raw_content = ''

    if raw_content:
        content.append({'type': 'text', 'text': raw_content})

    for tc in (msg.get('tool_calls') or []):
        fn = tc.get('function', {})
        try:
            args = json.loads(fn.get('arguments', '{}'))
        except Exception:
            args = {}
        content.append({'type': 'tool_use', 'id': tc.get('id', ''), 'name': fn.get('name', ''), 'input': args})

    if expect_thinking and not any(c.get('type') == 'thinking' for c in content) and content:
        content.insert(0, {'type': 'thinking', 'thinking': '[Reasoning not supported by this model; responding directly.]'})

    if not any(c.get('type') in ('text', 'tool_use') for c in content):
        content.append({'type': 'text', 'text': ''})

    u = o.get('usage') or {}
    cached = (u.get('prompt_tokens_details') or {}).get('cached_tokens', 0)

    out_chars = 0
    for b in content:
        if b.get('type') == 'text' and isinstance(b.get('text'), str):
            out_chars += len(b['text'])
        if b.get('type') == 'tool_use' and b.get('input'):
            out_chars += len(json.dumps(b['input']))

    usage = {
        'input_tokens': u.get('prompt_tokens', estimated_input or 0),
        'output_tokens': u.get('completion_tokens') or (max(1, (out_chars + 3) // 4) if out_chars > 0 else 0),
        'cache_creation_input_tokens': 0,
        'cache_read_input_tokens': cached,
    }

    return {
        'id': f'msg_{request_id}' if request_id else f'msg_{int(time.time() * 1000)}',
        'type': 'message',
        'role': 'assistant',
        'model': model,
        'content': content,
        'stop_reason': _FINISH_TO_STOP.get(choice.get('finish_reason')),
        'stop_sequence': None,
        'usage': usage,
    }


async def stream_openai_to_anthropic(stream, model: str, capture: dict = None,
                                     input_tokens: int = 0, request_id: str = None,
                                     expect_thinking: bool = False,
                                     start_ms: float = None, **kwargs) -> AsyncGenerator[str, None]:
    """Async generator: consume OpenAI SSE stream, emit Anthropic event stream."""
    if capture is None:
        capture = {}
    if start_ms is not None:
        capture['_startMs'] = int(start_ms)
    msg_id = f'msg_{request_id}' if request_id else f'msg_{int(time.time() * 1000)}'
    text_index = None
    thinking_index = None
    tool_map = {}
    next_index = 0
    open_idx = None
    sent_content_block_start = False
    sent_text_or_tool_block = False
    final_stop = 'end_turn'
    usage = {}
    in_think_tag = False
    completed_thinking = False
    in_dsml_mode = False
    dsml_buffer = ''
    current_tool_index = None
    current_tool_name = ''
    current_tool_id = ''
    current_tool_input = {}
    generated_chars = 0
    real_thinking_emitted = False
    synthetic_thinking_emitted = False
    errored = False
    error_message = ''

    async def stop_open():
        nonlocal open_idx, text_index, thinking_index
        if open_idx is not None:
            yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': open_idx})
            if open_idx == text_index:
                text_index = None
            if open_idx == thinking_index:
                thinking_index = None
            for k, v in list(tool_map.items()):
                if v == open_idx:
                    del tool_map[k]
                    break
            open_idx = None

    async def emit_text(text):
        nonlocal completed_thinking, text_index, open_idx, sent_content_block_start, sent_text_or_tool_block, next_index
        completed_thinking = True
        if expect_thinking and not real_thinking_emitted and not synthetic_thinking_emitted:
            async for chunk in emit_synthetic_thinking():
                yield chunk
        if text_index is None:
            async for chunk in stop_open():
                yield chunk
            text_index = next_index
            open_idx = text_index
            next_index += 1
            sent_content_block_start = True
            sent_text_or_tool_block = True
            yield _sse('content_block_start', {
                'type': 'content_block_start', 'index': text_index,
                'content_block': {'type': 'text', 'text': ''},
            })
        yield _sse('content_block_delta', {
            'type': 'content_block_delta', 'index': text_index,
            'delta': {'type': 'text_delta', 'text': text},
        })

    async def emit_thinking_start():
        nonlocal thinking_index, open_idx, real_thinking_emitted, sent_content_block_start, next_index
        if thinking_index is None:
            async for chunk in stop_open():
                yield chunk
            thinking_index = next_index
            open_idx = thinking_index
            next_index += 1
            real_thinking_emitted = True
            sent_content_block_start = True
            yield _sse('content_block_start', {
                'type': 'content_block_start', 'index': thinking_index,
                'content_block': {'type': 'thinking', 'thinking': ''},
            })

    async def emit_synthetic_thinking():
        nonlocal synthetic_thinking_emitted, thinking_index, open_idx, completed_thinking, next_index
        if synthetic_thinking_emitted or real_thinking_emitted:
            return
        synthetic_thinking_emitted = True
        async for chunk in stop_open():
            yield chunk
        thinking_index = next_index
        open_idx = thinking_index
        next_index += 1
        yield _sse('content_block_start', {
            'type': 'content_block_start', 'index': thinking_index,
            'content_block': {'type': 'thinking', 'thinking': ''},
        })
        yield _sse('content_block_delta', {
            'type': 'content_block_delta', 'index': thinking_index,
            'delta': {'type': 'thinking_delta', 'thinking': '[Reasoning not supported by this model; responding directly.]'},
        })
        yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': thinking_index})
        completed_thinking = True
        if open_idx == thinking_index:
            open_idx = None
            thinking_index = None

    async def emit_thinking_delta(text):
        async for chunk in emit_thinking_start():
            yield chunk
        yield _sse('content_block_delta', {
            'type': 'content_block_delta', 'index': thinking_index,
            'delta': {'type': 'thinking_delta', 'thinking': text},
        })

    async def process_dsml(chunk):
        nonlocal dsml_buffer, in_dsml_mode, current_tool_index, current_tool_name, current_tool_id, current_tool_input, next_index, open_idx, sent_content_block_start, sent_text_or_tool_block, real_thinking_emitted, synthetic_thinking_emitted
        dsml_buffer += chunk

        while True:
            normalized = dsml_buffer.replace('\uff5c', '|').replace('<|DSML|', '|DSML|')

            invoke_pair = re.search(r'\|DSML\|invoke\s+name="([^"]+)"[^>]*>([\s\S]*?)<\/\|DSML\|invoke>', normalized)
            if invoke_pair:
                tool_name = invoke_pair.group(1)
                inner = invoke_pair.group(2)
                pair_start = invoke_pair.start()
                pair_end = pair_start + len(invoke_pair.group(0))

                if current_tool_index is None:
                    ai = next_index
                    current_tool_index = ai
                    current_tool_name = tool_name
                    current_tool_id = f'toolu_dsml_{int(time.time() * 1000)}_{hash(tool_name) % 10000:04x}_{ai}'
                    current_tool_input = {}

                    if expect_thinking and not real_thinking_emitted and not synthetic_thinking_emitted:
                        async for chunk in emit_synthetic_thinking():
                            yield chunk
                    sent_text_or_tool_block = True
                    async for chunk in stop_open():
                        yield chunk
                    open_idx = ai
                    next_index += 1

                    yield _sse('content_block_start', {
                        'type': 'content_block_start',
                        'index': ai,
                        'content_block': {'type': 'tool_use', 'id': current_tool_id, 'name': current_tool_name, 'input': {}},
                    })

                params = {}
                param_regex = re.compile(r'\|DSML\|parameter\s+name="([^"]+)"[^>]*>([\s\S]*?)<\/\|DSML\|parameter>')
                for param_match in param_regex.finditer(inner):
                    params[param_match.group(1)] = param_match.group(2)

                if current_tool_index is not None:
                    yield _sse('content_block_delta', {
                        'type': 'content_block_delta', 'index': current_tool_index,
                        'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(params)},
                    })
                    yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': current_tool_index})
                    if open_idx == current_tool_index:
                        open_idx = None

                current_tool_index = None
                current_tool_name = ''
                current_tool_id = ''
                current_tool_input = {}
                dsml_buffer = dsml_buffer[pair_end:]
                continue

            end_tool_calls_match = re.search(r'</\|DSML\|tool_calls>', normalized)
            if end_tool_calls_match:
                full_tag = end_tool_calls_match.group(0)
                match_idx = normalized.find(full_tag)

                if current_tool_index is not None:
                    yield _sse('content_block_delta', {
                        'type': 'content_block_delta', 'index': current_tool_index,
                        'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(current_tool_input)},
                    })
                    yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': current_tool_index})
                    if open_idx == current_tool_index:
                        open_idx = None
                    current_tool_index = None
                    current_tool_name = ''
                    current_tool_id = ''
                    current_tool_input = {}

                in_dsml_mode = False
                after = dsml_buffer[match_idx + len(full_tag):]
                dsml_buffer = ''
                if after:
                    async for chunk in parse_and_emit(after, False):
                        yield chunk
                continue

            start_tool_calls_match = re.search(r'\|DSML\|tool_calls>', normalized)
            if start_tool_calls_match:
                in_dsml_mode = True
                dsml_buffer = dsml_buffer[start_tool_calls_match.end():]
                continue

            break

    async def parse_and_emit(chunk, is_reasoning):
        nonlocal in_dsml_mode, completed_thinking, in_think_tag, thinking_index, open_idx, next_index, sent_content_block_start, sent_text_or_tool_block, real_thinking_emitted, synthetic_thinking_emitted, generated_chars
        if in_dsml_mode:
            async for c in process_dsml(chunk):
                yield c
            return

        if completed_thinking:
            is_reasoning = False

        if not is_reasoning and in_think_tag and thinking_index is not None:
            async for chunk in stop_open():
                yield chunk
            in_think_tag = False
            completed_thinking = True

        if completed_thinking:
            dsml_start_idx = chunk.find('<|DSML|tool_calls>')
            if dsml_start_idx == -1:
                dsml_start_idx = chunk.find('｜DSML｜tool_calls>')
            if dsml_start_idx != -1:
                before = chunk[:dsml_start_idx]
                after = chunk[dsml_start_idx:]
                if before:
                    async for c in emit_text(before):
                        yield c
                in_dsml_mode = True
                async for c in process_dsml(after):
                    yield c
                return
            async for c in emit_text(chunk):
                yield c
            return

        if is_reasoning and not in_think_tag and thinking_index is not None:
            async for c in emit_text(chunk):
                yield c
            return
        if is_reasoning and not in_think_tag:
            in_think_tag = True
            async for c in emit_thinking_start():
                yield c

        if in_think_tag:
            end_idx = -1
            tag_len = 0
            end1 = chunk.find('</think>')
            end2 = chunk.find('</thinking>')
            end3 = chunk.find('<|DSML|tool_calls>')
            if end3 == -1:
                end3 = chunk.find('｜DSML｜tool_calls>')

            if end1 != -1:
                end_idx = end1
                tag_len = 8
            if end2 != -1 and (end_idx == -1 or end2 < end_idx):
                end_idx = end2
                tag_len = 11
            if end3 != -1 and (end_idx == -1 or end3 < end_idx):
                end_idx = end3
                tag_len = 0

            if end_idx != -1:
                inside = chunk[:end_idx]
                after = chunk[end_idx + tag_len:]
                if inside:
                    async for c in emit_thinking_delta(inside):
                        yield c
                async for c in stop_open():
                    yield c
                in_think_tag = False
                completed_thinking = True
                if after:
                    async for c in parse_and_emit(after, False):
                        yield c
            else:
                async for c in emit_thinking_delta(chunk):
                    yield c
        else:
            dsml_start_idx = chunk.find('<|DSML|tool_calls>')
            if dsml_start_idx == -1:
                dsml_start_idx = chunk.find('｜DSML｜tool_calls>')
            if dsml_start_idx != -1:
                before = chunk[:dsml_start_idx]
                after = chunk[dsml_start_idx:]
                if before:
                    async for c in emit_text(before):
                        yield c
                in_dsml_mode = True
                async for c in process_dsml(after):
                    yield c
                return

            start_idx = -1
            tag_len = 0
            start1 = chunk.find('<think>')
            start2 = chunk.find('<thinking>')
            if start1 != -1:
                start_idx = start1
                tag_len = 7
            if start2 != -1 and (start_idx == -1 or start2 < start_idx):
                start_idx = start2
                tag_len = 10

            if start_idx != -1:
                before = chunk[:start_idx]
                after = chunk[start_idx + tag_len:]
                if before:
                    async for c in emit_text(before):
                        yield c
                in_think_tag = True
                async for c in emit_thinking_start():
                    yield c
                if after:
                    async for c in parse_and_emit(after, False):
                        yield c
            else:
                async for c in emit_text(chunk):
                    yield c

    yield _sse('message_start', {
        'type': 'message_start',
        'message': {
            'id': msg_id, 'type': 'message', 'role': 'assistant', 'model': model,
            'content': [], 'stop_reason': None, 'stop_sequence': None,
            'usage': {'input_tokens': input_tokens, 'output_tokens': 0, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0},
        },
    })

    # Consume either an async iterator of bytes/str OR an object with .content.iter_any()
    buffer = ''
    last_usage_chunk = ''
    is_first_read = True
    capture = capture if isinstance(capture, dict) else {}
    if 'start_ms' in (capture or {}) or capture.get('_startMs'):
        pass
    else:
        capture.setdefault('_startMs', int(time.time() * 1000))

    async def _iter_upstream():
        """Normalize various stream shapes into async chunks of str/bytes."""
        if stream is None:
            return
        # aiohttp-like response
        if hasattr(stream, 'content') and hasattr(stream.content, 'iter_any'):
            async for chunk in stream.content.iter_any():
                yield chunk
            return
        # async iterator / generator
        if hasattr(stream, '__aiter__'):
            async for chunk in stream:
                yield chunk
            return
        # sync iterable fallback
        if hasattr(stream, '__iter__') and not isinstance(stream, (str, bytes)):
            for chunk in stream:
                yield chunk
            return

    try:
        async for value in _iter_upstream():
            if is_first_read:
                start_ms = capture.get('_startMs') or capture.get('start_ms') or 0
                if start_ms:
                    capture['ttftMs'] = int(time.time() * 1000) - int(start_ms)
                is_first_read = False
            chunk_text = value.decode('utf-8', errors='replace') if isinstance(value, (bytes, bytearray)) else str(value)
            buffer += chunk_text
            if '"usage"' in chunk_text:
                last_usage_chunk = chunk_text
            lines = buffer.split('\n')
            buffer = lines.pop() if lines else ''

            for line in lines:
                trimmed = line.strip()
                if not trimmed.startswith('data:'):
                    continue
                data = trimmed[5:].strip()
                if data == '[DONE]':
                    continue
                try:
                    chunk = json.loads(data)
                except Exception:
                    continue
                if chunk.get('usage') and isinstance(chunk['usage'], dict) and chunk['usage']:
                    usage = chunk['usage']
                    capture['usage'] = chunk['usage']
                if not chunk.get('choices'):
                    continue
                ch = chunk['choices'][0]
                delta = ch.get('delta', {})

                content_text = delta.get('content', '') or ''
                reasoning = delta.get('reasoning_content') or delta.get('reasoning')

                if reasoning:
                    generated_chars += len(reasoning)
                    async for c in parse_and_emit(reasoning, True):
                        yield c
                if content_text:
                    generated_chars += len(content_text)
                    async for c in parse_and_emit(content_text, False):
                        yield c

                for tc in (delta.get('tool_calls') or []):
                    oi = tc.get('index', 0)
                    fn = tc.get('function', {})
                    if oi not in tool_map:
                        if expect_thinking and not real_thinking_emitted and not synthetic_thinking_emitted:
                            async for c in emit_synthetic_thinking():
                                yield c
                        async for c in stop_open():
                            yield c
                        ai = next_index
                        tool_map[oi] = ai
                        open_idx = ai
                        sent_content_block_start = True
                        tool_call_id = tc.get('id') or f'toolu_{int(time.time() * 1000)}_{hash(str(ai)) % 10000:04x}_{ai}'
                        sent_text_or_tool_block = True
                        yield _sse('content_block_start', {
                            'type': 'content_block_start', 'index': ai,
                            'content_block': {'type': 'tool_use', 'id': tool_call_id, 'name': fn.get('name', ''), 'input': {}},
                        })
                        next_index += 1
                    ai = tool_map[oi]
                    if fn.get('arguments'):
                        generated_chars += len(fn['arguments'])
                        yield _sse('content_block_delta', {
                            'type': 'content_block_delta', 'index': ai,
                            'delta': {'type': 'input_json_delta', 'partial_json': fn['arguments']},
                        })

                if ch.get('finish_reason'):
                    final_stop = _FINISH_TO_STOP.get(ch['finish_reason']) or 'end_turn'
    except Exception as e:
        errored = True
        error_message = str(e) if e else 'upstream connection error'
    finally:
        # Best-effort release for aiohttp responses
        try:
            if hasattr(stream, 'release'):
                await stream.release()
        except Exception:
            pass
        if open_idx is not None:
            try:
                yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': open_idx})
            except Exception:
                pass
            open_idx = None

        if current_tool_index is not None:
            try:
                yield _sse('content_block_delta', {
                    'type': 'content_block_delta', 'index': current_tool_index,
                    'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(current_tool_input)},
                })
            except Exception:
                pass
            try:
                yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': current_tool_index})
            except Exception:
                pass
            current_tool_index = None

        _finalize_capture(capture, usage, input_tokens, generated_chars, last_usage_chunk, final_stop)

    if open_idx is not None:
        yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': open_idx})

    if errored:
        capture['errored'] = True
        capture['errorMessage'] = error_message or 'upstream connection error'
        return

    if not sent_text_or_tool_block and not errored:
        empty_idx = next_index
        yield _sse('content_block_start', {
            'type': 'content_block_start', 'index': empty_idx,
            'content_block': {'type': 'text', 'text': ''},
        })
        yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': empty_idx})

    estimated_output = max(1, (generated_chars + 3) // 4)
    reported_input = usage.get('prompt_tokens', input_tokens or 0)
    reported_output = usage.get('completion_tokens', estimated_output)
    capture['reportedInputTokens'] = reported_input
    capture['reportedOutputTokens'] = reported_output
    yield _sse('message_delta', {
        'type': 'message_delta',
        'delta': {'stop_reason': final_stop, 'stop_sequence': None},
        'usage': {
            'input_tokens': reported_input,
            'output_tokens': reported_output,
            'cache_creation_input_tokens': 0,
            'cache_read_input_tokens': (usage.get('prompt_tokens_details') or {}).get('cached_tokens', 0),
        },
    })
    yield _sse('message_stop', {'type': 'message_stop'})


def _finalize_capture(capture, usage, input_tokens, generated_chars, last_usage_chunk, final_stop):
    if not capture.get('usage') or not capture['usage']:
        if last_usage_chunk:
            usage_idx = last_usage_chunk.find('"usage"')
            if usage_idx != -1:
                colon_idx = last_usage_chunk.find(':', usage_idx)
                if colon_idx != -1:
                    brace_start = last_usage_chunk.find('{', colon_idx)
                    if brace_start != -1:
                        depth = 0
                        brace_end = -1
                        for i in range(brace_start, len(last_usage_chunk)):
                            if last_usage_chunk[i] == '{':
                                depth += 1
                            elif last_usage_chunk[i] == '}':
                                depth -= 1
                                if depth == 0:
                                    brace_end = i
                                    break
                        if brace_end != -1:
                            try:
                                capture['usage'] = json.loads(last_usage_chunk[brace_start:brace_end + 1])
                            except Exception:
                                pass
    if not capture.get('usage') or not capture['usage']:
        capture['usage'] = dict(usage) if usage else {}

    estimated_output = max(1, (generated_chars + 3) // 4)
    if capture.get('usage'):
        pt = capture['usage'].get('prompt_tokens', capture['usage'].get('input_tokens', 0))
        ct = capture['usage'].get('completion_tokens', capture['usage'].get('output_tokens', 0))
        if not pt:
            if 'prompt_tokens' in capture['usage']:
                capture['usage']['prompt_tokens'] = input_tokens
            elif 'input_tokens' in capture['usage']:
                capture['usage']['input_tokens'] = input_tokens
            else:
                capture['usage']['prompt_tokens'] = input_tokens
        if not ct:
            if 'completion_tokens' in capture['usage']:
                capture['usage']['completion_tokens'] = estimated_output
            elif 'output_tokens' in capture['usage']:
                capture['usage']['output_tokens'] = estimated_output
            else:
                capture['usage']['completion_tokens'] = estimated_output
    capture['stop'] = final_stop
