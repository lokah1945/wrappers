#!/usr/bin/env python3
"""
responses_compat.py — OpenAI Responses API support for wrapper-nvidia (Python).

Codex and other agent clients use the OpenAI Responses wire format. NVIDIA NIM
only exposes OpenAI-style chat/completions for the models handled by this
wrapper, so this module translates Responses⇄Chat and, critically, guarantees
that streamed Responses always emit a complete event lifecycle:

  response.created → response.in_progress → output_item/content_part added
  → deltas → done events → response.completed → data: [DONE]

The implementation intentionally favours tolerant input conversion and strict
terminal events because partial streams are the main reason Codex/Claude-style
agents stop mid-run.
"""

from __future__ import annotations

import json
import time
import random
import string
from typing import Dict, List, Optional, Any, Tuple, AsyncGenerator

from .anthropic_compat import extract_internal_reasoning

# previous_response_id store for Codex multi-turn server-side history.
# Values are OpenAI chat messages, including the assistant message that contained
# tool_calls. Without that assistant message a later role=tool result is orphaned
# and upstream rejects it, which makes agents stop mid-process.
_RESPONSE_STORE: Dict[str, list] = {}


def _rand(suffix: str) -> str:
    chars = string.ascii_lowercase + string.digits
    rand_part = ''.join(random.choices(chars, k=10))
    time_part = format(int(time.time() * 1000), 'x')[-4:]
    return f"{suffix}_{rand_part}{time_part}"


def _zero_usage() -> dict:
    return {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}


def _bounded_store(resp_id: str, messages: list) -> None:
    if not resp_id:
        return
    _RESPONSE_STORE[resp_id] = messages
    while len(_RESPONSE_STORE) > 200:
        _RESPONSE_STORE.pop(next(iter(_RESPONSE_STORE)))


def http_status_from_error(err: dict) -> int:
    if err and isinstance(err.get('status'), (int, float)):
        return int(err['status'])
    t = (err.get('type') or '').lower() if err else ''
    mapping = {
        'rate_limit_error': 429,
        'invalid_request_error': 400,
        'authentication_error': 401,
        'permission_error': 403,
        'not_found_error': 404,
        'request_too_large': 413,
        'unprocessable_entity_error': 422,
    }
    return mapping.get(t, 500)


