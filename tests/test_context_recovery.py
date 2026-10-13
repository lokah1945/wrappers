"""Context recovery: exact evidence, lossless input, bounded retries and ownership."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from common.context_recovery import (ContextRecoveryState, context_request, context_response_headers,
                                     parse_context_error, plan_context_recovery)
from common.wrapper_scope import select_wrappers
from test_active_native_passthrough import (_Upstream, _collect, _load, _openrouter_proxy_namespace,
                                            _pool, _registry, ROOT)
from test_nvidia_messages_request_controls import _server, _request, Peer

ACTIVE = select_wrappers(include_registry=False)
ERROR = {'error': {'code': 'context_length_exceeded', 'message':
    "This model's maximum context length is 10000 tokens. However, you requested 12000 tokens "
    '(8000 in the messages, 4000 in the completion).'}}
BODY = {'model': 'exact/model', 'max_tokens': 4000,
        'messages': [{'role': 'system', 'content': 'preserve'},
                     {'role': 'assistant', 'tool_calls': [{'id': 'call_x', 'type': 'function',
                         'function': {'name': 'run', 'arguments': '{}'}}]},
                     {'role': 'tool', 'tool_call_id': 'call_x', 'content': 'keep result'},
                     {'role': 'user', 'content': 'current request'}],
        'tools': [{'type': 'function', 'function': {'name': 'run', 'parameters': {'type': 'object'}}}],
        'future_field': {'opaque': ['keep', None]}}


@pytest.mark.parametrize('field', ['max_tokens', 'max_completion_tokens', 'max_output_tokens'])
def test_reduces_only_explicit_output_to_exact_room(field):
    body = copy.deepcopy(BODY)
    body[field] = body.pop('max_tokens') if field != 'max_tokens' else body['max_tokens']
    if field == 'max_output_tokens':
        body['input'] = body.pop('messages')
        body['previous_response_id'] = 'upstream-native-history'
    before = copy.deepcopy(body)
    plan = plan_context_recovery(body, 400, ERROR)
    assert plan.action == 'reduced_output'
    assert plan.evidence.context_window == 10000
    assert plan.evidence.input_tokens == 8000
    assert plan.retry_payload == {**before, field: 2000}
    assert body == before
    assert plan_context_recovery(plan.retry_payload, 400, ERROR, attempted=True).retry_payload is None


@pytest.mark.parametrize('error,expected', [
    ({'error': {'message': 'prompt is too long: 213462 tokens > 200000 maximum'}}, (200000, 213462, None)),
    ({'error': {'message': 'input length and max_tokens exceed context limit: 8000 + 4000 > 10000'}}, (10000, 8000, None)),
    ({'error': {'message': 'max_tokens must be <= 8192'}}, (None, None, 8192)),
    ({'detail': [{'loc': ['body', 'max_tokens'], 'type': 'less_than_equal', 'ctx': {'le': 8192},
                 'input': 100000}]}, (None, None, 8192)),
    ({'error': {'code': 'context_length_exceeded', 'max_context_length': 10000, 'input_tokens': 8000}}, (10000, 8000, None)),
    ({'error': {'metadata': {'raw': json.dumps(ERROR)}}}, (10000, 8000, None)),
])
def test_definite_provider_error_shapes(error, expected):
    evidence = parse_context_error(400, error)
    assert (evidence.context_window, evidence.input_tokens, evidence.max_output_tokens) == expected


@pytest.mark.parametrize('status,error', [
    (200, ERROR), (401, ERROR), (429, ERROR), (503, ERROR), (404, ERROR),
    (400, {'error': {'message': 'invalid model'}}),
    (413, {'error': {'message': 'request body too large'}}),
    (400, {'error': {'message': 'max_tokens must be at least 4096'}}),
    (400, {'detail': [{'loc': ['body', 'tools', 0, 'max_tokens'], 'type': 'less_than_equal', 'ctx': {'le': 1}}]}),
])
def test_unrelated_failures_never_trigger_recovery(status, error):
    assert plan_context_recovery(BODY, status, error).evidence is None


@pytest.mark.parametrize('changes,error,action', [
    ({}, {'error': {'message': 'prompt is too long: 213462 tokens > 200000 maximum'}}, 'compact_input'),
    ({}, {'error': {'code': 'context_length_exceeded'}}, 'compact_input_or_reduce_output'),
    ({'thinking': {'type': 'enabled', 'budget_tokens': 2048}}, ERROR, 'adjust_reasoning_budget'),
    ({'min_tokens': 2500}, ERROR, 'adjust_reasoning_budget'),
    ({'max_tokens': None}, ERROR, 'set_explicit_output_limit'),
    ({'max_completion_tokens': 4000}, ERROR, 'set_explicit_output_limit'),
])
def test_unsafe_recovery_returns_action_without_mutation(changes, error, action):
    body = {**copy.deepcopy(BODY), **changes}
    before = copy.deepcopy(body)
    plan = plan_context_recovery(body, 400, error)
    assert plan.action == action and plan.retry_payload is None
    assert body == before


@pytest.mark.parametrize('controls', [
    {'reasoning_budget': 2000}, {'max_thinking_tokens': 2000},
    {'reasoning': {'budget_tokens': 2000}}, {'reasoning': {'max_tokens': 2000}},
    {'thinking': {'budget_tokens': 2000}}, {'thinking': {'max_tokens': 2000}},
    {'chat_template_kwargs': {'reasoning_budget': 2000}},
    {'chat_template_kwargs': {'max_thinking_tokens': 2000}},
    {'chat_template_kwargs': {'thinking_budget': 2000}},
])
def test_recovery_preserves_explicit_native_reasoning_allocation(controls):
    body = {**copy.deepcopy(BODY), **controls}
    original = copy.deepcopy(body)
    result = plan_context_recovery(body, 400, ERROR)
    assert result.retry_payload is None and result.action == 'adjust_reasoning_budget'
    assert body == original


async def test_adjusted_output_survives_key_rotation_without_mutating_input():
    state = ContextRecoveryState()
    peers = [_Upstream(status=400, reply=ERROR), _Upstream(status=429), _Upstream(reply={'ok': True})]
    sent = []
    async def send(payload):
        sent.append(copy.deepcopy(payload))
        return peers[len(sent) - 1]
    original = copy.deepcopy(BODY)
    async with context_request(send, BODY, state=state, credential='synthetic-first') as response:
        assert response.status == 429
    async with context_request(send, BODY, state=state, credential='synthetic-second') as response:
        assert response.status == 200
        assert context_response_headers(response)['X-Context-Output-Tokens'] == '2000'
    assert sent == [original, {**original, 'max_tokens': 2000}, {**original, 'max_tokens': 2000}]
    assert BODY == original and all(peer.release_count == 1 for peer in peers)


@pytest.mark.parametrize('invalid_output', ['4000', True, None, 4000.0])
async def test_existing_recovery_state_never_coerces_invalid_output(invalid_output):
    state = ContextRecoveryState()
    state.attempted, state.output_field, state.output_tokens = True, 'max_tokens', 2000
    body = {**copy.deepcopy(BODY), 'max_tokens': invalid_output}
    sent = []
    async def send(payload):
        sent.append(copy.deepcopy(payload))
        return _Upstream(status=400, reply={'error': {'message': 'invalid output value'}})
    async with context_request(send, body, state=state) as response:
        assert response.status == 400
    assert sent == [body]


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('oauth_first', [False, True])
@pytest.mark.parametrize('path,field', [('v1/chat/completions', 'max_tokens'),
                                      ('v1/messages', 'max_tokens'),
                                      ('v1/responses', 'max_output_tokens')])
async def test_nous_recovery_state_survives_real_credential_retry(stream, oauth_first, path, field, monkeypatch):
    from common.compat import responses_chat_fallback_allowed
    from common.model import classify_upstream_error
    from common.translations import parse_retry_after, should_cooldown_key

    monkeypatch.setenv('RETRY_BACKOFF_BASE_SEC', '0')
    body = copy.deepcopy(BODY)
    if field != 'max_tokens':
        body[field] = body.pop('max_tokens')
        body['input'] = body.pop('messages')
        body['previous_response_id'] = 'keep-native-history'
    body['stream'] = stream
    original = copy.deepcopy(body)
    wire = b'event: native_future\ndata: {"keep":true}\n\n'
    peers = [_Upstream(status=400, reply=ERROR),
             _Upstream(status=429, reply={'error': {'message': 'rate limited'}}),
             _Upstream([wire], reply=None) if stream else _Upstream(reply={'ok': True})]
    sent = []
    def send(url, *, json, headers, **kwargs):
        sent.append((copy.deepcopy(json), headers['Authorization']))
        return peers[len(sent)-1]
    async def session(): return SimpleNamespace(post=send)
    pool = _pool('nous')
    names = {'post_nous_with_retries', 'post_nous', '_UpstreamJSON', '_prepare_nous_request_metadata',
             '_retry_after_seconds', '_is_retriable_upstream_status', '_looks_model_capacity_error'}
    namespace = _load(ROOT / 'nous/src/main.py', names,
                      NOUS_BASE='http://synthetic.invalid', get_session=session,
                      CONNECT_TIMEOUT_SEC=1, STREAM_SOCK_READ_TIMEOUT_SEC=1,
                      MODEL_REGISTRY=_registry(), same_provider_model_id=lambda *args: True,
                      KEY_POOL=pool, _read_token_from_auth_path=lambda: 'synthetic-oauth' if oauth_first else None,
                      _parse_retry_after=parse_retry_after, _should_cooldown_key=should_cooldown_key,
                      _normalize_upstream_error=lambda status, data: data,
                      classify_upstream_error=classify_upstream_error,
                      _responses_chat_fallback_allowed=responses_chat_fallback_allowed)
    status, result, entry = await namespace['post_nous_with_retries'](body, stream=stream, path=path)
    assert status == 200
    if stream:
        try:
            assert b''.join([chunk async for chunk in result]) == wire
        finally:
            result.release()
            await pool.release(entry)
    else:
        assert result == {'ok': True} and entry is None
    assert context_response_headers(result)['X-Context-Output-Tokens'] == '2000'
    expected = {**original, 'tags': sent[0][0]['tags']}
    assert [payload for payload, _ in sent] == [expected, {**expected, field: 2000}, {**expected, field: 2000}]
    assert sent[0][1] == sent[1][1] and sent[1][1] != sent[2][1]
    assert body == original
    assert all(key.in_flight == 0 for key in pool.keys)
    assert all(peer.release_count == 1 for peer in peers)


async def test_retry_failure_is_bounded_and_preserves_original_error():
    peers = [_Upstream(status=400, reply=ERROR), _Upstream(status=400, reply=ERROR)]
    calls = []
    async def send(body):
        calls.append(copy.deepcopy(body))
        return peers[len(calls)-1]
    async with context_request(send, copy.deepcopy(BODY)) as response:
        assert json.loads(await response.text()) == ERROR
        assert context_response_headers(response)['X-Context-Recovery'] == 'retry_exhausted'
    assert len(calls) == 2 and all(peer.release_count == 1 for peer in peers)


async def test_cancellation_reading_error_closes_the_owned_response():
    waiting = asyncio.Event()
    class Pending(_Upstream):
        async def text(self):
            waiting.set()
            await asyncio.Future()
    peer = Pending(status=400)
    async def send(body): return peer
    async def run(): return await context_request(send, BODY)
    task = asyncio.create_task(run())
    await asyncio.wait_for(waiting.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert peer.release_count == 1


@pytest.mark.parametrize('wrapper', ACTIVE)
@pytest.mark.parametrize('surface,field', [('chat', 'max_tokens'), ('responses', 'max_output_tokens'), ('messages', 'max_tokens')])
@pytest.mark.parametrize('stream', [False, True])
async def test_active_transports_recover_once_preserve_native_data_and_release(wrapper, surface, field, stream):
    body = copy.deepcopy(BODY)
    if field != 'max_tokens':
        body[field] = body.pop('max_tokens')
        body['input'] = body.pop('messages')
        body['previous_response_id'] = 'native-history-id'
    body['stream'] = stream
    expected = {**copy.deepcopy(body), field: 2000}
    wire = b'event: future_native_event\ndata: {"opaque":true}\n\n'
    success = {'id': 'native_ok', 'future_result': {'opaque': True}}
    peers = [Peer(status=400, reply=ERROR), Peer([wire], reply=None) if stream else Peer(reply=success)]
    records = []
    if wrapper == 'nvidia-python':
        server, _ = _server(peers, records)
        if surface == 'chat':
            result = await server.proxy_openai(body, {}, body['model'], _request(body))
            if stream:
                actual = b''.join([chunk async for chunk in result['stream']])
                assert actual == wire
            else:
                assert result['data'] == success
        elif surface == 'responses':
            # _handle_catch_all uses _stream_proxy from the normal class.
            async def stream_proxy(resp, key, on_close):
                try:
                    async for chunk in resp: yield chunk
                finally: on_close()
            server._stream_proxy = stream_proxy
            result = await server._handle_catch_all(_request(body), '/v1/responses')
            assert await _collect(result) == wire if stream else json.loads(result.body) == success
        else:
            status, result, key = await server._compat2_proxy(body, _request(body))
            assert status == 200
            if stream:
                assert b''.join([chunk async for chunk in result]) == wire
                result.release()
                key.decrement_in_flight()
                server._in_flight -= 1
            else: assert result == success
        assert server._in_flight == 0
        assert all(key.in_flight == 0 for key in server.pool.keys)
        sent = [item[1]['json'] for item in records]
        credentials = [item[1]['headers']['Authorization'] for item in records]
    else:
        def send(*args, json, headers, **kwargs):
            records.append((copy.deepcopy(json), headers['Authorization']))
            return peers[len(records)-1]
        if wrapper == 'openrouter':
            pool = _pool(wrapper)
            namespace = _openrouter_proxy_namespace(pool, SimpleNamespace(request=send))
            result = await namespace['_proxy_request']('POST', {'chat': 'chat/completions', 'responses': 'responses', 'messages': 'messages'}[surface], body, stream=stream)
            assert result.status_code == 200
            assert await _collect(result) == wire if stream else json.loads(result.body) == success
            assert result.headers['x-context-recovery'] == 'reduced_output'
            assert all(key.in_flight == 0 for key in pool.keys)
        else:
            async def session(): return SimpleNamespace(post=send)
            namespace = _load(ROOT / wrapper / 'src/main.py',
                              {'post_nous', '_UpstreamJSON', '_prepare_nous_request_metadata'},
                              NOUS_BASE='http://controlled.invalid', get_session=session,
                              CONNECT_TIMEOUT_SEC=1, STREAM_SOCK_READ_TIMEOUT_SEC=1,
                              _normalize_upstream_error=lambda status, data: data)
            status, result = await namespace['post_nous'](body, 'fake-credential', stream=stream,
                path={'chat': 'v1/chat/completions', 'responses': 'v1/responses', 'messages': 'v1/messages'}[surface])
            assert status == 200
            if stream:
                assert b''.join([chunk async for chunk in result]) == wire
                result.release()
            else: assert result == success
            # Provider routing tags are honest transport metadata and remain stable.
            expected['tags'] = records[0][0]['tags']
        sent = [record[0] for record in records]
        credentials = [record[1] for record in records]
    assert len(sent) == 2 and sent[1] == expected
    assert credentials[0] == credentials[1]
    assert body[field] == 4000
    assert all(peer.release_count == 1 for peer in peers)
