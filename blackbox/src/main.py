#!/usr/bin/env python3
"""wrapper-blackbox — BLACKBOX AI OpenAI + Anthropic compatible proxy.

BLACKBOX's public API is OpenAI-compatible at https://api.blackbox.ai.  This
wrapper exposes the same monorepo contract as nvidia-python, nous, and opencode:
Chat Completions, Responses API, Anthropic Messages, multi-key retry/cooldown,
structured tools, dynamic aliases, and strict stream finalization.
"""

from __future__ import annotations

import os
import json
import time
import threading
import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager

# Shared persistent catalog/state layer; bootstrap repo root for systemd launches.
try:
    from common.model_state import ModelStateStore, classify_upstream_error
    from common.model import LocalModelRegistry, ModelRegistryClient
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from common.model_state import ModelStateStore, classify_upstream_error
    from common.model import LocalModelRegistry, ModelRegistryClient

import aiohttp
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False

from .key_pool import KeyPool
from .metrics import Metrics

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')
load_dotenv()

LOG_FILE = os.environ.get('LOG_FILE', '/root/wrapper/blackbox/blackbox.log')
try:
    os.makedirs(os.path.dirname(LOG_FILE) or '.', exist_ok=True)
    _log_file_handler = logging.FileHandler(LOG_FILE)
except Exception:
    LOG_FILE = '/tmp/wrapper-blackbox.log'
    _log_file_handler = logging.FileHandler(LOG_FILE)
logger = logging.getLogger('wrapper-blackbox')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [blackbox] %(message)s', handlers=[_log_file_handler, logging.StreamHandler()])

LISTEN_PORT = int(os.environ.get('LISTEN_PORT', '9104'))
BIND_HOST = os.environ.get('LISTEN_HOST', '0.0.0.0')
BLACKBOX_BASE = os.environ.get('BLACKBOX_BASE_URL', 'https://api.blackbox.ai').rstrip('/')
MODEL_STATE_DB = os.environ.get('MODEL_STATE_DB', str(Path(__file__).resolve().parents[1] / 'model-state.db'))
MODEL_CATALOG_TTL_SEC = int(os.environ.get('MODEL_CATALOG_TTL_SEC', '21600'))
MODEL_CATALOG_REFRESH_SEC = int(os.environ.get('MODEL_CATALOG_REFRESH_SEC', '86400'))
MODEL_STORE = ModelStateStore('blackbox', MODEL_STATE_DB, MODEL_CATALOG_TTL_SEC)
MODEL_REGISTRY = LocalModelRegistry('blackbox')
MODEL_REGISTRY_CLIENT = ModelRegistryClient()
_MODEL_REFRESH_TASK = None
BEARER_TOKEN = os.environ.get('BEARER_TOKEN', '').strip()
HEARTBEAT_MS = int(os.environ.get('HEARTBEAT_INTERVAL_MS', '5000'))
MAX_CONNECTIONS = int(os.environ.get('MAX_CONNECTIONS', '200'))
MAX_CONNECTIONS_PER_HOST = int(os.environ.get('MAX_CONNECTIONS_PER_HOST', '100'))
CONNECT_TIMEOUT_SEC = int(os.environ.get('CONNECT_TIMEOUT_SEC', '30'))
REQUEST_TIMEOUT_SEC = int(os.environ.get('REQUEST_TIMEOUT_SEC', '600'))
STREAM_REQUEST_TIMEOUT_SEC = int(os.environ.get('STREAM_REQUEST_TIMEOUT_SEC', '900'))
VERSION = '1.0.0-contract'

# Conservative curated fallback. Upstream /models is authoritative when online.
CURATED_FREE_MODELS = [
    {'id': 'blackboxai/nvidia/nemotron-3-super-120b-a12b:free', 'object': 'model', 'owned_by': 'blackbox', 'supports_tools': True},
    {'id': 'blackboxai/x-ai/grok-code-fast-1:free', 'object': 'model', 'owned_by': 'blackbox', 'supports_tools': True},
    {'id': 'blackboxai/nvidia/nemotron-nano-12b-v2-vl', 'object': 'model', 'owned_by': 'blackbox', 'supports_tools': True},
]

_ALIAS_NAME_SET = {
    'sonnet', 'opus', 'haiku',
    'claude-sonnet', 'claude-opus', 'claude-haiku',
    'claude-sonnet-4', 'claude-opus-4', 'claude-haiku-4',
    'claude-sonnet-4-20250514', 'claude-opus-4-20250514', 'claude-haiku-4-20250514',
    'claude-sonnet-4-5', 'claude-opus-4-5', 'claude-haiku-4-5',
    'claude-sonnet-4-6', 'claude-opus-4-6', 'claude-3-5-sonnet-20241022', 'claude-3-5-haiku-20241022',
}
_dynamic_alias_target = ''
_dynamic_alias_lock = threading.Lock()
_known_models = {m['id'] for m in CURATED_FREE_MODELS}

pool = KeyPool()
metrics = Metrics()
_session = None


def free_only_enabled() -> bool:
    return (os.environ.get('FREE_ONLY') or 'yes').strip().lower() in ('yes', 'true', '1', 'on', 'y')


def _allowlist() -> set[str]:
    raw = os.environ.get('FREE_MODEL_ALLOWLIST') or 'blackboxai/nvidia/nemotron-nano-12b-v2-vl'
    return {x.strip().lower() for x in raw.split(',') if x.strip()}


def is_free_model(model_id: str) -> bool:
    if not model_id:
        return False
    mid = str(model_id).strip().lower()
    bare = mid.split('/')[-1]
    if 'free' in mid or mid in _allowlist() or bare in _allowlist():
        return True
    return False


def free_only_error(model_id: str) -> dict:
    return {'error': {'type': 'invalid_request_error', 'message': f'Model "{model_id}" is blocked by FREE_ONLY=yes. Send a free model id, add it to FREE_MODEL_ALLOWLIST, or set FREE_ONLY=no.', 'code': 'free_only_restricted', 'param': 'model'}}


