#!/usr/bin/env python3
"""
responses_compat.py — OpenAI Responses API support for wrapper-nvidia (Python).

Codex >= 0.144 requires wire_api="responses" (the OpenAI Responses API).
wrapper-nvidia is a pure NVIDIA NIM proxy, so this module translates the
Responses request into a NIM chat/completions call and converts the
response back into the Responses event format. No third-party routing.
"""

import json
import time
import random
import string
from typing import Dict, List, Optional, Any, Tuple, AsyncGenerator

from .anthropic_compat import extract_internal_reasoning

# P2: previous_response_id store for codex multi-turn server-side history
_RESPONSE_STORE: Dict[str, list] = {}


def _rand(suffix: str) -> str:
    chars = string.ascii_lowercase + string.digits
    rand_part = ''.join(random.choices(chars, k=10))
    time_part = format(int(time.time() * 1000), 'x')[-4:]
    return f"{suffix}_{rand_part}{time_part}"


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


def input_to_messages(input_val: Any, instructions: Optional[str] = None) -> List[dict]:
    messages = []
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

        if item.get('type') == 'message' or role in ('user', 'system', 'developer', 'assistant'):
            content = item.get('content')
            if isinstance(content, list):
                new_content = []
                for part in content:
                    if not part or not isinstance(part, dict):
                        new_content.append({'type': 'text', 'text': ''})
                    elif part.get('type') == 'input_text':
                        new_content.append({'type': 'text', 'text': part.get('text', '')})
                    elif part.get('type') == 'input_image':
                        url = part.get('image_url', {}).get('url') if isinstance(part.get('image_url'), dict) else part.get('image_url') or part.get('url')
                        new_content.append({'type': 'image_url', 'image_url': {'url': url}})
                    elif part.get('type') == 'input_file':
                        new_content.append({'type': 'text', 'text': part.get('text', '')})
                    else:
                        new_content.append({'type': 'text', 'text': ''})
                content = new_content
            messages.append({'role': normalized_role or 'user', 'content': content if content is not None else ''})

        elif item.get('type') == 'function_call':
            raw_args = item.get('arguments')
            # Normalize: if Codex sent a JSON string, parse it so we can
            # re-serialize with correct types (ints stay ints, not "250").
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except (json.JSONDecodeError, ValueError):
                    pass
            if isinstance(raw_args, (dict, list, int, float, bool)) or raw_args is None:
                arguments = json.dumps(raw_args) if raw_args is not None else ''
            else:
                arguments = str(raw_args)
            messages.append({
                'role': 'assistant',
                'content': None,
                'tool_calls': [{
                    'id': item.get('call_id') or _rand('call'),
                    'type': 'function',
                    'function': {
                        'name': item.get('name', ''),
                        'arguments': arguments,
                    },
                }],
            })

        elif item.get('type') == 'function_call_output':
            output = item.get('output')
            messages.append({
                'role': 'tool',
                'tool_call_id': item.get('call_id'),
                'content': output if isinstance(output, str) else json.dumps(output or ''),
            })

    return messages


def convert_tools(tools: Optional[list]) -> Optional[list]:
    """Convert Responses/OpenAI tools → chat tools.

    Drops tools with missing/null/empty names (Codex/Hermes sometimes send
    placeholder tools with name:null which break upstream validators).
    Also accepts bare function-shaped tools without the outer {type,function} wrap.
    """
    if not tools or not isinstance(tools, list):
        return None
    out = []
    for t in tools:
        if not t or not isinstance(t, dict):
            continue
        # Responses / OpenAI chat shape: {type:"function", function:{name,...}}
        if t.get('type') == 'function' and isinstance(t.get('function'), dict):
            fn = t['function']
            name = fn.get('name')
            if not name:
                continue  # Codex/Hermes name:null filter
            out.append({
                'type': 'function',
                'function': {
                    'name': name,
                    'description': fn.get('description', '') or '',
                    'parameters': fn.get('parameters', {}) or {},
                },
            })
            continue
        # Bare function shape: {name, description, parameters} (some SDKs)
        name = t.get('name')
        if name and t.get('type') in (None, 'function'):
            out.append({
                'type': 'function',
                'function': {
                    'name': name,
                    'description': t.get('description', '') or '',
                    'parameters': t.get('parameters') or t.get('input_schema') or {},
                },
            })
    return out if out else None