def _stringify_content(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for p in value:
            if isinstance(p, dict):
                if p.get('type') in ('text', 'input_text', 'output_text'):
                    parts.append(p.get('text', '') or '')
                elif p.get('type') == 'input_file':
                    parts.append(p.get('text', '') or '')
            else:
                parts.append(str(p))
        return ''.join(parts)
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def _convert_content_parts(content: Any) -> Any:
    """Responses content parts -> Chat content parts/string."""
    if not isinstance(content, list):
        return content if content is not None else ''
    new_content = []
    for part in content:
        if not part or not isinstance(part, dict):
            new_content.append({'type': 'text', 'text': '' if part is None else str(part)})
        elif part.get('type') in ('input_text', 'output_text', 'text'):
            new_content.append({'type': 'text', 'text': part.get('text', '') or ''})
        elif part.get('type') == 'input_image':
            url = part.get('image_url', {}).get('url') if isinstance(part.get('image_url'), dict) else part.get('image_url') or part.get('url')
            if url:
                new_content.append({'type': 'image_url', 'image_url': {'url': url}})
        elif part.get('type') == 'input_file':
            new_content.append({'type': 'text', 'text': part.get('text', '') or ''})
        else:
            # Unknown Responses content parts are preserved as inert text rather
            # than forwarded verbatim to NIM validators.
            new_content.append({'type': 'text', 'text': part.get('text', '') or ''})
    if not new_content:
        return ''
    # If everything is text, collapse to a string for broadest upstream support.
    if all(isinstance(p, dict) and p.get('type') == 'text' for p in new_content):
        return ''.join(p.get('text', '') for p in new_content)
    return new_content


def _repair_orphan_tool_messages(messages: List[dict]) -> List[dict]:
    """Convert orphan role=tool messages into user text instead of sending an
    invalid OpenAI chat sequence upstream.

    This is a last-resort recovery for process restarts or missing
    previous_response_id history. When history is available the assistant
    tool_calls are preserved and this function is a no-op.
    """
    seen_call_ids = set()
    repaired = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get('role') == 'assistant':
            for tc in msg.get('tool_calls') or []:
                if isinstance(tc, dict) and tc.get('id'):
                    seen_call_ids.add(tc['id'])
            repaired.append(msg)
            continue
        if msg.get('role') == 'tool':
            tcid = msg.get('tool_call_id') or ''
            if tcid and tcid in seen_call_ids:
                repaired.append(msg)
            else:
                repaired.append({
                    'role': 'user',
                    'content': f"Tool result{f' for {tcid}' if tcid else ''}: {_stringify_content(msg.get('content'))}",
                })
            continue
        repaired.append(msg)
    return repaired


def input_to_messages(input_val: Any, instructions: Optional[str] = None) -> List[dict]:
    messages: List[dict] = []
    if instructions and isinstance(instructions, str) and instructions.strip():
        messages.append({'role': 'system', 'content': instructions})

    if isinstance(input_val, str):
        items = [{'role': 'user', 'content': input_val}]
    elif isinstance(input_val, list):
        items = input_val
    elif input_val is not None:
        items = [input_val]
    else:
        items = []

    for item in items:
        if not item or not isinstance(item, dict):
            if isinstance(item, str):
                messages.append({'role': 'user', 'content': item})
            continue

        role = item.get('role')
        normalized_role = 'system' if role == 'developer' else role
        itype = item.get('type')

        # Stored OpenAI chat tool result or Responses function_call_output.
        if normalized_role == 'tool' or itype == 'function_call_output':
            output = item.get('output') if itype == 'function_call_output' else item.get('content')
            messages.append({
                'role': 'tool',
                'tool_call_id': item.get('call_id') or item.get('tool_call_id') or item.get('tool_use_id') or '',
                'content': output if isinstance(output, str) else json.dumps(output if output is not None else '', ensure_ascii=False),
            })
            continue

        # Responses function_call item.
        if itype == 'function_call':
            raw_args = item.get('arguments')
            if isinstance(raw_args, str):
                # Keep valid JSON strings as-is to avoid double-encoding Codex
                # arguments. Parse+dump normalizes whitespace only.
                try:
                    raw_args = json.loads(raw_args)
                    arguments = json.dumps(raw_args, ensure_ascii=False)
                except (json.JSONDecodeError, ValueError):
                    arguments = raw_args
            elif raw_args is None:
                arguments = ''
            else:
                arguments = json.dumps(raw_args, ensure_ascii=False)
            messages.append({
                'role': 'assistant',
                'content': None,
                'tool_calls': [{
                    'id': item.get('call_id') or item.get('id') or _rand('call'),
                    'type': 'function',
                    'function': {'name': item.get('name', '') or '', 'arguments': arguments},
                }],
            })
            continue

        # Stored OpenAI chat message or Responses message item.
        if itype == 'message' or normalized_role in ('user', 'system', 'assistant'):
            content = _convert_content_parts(item.get('content'))
            msg = {'role': normalized_role or 'user', 'content': content if content is not None else ''}
            if (normalized_role == 'assistant') and isinstance(item.get('tool_calls'), list) and item['tool_calls']:
                msg['tool_calls'] = item['tool_calls']
                if not msg.get('content'):
                    msg['content'] = None
            messages.append(msg)
            continue

        # Reasoning and other output item types are not valid chat messages.

    return _repair_orphan_tool_messages(messages)


def convert_tools(tools: Optional[list]) -> Optional[list]:
    """Convert Responses/OpenAI tools → chat tools.

    Drops unsupported/built-in tools and tools with missing/null/empty names
    (Codex/Hermes sometimes send placeholders with name:null which break
    upstream validators).
    """
    if not tools or not isinstance(tools, list):
        return None
    out = []
    for t in tools:
        if not t or not isinstance(t, dict):
            continue
        if t.get('type') == 'function' and isinstance(t.get('function'), dict):
            fn = t['function']
        elif t.get('name') and t.get('type') in (None, 'function'):
            fn = t
        else:
            continue
        name = fn.get('name')
        if not name:
            continue
        out.append({
            'type': 'function',
            'function': {
                'name': name,
                'description': fn.get('description', '') or '',
                'parameters': fn.get('parameters') or fn.get('input_schema') or {},
            },
        })
    return out if out else None


def convert_usage(u: Optional[dict]) -> dict:
    if not u:
        return _zero_usage()
    prompt_tokens = u.get('prompt_tokens', u.get('input_tokens', 0)) or 0
    completion_tokens = u.get('completion_tokens', u.get('output_tokens', 0)) or 0
    return {
        'input_tokens': int(prompt_tokens),
        'output_tokens': int(completion_tokens),
        'total_tokens': int(u.get('total_tokens') or (prompt_tokens + completion_tokens)),
    }


def base_response(resp_id: str, model: str, status: str, output: list = None, usage: dict = None) -> dict:
    return {
        'id': resp_id,
        'object': 'response',
        'created_at': int(time.time()),
        'model': model,
        'status': status,
        'output': output or [],
        'usage': usage if usage is not None else _zero_usage(),
    }


def make_reasoning_item(text: str) -> dict:
    return {'id': _rand('rsn'), 'type': 'reasoning', 'status': 'completed', 'summary': '', 'text': text}


def _assistant_message_from_chat(data: dict, fallback_text: str = '', tool_accs: Optional[list] = None) -> dict:
    msg = (data.get('choices') or [{}])[0].get('message', {}) if isinstance(data, dict) else {}
    content = msg.get('content')
    if content is None:
        content = fallback_text if fallback_text is not None else None
    tool_calls = msg.get('tool_calls') or []
    if tool_accs:
        tool_calls = [
            {
                'id': acc.get('call_id') or _rand('call'),
                'type': 'function',
                'function': {'name': acc.get('name', '') or '', 'arguments': acc.get('args', '') or ''},
            }
            for acc in tool_accs if acc
        ]
    out = {'role': 'assistant', 'content': content if content not in ('', None) else (None if tool_calls else '')}
    if tool_calls:
        out['tool_calls'] = tool_calls
    return out


def respond_non_streaming(data: dict, model: str) -> dict:
    msg = (data.get('choices') or [{}])[0].get('message', {}) if data.get('choices') else {}
    nr = extract_internal_reasoning(msg)
    text = nr.get('content', '') or ''
    reason_text = nr.get('reasoning', '')
    tool_calls = msg.get('tool_calls') if msg else None
    resp_id = _rand('resp')
    output = []

    if reason_text:
        output.append(make_reasoning_item(reason_text))

    if text or not tool_calls:
        output.append({
            'id': _rand('msg'),
            'type': 'message',
            'status': 'completed',
            'role': 'assistant',
            'content': [{'type': 'output_text', 'text': text, 'annotations': []}],
        })

    for tc in tool_calls or []:
        output.append({
            'id': _rand('fc'),
            'type': 'function_call',
            'status': 'completed',
            'call_id': tc.get('id') or _rand('call'),
            'name': tc.get('function', {}).get('name', '') or '',
            'arguments': tc.get('function', {}).get('arguments', '') or '',
        })

    return {
        'id': resp_id,
        'object': 'response',
        'created_at': int(time.time()),
        'model': data.get('model') or model,
        'status': 'completed',
        'output': output,
        'usage': convert_usage(data.get('usage')),
    }


def build_chat_body(body: dict, model: str, translate_thinking_to_nim) -> dict:
    chat_body = {
        'model': model,
        'messages': input_to_messages(body.get('input'), body.get('instructions')),
        'stream': bool(body.get('stream')),
    }

    if body.get('temperature') is not None:
        try:
            chat_body['temperature'] = float(body['temperature'])
        except (TypeError, ValueError):
            pass
    if body.get('top_p') is not None:
        try:
            chat_body['top_p'] = float(body['top_p'])
        except (TypeError, ValueError):
            pass
    if body.get('max_output_tokens') is not None:
        try:
            chat_body['max_tokens'] = int(body['max_output_tokens'])
        except (TypeError, ValueError):
            pass
    elif body.get('max_tokens') is not None:
        try:
            chat_body['max_tokens'] = int(body['max_tokens'])
        except (TypeError, ValueError):
            pass

    tools = convert_tools(body.get('tools'))
    if tools:
        chat_body['tools'] = tools

    tool_choice = body.get('tool_choice')
    if isinstance(tool_choice, dict):
        tc_type = tool_choice.get('type')
        if tc_type == 'function' and (tool_choice.get('name') or (tool_choice.get('function') or {}).get('name')):
            chat_body['tool_choice'] = {'type': 'function', 'function': {'name': tool_choice.get('name') or tool_choice.get('function', {}).get('name')}}
        elif tc_type == 'required':
            chat_body['tool_choice'] = 'required'
        elif tc_type == 'auto':
            chat_body['tool_choice'] = 'auto'
    elif isinstance(tool_choice, str) and tool_choice in ('auto', 'required', 'none'):
        chat_body['tool_choice'] = tool_choice

    if body.get('reasoning') is not None and translate_thinking_to_nim:
        translate_thinking_to_nim(chat_body, model, body['reasoning'])

    return chat_body


class ResponsesHandler:
    """Handles OpenAI Responses API requests by translating to NIM chat/completions."""

    def __init__(self, deps: dict):
        self.pool = deps['pool']
        self.resolve_target_model = deps['resolve_target_model']
        self.proxy_openai = deps['proxy_openai']
        self.forward_headers = deps['forward_headers']
        self.base_llm = deps.get('BASE_LLM', '')
        self.base_genai = deps.get('BASE_GENAI', '')
        self.describe = deps.get('describe')
        self.curated_genai = deps.get('CURATED_GENAI', [])
        self.translate_thinking_to_nim = deps.get('translate_thinking_to_nim')
        self.get_deprecated_redirect_info = deps.get('get_deprecated_redirect_info')
        self.extract_internal_reasoning = deps.get('extract_internal_reasoning', extract_internal_reasoning)
        self.guard_stream_unsupported = deps.get('guard_stream_unsupported')

    def is_nvidia_model(self, model_id: str) -> bool:
        if not model_id:
            return False
        cached = getattr(self.pool, 'models_cached', None) or []
        curated = self.curated_genai or []
        if model_id in cached or model_id in curated:
            return True
        if not cached:
            return True
        if '/' in model_id and ':' not in model_id:
            return True
        return False

    async def translate_to_nim(self, request: Any, body: dict, model: str) -> Tuple[Optional[dict], Optional[AsyncGenerator[str, None]]]:
        chat_body = build_chat_body(body, model, self.translate_thinking_to_nim)
        result = await self.proxy_openai(chat_body, self.forward_headers(request), model, request)

        if not result.get('stream') and result.get('status') and result['status'] != 200:
            data = result.get('data', {})
            if data and data.get('error'):
                err = dict(data['error'])
                err['status'] = result['status']
                return {'error': err}, None

        if not chat_body.get('stream'):
            data = result.get('data', {})
            resp_obj = respond_non_streaming(data, model)
            _bounded_store(resp_obj.get('id'), chat_body.get('messages', []) + [_assistant_message_from_chat(data)])
            return resp_obj, None

        async def stream_gen() -> AsyncGenerator[str, None]:
            resp_id = _rand('resp')
            msg_id = _rand('msg')
            seq = 0
            next_extra_index = 1
            msg_index = 0
            rsn_index = None
            rsn_id = _rand('rsn')
            rsn_started = False
            acc_reason = ''
            acc_text = ''
            tool_accs: List[Optional[dict]] = []
            has_tool = False
            usage = _zero_usage()
            buffer = ''
            stream_error = None

            def next_seq() -> int:
                nonlocal seq
                seq += 1
                return seq

            def emit(obj: dict) -> str:
                return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

            def make_tool_acc(idx: int, tc: dict) -> dict:
                nonlocal next_extra_index
                acc = {
                    'name': '', 'args': '',
                    'call_id': tc.get('id') or _rand('call'),
                    'output_index': next_extra_index,
                    'added': False,
                }
                next_extra_index += 1
                while len(tool_accs) <= idx:
                    tool_accs.append(None)
                tool_accs[idx] = acc
                return acc

            base = base_response(resp_id, model, 'in_progress')
            yield emit({'type': 'response.created', 'sequence_number': next_seq(), 'response': base})
            yield emit({'type': 'response.in_progress', 'sequence_number': next_seq(), 'response': base})
            yield emit({
                'type': 'response.output_item.added', 'sequence_number': next_seq(), 'output_index': msg_index,
                'item': {'id': msg_id, 'type': 'message', 'status': 'in_progress', 'role': 'assistant', 'content': []},
            })
            yield emit({
                'type': 'response.content_part.added', 'sequence_number': next_seq(),
                'item_id': msg_id, 'output_index': msg_index, 'content_index': 0,
                'part': {'type': 'output_text', 'text': '', 'annotations': []},
            })

            stream = result.get('stream')
            try:
                if stream is not None:
                    async for raw in stream:
                        chunk = raw.decode('utf-8', errors='replace') if isinstance(raw, (bytes, bytearray)) else str(raw)
                        buffer += chunk
                        lines = buffer.split('\n')
                        buffer = lines.pop() if lines else ''
                        for line in lines:
                            t = line.strip()
                            if not t.startswith('data:'):
                                continue
                            payload = t[5:].strip()
                            if payload in ('[DONE]', '"[DONE]"', ''):
                                continue
                            try:
                                c = json.loads(payload)
                            except (json.JSONDecodeError, ValueError):
                                continue
                            if c.get('usage'):
                                usage = convert_usage(c['usage'])
                            if not c.get('choices'):
                                continue
                            ch = c['choices'][0]
                            d = ch.get('delta') or {}
                            if isinstance(d.get('content'), str) and d['content']:
                                acc_text += d['content']
                                yield emit({
                                    'type': 'response.output_text.delta', 'sequence_number': next_seq(),
                                    'response_id': resp_id, 'item_id': msg_id, 'output_index': msg_index,
                                    'content_index': 0, 'delta': d['content'],
                                })
                            reason_delta = d.get('reasoning_content') if isinstance(d.get('reasoning_content'), str) else d.get('reasoning') if isinstance(d.get('reasoning'), str) else ''
                            if reason_delta:
                                if not rsn_started:
                                    rsn_started = True
                                    rsn_index = next_extra_index
                                    next_extra_index += 1
                                    yield emit({
                                        'type': 'response.output_item.added', 'sequence_number': next_seq(),
                                        'output_index': rsn_index,
                                        'item': {'id': rsn_id, 'type': 'reasoning', 'status': 'in_progress', 'summary': '', 'content': []},
                                    })
                                acc_reason += reason_delta
                                yield emit({
                                    'type': 'response.reasoning_text.delta', 'sequence_number': next_seq(),
                                    'item_id': rsn_id, 'output_index': rsn_index, 'content_index': 0,
                                    'delta': reason_delta,
                                })
                            for tc in d.get('tool_calls') or []:
                                idx = tc.get('index') if isinstance(tc.get('index'), int) else len(tool_accs)
                                acc = tool_accs[idx] if idx < len(tool_accs) else None
                                if acc is None:
                                    acc = make_tool_acc(idx, tc)
                                fn = tc.get('function') or {}
                                if tc.get('id') and (not acc.get('call_id') or acc['call_id'].startswith('call_')):
                                    acc['call_id'] = tc['id']
                                if not acc['added']:
                                    acc['added'] = True
                                    yield emit({
                                        'type': 'response.output_item.added', 'sequence_number': next_seq(),
                                        'output_index': acc['output_index'],
                                        'item': {'id': acc['call_id'], 'type': 'function_call', 'status': 'in_progress',
                                                 'call_id': acc['call_id'], 'name': acc['name'], 'arguments': ''},
                                    })
                                if fn.get('name'):
                                    acc['name'] += fn['name']
                                    yield emit({
                                        'type': 'response.function_call.delta', 'sequence_number': next_seq(),
                                        'item_id': acc['call_id'], 'output_index': acc['output_index'],
                                        'delta': fn['name'], 'name': acc['name'],
                                    })
                                if fn.get('arguments'):
                                    acc['args'] += fn['arguments']
                                    yield emit({
                                        'type': 'response.function_call.delta', 'sequence_number': next_seq(),
                                        'item_id': acc['call_id'], 'output_index': acc['output_index'],
                                        'delta': fn['arguments'],
                                    })
                                has_tool = True
                # Process a final single-line data payload if the upstream closed
                # without a trailing newline.
                if buffer.strip().startswith('data:'):
                    # Recursion would complicate generator state; the worst case is
                    # a final usage chunk, so ignore malformed tail rather than leak.
                    pass
            except Exception as e:
                stream_error = e
                import logging
                logging.getLogger('responses').error(f"[responses:nim stream] {e}")

            if not acc_text and not has_tool:
                acc_text = f"[upstream stream error: {stream_error}]" if stream_error else '[No text response; the model returned no visible text.]'
                yield emit({
                    'type': 'response.output_text.delta', 'sequence_number': next_seq(),
                    'response_id': resp_id, 'item_id': msg_id, 'output_index': msg_index,
                    'content_index': 0, 'delta': acc_text,
                })

            outputs_by_index = {
                msg_index: {'id': msg_id, 'type': 'message', 'status': 'completed', 'role': 'assistant',
                            'content': [{'type': 'output_text', 'text': acc_text, 'annotations': []}]}
            }

            if rsn_started:
                yield emit({'type': 'response.reasoning_text.done', 'sequence_number': next_seq(),
                            'item_id': rsn_id, 'output_index': rsn_index, 'content_index': 0, 'text': acc_reason})
                rsn_item = {'id': rsn_id, 'type': 'reasoning', 'status': 'completed', 'summary': '', 'text': acc_reason}
                yield emit({'type': 'response.output_item.done', 'sequence_number': next_seq(), 'output_index': rsn_index, 'item': rsn_item})
                outputs_by_index[rsn_index] = make_reasoning_item(acc_reason)

            yield emit({'type': 'response.output_text.done', 'sequence_number': next_seq(),
                        'response_id': resp_id, 'item_id': msg_id, 'output_index': msg_index,
                        'content_index': 0, 'text': acc_text})
            yield emit({'type': 'response.content_part.done', 'sequence_number': next_seq(),
                        'item_id': msg_id, 'output_index': msg_index, 'content_index': 0,
                        'part': {'type': 'output_text', 'text': acc_text, 'annotations': []}})
            yield emit({'type': 'response.output_item.done', 'sequence_number': next_seq(),
                        'output_index': msg_index, 'item': outputs_by_index[msg_index]})

            completed_tools = [acc for acc in tool_accs if acc]
            for acc in completed_tools:
                fc_item = {'id': acc['call_id'], 'type': 'function_call', 'status': 'completed',
                           'call_id': acc['call_id'], 'name': acc['name'], 'arguments': acc['args']}
                yield emit({'type': 'response.output_item.done', 'sequence_number': next_seq(),
                            'output_index': acc['output_index'], 'item': fc_item})
                outputs_by_index[acc['output_index']] = fc_item

            outputs = [outputs_by_index[i] for i in sorted(outputs_by_index)]
            yield emit({'type': 'response.completed', 'sequence_number': next_seq(),
                        'response': base_response(resp_id, model, 'completed', outputs, usage)})
            yield 'data: [DONE]\n\n'

            _bounded_store(resp_id, chat_body.get('messages', []) + [_assistant_message_from_chat({}, acc_text, completed_tools)])

        return None, stream_gen()

    async def handle_responses_api(self, request: Any, raw_body: bytes) -> Tuple[Optional[dict], Optional[AsyncGenerator[str, None]], Optional[int]]:
        try:
            body = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError) as e:
            return {'error': {'message': f'Invalid JSON in /v1/responses: {e}', 'type': 'invalid_request_error'}}, None, None

        if not body or not body.get('model'):
            return {'error': {'message': 'Missing "model" in /v1/responses request', 'type': 'invalid_request_error'}}, None, None

        model = self.resolve_target_model(body['model'])

        prev = body.get('previous_response_id')
        if prev and prev in _RESPONSE_STORE:
            stored = _RESPONSE_STORE[prev]
            cur = body.get('input')
            if isinstance(cur, list):
                body['input'] = stored + cur
            elif isinstance(cur, str):
                body['input'] = stored + [{'role': 'user', 'content': cur}]
            elif cur is None:
                body['input'] = stored

        if self.get_deprecated_redirect_info:
            dep_r = self.get_deprecated_redirect_info(body['model'])
            if dep_r:
                return {'error': {'message': f'Model "{dep_r["from"]}" has been renamed to "{dep_r["to"]}" in the NVIDIA NIM catalog. Update your request to use "{dep_r["to"]}".', 'type': 'invalid_request_error'}}, None, None

        if self.guard_stream_unsupported:
            stream_guard = self.guard_stream_unsupported(body, body['model'])
            if stream_guard:
                return {'error': {'message': stream_guard['data']['error']['message'], 'type': stream_guard['data']['error']['type']}}, None, None

        if not self.is_nvidia_model(model):
            return {'error': {'message': f'Model "{model}" is not a NVIDIA NIM model and cannot be served by wrapper-nvidia. Use a NVIDIA NIM model (e.g. nvidia/llama-3.3-nemotron-super-49b-v1). wrapper-nvidia is NVIDIA-NIM-only.', 'type': 'invalid_request_error'}}, None, None

        result, stream = await self.translate_to_nim(request, body, model)

        if result is not None and result.get('error'):
            err = result['error']
            status = http_status_from_error(err)
            error_out = {k: v for k, v in err.items() if k != 'status'}
            return {'error': error_out}, None, status

        if result is not None and result.get('id'):
            return result, None, None

        return None, stream, None