def free_only_anthropic_error(model_id: str) -> dict:
    return {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': free_only_error(model_id)['error']['message']}}


def is_alias_name(model_id: str) -> bool:
    return bool(model_id) and str(model_id).lower().strip() in _ALIAS_NAME_SET


def get_dynamic_alias_target() -> str:
    with _dynamic_alias_lock:
        return _dynamic_alias_target or ''


def set_dynamic_alias_target(model_id: str, force: bool = False) -> None:
    global _dynamic_alias_target
    if not model_id or is_alias_name(model_id):
        return
    mid = str(model_id).strip()
    if not mid:
        return
    if free_only_enabled() and not is_free_model(mid) and not force:
        return
    if not force and _known_models and mid not in _known_models and free_only_enabled() and not is_free_model(mid):
        logger.debug(f'[alias] ignoring unknown non-free model {mid!r}')
        return
    with _dynamic_alias_lock:
        if _dynamic_alias_target != mid:
            logger.info(f'[alias] dynamic target bound -> {mid}')
        _dynamic_alias_target = mid


def _normalize_model(model: str) -> str:
    if model is None:
        return ''
    m = str(model).strip()
    if not m:
        return ''
    if is_alias_name(m):
        return get_dynamic_alias_target() or m
    # Concrete requests never mutate process-wide alias state.
    return m


def model_allowed(model_id: str) -> bool:
    if not free_only_enabled():
        return True
    if is_alias_name(model_id):
        tgt = get_dynamic_alias_target()
        return bool(tgt and is_free_model(tgt))
    return is_free_model(model_id)


async def get_session():
    global _session
    need_new = _session is None or _session.closed
    if not need_new:
        try:
            loop = asyncio.get_running_loop()
            sess_loop = getattr(_session, '_loop', None)
            if sess_loop is not None and (sess_loop.is_closed() or sess_loop is not loop):
                need_new = True
        except Exception:
            need_new = True
    if need_new:
        if _session is not None and not _session.closed:
            try:
                await _session.close()
            except Exception:
                pass
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=max(REQUEST_TIMEOUT_SEC, STREAM_REQUEST_TIMEOUT_SEC), sock_connect=CONNECT_TIMEOUT_SEC), connector=aiohttp.TCPConnector(limit=MAX_CONNECTIONS, limit_per_host=MAX_CONNECTIONS_PER_HOST, ttl_dns_cache=300, enable_cleanup_closed=True))
    return _session


def _auth_headers(api_key: str, request: Request = None) -> dict:
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json', 'Accept-Encoding': 'identity'}
    if request is not None:
        for k in ('anthropic-beta', 'anthropic-version', 'openai-beta', 'x-request-id', 'user-agent'):
            v = request.headers.get(k)
            if v:
                headers[k] = v
    return headers


def _normalize_upstream_error(status: int, text_or_data) -> dict:
    msg = text_or_data
    etype = 'api_error'
    if isinstance(text_or_data, dict):
        if isinstance(text_or_data.get('error'), dict):
            err = text_or_data['error']
            msg = err.get('message') or err.get('msg') or str(err)
            etype = err.get('type') or etype
        elif text_or_data.get('message'):
            msg = text_or_data.get('message')
            etype = text_or_data.get('type') or etype
        else:
            msg = json.dumps(text_or_data)[:2000]
    else:
        msg = str(text_or_data or '')
        try:
            return _normalize_upstream_error(status, json.loads(msg))
        except Exception:
            pass
    if status == 429:
        etype = 'rate_limit_error'
    elif status in (401, 402, 403):
        etype = 'authentication_error'
    elif status == 404:
        etype = 'not_found_error'
    elif status >= 500:
        etype = 'server_error'
    return {'error': {'message': str(msg)[:2000], 'type': etype, 'code': status}}


async def proxy_request(method: str, url: str, json_body: dict = None, headers: dict = None, is_stream: bool = False):
    import aiohttp as _aiohttp
    sess = await get_session()
    headers = headers or {}
    try:
        if is_stream:
            resp = await sess.request(method, url, json=json_body, headers=headers, timeout=_aiohttp.ClientTimeout(total=STREAM_REQUEST_TIMEOUT_SEC, sock_connect=CONNECT_TIMEOUT_SEC))
            if resp.status >= 400:
                text = await resp.text()
                resp.release()
                try:
                    data = json.loads(text)
                except Exception:
                    data = text
                return resp.status, _normalize_upstream_error(resp.status, data)
            return 200, resp
        async with sess.request(method, url, json=json_body, headers=headers, timeout=_aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SEC, sock_connect=CONNECT_TIMEOUT_SEC)) as resp:
            text = await resp.text()
            try:
                data = json.loads(text) if text else {}
            except Exception:
                data = text
            if resp.status >= 400:
                return resp.status, _normalize_upstream_error(resp.status, data)
            if not isinstance(data, dict):
                data = {'error': {'message': str(data)[:2000], 'type': 'api_error'}}
            return resp.status, data
    except Exception as e:
        return 502, {'error': {'message': str(e), 'type': 'api_error'}}


def _retry_after_seconds(data, default=65) -> int:
    if isinstance(data, dict):
        err = data.get('error') if isinstance(data.get('error'), dict) else data
        for k in ('retry_after', 'retry_after_seconds', 'retry-after'):
            v = err.get(k) if isinstance(err, dict) else None
            if v is not None:
                try:
                    return max(1, int(float(v)))
                except (TypeError, ValueError):
                    pass
    return default


def _is_retriable_upstream_status(status: int) -> bool:
    return status in (401, 402, 403, 408, 409, 429) or status >= 500


def _looks_model_capacity_error(data) -> bool:
    blob = json.dumps(data, ensure_ascii=False).lower() if isinstance(data, dict) else str(data).lower()
    return any(x in blob for x in ('no deployments available', 'selected model', 'cooldown_list', 'invalid model name', 'model unavailable'))


def _should_cooldown_key(status: int, data) -> bool:
    if status == 429 and _looks_model_capacity_error(data):
        return False
    if status == 404 and _looks_model_capacity_error(data):
        return False
    return status in (401, 402, 403, 408, 409, 429) or status >= 500


