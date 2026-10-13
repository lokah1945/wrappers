"""OpenRouter's documented Messages request controls reach the real transport.

AST loading avoids dotenv/startup/state access. The route, converter, proxy and
key pool are production functions; only the credential-free peer is simulated.
"""
import copy
import json
from types import SimpleNamespace

import pytest
from starlette.responses import JSONResponse

from common.sse import IDLE, iter_chunks_with_idle
from common.translations import strip_cache_control
from test_active_native_passthrough import (
    ROOT, _Upstream, _collect, _load, _openrouter_proxy_namespace, _pool, _request,
)

SOURCE = ROOT / 'openrouter/src/main.py'
CONTROLS = [
    ({'thinking': {'type': 'adaptive'}}, {'enabled': True}),
    ({'thinking': {'type': 'disabled'}}, {'enabled': False}),
    ({'thinking': {'type': 'enabled', 'budget_tokens': 2048}}, {'max_tokens': 2048}),
    ({'thinking': {'type': 'enabled'}}, {'enabled': True}),
    ({'output_config': {'effort': 'high'}}, {'effort': 'high'}),
    ({'thinking': {'type': 'adaptive'}, 'output_config': {'effort': 'low'}}, {'effort': 'low'}),
    ({'thinking': {'type': 'enabled', 'budget_tokens': 2048},
      'output_config': {'effort': 'max'}}, {'effort': 'max'}),
    ({'thinking': {'type': 'disabled', 'budget_tokens': 2048},
      'output_config': {'effort': 'high'}}, {'enabled': False}),
    # Preserve invalid values for upstream validation; never clamp or invent effort.
    ({'thinking': {'type': 'enabled', 'budget_tokens': 0}}, {'max_tokens': 0}),
    ({'output_config': {'effort': 'future-effort'}}, {'effort': 'future-effort'}),
]


def _body(controls):
    return {'model': 'selected/exact-model', 'max_tokens': 256,
            'messages': [{'role': 'user', 'content': 'Reply briefly.'}], **copy.deepcopy(controls)}


def _converter():
    return _load(SOURCE, {'_anthropic_to_openai'},
                 _strip_cache=strip_cache_control)['_anthropic_to_openai']


@pytest.mark.parametrize('controls,expected', CONTROLS)
def test_documented_control_mapping_and_precedence(controls, expected):
    body = _body(controls)
    before = copy.deepcopy(body)
    actual = _converter()(body)
    assert actual['reasoning'] == expected
    assert actual['model'] == before['model'] and actual['max_tokens'] == 256
    assert 'thinking' not in actual and 'output_config' not in actual
    assert body == before


@pytest.mark.parametrize('controls,expected', CONTROLS[:8])
def test_mapped_intent_wins_conflicts_but_keeps_explicit_extensions(controls, expected):
    body = _body(controls)
    body['reasoning'] = {'enabled': False, 'effort': 'medium', 'max_tokens': 999,
                         'exclude': False, 'future_extension': {'opaque': [None, True]}}
    before = copy.deepcopy(body)
    actual = _converter()(body)
    assert actual['reasoning'] == {'exclude': False,
                                  'future_extension': {'opaque': [None, True]}, **expected}
    assert body == before


@pytest.mark.parametrize('reasoning', [{}, {'effort': 'low', 'context': 'current_turn'},
                                     ['invalid explicit value'], 'invalid explicit value'])
def test_explicit_reasoning_is_preserved_without_anthropic_controls(reasoning):
    body = _body({'reasoning': reasoning})
    before = copy.deepcopy(body)
    assert _converter()(body)['reasoning'] == reasoning
    assert body == before


def test_absent_reasoning_does_not_invent_a_default():
    assert 'reasoning' not in _converter()(_body({}))


def test_explicit_anthropic_none_tool_choice_disables_chat_tools():
    body = _body({'tool_choice': {'type': 'none'},
                  'tools': [{'name': 'inspect', 'input_schema': {'type': 'object'}}]})
    converted = _converter()(body)
    assert converted['tool_choice'] == 'none'
    assert converted['tools'][0]['function']['name'] == 'inspect'
    assert body['tool_choice'] == {'type': 'none'}