def convert_usage(u: Optional[dict]) -> Optional[dict]:
    if not u:
        return None
    prompt_tokens = u.get('prompt_tokens', 0) or 0
    completion_tokens = u.get('completion_tokens', 0) or 0
    return {
        'input_tokens': prompt_tokens,
        'output_tokens': completion_tokens,
        'total_tokens': u.get('total_tokens') or (prompt_tokens + completion_tokens),
    }


def base_response(resp_id: str, model: str, status: str, output: list = None, usage: dict = None) -> dict:
    return {
        'id': resp_id,
        'object': 'response',
        'created_at': int(time.time()),
        'model': model,
        'status': status,
        'output': output or [],
        'usage': usage or None,
    }


def make_reasoning_item(text: str) -> dict:
    return {
        'id': _rand('rsn'),
        'type': 'reasoning',
        'status': 'completed',
        'summary': '',
        'text': text,
    }


def respond_non_streaming(data: dict, model: str) -> dict:
    msg = (data.get('choices') or [{}])[0].get('message', {}) if data.get('choices') else {}
    text = (msg.get('content') or '') if msg else ''
    tool_calls = msg.get('tool_calls') if msg else None
    resp_id = _rand('resp')

    nr = extract_internal_reasoning(msg)
    reason_text = nr.get('reasoning', '')

    if tool_calls and len(tool_calls) > 0:
        output = [
            {
                'id': _rand('fc'),
                'type': 'function_call',
                'status': 'completed',
                'call_id': tc.get('id') or _rand('call'),
                'name': tc.get('function', {}).get('name', ''),
                'arguments': tc.get('function', {}).get('arguments', ''),
            }
            for tc in tool_calls
        ]
    else:
        output = [
            {
                'id': _rand('msg'),
                'type': 'message',
                'status': 'completed',
                'role': 'assistant',
                'content': [{'type': 'output_text', 'text': text, 'annotations': []}],
            }
        ]

    if reason_text:
        output.insert(0, make_reasoning_item(reason_text))

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
    if tool_choice and isinstance(tool_choice, dict):
        tc_type = tool_choice.get('type')
        if tc_type == 'function' and tool_choice.get('name'):
            chat_body['tool_choice'] = {'type': 'function', 'function': {'name': tool_choice['name']}}
        elif tc_type == 'required':
            chat_body['tool_choice'] = 'required'
        elif tc_type == 'auto':
            chat_body['tool_choice'] = 'auto'

    if body.get('reasoning') is not None:
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
        """Return True if model looks like a NIM target.

        When the models cache is still empty (boot / offline), allow the request
        through so aliases and first-hit models are not falsely rejected.
        """
        if not model_id:
            return False
        cached = getattr(self.pool, 'models_cached', None) or []
        curated = self.curated_genai or []
        if model_id in cached or model_id in curated:
            return True
        # Cache not warm yet — allow through (upstream will 404 if invalid)
        if not cached:
            return True
        # Heuristic: org/name NIM ids
        if '/' in model_id and ':' not in model_id:
            return True
        return False

    async def translate_to_nim(
        self,
        request: Any,
        body: dict,
        model: str,
    ) -> Tuple[Optional[dict], Optional[AsyncGenerator[str, None]]]:
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
            # P2: store conversation for previous_response_id multi-turn
            rid_store = resp_obj.get('id')
            if rid_store:
                _RESPONSE_STORE[rid_store] = chat_body.get('messages', [])
                if len(_RESPONSE_STORE) > 200:
                    _RESPONSE_STORE.pop(next(iter(_RESPONSE_STORE)))
            return resp_obj, None

        # Streaming: return async generator
        async def stream_gen() -> AsyncGenerator[str, None]:
            resp_id = _rand('resp')
            msg_id = _rand('msg')
            rsn_id = _rand('rsn')
            RSN_INDEX = 0
            MSG_INDEX = 1
            seq = [0]

            def next_seq():
                seq[0] += 1
                return seq[0]

            def emit(obj):
                return f"data: {json.dumps(obj)}\n\n"

            base = base_response(resp_id, model, 'in_progress')
            yield emit({'type': 'response.created', 'sequence_number': next_seq(), 'response': base})
            yield emit({'type': 'response.in_progress', 'sequence_number': next_seq(), 'response': base})

            rsn_started = False
            acc_reason = ''
            acc_text = ''
            tool_accs = None
            has_tool = False
            usage = None

            yield emit({
                'type': 'response.output_item.added',
                'sequence_number': next_seq(),
                'output_index': MSG_INDEX,
                'item': {'id': msg_id, 'type': 'message', 'status': 'in_progress', 'role': 'assistant', 'content': []},
            })
            yield emit({
                'type': 'response.content_part.added',
                'sequence_number': next_seq(),
                'item_id': msg_id,
                'output_index': MSG_INDEX,
                'content_index': 0,
                'part': {'type': 'output_text', 'text': '', 'annotations': []},
            })

            stream = result.get('stream')
            if stream is None:
                return

            try:
                async for chunk in stream:
                    if isinstance(chunk, bytes):
                        chunk = chunk.decode('utf-8', errors='replace')
                    if isinstance(chunk, str):
                        for line in chunk.split('\n'):
                            t = line.strip()
                            if not t.startswith('data:'):
                                continue
                            payload = t[5:].strip()
                            if payload == '[DONE]':
                                continue
                            try:
                                c = json.loads(payload)
                            except (json.JSONDecodeError, ValueError):
                                continue

                            if c.get('usage'):
                                usage = convert_usage(c['usage'])

                            d = (c.get('choices') or [{}])[0].get('delta', {}) if c.get('choices') else {}
                            if not d:
                                continue

                            if isinstance(d.get('content'), str) and d['content']:
                                acc_text += d['content']
                                yield emit({
                                    'type': 'response.output_text.delta',
                                    'sequence_number': next_seq(),
                                    'response_id': resp_id,
                                    'item_id': msg_id,
                                    'output_index': MSG_INDEX,
                                    'content_index': 0,
                                    'delta': d['content'],
                                })

                            reason_delta = ''
                            if isinstance(d.get('reasoning_content'), str) and d['reasoning_content']:
                                reason_delta = d['reasoning_content']
                            elif isinstance(d.get('reasoning'), str) and d['reasoning']:
                                reason_delta = d['reasoning']

                            if reason_delta:
                                if not rsn_started:
                                    rsn_started = True
                                    yield emit({
                                        'type': 'response.output_item.added',
                                        'sequence_number': next_seq(),
                                        'output_index': RSN_INDEX,
                                        'item': {'id': rsn_id, 'type': 'reasoning', 'status': 'in_progress', 'summary': '', 'content': []},
                                    })
                                    yield emit({
                                        'type': 'response.reasoning_text.delta',
                                        'sequence_number': next_seq(),
                                        'item_id': rsn_id,
                                        'output_index': RSN_INDEX,
                                        'content_index': 0,
                                        'delta': '',
                                    })
                                acc_reason += reason_delta
                                yield emit({
                                    'type': 'response.reasoning_text.delta',
                                    'sequence_number': next_seq(),
                                    'item_id': rsn_id,
                                    'output_index': RSN_INDEX,
                                    'content_index': 0,
                                    'delta': reason_delta,
                                })

                            if isinstance(d.get('tool_calls'), list):
                                if tool_accs is None:
                                    tool_accs = []
                                for tc in d['tool_calls']:
                                    idx = tc.get('index') if isinstance(tc.get('index'), (int, float)) else len(tool_accs)
                                    acc = tool_accs[idx] if idx < len(tool_accs) else None
                                    if acc is None:
                                        acc = {'name': '', 'args': '', 'id': _rand('fc'), 'call_id': _rand('call'), 'added': False}
                                        while len(tool_accs) <= idx:
                                            tool_accs.append(None)
                                        tool_accs[idx] = acc
                                    # P3: emit function_call.delta (Codex v0.145 expects this)
                                    if not acc['added']:
                                        acc['added'] = True
                                        yield emit({
                                            'type': 'response.output_item.added',
                                            'sequence_number': next_seq(),
                                            'output_index': MSG_INDEX + 1 + idx,
                                            'item': {'id': acc['call_id'], 'type': 'function_call', 'status': 'in_progress',
                                                     'call_id': acc['call_id'], 'name': acc['name'], 'arguments': ''},
                                        })
                                    if tc.get('function', {}).get('name'):
                                        acc['name'] += tc['function']['name']
                                        yield emit({
                                            'type': 'response.function_call.delta',
                                            'sequence_number': next_seq(),
                                            'item_id': acc['call_id'],
                                            'output_index': MSG_INDEX + 1 + idx,
                                            'delta': tc['function']['name'],
                                            'name': acc['name'],
                                        })
                                    if tc.get('function', {}).get('arguments'):
                                        chunk = tc['function']['arguments']
                                        acc['args'] += chunk
                                        yield emit({
                                            'type': 'response.function_call.delta',
                                            'sequence_number': next_seq(),
                                            'item_id': acc['call_id'],
                                            'output_index': MSG_INDEX + 1 + idx,
                                            'delta': chunk,
                                        })
                                    has_tool = True
            except Exception as e:
                import logging
                logging.getLogger('responses').error(f"[responses:nim stream] {e}")

            outputs = []
            if rsn_started:
                yield emit({
                    'type': 'response.reasoning_text.done',
                    'sequence_number': next_seq(),
                    'item_id': rsn_id,
                    'output_index': RSN_INDEX,
                    'content_index': 0,
                    'text': acc_reason,
                })
                yield emit({
                    'type': 'response.output_item.done',
                    'sequence_number': next_seq(),
                    'output_index': RSN_INDEX,
                    'item': {'id': rsn_id, 'type': 'reasoning', 'status': 'completed', 'summary': '', 'text': acc_reason},
                })
                outputs.append(make_reasoning_item(acc_reason))

            if has_tool and tool_accs:
                tool_items = [
                    {
                        'id': acc['call_id'],
                        'type': 'function_call',
                        'status': 'completed',
                        'call_id': acc['call_id'],
                        'name': acc['name'],
                        'arguments': acc['args'],
                    }
                    for acc in tool_accs if acc
                ]
                # P3: emit output_item.done for each tool (matching the added events)
                for i, acc in enumerate([a for a in tool_accs if a]):
                    yield emit({
                        'type': 'response.output_item.done',
                        'sequence_number': next_seq(),
                        'output_index': MSG_INDEX + 1 + i,
                        'item': {'id': acc['call_id'], 'type': 'function_call', 'status': 'completed',
                                 'call_id': acc['call_id'], 'name': acc['name'], 'arguments': acc['args']},
                    })
                yield emit({
                    'type': 'response.output_text.done',
                    'sequence_number': next_seq(),
                    'response_id': resp_id,
                    'item_id': msg_id,
                    'output_index': MSG_INDEX,
                    'content_index': 0,
                    'text': acc_text,
                })
                yield emit({
                    'type': 'response.content_part.done',
                    'sequence_number': next_seq(),
                    'item_id': msg_id,
                    'output_index': MSG_INDEX,
                    'content_index': 0,
                    'part': {'type': 'output_text', 'text': acc_text, 'annotations': []},
                })
                yield emit({
                    'type': 'response.output_item.done',
                    'sequence_number': next_seq(),
                    'output_index': MSG_INDEX,
                    'item': {'id': msg_id, 'type': 'message', 'status': 'completed', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': acc_text, 'annotations': []}]},
                })
                for i, fc_item in enumerate(tool_items):
                    oi = i + 2
                    yield emit({'type': 'response.output_item.added', 'sequence_number': next_seq(), 'output_index': oi, 'item': fc_item})
                    yield emit({'type': 'response.output_item.done', 'sequence_number': next_seq(), 'output_index': oi, 'item': fc_item})
                    outputs.append(fc_item)
                outputs.append({'id': msg_id, 'type': 'message', 'status': 'completed', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': acc_text, 'annotations': []}]})
                yield emit({
                    'type': 'response.completed',
                    'sequence_number': next_seq(),
                    'response': base_response(resp_id, model, 'completed', outputs, usage),
                })
            else:
                yield emit({
                    'type': 'response.output_text.done',
                    'sequence_number': next_seq(),
                    'response_id': resp_id,
                    'item_id': msg_id,
                    'output_index': MSG_INDEX,
                    'content_index': 0,
                    'text': acc_text,
                })
                yield emit({
                    'type': 'response.content_part.done',
                    'sequence_number': next_seq(),
                    'item_id': msg_id,
                    'output_index': MSG_INDEX,
                    'content_index': 0,
                    'part': {'type': 'output_text', 'text': acc_text, 'annotations': []},
                })
                yield emit({
                    'type': 'response.output_item.done',
                    'sequence_number': next_seq(),
                    'output_index': MSG_INDEX,
                    'item': {'id': msg_id, 'type': 'message', 'status': 'completed', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': acc_text, 'annotations': []}]},
                })
                outputs.append({'id': msg_id, 'type': 'message', 'status': 'completed', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': acc_text, 'annotations': []}]})
                yield emit({
                    'type': 'response.completed',
                    'sequence_number': next_seq(),
                    'response': base_response(resp_id, model, 'completed', outputs, usage),
                })

            if not acc_text and not has_tool:
                yield emit({
                    'type': 'response.output_text.delta',
                    'sequence_number': next_seq(),
                    'response_id': resp_id,
                    'item_id': msg_id,
                    'output_index': MSG_INDEX,
                    'content_index': 0,
                    'delta': '[No text response; the model returned reasoning only.]',
                })

        return None, stream_gen()

    async def handle_responses_api(self, request: Any, raw_body: bytes) -> Tuple[Optional[dict], Optional[AsyncGenerator[str, None]], Optional[int]]:
        try:
            body = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError) as e:
            return {'error': {'message': f'Invalid JSON in /v1/responses: {e}', 'type': 'invalid_request_error'}}, None, None

        if not body or not body.get('model'):
            return {'error': {'message': 'Missing "model" in /v1/responses request', 'type': 'invalid_request_error'}}, None, None

        model = self.resolve_target_model(body['model'])

        # P2: if previous_response_id references a stored conversation, inject it into input
        prev = body.get('previous_response_id')
        if prev and prev in _RESPONSE_STORE:
            stored = _RESPONSE_STORE[prev]
            cur = body.get('input')
            if isinstance(cur, list):
                body['input'] = stored + cur
            elif isinstance(cur, str):
                body['input'] = stored + [{'role': 'user', 'content': cur}]

        if self.get_deprecated_redirect_info:
            dep_r = self.get_deprecated_redirect_info(body['model'])
            if dep_r:
                return {'error': {'message': f'Model "{dep_r["from"]}" has been renamed to "{dep_r["to"]}" in the NVIDIA NIM catalog. Update your request to use "{dep_r["to"]}".', 'type': 'invalid_request_error'}}, None, None

        if self.guard_stream_unsupported:
            stream_guard = self.guard_stream_unsupported(body, body['model'])
            if stream_guard:
                return {'error': {'message': stream_guard['data']['error']['message'], 'type': stream_guard['data']['error']['type']}}, None, None

        if not self.is_nvidia_model(model):
            return {
                'error': {
                    'message': f'Model "{model}" is not a NVIDIA NIM model and cannot be served by wrapper-nvidia. Use a NVIDIA NIM model (e.g. nvidia/llama-3.3-nemotron-super-49b-v1). wrapper-nvidia is NVIDIA-NIM-only.',
                    'type': 'invalid_request_error',
                }
            }, None, None

        result, stream = await self.translate_to_nim(request, body, model)

        if result is not None and result.get('error'):
            err = result['error']
            status = http_status_from_error(err)
            error_out = {k: v for k, v in err.items() if k != 'status'}
            return {'error': error_out}, None, status

        if result is not None and result.get('id'):
            return result, None, None

        return None, stream, None
