#!/usr/bin/env python3
"""
anthropic_compat.py — Anthropic Messages API <-> OpenAI Chat Completions translation.
Migrated from anthropic_compat.js — functionally identical.

Three translators:
  - anthropicToOpenai(body, official_context)  -> request  A->O
  - openaiToAnthropic(resp, model, ...)        -> response O->A  (non-streaming)
  - streamOpenaiToAnthropic(stream, ...)       -> response O->A  (SSE async generator)
"""

import os
import re
import json
import secrets
import time
import asyncio
from typing import Optional, AsyncGenerator

from .capabilities import get_context_window


def _compose_msg_id(request_id: Optional[str]) -> str:
    """Anthropic message id with exactly one ``msg_`` prefix and uniqueness.

    R9 fixes: (a) the non-stream caller used to pass a pre-prefixed
    ``msg_<epoch_seconds>`` → ids became ``msg_msg_<epoch_seconds>``;
    (b) the no-request-id fallback used a bare ms timestamp, which collides
    across concurrent turns (same class as the R7 store-key fix).
    """
    if request_id:
        rid = str(request_id)
        return rid if rid.startswith('msg_') else f'msg_{rid}'
    return f'msg_{int(time.time() * 1000)}-{secrets.token_hex(4)}'

# P0-4 fix (audit 2026-08-03): central special-token scrubbing — NVIDIA's
# byte-level-BPE models (Nemotron, DeepSeek-distill, Qwen, Kimi) leak
# tokenizer control tokens (<unk>, often fragmented across SSE chunks as
# '<un' + 'k>', <s>, </s>, <|im_start|>, U+0800 …) into content/reasoning
# streams; they arrived verbatim in Claude Code (user report
# '"><unk><unk><unk>…"'). One stateful filter per visible channel.
try:
    from common.sanitize_tokens import (
        SpecialTokenFilter as _SpecialTokenFilter,
        filter_special_tokens as _filter_special_tokens,
        DsmlMarkupFilter as _DsmlMarkupFilter,
    )
except ImportError:  # pragma: no cover - standalone fallback
    class _SpecialTokenFilter:  # type: ignore[no-redef]
        def feed(self, t):
            return t

        def flush(self):
            return ''

    _DsmlMarkupFilter = None  # type: ignore[assignment]

    def _filter_special_tokens(t):  # type: ignore[misc]
        return t


try:
    # Shared DSML tool-markup parser (strip + recover), used to re-emit
    # recovered MiniMax tool calls as Anthropic tool_use blocks.
    from common.translations import parse_dsml_from_text as _parse_dsml_markup
except ImportError:  # pragma: no cover - standalone fallback
    _parse_dsml_markup = None  # type: ignore[assignment]


# B-33.2: shared usage-counter clamp (NaN/Inf/negative -> 0) applied at the
# response surface so a malformed upstream usage payload can never crash the
# JSON renderer (Starlette renders with allow_nan=False -> ValueError -> 500
# on an otherwise successful turn; CONTRACT §3.3). §7: single-source import
# with a byte-parity fallback twin.
try:
    from common.translations.shared import finite_nonneg_int as _finite_nonneg_int
except ImportError:  # pragma: no cover - standalone fallback
    def _finite_nonneg_int(value):  # type: ignore[misc]
        # Twin of common.translations.shared.finite_nonneg_int (verbatim).
        try:
            v = int(value or 0)
        except (TypeError, ValueError, OverflowError):
            return 0
        return v if v > 0 else 0


# B-36.1: shared upstream-payload sanitizer — NaN/±Infinity literals (accepted
# by json.loads) are replaced with None at the ingest boundary so the
# response render (allow_nan=False) can never 500 on a successful turn.
try:
    from common.model.sanitize import sanitize_nonfinite_numbers as _sanitize_nonfinite