def test_invalid_explicit_reasoning_remains_visible_to_upstream_validation():
    body = _body({'thinking': {'type': 'adaptive'}, 'reasoning': 'invalid'})
    assert _converter()(body)['reasoning'] == 'invalid'


def _route(pool, agent, *, native=False):
    proxy = _openrouter_proxy_namespace(pool, agent)['_proxy_request']
    return _load(SOURCE, {'messages', 'chat_completions', '_anthropic_to_openai',
                          '_openai_to_anthropic_response', '_translate_openai_stream_to_anthropic'},
                 _proxy_request=proxy, _is_anthropic_upstream=lambda: native,
                 _check_free_only=lambda model: None, _strip_cache=strip_cache_control,
                 _error_response=lambda content, status_code=500, **kwargs:
                    JSONResponse(content, status_code=status_code),
                 _iter_chunks_with_idle=iter_chunks_with_idle, _IDLE=IDLE,
                 HEARTBEAT_MS=100000)


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('controls,expected', CONTROLS[:8])
async def test_messages_route_pool_and_proxy_emit_controls_to_actual_peer(stream, controls, expected):
    pool = _pool('openrouter')
    records = []
    reply = {'id': 'peer-result', 'choices': [{'message': {'content': 'OK'}, 'finish_reason': 'stop'}]}
    wire = (b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n'
            b'data: [DONE]\n\n')
    upstream = _Upstream([wire], reply=None) if stream else _Upstream(reply=reply)

    def request(method, url, **kwargs):
        records.append((url, copy.deepcopy(kwargs['json'])))
        return upstream

    namespace = _route(pool, SimpleNamespace(request=request))
    body = _body(controls)
    body['stream'] = stream
    before = copy.deepcopy(body)
    response = await namespace['messages'](_request(body))
    assert response.status_code == 200
    if stream:
        assert b'event: message_stop\n' in await _collect(response)
    else:
        assert json.loads(response.body)['content'] == [{'type': 'text', 'text': 'OK'}]
    assert len(records) == 1 and records[0][0].endswith('/chat/completions')
    assert records[0][1]['reasoning'] == expected
    assert records[0][1]['model'] == before['model']
    assert records[0][1]['max_tokens'] == 256 and records[0][1]['stream'] is stream
    assert body == before and all(key.in_flight == 0 for key in pool.keys)
    assert upstream.release_count == 1


@pytest.mark.parametrize('native_anthropic', [False, True])
@pytest.mark.parametrize('stream', [False, True])
async def test_matching_native_chat_and_messages_keep_controls_and_future_fields_opaque(native_anthropic, stream):
    pool = _pool('openrouter')
    records = []
    body = _body({'thinking': {'type': 'adaptive'}, 'output_config': {'effort': 'max'},
                  'reasoning': {'future_extension': [None]}, 'future_parameter': {'value': True}})
    body['stream'] = stream
    before = copy.deepcopy(body)
    reply = {'future_response': [None, True]}
    wire = b'event: future_event\ndata: {"future":true}\n\n'
    upstream = _Upstream([wire]) if stream else _Upstream(reply=reply)

    def request(method, url, **kwargs):
        records.append((url, copy.deepcopy(kwargs['json'])))
        return upstream

    namespace = _route(pool, SimpleNamespace(request=request), native=native_anthropic)

    def no_conversion(*args, **kwargs):
        pytest.fail('matching native request called a converter')

    namespace['_anthropic_to_openai'] = no_conversion
    response = await namespace['messages' if native_anthropic else 'chat_completions'](_request(body))
    assert await _collect(response) == wire if stream else json.loads(response.body) == reply
    assert len(records) == 1 and records[0][1] == before
    assert records[0][0].endswith('/messages' if native_anthropic else '/chat/completions')
    assert body == before and all(key.in_flight == 0 for key in pool.keys)
