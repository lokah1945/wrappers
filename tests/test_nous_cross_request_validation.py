"""Exercise the actual Messages route/converter against a recording Chat peer."""
import copy
import json

import pytest

from common.translations import strip_cache_control
from test_active_native_passthrough import ROOT, _load, _request, _route_namespace


def _route(*, native=False):
    captured = []
    reply = {'id': 'chat-test', 'model': 'exact/model',
             'choices': [{'message': {'role': 'assistant', 'content': 'OK'},
                          'finish_reason': 'stop'}], 'usage': {}}
    namespace = _route_namespace('nous', anthropic=native, response=reply, captured=captured)
    namespace.update(_load(ROOT / 'nous/src/main.py', {
        'anthropic_to_openai', '_validate_anthropic_chat_request',
        'normalize_schema', 'openai_to_anthropic'},
        resolve_model=lambda model: model, strip_cache_control=strip_cache_control,
        _parse_dsml_from_text=lambda text: (text, [])))
    return namespace['messages'], captured


def _body():
    return {'model': 'exact/model', 'max_tokens': 256,
            'messages': [{'role': 'user', 'content': 'Hello'}]}


@pytest.mark.parametrize('content', ['omitted', '', [], [{'type': 'text', 'text': 'OK'}]])
async def test_valid_tool_result_including_omitted_content_reaches_transport(content):
    route, captured = _route()
    result = {'type': 'tool_result', 'tool_use_id': 'call-1'}
    if content != 'omitted':
        result['content'] = content
    body = _body()
    body['messages'] = [
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call-1',
                                         'name': 'inspect', 'input': {}}]},
        {'role': 'user', 'content': [result]}]
    response = await route(_request(body))
    assert response['content'] == [{'type': 'text', 'text': 'OK'}]
    sent = captured[0][0][0]
    assert sent['model'] == 'exact/model'
    assert sent['messages'][0]['tool_calls'][0]['id'] == 'call-1'
    assert sent['messages'][1] == {'role': 'tool', 'tool_call_id': 'call-1',
                                  'content': 'OK' if isinstance(content, list) and content else ''}


@pytest.mark.parametrize('update', [
    {'model': {'invalid': True}},
    {'tools': 7}, {'tools': [None]}, {'tools': [{'input_schema': {}}]},
    {'messages': 7}, {'messages': [None]},
    {'messages': [{'role': 'user', 'content': None}]},
    {'messages': [{'role': 'user', 'content': [None]}]},
    {'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': None}]}]},
    {'messages': [{'role': 'user', 'content': [{'type': 'image', 'source': 'invalid'}]}]},
    {'messages': [{'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': None}]}]},
    {'messages': [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'x', 'name': 'f', 'input': 7}]}]},
    {'messages': [{'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'x', 'content': None}]}]},
    {'messages': [{'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'x', 'content': [None]}]}]},
    {'system': [None]},
])
async def test_malformed_nested_cross_input_returns_400_before_upstream(update):
    route, captured = _route()
    response = await route(_request({**_body(), **copy.deepcopy(update)}))
    assert response.status_code == 400
    error = json.loads(response.body)
    assert error['type'] == 'error'
    assert error['error']['type'] == 'invalid_request_error'
    assert error['error']['message']
    assert captured == []


async def test_anthropic_none_tool_choice_translates_to_openai_none():
    route, captured = _route()
    await route(_request({**_body(), 'tool_choice': {'type': 'none'}}))
    assert captured[0][0][0]['tool_choice'] == 'none'


async def test_native_anthropic_bypasses_cross_validation_and_retains_extensions():
    route, captured = _route(native=True)
    body = {**_body(), 'tools': [{'type': 'future-tool', 'opaque': [None]}],
            'messages': [{'role': 'future-role', 'content': [{'type': 'future-block', 'raw': None}]}],
            'thinking': {'type': 'future-mode'}, 'future_parameter': {'v': 1}}
    response = await route(_request(body))
    assert response.status_code == 200
    assert captured[0][0][0] == body