except ImportError:  # pragma: no cover - standalone fallback
    import math as _math_nonfinite

    def _sanitize_nonfinite(payload):  # type: ignore[misc]
        # B-36.1: twin of common.model.sanitize.sanitize_nonfinite_numbers (verbatim).
        if isinstance(payload, float):
            return payload if _math_nonfinite.isfinite(payload) else None
        if isinstance(payload, (dict, list)):
            # Single stack, typed nodes: dict mutates by key, list by index.
            # (The first version popped list frames through dict.items() — the
            # unit test caught it: mixed containers are the norm.)
            stack = [payload]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    for key, child in node.items():
                        if isinstance(child, float):
                            if not _math_nonfinite.isfinite(child):
                                node[key] = None
                        elif isinstance(child, (dict, list)):
                            stack.append(child)
                else:
                    for idx, child in enumerate(node):
                        if isinstance(child, float):
                            if not _math_nonfinite.isfinite(child):
                                node[idx] = None
                        elif isinstance(child, (dict, list)):
                            stack.append(child)
            return payload
        return payload



def _env_flag(name: str, default: str = '0') -> bool:
    return os.environ.get(name, default).strip().lower() in ('1', 'true', 'yes', 'on')


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
    """DEPRECATED outbound helper — DO NOT use for Claude Code path.\n\n    Kept only as documentation of MiniMax DSML shape. Outbound A→O must use\n    OpenAI tool_calls; inbound O→A may still *parse* DSML if upstream leaks it.\n    """
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
    if not isinstance(tools, list) or not tools:
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

    # V19 fix (transparency audit 2026-07-27): silent history truncation is
    # opt-in via WRAPPER_AUTO_TRUNCATE. Default OFF: forward the conversation
    # as-is and let upstream decide (its context error is visible to the client).
    if current_tokens > max_allowed_tokens and _env_flag('WRAPPER_AUTO_TRUNCATE', '0'):
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

        # Anthropic rarely uses role=tool; tool_result is usually in user content blocks.
        if role == 'tool':
            tcid = m.get('tool_call_id') or m.get('tool_use_id') or ''
            c = content
            if isinstance(c, list):
                c = _flatten_text(c)
            elif not isinstance(c, str):
                c = json.dumps(c) if c is not None else ''
            msgs.append({'role': 'tool', 'tool_call_id': tcid, 'content': c or ''})
            continue

        if isinstance(content, str):
            msgs.append({'role': role, 'content': content})
            continue

        parts = []
        tool_uses = []
        reasoning_parts = []
        raw_content = content if isinstance(content, list) else []

        for blk in raw_content:
            if not isinstance(blk, dict):
                continue
            bt = blk.get('type')
            if bt == 'text':
                parts.append({'type': 'text', 'text': blk.get('text', '')})
            elif bt == 'thinking':
                # Keep reasoning as separate field for models that accept it; do NOT
                # inject MiniMax-style "thinking/response" markup into visible text.
                reasoning_parts.append(blk.get('thinking', '') or '')
            elif bt == 'redacted_thinking':
                reasoning_parts.append('[redacted]')
            elif bt == 'image':
                src = blk.get('source', {}) or {}
                if src.get('type') == 'base64':
                    url = f'data:{src.get("media_type", "image/png")};base64,{src.get("data", "")}'
                elif src.get('type') == 'url':
                    url = src.get('url', '')
                else:
                    url = src.get('url', '')
                parts.append({'type': 'image_url', 'image_url': {'url': url}})
            elif bt == 'tool_use':
                tool_uses.append(blk)
            elif bt == 'tool_result':
                # Standard OpenAI tool result message (NOT user text / NOT DSML)
                rc = blk.get('content', '')
                if isinstance(rc, list):
                    txt = _flatten_text(rc)
                elif isinstance(rc, str):
                    txt = rc
                else:
                    txt = '' if rc is None else json.dumps(rc)
                msgs.append({
                    'role': 'tool',
                    'tool_call_id': blk.get('tool_use_id') or blk.get('id') or '',
                    'content': txt,
                })

        if role == 'user':
            # Only emit user message for non-tool_result content
            if parts:
                if all(p.get('type') == 'text' for p in parts):
                    msgs.append({'role': 'user', 'content': '\n\n'.join(p['text'] for p in parts)})
                else:
                    msgs.append({'role': 'user', 'content': parts})
        elif role == 'assistant':
            am = {'role': 'assistant'}
            if len(parts) > 1:
                am['content'] = parts
            elif parts:
                am['content'] = parts[0].get('text', '') if parts[0].get('type') == 'text' else parts
            else:
                am['content'] = None if tool_uses else ''
            if tool_uses:
                am['tool_calls'] = []
                for blk in tool_uses:
                    args = blk.get('input', {})
                    if not isinstance(args, str):
                        args = json.dumps(args or {})
                    am['tool_calls'].append({
                        'id': blk.get('id') or f'toolu_{int(time.time()*1000)}-{secrets.token_hex(3)}',
                        'type': 'function',
                        'function': {
                            'name': blk.get('name') or '',
                            'arguments': args,
                        },
                    })
                if am.get('content') is None:
                    am['content'] = ''
            if reasoning_parts:
                am['reasoning_content'] = '\n'.join(reasoning_parts)
            # Skip empty assistant shells
            if not tool_uses and not am.get('content') and not reasoning_parts:
                continue
            msgs.append(am)

    oai['messages'] = msgs
    # TRANSPARENT PROXY: only set max_tokens if the client explicitly sent one.
    # Anthropic spec requires max_tokens, so most clients send it. But if a
    # client omits it, let upstream return a clear 400 rather than us
    # silently injecting 8192 (which mutates client intent).
    if a.get('max_tokens') is not None:
        oai['max_tokens'] = a['max_tokens']

    # Forward ALL client params verbatim (transparent proxy — no silent drops).
    # Ported from opencode's 15-param list for cross-wrapper normalization.
    param_map = [
        ('temperature', 'temperature'),
        ('top_p', 'top_p'),
        ('top_k', 'top_k'),
        ('stop_sequences', 'stop'),
        ('seed', 'seed'),
        ('parallel_tool_calls', 'parallel_tool_calls'),
        ('frequency_penalty', 'frequency_penalty'),
        ('presence_penalty', 'presence_penalty'),
        ('logit_bias', 'logit_bias'),
        ('logprobs', 'logprobs'),
        ('top_logprobs', 'top_logprobs'),
        ('response_format', 'response_format'),
        ('service_tier', 'service_tier'),
        ('user', 'user'),
        ('metadata', 'metadata'),
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
            oai_tools = []
            for ttool in cleaned:
                if not isinstance(ttool, dict):
                    continue
                # Anthropic native: {name, description, input_schema}
                # OpenAI nested: {type, function:{name, description, parameters}}
                if isinstance(ttool.get('function'), dict):
                    fn = ttool['function']
                    name = fn.get('name') or ''
                    desc = fn.get('description') or ''
                    params = fn.get('parameters') or fn.get('input_schema') or {}
                else:
                    name = ttool.get('name') or ''
                    desc = ttool.get('description') or ''
                    params = ttool.get('input_schema') or ttool.get('parameters') or {}
                if not name:
                    continue
                oai_tools.append({
                    'type': 'function',
                    'function': {
                        'name': name,
                        'description': desc,
                        'parameters': params if isinstance(params, dict) else {},
                    },
                })
            if oai_tools:
                oai['tools'] = oai_tools

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
    if isinstance(o, dict) and o.get('type') == 'message' and 'content' in o:
        return o

    choice = (o.get('choices') or [{}])[0] if o.get('choices') else {}
    msg = choice.get('message', {})
    content = []

    _nr = extract_internal_reasoning(msg)
    # P0-4: scrub special tokens from visible channels.
    reasoning = _filter_special_tokens(_nr['reasoning'] or '')
    raw_content = _filter_special_tokens(_nr['content'] or '')

    if reasoning:
        # P1-2: thinking blocks require a `signature` field (strict SDK parse).
        content.append({'type': 'thinking', 'thinking': reasoning, 'signature': ''})

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
                # R5/§8 parity fix (same class as common.translations.shared):
                # an UNTERMINATED DSML segment was appended to the visible
                # text — leaking raw tool-protocol markup to Claude Code when
                # a non-stream reply was truncated mid-markup. Drop it.
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
            args = _sanitize_nonfinite(json.loads(fn.get('arguments') or '{}'))
            if not isinstance(args, dict):
                args = {'value': args}
        except Exception:
            # BUG-D2 fix: preserve raw arguments instead of silently dropping
            args = {'raw': fn.get('arguments', '')}
        content.append({'type': 'tool_use', 'id': tc.get('id', ''), 'name': fn.get('name', ''), 'input': args})

    # V25 fix (transparency audit 2026-07-27): fabricating a thinking block is
    # opt-in via WRAPPER_SYNTHETIC_THINKING. Default OFF: omit the block.
    if (expect_thinking and _env_flag('WRAPPER_SYNTHETIC_THINKING', '0')
            and not any(c.get('type') == 'thinking' for c in content) and content):
        # P1-2: thinking blocks require a `signature` field (strict SDK parse).
        content.insert(0, {'type': 'thinking', 'thinking': '[Reasoning not supported by this model; responding directly.]', 'signature': ''})

    if not any(c.get('type') in ('text', 'tool_use') for c in content):
        content.append({'type': 'text', 'text': ''})

    u = o.get('usage') or {}
    # B-33.2: clamp every upstream usage counter before it reaches the wire —
    # Python's json.loads ACCEPTS NaN/Infinity literals, and forwarding one
    # made JSONResponse raise ValueError (allow_nan=False) -> HTTP 500 on a
    # successful upstream turn (CONTRACT §3.3).
    cached = _finite_nonneg_int((u.get('prompt_tokens_details') or {}).get('cached_tokens', 0))

    out_chars = 0
    for b in content:
        if b.get('type') == 'text' and isinstance(b.get('text'), str):
            out_chars += len(b['text'])
        if b.get('type') == 'tool_use' and b.get('input'):
            out_chars += len(json.dumps(b['input']))

    usage = {
        # B-33.2: explicit-None/NaN/negative counters collapse to the local
        # estimate (never JSON-null / NaN on required SDK int fields).
        'input_tokens': _finite_nonneg_int(u.get('prompt_tokens')) or (estimated_input or 0),
        'output_tokens': _finite_nonneg_int(u.get('completion_tokens')) or (max(1, (out_chars + 3) // 4) if out_chars > 0 else 0),
        'cache_creation_input_tokens': 0,
        'cache_read_input_tokens': cached,
    }

    return {
        'id': _compose_msg_id(request_id),
        'type': 'message',
        'role': 'assistant',
        'model': model,
        'content': content,
        'stop_reason': (
            'tool_use' if any(c.get('type') == 'tool_use' for c in content)
            else _FINISH_TO_STOP.get(choice.get('finish_reason'), 'end_turn')
        ),
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
    msg_id = _compose_msg_id(request_id)
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
    # P0-1: did the upstream send a finish_reason? EOF without one (and
    # without an error frame) is a premature close → surface an error, don't
    # fabricate end_turn (CONTRACT §3.3).
    saw_finish = False
    client_gone = False  # V-15 fix: set on GeneratorExit/CancelledError
    # P0-4: stateful special-token scrubbers (one per channel) — catch tokens
    # fragmented across chunks ('<un' + 'k>').
    _tok_text = _SpecialTokenFilter()
    _tok_reason = _SpecialTokenFilter()
    # R5 (double-scrub fix): cross-chunk MiniMax DSML tool-markup suppressor
    # + collector (shared common.sanitize_tokens.DsmlMarkupFilter). The old
    # per-chunk `chunk.find('<|DSML|tool_calls>')` check missed an opener
    # split across two chunks and leaked the fragment ('<|DSML') as visible
    # assistant text. Complete markup is collected and re-emitted as real
    # tool_use blocks at stream end — parity with
    # common.translations.anthropic_stream (CONTRACT §7/§8). When the shared
    # module is unavailable the legacy per-chunk machine below still applies.
    _dsml_text = _DsmlMarkupFilter() if _DsmlMarkupFilter is not None else None
    dsml_tool_n = 0

    async def stop_open():
        """Close the open TEXT/THINKING block only.

        R-02 fix: this used to also close whichever tool block was open AND
        delete it from `tool_map`. With parallel tool calls OpenAI interleaves
        argument fragments across indices, so opening tool #2 closed tool #1
        and forgot it; the next `{'index':0,...}` fragment then re-created a
        PHANTOM tool_use block with an empty name and split the arguments
        across four blocks (observed live: 4 blocks, none valid JSON).
        Tool blocks now stay open concurrently and are closed together at the
        terminal path via stop_all_tools().
        """
        nonlocal open_idx, text_index, thinking_index
        if open_idx is not None and open_idx not in set(tool_map.values()):
            yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': open_idx})
            if open_idx == text_index:
                text_index = None
            if open_idx == thinking_index:
                thinking_index = None
            open_idx = None

    async def stop_all_tools():
        """R-02: close every open tool_use block, lowest index first."""
        nonlocal open_idx
        for _ai in sorted(set(tool_map.values())):
            yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': _ai})
            if open_idx == _ai:
                open_idx = None
        tool_map.clear()

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
                # P1-2: thinking blocks require a `signature` (strict SDK parse).
                'content_block': {'type': 'thinking', 'thinking': '', 'signature': ''},
            })

    async def emit_synthetic_thinking():
        nonlocal synthetic_thinking_emitted, thinking_index, open_idx, completed_thinking, next_index
        if synthetic_thinking_emitted or real_thinking_emitted:
            return
        # V25 fix: synthetic thinking is opt-in; default is to omit the block.
        if not _env_flag('WRAPPER_SYNTHETIC_THINKING', '0'):
            return
        synthetic_thinking_emitted = True
        async for chunk in stop_open():
            yield chunk
        thinking_index = next_index
        open_idx = thinking_index
        next_index += 1
        yield _sse('content_block_start', {
            'type': 'content_block_start', 'index': thinking_index,
            # P1-2: thinking blocks require a `signature` (strict SDK parse).
            'content_block': {'type': 'thinking', 'thinking': '', 'signature': ''},
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
        # B-18 (hygiene): only names actually rebound in THIS scope are
        # declared. The removed names (sent_content_block_start,
        # real_thinking_emitted, synthetic_thinking_emitted) are mutated by the
        # sibling closures emit_text/emit_thinking_start/emit_synthetic_thinking,
        # which declare them correctly — so behaviour is unchanged; these were
        # redundant declarations, not a state-desync bug.
        nonlocal dsml_buffer, in_dsml_mode, current_tool_index, current_tool_name
        nonlocal current_tool_id, current_tool_input, next_index, open_idx
        nonlocal sent_text_or_tool_block
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
                    current_tool_id = f'toolu_dsml_{int(time.time() * 1000)}_{hash(tool_name) % 10000:04x}_{ai}_{secrets.token_hex(3)}'
                    current_tool_input = {}

                    if expect_thinking and not real_thinking_emitted and not synthetic_thinking_emitted:
                        # R-04 (same class): use distinct loop vars so the
                        # `chunk` PARAMETER is never overwritten with an SSE
                        # frame string.
                        async for _ev in emit_synthetic_thinking():
                            yield _ev
                    sent_text_or_tool_block = True
                    async for _ev in stop_open():
                        yield _ev
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
                    # R-04 (same class): distinct loop variable.
                    async for _ev in parse_and_emit(after, False):
                        yield _ev
                continue

            start_tool_calls_match = re.search(r'\|DSML\|tool_calls>', normalized)
            if start_tool_calls_match:
                in_dsml_mode = True
                dsml_buffer = dsml_buffer[start_tool_calls_match.end():]
                continue

            break

    async def parse_and_emit(chunk, is_reasoning):
        # B-18 (hygiene): this scope only rebinds these three; the rest are
        # mutated by sibling closures that declare them properly.
        nonlocal in_dsml_mode, completed_thinking, in_think_tag
        if in_dsml_mode:
            async for c in process_dsml(chunk):
                yield c
            return

        if completed_thinking:
            is_reasoning = False

        if not is_reasoning and in_think_tag and thinking_index is not None:
            # RUNTIME FINDING R-04 (CRITICAL): the loop variable was `chunk`,
            # SHADOWING this function's `chunk` parameter — the model's actual
            # text. After the thinking->text transition, `chunk` held the last
            # SSE frame emitted by stop_open(), so the literal string
            #   'event: content_block_stop\ndata: {...}\n\n'
            # was passed on and rendered to the user as assistant prose.
            # This is the same visible defect as the nous B-10 leak, reached by
            # a completely different route, and it only triggers when a
            # reasoning model transitions from thinking to text — which is why
            # no unit test caught it. Use a distinct loop variable.
            async for _stop_ev in stop_open():
                yield _stop_ev
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
                # R-03: an upstream {"error": ...} frame has no "choices" and
                # was silently dropped here, so the stream closed with a
                # fabricated end_turn and the client could not detect or retry.
                if isinstance(chunk, dict) and chunk.get('error') is not None and 'choices' not in chunk:
                    _e = chunk['error']
                    errored = True
                    error_message = (_e.get('message') if isinstance(_e, dict) else str(_e)) or 'upstream error'
                    break
                # R-08: guard against an empty choices array.
                if not chunk.get('choices'):
                    continue
                _cl = chunk.get('choices') or []
                if not _cl:
                    continue
                ch = _cl[0] or {}
                delta = ch.get('delta', {})

                content_text = delta.get('content', '') or ''
                reasoning = delta.get('reasoning_content') or delta.get('reasoning')

                # P0-4: scrub special tokens (incl. cross-chunk fragments).
                if reasoning:
                    reasoning = _tok_reason.feed(reasoning)
                    if reasoning:
                        generated_chars += len(reasoning)
                        async for c in parse_and_emit(reasoning, True):
                            yield c
                if content_text:
                    # R5: suppress DSML markup first (cross-chunk safe),
                    # then P0-4 tokenizer-special scrub (shared order).
                    if _dsml_text is not None:
                        content_text = _dsml_text.feed(content_text)
                    content_text = _tok_text.feed(content_text)
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
                        tool_call_id = tc.get('id') or f'toolu_{int(time.time() * 1000)}_{hash(str(ai)) % 10000:04x}_{ai}_{secrets.token_hex(3)}'
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
                    saw_finish = True  # P0-1
                    final_stop = _FINISH_TO_STOP.get(ch['finish_reason']) or 'end_turn'

        if buffer:
            trimmed = buffer.strip()
            if trimmed.startswith('data:'):
                data = trimmed[5:].strip()
                if data and data != '[DONE]':
                    try:
                        chunk = json.loads(data)
                    except Exception:
                        chunk = None
                    if isinstance(chunk, dict):
                        # B-33.1 (R-08 follow-up): the V-16/R-08 tail recovery
                        # only handled text/reasoning/finish. The final frame
                        # of a turn can equally carry usage, an upstream error,
                        # or tool_call argument fragments — all were dropped,
                        # reporting estimates as real usage, masking the real
                        # upstream error behind a generic premature-EOF, or
                        # truncating the accumulated tool_use partial_json.
                        if chunk.get('usage') and isinstance(chunk['usage'], dict) and chunk['usage']:
                            usage = chunk['usage']
                            capture['usage'] = chunk['usage']
                        if chunk.get('error') is not None and 'choices' not in chunk:
                            _e = chunk['error']
                            errored = True
                            error_message = (_e.get('message') if isinstance(_e, dict) else str(_e)) or 'upstream error'
                        if chunk.get('choices'):
                            # R-08: empty choices array is legal.
                            ch = (chunk.get('choices') or [{}])[0] or {}
                            delta = ch.get('delta', {})
                            content_text = delta.get('content', '') or ''
                            reasoning = delta.get('reasoning_content') or delta.get('reasoning')
                            # P0-4: scrub special tokens (cross-chunk fragments).
                            if reasoning:
                                reasoning = _tok_reason.feed(reasoning)
                                if reasoning:
                                    generated_chars += len(reasoning)
                                    async for c in parse_and_emit(reasoning, True):
                                        yield c
                            if content_text:
                                # R5: DSML suppress first, then token scrub.
                                if _dsml_text is not None:
                                    content_text = _dsml_text.feed(content_text)
                                content_text = _tok_text.feed(content_text)
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
                                    tool_call_id = tc.get('id') or f'toolu_{int(time.time() * 1000)}_{hash(str(ai)) % 10000:04x}_{ai}_{secrets.token_hex(3)}'
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
                                saw_finish = True  # P0-1
                                final_stop = _FINISH_TO_STOP.get(ch['finish_reason']) or 'end_turn'
            buffer = ''
    except (GeneratorExit, asyncio.CancelledError):
        # V-15 fix (audit 2026-07-27): client disconnected / task cancelled.
        # Async generator finalization forbids further yields — mark it so the
        # finally block does cleanup only, then re-raise.
        client_gone = True
        raise
    except Exception as e:
        errored = True
        error_message = str(e) if e else 'upstream connection error'
    finally:
        # Best-effort release for aiohttp responses
        try:
            if hasattr(stream, 'release'):
                maybe = stream.release()
                if hasattr(maybe, '__await__'):
                    await maybe
        except Exception:
            pass
        if not client_gone:
            # P0-4: release any text withheld by the special-token filters
            # into its own channel BEFORE the blocks close.
            _rest_r = _tok_reason.flush()
            if _rest_r:
                try:
                    if thinking_index is None:
                        async for _c in emit_thinking_start():
                            yield _c
                    yield _sse('content_block_delta', {
                        'type': 'content_block_delta', 'index': thinking_index,
                        'delta': {'type': 'thinking_delta', 'thinking': _rest_r},
                    })
                except Exception:
                    pass
            # R5: flush the DSML filter's clean remainder through the token
            # filter first (shared order), then release the token holdback.
            _rest_dsml = _dsml_text.flush() if _dsml_text is not None else ''
            _rest_t = (_tok_text.feed(_rest_dsml) if _rest_dsml else '') + _tok_text.flush()
            if _rest_t:
                try:
                    async for _c in emit_text(_rest_t):
                        yield _c
                except Exception:
                    pass
            if open_idx is not None and open_idx not in set(tool_map.values()):
                try:
                    yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': open_idx})
                except Exception:
                    pass
                open_idx = None
            # R-02: close every concurrently-open tool_use block.
            try:
                async for _c in stop_all_tools():
                    yield _c
            except Exception:
                pass

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

            # R5: re-emit complete MiniMax DSML tool markup collected by the
            # stream filter (suppressed from the visible text channel) as
            # real Anthropic tool_use blocks BEFORE the terminal frames —
            # stream/non-stream parity (openai_to_anthropic above) and
            # cross-wrapper parity (common.translations.anthropic_stream,
            # CONTRACT §7/§8). Blocks are opened and closed cleanly here so
            # SDK block bookkeeping never dangles.
            if _dsml_text is not None and _parse_dsml_markup is not None:
                try:
                    _markup = getattr(_dsml_text, 'collected_text', '') or ''
                    _clean_dsml, _dsml_tools = _parse_dsml_markup(_markup) if _markup else ('', [])
                except Exception:
                    _dsml_tools = []
                for _tu in _dsml_tools:
                    if not isinstance(_tu, dict):
                        continue
                    try:
                        _args_json = json.dumps(_tu.get('input') or {}, ensure_ascii=False)
                    except Exception:
                        _args_json = '{}'
                    ai = next_index
                    next_index += 1
                    dsml_tool_n += 1
                    sent_text_or_tool_block = True
                    try:
                        yield _sse('content_block_start', {
                            'type': 'content_block_start', 'index': ai,
                            'content_block': {'type': 'tool_use',
                                              'id': _tu.get('id') or f'toolu_dsml_{ai}-{secrets.token_hex(3)}',
                                              'name': _tu.get('name') or '', 'input': {}},
                        })
                        yield _sse('content_block_delta', {
                            'type': 'content_block_delta', 'index': ai,
                            'delta': {'type': 'input_json_delta', 'partial_json': _args_json},
                        })
                        yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': ai})
                    except Exception:
                        pass

        _finalize_capture(capture, usage, input_tokens, generated_chars, last_usage_chunk, final_stop)

    # P0-1 (CONTRACT §3.3): the upstream stream ended WITHOUT any terminal
    # signal — no finish_reason, no error frame. The old code closed with a
    # fabricated end_turn, so a truncated answer persisted as a successful
    # turn (the "stops mid-way" symptom). Route through the errored path so
    # a real Anthropic `error` event precedes the terminal frames.
    if not errored and not saw_finish:
        errored = True
        error_message = ('upstream stream ended prematurely: EOF without '
                         'finish_reason or [DONE]')
        logger_msg = '[anthropic_compat] upstream stream ended prematurely (no finish_reason)'
        try:
            import logging as _logging
            _logging.getLogger('wrapper-nvidia').error(logger_msg)
        except Exception:
            pass

    if open_idx is not None and open_idx not in set(tool_map.values()):
        yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': open_idx})
        open_idx = None
    # R-02: ensure no tool_use block is left unclosed.
    async for _c in stop_all_tools():
        yield _c

    if errored:
        capture['errored'] = True
        capture['errorMessage'] = error_message or 'upstream connection error'
        # Still close the Anthropic SSE cleanly so Claude Code / SDKs do not hang mid-turn
        # B-13 fix: do NOT fabricate model text from a transport error. Emitting
        # "[upstream stream error: ...]" as a text_delta made the client persist
        # an infrastructure failure as the assistant's answer, with no way to
        # detect it or retry. Emit a real Anthropic `error` event instead.
        yield _sse('error', {
            'type': 'error',
            'error': {'type': 'api_error',
                      'message': f'upstream stream error: {str(error_message)[:2000]}'},
        })
        if not sent_text_or_tool_block:
            # Still open+close an empty text block so SDKs that require at least
            # one content block do not choke on the envelope.
            empty_idx = next_index
            yield _sse('content_block_start', {
                'type': 'content_block_start', 'index': empty_idx,
                'content_block': {'type': 'text', 'text': ''},
            })
            yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': empty_idx})
        # stop_reason=None (audit 2026-08-03): the turn FAILED — claiming
        # end_turn fabricates a clean completion. The `error` event above is
        # the real signal; message_stop still follows so no client hangs.
        yield _sse('message_delta', {
            'type': 'message_delta',
            'delta': {'stop_reason': None, 'stop_sequence': None},
            'usage': {'input_tokens': input_tokens or 0, 'output_tokens': 0,
                      'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0},
        })
        yield _sse('message_stop', {'type': 'message_stop'})
        return

    if not sent_text_or_tool_block and not errored:
        empty_idx = next_index
        yield _sse('content_block_start', {
            'type': 'content_block_start', 'index': empty_idx,
            'content_block': {'type': 'text', 'text': ''},
        })
        yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': empty_idx})

    # Prefer tool_use stop when tools were emitted — including R5 DSML-recovered
    # tool_use blocks (MiniMax reports finish_reason 'stop' for those turns;
    # claiming end_turn would leave Claude Code waiting on a tool_result).
    if (tool_map or dsml_tool_n) and final_stop == 'end_turn':
        final_stop = 'tool_use'

    estimated_output = max(1, (generated_chars + 3) // 4)
    # B-33.2: clamp — NaN/Inf/None upstream counters must never reach the wire.
    reported_input = _finite_nonneg_int(usage.get('prompt_tokens')) or (input_tokens or 0)
    reported_output = _finite_nonneg_int(usage.get('completion_tokens')) or estimated_output
    capture['reportedInputTokens'] = reported_input
    capture['reportedOutputTokens'] = reported_output
    yield _sse('message_delta', {
        'type': 'message_delta',
        'delta': {'stop_reason': final_stop, 'stop_sequence': None},
        'usage': {
            'input_tokens': reported_input,
            'output_tokens': reported_output,
            'cache_creation_input_tokens': 0,
            'cache_read_input_tokens': _finite_nonneg_int((usage.get('prompt_tokens_details') or {}).get('cached_tokens', 0)),
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