async def proxy_request_with_pool(method: str, url: str, json_body: dict, request: Request, is_stream: bool = False):
    attempts = max(1, pool.total_keys)
    last_status = 503
    last_data = {'error': {'message': 'No capacity', 'type': 'server_error'}}
    tried = 0
    for _ in range(attempts):
        key_result = await pool.acquire(json_body.get('model', '') if isinstance(json_body, dict) else '')
        if not key_result:
            break
        key = key_result['key']
        headers = _auth_headers(key.api_key, request)
        status, data = await proxy_request(method, url, json_body, headers, is_stream=is_stream)
        model_id = json_body.get('model', '') if isinstance(json_body, dict) else ''
        if model_id:
            surface = 'anthropic_messages' if '/messages' in url else ('openai_responses' if '/responses' in url else 'openai_chat')
            try:
                call_plan = MODEL_REGISTRY.call_plan(model_id, surface)
                if call_plan.model.provider_model_id != model_id:
                    pool.release(key)
                    return 500, {'error': {'type': 'server_error', 'message': 'Model identity changed during call-plan resolution', 'code': 'MODEL_ID_MUTATION'}}, None
            except ValueError as exc:
                pool.release(key)
                return 400, {'error': {'type': 'invalid_request_error', 'message': str(exc), 'code': 'MODEL_CALL_PLAN_INVALID'}}, None
        if model_id:
            try:
                if status == 200:
                    from common.model_state import credential_fingerprint
                    MODEL_STORE.record_status(model_id, credential_fingerprint(key.api_key), 'available', status, 'OK', endpoint=url)
                else:
                    MODEL_STORE.record_error(model_id, key.api_key, status, data, endpoint=url)
            except Exception as e:
                logger.warning(f'[model-state] Blackbox result record failed: {e}')
        if status == 200:
            if is_stream:
                return status, data, key
            pool.release(key)
            return status, data, None
        tried += 1
        last_status, last_data = status, data
        classification = classify_upstream_error(status, data)
        if _is_retriable_upstream_status(status) and classification['retry_same_model']:
            if _should_cooldown_key(status, data):
                pool.mark_failure(key, status, _retry_after_seconds(data), 'upstream')
            pool.release(key)
            continue
        pool.release(key)
        return status, data, None
    if tried >= max(1, pool.total_keys) and isinstance(last_data, dict) and last_data.get('error'):
        last_data = {'error': {**last_data['error'], 'message': f"All configured Blackbox keys failed or are rate-limited. Last error: {last_data['error'].get('message', '')}"[:2000]}}
    return last_status, last_data, None


def _ensure_chat_message(data: dict) -> dict:
    if not isinstance(data, dict):
        return data
    try:
        choices = data.get('choices') or []
        if choices:
            msg = choices[0].get('message') or {}
            if msg.get('content') is None:
                msg['content'] = ''
            choices[0]['message'] = msg
            data['choices'] = choices
        data.setdefault('usage', {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0})
    except Exception:
        pass
    return data


def _strip_cache(obj):
    if isinstance(obj, dict):
        obj.pop('cache_control', None)
        for v in list(obj.values()):
            _strip_cache(v)
    elif isinstance(obj, list):
        for x in obj:
            _strip_cache(x)


def anthropic_to_openai(body: dict) -> dict:
    _strip_cache(body)
    model = _normalize_model(body.get('model') or '')
    msgs = []
    sys = body.get('system')
    if isinstance(sys, str) and sys:
        msgs.append({'role': 'system', 'content': sys})
    elif isinstance(sys, list):
        texts = [s.get('text', str(s)) if isinstance(s, dict) else str(s) for s in sys]
        if texts:
            msgs.append({'role': 'system', 'content': '\n'.join(texts)})
    for m in body.get('messages') or []:
        role, c = m.get('role'), m.get('content')
        if isinstance(c, str):
            msgs.append({'role': role, 'content': c})
            continue
        if not isinstance(c, list):
            msgs.append({'role': role, 'content': c if c is not None else ''})
            continue
        parts, tools, reasoning = [], [], []
        for b in c:
            if not isinstance(b, dict):
                continue
            t = b.get('type')
            if t == 'text':
                parts.append({'type': 'text', 'text': b.get('text', '')})
            elif t == 'image':
                src = b.get('source') or {}
                if src.get('type') == 'base64':
                    url = f"data:{src.get('media_type','image/png')};base64,{src.get('data','')}"
                else:
                    url = src.get('url', '')
                if url:
                    parts.append({'type': 'image_url', 'image_url': {'url': url}})
            elif t == 'thinking':
                reasoning.append(b.get('thinking') or '')
            elif t == 'tool_use':
                tools.append({'id': b.get('id'), 'type': 'function', 'function': {'name': b.get('name') or '', 'arguments': json.dumps(b.get('input') or {}, ensure_ascii=False)}})
            elif t == 'tool_result':
                rc = b.get('content')
                txt = rc if isinstance(rc, str) else '\n'.join(x.get('text', '') for x in (rc or []) if isinstance(x, dict))
                msgs.append({'role': 'tool', 'tool_call_id': b.get('tool_use_id') or b.get('id') or '', 'content': txt})
        final = parts if len(parts) > 1 else (parts[0]['text'] if parts else ('' if tools else None))
        if role == 'user' and not parts and not tools and not reasoning:
            continue
        if role == 'assistant' and not parts and not tools and not reasoning:
            continue
        am = {'role': role, 'content': final if final is not None else ('' if tools else None)}
        if tools:
            am['tool_calls'] = tools
            if am.get('content') is None:
                am['content'] = ''
        if reasoning:
            am['reasoning_content'] = '\n'.join(reasoning)
        msgs.append(am)
    out = {'model': model, 'messages': _repair_orphan_tool_messages(msgs), 'stream': bool(body.get('stream')), 'max_tokens': max(int(body.get('max_tokens') or 4096), 1)}
    if body.get('tools'):
        tools = []
        for t in body.get('tools') or []:
            if not isinstance(t, dict) or not t.get('name'):
                continue
            tools.append({'type': 'function', 'function': {'name': t['name'], 'description': t.get('description', '') or '', 'parameters': t.get('input_schema') or {}}})
        if tools:
            out['tools'] = tools
    return out


def _parse_tool_args(s: str):
    try:
        parsed = json.loads(s or '{}')
        return parsed if isinstance(parsed, dict) else {'value': parsed}
    except Exception:
        return {'raw': s or ''}


def openai_to_anthropic(model: str, data: dict) -> dict:
    msg = (data.get('choices') or [{}])[0].get('message', {}) or {}
    text = msg.get('content') or ''
    reasoning = msg.get('reasoning_content') or msg.get('reasoning') or ''
    content = []
    if reasoning:
        content.append({'type': 'thinking', 'thinking': reasoning})
    tool_calls = list(msg.get('tool_calls') or [])
    if text or not tool_calls:
        content.append({'type': 'text', 'text': text if isinstance(text, str) else str(text)})
    for tc in tool_calls:
        fn = tc.get('function') or {}
        content.append({'type': 'tool_use', 'id': tc.get('id') or f"toolu_{int(time.time()*1000)}", 'name': fn.get('name', '') or '', 'input': _parse_tool_args(fn.get('arguments', ''))})
    if not content:
        content.append({'type': 'text', 'text': ''})
    fr = (data.get('choices') or [{}])[0].get('finish_reason')
    stop = 'tool_use' if tool_calls else {'tool_calls': 'tool_use', 'stop': 'end_turn', 'length': 'max_tokens', 'content_filter': 'refusal'}.get(fr, 'end_turn')
    u = data.get('usage') or {}
    return {'id': data.get('id') or f"msg_{int(time.time()*1000)}", 'type': 'message', 'role': 'assistant', 'model': model, 'content': content, 'stop_reason': stop, 'stop_sequence': None, 'usage': {'input_tokens': u.get('prompt_tokens', 0) or 0, 'output_tokens': u.get('completion_tokens', 0) or 0}}


_RESPONSE_STORE: dict[str, list] = {}


def _repair_orphan_tool_messages(messages):
    seen = set()
    out = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get('role') == 'assistant':
            for tc in m.get('tool_calls') or []:
                if isinstance(tc, dict) and tc.get('id'):
                    seen.add(tc['id'])
            out.append(m)
        elif m.get('role') == 'tool' and (m.get('tool_call_id') not in seen):
            tcid = m.get('tool_call_id') or ''
            out.append({'role': 'user', 'content': f"Tool result{(' for ' + tcid) if tcid else ''}: {m.get('content', '')}"})
        else:
            out.append(m)
    return out


def responses_to_chat(body: dict) -> dict:
    model = _normalize_model(body.get('model') or '')
    msgs = []
    prev = body.get('previous_response_id')
    if prev and prev in _RESPONSE_STORE:
        msgs.extend(_RESPONSE_STORE[prev])
    raw = body.get('input')
    if isinstance(raw, str):
        msgs.append({'role': 'user', 'content': raw})
    elif isinstance(raw, list):
        for it in raw:
            if isinstance(it, str):
                msgs.append({'role': 'user', 'content': it})
                continue
            if not isinstance(it, dict):
                continue
            t = it.get('type')
            if t == 'function_call_output':
                outv = it.get('output', '')
                msgs.append({'role': 'tool', 'tool_call_id': it.get('call_id') or '', 'content': outv if isinstance(outv, str) else json.dumps(outv, ensure_ascii=False)})
            elif t == 'function_call':
                args = it.get('arguments', '')
                if not isinstance(args, str):
                    args = json.dumps(args or {}, ensure_ascii=False)
                msgs.append({'role': 'assistant', 'content': None, 'tool_calls': [{'id': it.get('call_id') or it.get('id') or 'call_1', 'type': 'function', 'function': {'name': it.get('name', '') or '', 'arguments': args}}]})
            else:
                role = it.get('role', 'user')
                if role == 'developer':
                    role = 'system'
                c = it.get('content', '')
                if isinstance(c, list):
                    c = ''.join(p.get('text', '') for p in c if isinstance(p, dict) and p.get('type') in ('input_text', 'text', 'output_text'))
                msgs.append({'role': role or 'user', 'content': c})
    if body.get('instructions'):
        if msgs and msgs[0].get('role') == 'system':
            msgs[0]['content'] = body['instructions'] + '\n\n' + str(msgs[0].get('content') or '')
        else:
            msgs.insert(0, {'role': 'system', 'content': body['instructions']})
    msgs = _repair_orphan_tool_messages(msgs)
    out = {'model': model, 'messages': msgs, 'stream': bool(body.get('stream', False))}
    if body.get('max_output_tokens') is not None:
        out['max_tokens'] = int(body['max_output_tokens'])
    elif body.get('max_tokens') is not None:
        out['max_tokens'] = int(body['max_tokens'])
    for k in ('temperature', 'top_p', 'tool_choice'):
        if body.get(k) is not None:
            out[k] = float(body[k]) if k in ('temperature', 'top_p') else body[k]
    if body.get('tools'):
        tools = []
        for t in body['tools']:
            if not isinstance(t, dict):
                continue
            fn = t.get('function') if isinstance(t.get('function'), dict) else t
            name = fn.get('name') if isinstance(fn, dict) else None
            if not name:
                continue
            tools.append({'type': 'function', 'function': {'name': name, 'description': fn.get('description', '') or '', 'parameters': fn.get('parameters') or fn.get('input_schema') or {}}})
        if tools:
            out['tools'] = tools
    return out


def chat_to_responses(model: str, data: dict) -> dict:
    msg = (data.get('choices') or [{}])[0].get('message', {}) or {}
    text = msg.get('content') or ''
    output = []
    for tc in msg.get('tool_calls') or []:
        fn = tc.get('function') or {}
        output.append({'id': tc.get('id') or f'fc_{len(output)}', 'type': 'function_call', 'status': 'completed', 'call_id': tc.get('id'), 'name': fn.get('name', '') or '', 'arguments': fn.get('arguments', '') or ''})
    output.append({'id': f"msg_{int(time.time()*1000)}", 'type': 'message', 'status': 'completed', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text, 'annotations': []}]})
    u = data.get('usage') or {}
    return {'id': data.get('id') or f"resp_{int(time.time()*1000)}", 'object': 'response', 'created_at': int(time.time()), 'model': model, 'status': 'completed', 'output': output, 'usage': {'input_tokens': u.get('prompt_tokens', 0) or 0, 'output_tokens': u.get('completion_tokens', 0) or 0, 'total_tokens': u.get('total_tokens') or ((u.get('prompt_tokens', 0) or 0) + (u.get('completion_tokens', 0) or 0))}}


def _assistant_message_from_chat(data: dict, fallback_text: str = '', tool_accs=None) -> dict:
    msg = (data.get('choices') or [{}])[0].get('message', {}) if isinstance(data, dict) else {}
    content = msg.get('content')
    if content is None:
        content = fallback_text if fallback_text is not None else None
    tool_calls = msg.get('tool_calls') or []
    if tool_accs:
        tool_calls = [{'id': acc.get('call_id'), 'type': 'function', 'function': {'name': acc.get('name', ''), 'arguments': acc.get('args', '')}} for acc in tool_accs if acc]
    out = {'role': 'assistant', 'content': content if content not in ('', None) else (None if tool_calls else '')}
    if tool_calls:
        out['tool_calls'] = tool_calls
    return out


async def stream_passthrough(resp, key, heartbeat=True):
    last_hb = time.time()
    saw_done = False
    try:
        async for chunk in resp.content.iter_any():
            chunk_text = chunk.decode('utf-8', errors='replace') if isinstance(chunk, (bytes, bytearray)) else str(chunk)
            if 'data: [DONE]' in chunk_text or 'data:[DONE]' in chunk_text:
                saw_done = True
            yield chunk
            if heartbeat and (time.time() - last_hb) > (HEARTBEAT_MS / 1000.0):
                yield b': heartbeat\n\n'
                last_hb = time.time()
        if not saw_done:
            yield b'data: [DONE]\n\n'
    finally:
        try:
            resp.release()
        except Exception:
            pass
        pool.release(key)


class AnthropicStreamState:
    def __init__(self, model: str):
        self.model = model
        self.index = -1
        self.message_started = False
        self.current_block = None
        self.tool_map = {}
        self.finished = False
        self.msg_id = f"msg_{int(time.time()*1000)}"

    def _sse(self, event: str, data: dict) -> str:
        payload = dict(data or {})
        payload.setdefault('type', event)
        return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def start_events(self):
        if self.message_started:
            return []
        self.message_started = True
        return [self._sse('message_start', {'type': 'message_start', 'message': {'id': self.msg_id, 'type': 'message', 'role': 'assistant', 'model': self.model, 'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 0, 'output_tokens': 0, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0}}})]

    def _close_block(self):
        if self.current_block is None:
            return []
        ev = [self._sse('content_block_stop', {'type': 'content_block_stop', 'index': self.index})]
        self.current_block = None
        return ev

    def translate_chunk(self, chunk: dict):
        events = self.start_events()
        if not isinstance(chunk, dict) or 'choices' not in chunk:
            return events
        ch = (chunk.get('choices') or [{}])[0]
        delta = ch.get('delta') or {}
        reason = delta.get('reasoning_content') or delta.get('reasoning')
        if isinstance(reason, str) and reason:
            if self.current_block != 'thinking':
                events.extend(self._close_block())
                self.index += 1
                events.append(self._sse('content_block_start', {'type': 'content_block_start', 'index': self.index, 'content_block': {'type': 'thinking', 'thinking': ''}}))
                self.current_block = 'thinking'
            events.append(self._sse('content_block_delta', {'type': 'content_block_delta', 'index': self.index, 'delta': {'type': 'thinking_delta', 'thinking': reason}}))
        content = delta.get('content')
        if content:
            if self.current_block != 'text':
                events.extend(self._close_block())
                self.index += 1
                events.append(self._sse('content_block_start', {'type': 'content_block_start', 'index': self.index, 'content_block': {'type': 'text', 'text': ''}}))
                self.current_block = 'text'
            events.append(self._sse('content_block_delta', {'type': 'content_block_delta', 'index': self.index, 'delta': {'type': 'text_delta', 'text': content}}))
        for tc in delta.get('tool_calls') or []:
            oi = tc.get('index', 0)
            fn = tc.get('function') or {}
            if oi not in self.tool_map:
                events.extend(self._close_block())
                self.index += 1
                self.tool_map[oi] = self.index
                events.append(self._sse('content_block_start', {'type': 'content_block_start', 'index': self.index, 'content_block': {'type': 'tool_use', 'id': tc.get('id') or f'toolu_{self.index}', 'name': fn.get('name') or '', 'input': {}}}))
                self.current_block = 'tool_use'
            if fn.get('arguments'):
                events.append(self._sse('content_block_delta', {'type': 'content_block_delta', 'index': self.tool_map[oi], 'delta': {'type': 'input_json_delta', 'partial_json': fn['arguments']}}))
        fr = ch.get('finish_reason')
        if fr and not self.finished:
            stop = 'tool_use' if (fr == 'tool_calls' or self.tool_map) else {'stop': 'end_turn', 'length': 'max_tokens', 'content_filter': 'refusal'}.get(fr, 'end_turn')
            events.extend(self.force_done(stop))
        return events

    def force_done(self, stop='end_turn'):
        if self.finished:
            return []
        self.finished = True
        events = []
        if not self.message_started:
            events.extend(self.start_events())
        events.extend(self._close_block())
        if self.tool_map and stop == 'end_turn':
            stop = 'tool_use'
        events.append(self._sse('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': stop, 'stop_sequence': None}, 'usage': {'input_tokens': 0, 'output_tokens': 0, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0}}))
        events.append(self._sse('message_stop', {'type': 'message_stop'}))
        return events


def start_env_watcher():
    if not HAS_WATCHDOG:
        return
    try:
        class EnvWatcher(FileSystemEventHandler):
            def on_modified(self, event):
                if '.env' in event.src_path:
                    load_dotenv(ROOT / '.env', override=True)
                    logger.info('[env] .env hot-reloaded')
        obs = Observer()
        obs.schedule(EnvWatcher(), path=str(ROOT), recursive=False)
        obs.start()
    except Exception as e:
        logger.warning(f'[env] watcher failed: {e}')


class _CatalogRequest:
    headers = {}


async def refresh_model_catalog_once():
    """Refresh the persistent Blackbox catalog independently of user traffic."""
    try:
        status, data, _ = await proxy_request_with_pool(
            'GET', f'{BLACKBOX_BASE}/models', None, _CatalogRequest()
        )
        models_data = (data.get('data') or data.get('models') or []) if status == 200 and isinstance(data, dict) else []
        if models_data:
            MODEL_STORE.upsert_catalog(models_data, source='blackbox:/models')
            MODEL_REGISTRY.register_catalog(models_data, revision='runtime-catalog')
            await MODEL_REGISTRY_CLIENT.ingest_catalog('blackbox', models_data, 'runtime-catalog')
            logger.info(f'[model-catalog] Blackbox refreshed {len(models_data)} models')
    except Exception as e:
        logger.warning(f'[model-catalog] Blackbox refresh failed: {e}')


async def model_catalog_refresh_loop():
    while True:
        await asyncio.sleep(max(60, MODEL_CATALOG_REFRESH_SEC))
        await refresh_model_catalog_once()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _session, _MODEL_REFRESH_TASK
    pool.load_from_env()
    seed = (os.environ.get('DYNAMIC_ALIAS_TARGET') or '').strip()
    if seed:
        set_dynamic_alias_target(seed, force=True)
    start_env_watcher()
    logger.info(f'wrapper-blackbox starting on {BIND_HOST}:{LISTEN_PORT} base={BLACKBOX_BASE} free_only={free_only_enabled()} alias_target={get_dynamic_alias_target() or None}')
    _MODEL_REFRESH_TASK = asyncio.create_task(model_catalog_refresh_loop())
    yield
    if _MODEL_REFRESH_TASK:
        _MODEL_REFRESH_TASK.cancel()
        try:
            await _MODEL_REFRESH_TASK
        except asyncio.CancelledError:
            pass
        _MODEL_REFRESH_TASK = None
    if _session is not None and not _session.closed:
        await _session.close()


app = FastAPI(title='wrapper-blackbox', version=VERSION, lifespan=lifespan)


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict) and ('error' in detail or detail.get('type') == 'error'):
        return JSONResponse(status_code=exc.status_code, content=detail)
    return JSONResponse(status_code=exc.status_code, content={'error': {'type': 'api_error', 'message': str(detail)}})


app.add_middleware(CORSMiddleware, allow_origin_regex=r'https?://(127\.0\.0\.1|localhost|\[::1\])(:[0-9]+)?$', allow_methods=['*'], allow_headers=['*'], expose_headers=['*'], allow_credentials=True)


def _auth_check(request: Request):
    if request.method == 'OPTIONS':
        return
    if not BEARER_TOKEN:
        return
    auth = request.headers.get('authorization', '') or request.headers.get('x-api-key', '')
    token = auth.replace('Bearer ', '', 1).strip()
    if token != BEARER_TOKEN:
        raise HTTPException(401, {'error': {'type': 'authentication_error', 'message': 'Unauthorized'}})


@app.get('/health')
async def health():
    return {'status': 'ok' if pool.available_keys > 0 else 'degraded', 'version': VERSION, 'keys': pool.total_keys, 'available': pool.available_keys, 'free_only': free_only_enabled(), 'dynamic_alias_target': get_dynamic_alias_target() or None, 'base': BLACKBOX_BASE, 'metrics': await metrics.summary()}


@app.get('/ready')
async def ready(request: Request):
    _auth_check(request)
    try:
        status, data, _ = await proxy_request_with_pool('GET', f'{BLACKBOX_BASE}/models', None, request)
        return {'ready': status == 200, 'upstream_ok': status == 200, 'status_code': status, 'last_error': None if status == 200 else (data.get('error') if isinstance(data, dict) else str(data)), 'keys': pool.total_keys, 'available': pool.available_keys}
    except Exception as e:
        return JSONResponse(status_code=503, content={'ready': False, 'upstream_ok': False, 'last_error': str(e), 'keys': pool.total_keys, 'available': pool.available_keys})


@app.get('/version')
async def version():
    return {'version': VERSION}


@app.get('/v1/models')
async def models(request: Request):
    _auth_check(request)
    cached = MODEL_STORE.get_catalog(fresh_only=True)
    fallback = {'object': 'list', 'data': _model_list_with_aliases(CURATED_FREE_MODELS), 'free_only': free_only_enabled(), 'dynamic_alias_target': get_dynamic_alias_target() or None}
    try:
        if cached:
            upstream = cached
        else:
            status, data, _ = await proxy_request_with_pool('GET', f'{BLACKBOX_BASE}/models', None, request)
            upstream = (data.get('data') or data.get('models') or []) if status == 200 and isinstance(data, dict) else []
            if upstream:
                MODEL_STORE.upsert_catalog(upstream, source='blackbox:/models')
                MODEL_REGISTRY.register_catalog(upstream, revision='runtime-catalog')
                await MODEL_REGISTRY_CLIENT.ingest_catalog('blackbox', upstream, 'runtime-catalog')
            else:
                upstream = MODEL_STORE.get_catalog(fresh_only=False)
        normalized = []
        for m in upstream:
            entry = m if isinstance(m, dict) else {'id': str(m), 'object': 'model', 'owned_by': 'blackbox'}
            if entry.get('id'):
                _known_models.add(entry['id'])
                if not free_only_enabled() or model_allowed(entry['id']):
                    normalized.append(entry)
        if not normalized:
            normalized = fallback['data']
        return {'object': 'list', 'data': _model_list_with_aliases(normalized), 'free_only': free_only_enabled(), 'dynamic_alias_target': get_dynamic_alias_target() or None, 'catalog_cached': bool(cached)}
    except Exception as e:
        logger.warning(f'models fallback: {e}')
        return fallback


def _model_list_with_aliases(models_in: list) -> list:
    data = []
    seen = set()
    for m in models_in:
        if not isinstance(m, dict):
            continue
        mid = m.get('id')
        if not mid or mid in seen:
            continue
        if free_only_enabled() and not model_allowed(mid):
            continue
        seen.add(mid)
        state = MODEL_STORE.status_for(mid) or {}
        data.append({
            **m,
            'object': m.get('object', 'model'),
            'catalog_listed': True,
            'availability_state': state.get('state', 'unknown'),
            'availability_scope': 'account',
            'reason_code': state.get('reason_code', ''),
            'checked_at': state.get('checked_at'),
        })
    tgt = get_dynamic_alias_target()
    for alias in ('sonnet', 'opus', 'haiku'):
        if alias in seen:
            continue
        if free_only_enabled() and not (tgt and is_free_model(tgt)):
            continue
        data.append({'id': alias, 'object': 'model', 'owned_by': 'alias', 'dynamic_alias': True, 'rooted_model': tgt or None})
    return data


@app.get('/v1/capabilities')
async def capabilities(request: Request):
    model_data = (await models(request)).get('data', [])
    return {'object': 'list', 'models': [{'id': m.get('id'), 'capabilities': ['chat', 'completion'], 'streaming': True} for m in model_data if isinstance(m, dict)], 'summary': {'total': len(model_data), 'by_type': {'chat': len(model_data)}}, 'dynamic_alias_target': get_dynamic_alias_target() or None}


@app.post('/v1/messages/count_tokens')
async def count_tokens(request: Request):
    _auth_check(request)
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(status_code=400, content={'error': {'type': 'invalid_request_error', 'message': f'Invalid JSON: {e}'}})
    return {'input_tokens': max(1, len(json.dumps(body, ensure_ascii=False)) // 4)}


def _validate_chat_body(body: dict):
    if body.get('max_tokens') is not None and (not isinstance(body.get('max_tokens'), int) or body['max_tokens'] <= 0):
        return {'error': {'type': 'invalid_request_error', 'message': 'max_tokens must be a positive integer'}}
    for m in body.get('messages', []) or []:
        if isinstance(m, dict) and m.get('role') not in (None, 'system', 'user', 'assistant', 'tool', 'developer', 'function'):
            return {'error': {'type': 'invalid_request_error', 'message': f"Invalid role: {m.get('role')!r}"}}
        if isinstance(m, dict) and m.get('role') == 'tool' and not m.get('tool_call_id'):
            return {'error': {'type': 'invalid_request_error', 'message': 'tool role requires tool_call_id'}}
    return None


def _clean_tools(body: dict):
    if isinstance(body.get('tools'), list):
        cleaned = []
        for tool in body['tools']:
            if not isinstance(tool, dict):
                continue
            fn = tool.get('function') if isinstance(tool.get('function'), dict) else tool
            name = fn.get('name') if isinstance(fn, dict) else None
            if name:
                cleaned.append(tool)
        if cleaned:
            body['tools'] = cleaned
        else:
            body.pop('tools', None)


@app.post('/v1/chat/completions')
async def chat_completions(request: Request):
    _auth_check(request)
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(status_code=400, content={'error': {'type': 'invalid_request_error', 'message': f'Invalid JSON: {e}'}})
    err = _validate_chat_body(body)
    if err:
        return JSONResponse(status_code=400, content=err)
    requested = body.get('model')
    if requested is not None:
        body['model'] = _normalize_model(requested)
    if free_only_enabled() and requested and not model_allowed(requested) and not model_allowed(body.get('model') or ''):
        return JSONResponse(status_code=400, content=free_only_error(requested))
    if free_only_enabled() and body.get('model') and not model_allowed(body['model']):
        return JSONResponse(status_code=400, content=free_only_error(requested or body['model']))
    _clean_tools(body)
    is_stream = bool(body.get('stream', False))
    url = f'{BLACKBOX_BASE}/chat/completions'
    if is_stream:
        status, resp, key = await proxy_request_with_pool('POST', url, body, request, is_stream=True)
        if status != 200:
            return JSONResponse(status_code=status, content=resp if isinstance(resp, dict) else {'error': {'message': str(resp), 'type': 'api_error'}})
        return StreamingResponse(stream_passthrough(resp, key), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'})
    status, data, _ = await proxy_request_with_pool('POST', url, body, request)
    if status != 200:
        return JSONResponse(status_code=status, content=data if isinstance(data, dict) else {'error': {'message': str(data), 'type': 'api_error'}})
    await metrics.record_request(model=body.get('model'), path='/v1/chat/completions', prompt_tokens=(data.get('usage') or {}).get('prompt_tokens', 0), completion_tokens=(data.get('usage') or {}).get('completion_tokens', 0))
    return JSONResponse(_ensure_chat_message(data))


@app.post('/v1/responses')
async def responses(request: Request):
    _auth_check(request)
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(status_code=400, content={'error': {'type': 'invalid_request_error', 'message': f'Invalid JSON: {e}'}})
    requested = body.get('model')
    model = _normalize_model(requested) if requested else ''
    if requested is not None:
        body['model'] = model
    if free_only_enabled() and requested and not model_allowed(requested) and not model_allowed(model):
        return JSONResponse(status_code=400, content=free_only_error(requested))
    if free_only_enabled() and model and not model_allowed(model):
        return JSONResponse(status_code=400, content=free_only_error(requested or model))
    chat_body = responses_to_chat(body)
    chat_body['stream'] = bool(body.get('stream', False))
    url = f'{BLACKBOX_BASE}/chat/completions'
    if chat_body['stream']:
        status, resp, key = await proxy_request_with_pool('POST', url, chat_body, request, is_stream=True)
        if status != 200:
            return JSONResponse(status_code=status, content=resp if isinstance(resp, dict) else {'error': {'message': str(resp), 'type': 'api_error'}})
        rid = f"resp_{int(time.time()*1000)}"
        return StreamingResponse(_responses_stream(resp, key, rid, model, chat_body), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'})
    status, data, _ = await proxy_request_with_pool('POST', url, chat_body, request)
    if status != 200:
        return JSONResponse(status_code=status, content=data if isinstance(data, dict) else {'error': {'message': str(data), 'type': 'api_error'}})
    resp_obj = chat_to_responses(model, data)
    _store_response(resp_obj.get('id'), chat_body.get('messages', []) + [_assistant_message_from_chat(data)])
    return JSONResponse(resp_obj)


def _emit_response_event(seq_ref, etype, payload):
    seq_ref[0] += 1
    return f"event: {etype}\ndata: {json.dumps({'type': etype, 'sequence_number': seq_ref[0], **payload}, ensure_ascii=False)}\n\n"


async def _responses_stream(resp, key, rid: str, model: str, chat_body: dict):
    seq = [0]
    msg_id = 'msg-1'
    acc_text = ''
    acc_usage = None
    buffer = b''
    tool_accs = []
    next_output_index = 1

    def emit(etype, payload):
        return _emit_response_event(seq, etype, payload)

    def usage_obj():
        return acc_usage or {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}

    def get_tool_acc(tc):
        nonlocal next_output_index
        idx = tc.get('index') if isinstance(tc.get('index'), int) else len(tool_accs)
        acc = tool_accs[idx] if idx < len(tool_accs) else None
        if acc is None:
            acc = {'call_id': tc.get('id') or f"call_{idx}_{int(time.time()*1000)}", 'name': '', 'args': '', 'output_index': next_output_index, 'added': False}
            next_output_index += 1
            while len(tool_accs) <= idx:
                tool_accs.append(None)
            tool_accs[idx] = acc
        if tc.get('id'):
            acc['call_id'] = tc['id']
        return acc

    async def process_payload(payload: bytes):
        nonlocal acc_text, acc_usage
        if payload in (b'[DONE]', b'', b'"[DONE]"'):
            return
        try:
            c = json.loads(payload)
        except Exception:
            return
        if c.get('usage'):
            u = c['usage']
            acc_usage = {'input_tokens': u.get('prompt_tokens', u.get('input_tokens', 0)) or 0, 'output_tokens': u.get('completion_tokens', u.get('output_tokens', 0)) or 0, 'total_tokens': u.get('total_tokens') or ((u.get('prompt_tokens', 0) or 0) + (u.get('completion_tokens', 0) or 0))}
        d = ((c.get('choices') or [{}])[0].get('delta') or {})
        if d.get('content'):
            acc_text += d['content']
            yield emit('response.output_text.delta', {'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'delta': d['content']})
        for tc in d.get('tool_calls') or []:
            acc = get_tool_acc(tc)
            fn = tc.get('function') or {}
            if not acc['added']:
                acc['added'] = True
                yield emit('response.output_item.added', {'output_index': acc['output_index'], 'item': {'id': acc['call_id'], 'type': 'function_call', 'status': 'in_progress', 'call_id': acc['call_id'], 'name': acc['name'], 'arguments': ''}})
            if fn.get('name'):
                acc['name'] += fn['name']
                yield emit('response.function_call.delta', {'item_id': acc['call_id'], 'output_index': acc['output_index'], 'delta': fn['name'], 'name': acc['name']})
            if fn.get('arguments'):
                acc['args'] += fn['arguments']
                yield emit('response.function_call.delta', {'item_id': acc['call_id'], 'output_index': acc['output_index'], 'delta': fn['arguments']})

    try:
        yield emit('response.created', {'response': {'id': rid, 'model': model, 'status': 'in_progress'}})
        yield emit('response.in_progress', {'response': {'id': rid, 'status': 'in_progress'}})
        yield emit('response.output_item.added', {'output_index': 0, 'item': {'id': msg_id, 'type': 'message', 'status': 'in_progress', 'role': 'assistant', 'content': []}})
        yield emit('response.content_part.added', {'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': ''}})
        async for chunk in resp.content.iter_any():
            buffer += chunk
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                line = line.strip()
                if line.startswith(b'data:'):
                    async for out in process_payload(line[5:].strip()):
                        yield out
        tail = buffer.strip()
        if tail.startswith(b'data:'):
            async for out in process_payload(tail[5:].strip()):
                yield out
    except Exception as e:
        logger.error(f'[responses stream] {e}')
        if not acc_text and not any(tool_accs):
            acc_text = f'[upstream stream error: {e}]'
            yield emit('response.output_text.delta', {'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'delta': acc_text})
    finally:
        try:
            resp.release()
        except Exception:
            pass
        pool.release(key)

    msg_item = {'id': msg_id, 'type': 'message', 'status': 'completed', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': acc_text, 'annotations': []}]}
    yield emit('response.output_text.done', {'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'text': acc_text})
    yield emit('response.content_part.done', {'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': acc_text, 'annotations': []}})
    yield emit('response.output_item.done', {'output_index': 0, 'item': msg_item})
    outputs = [msg_item]
    completed_tools = [a for a in tool_accs if a]
    for acc in completed_tools:
        fc_item = {'id': acc['call_id'], 'type': 'function_call', 'status': 'completed', 'call_id': acc['call_id'], 'name': acc['name'], 'arguments': acc['args']}
        yield emit('response.output_item.done', {'output_index': acc['output_index'], 'item': fc_item})
        outputs.append(fc_item)
    yield emit('response.completed', {'response': {'id': rid, 'object': 'response', 'created_at': int(time.time()), 'model': model, 'status': 'completed', 'output': outputs, 'usage': usage_obj()}})
    yield 'data: [DONE]\n\n'
    _store_response(rid, list(chat_body.get('messages', [])) + [_assistant_message_from_chat({}, acc_text, completed_tools)])


def _store_response(rid: str, messages: list):
    if not rid:
        return
    _RESPONSE_STORE[rid] = messages
    while len(_RESPONSE_STORE) > 200:
        _RESPONSE_STORE.pop(next(iter(_RESPONSE_STORE)))


@app.post('/v1/messages')
async def anthropic_messages(request: Request):
    _auth_check(request)
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(status_code=400, content={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': f'Invalid JSON: {e}'}})
    if not isinstance(body.get('max_tokens'), int) or body['max_tokens'] <= 0:
        return JSONResponse(status_code=400, content={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'max_tokens is required and must be a positive integer'}})
    sys_field = body.get('system')
    if sys_field is not None and not isinstance(sys_field, (str, list)):
        return JSONResponse(status_code=400, content={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': '"system" must be a string or array of content blocks'}})
    for t in body.get('tools', []) or []:
        if not isinstance(t.get('input_schema'), dict):
            return JSONResponse(status_code=400, content={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'tool.input_schema must be an object'}})
    requested = body.get('model')
    model = _normalize_model(requested) if requested else ''
    if requested is not None:
        body['model'] = model
    if free_only_enabled() and requested and not model_allowed(requested) and not model_allowed(model):
        return JSONResponse(status_code=400, content=free_only_anthropic_error(requested))
    if free_only_enabled() and model and not model_allowed(model):
        return JSONResponse(status_code=400, content=free_only_anthropic_error(requested or model))
    openai_body = anthropic_to_openai(body)
    openai_body['stream'] = bool(body.get('stream', False))
    url = f'{BLACKBOX_BASE}/chat/completions'
    if openai_body['stream']:
        status, resp, key = await proxy_request_with_pool('POST', url, openai_body, request, is_stream=True)
        if status != 200:
            return JSONResponse(status_code=status, content={'type': 'error', 'error': {'type': 'api_error', 'message': str(resp)}})
        state = AnthropicStreamState(model)
        async def gen():
            try:
                for ev in state.start_events():
                    yield ev
                buf = b''
                async for chunk in resp.content.iter_any():
                    buf += chunk
                    while b'\n' in buf:
                        line, buf = buf.split(b'\n', 1)
                        line = line.strip()
                        if not line.startswith(b'data:'):
                            continue
                        payload = line[5:].strip()
                        if payload in (b'[DONE]', b'', b'"[DONE]"'):
                            for ev in state.force_done():
                                yield ev
                            return
                        try:
                            c = json.loads(payload)
                        except Exception:
                            continue
                        for ev in state.translate_chunk(c):
                            yield ev
                for ev in state.force_done():
                    yield ev
            except Exception as e:
                logger.error(f'[anthropic stream] {e}')
                for ev in state.force_done():
                    yield ev
            finally:
                try:
                    resp.release()
                except Exception:
                    pass
                pool.release(key)
        return StreamingResponse(gen(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'})
    status, data, _ = await proxy_request_with_pool('POST', url, openai_body, request)
    if status != 200:
        err = data.get('error', {}) if isinstance(data, dict) else {'message': str(data), 'type': 'api_error'}
        return JSONResponse(status_code=status, content={'type': 'error', 'error': {'type': err.get('type', 'api_error'), 'message': err.get('message', 'Unknown error')}})
    return JSONResponse(openai_to_anthropic(model, data))


@app.get('/metrics')
async def get_metrics():
    return await metrics.summary()


@app.get('/metrics/prom')
async def prom():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(pool.prom_metrics() + metrics.prom_metrics(), media_type='text/plain; version=0.0.4')


@app.get('/metrics/model-status')
async def model_status():
    return {
        'provider': 'blackbox',
        'catalog_age_sec': MODEL_STORE.catalog_age_sec(),
        'states': MODEL_STORE.status_map(),
    }


@app.api_route('/{path:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
async def catch_all(path: str, request: Request):
    return JSONResponse(status_code=404, content={'error': {'message': f'Unsupported: /{path}', 'type': 'not_found_error'}})


def main():
    import uvicorn
    uvicorn.run('src.main:app', host=BIND_HOST, port=LISTEN_PORT, log_level='info')


if __name__ == '__main__':
    main()
