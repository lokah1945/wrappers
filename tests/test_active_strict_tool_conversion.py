"""Strict tool intent must survive cross protocols and bypass native conversion.

These checks call the production routes against recording, credential-free peers.
They select active wrappers from metadata and never import pending providers.
"""
import ast
import copy
import json

import pytest
from starlette.responses import JSONResponse

from common.translations import (
    anthropic_to_openai_response, openai_chat_to_anthropic_request, repair_orphan_tool_messages,
    strip_cache_control,
)
from common.wrapper_scope import production_wrappers
from test_active_native_passthrough import ROOT, _load, _request, _route_namespace
from test_nvidia_messages_request_controls import COMPAT, Peer, SOURCE, _module, _request as nvidia_request, _server

ACTIVE = production_wrappers(include_registry=False)
RESPONSES = _module('nvidia_controls_fixture.responses_compat',
                    ROOT / 'nvidia-python/src/responses_compat.py', package='nvidia_controls_fixture')
SCHEMA = {'type': 'object', 'properties': {'value': {'type': 'string', 'enum': ['OK']}},
          'required': ['value'], 'additionalProperties': False}
CHAT_REPLY = {'id': 'chat-peer', 'model': 'exact/model', 'choices': [
    {'message': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': 'stop'}], 'usage': {}}
MESSAGES_REPLY = {'id': 'message-peer', 'type': 'message', 'role': 'assistant',
                  'model': 'exact/model', 'content': [{'type': 'text', 'text': 'OK'}],
                  'stop_reason': 'end_turn', 'usage': {'input_tokens': 1, 'output_tokens': 1}}


def _body(surface, strict):
    tool = {'name': 'echo', 'description': 'Return the given value.'}
    if strict != 'absent':
        tool['strict'] = copy.deepcopy(strict)
    if surface == 'messages':
        tool.update({'input_schema': copy.deepcopy(SCHEMA), 'defer_loading': False,
                     'cache_control': {'type': 'ephemeral'}})
    else:
        tool.update({'parameters': copy.deepcopy(SCHEMA)})
        tool = {'type': 'function', 'function': tool}
    return {'model': 'exact/model', 'max_tokens': 256,
            'messages': [{'role': 'user', 'content': 'Use echo with value OK.'}], 'tools': [tool]}


def _strict_equal(tool, strict):
    if strict == 'absent':
        assert 'strict' not in tool
    else:
        assert tool['strict'] == strict
        assert type(tool['strict']) is type(strict)


def _route(wrapper, surface, *, upstream_anthropic, native=False):
    captured = []
    reply = MESSAGES_REPLY if upstream_anthropic else CHAT_REPLY
    if wrapper == 'nvidia-python':
        records = []
        server, namespace = _server([Peer(reply=copy.deepcopy(reply))], records,
                                     native=upstream_anthropic)
        # Reuse the isolated pool/transport fixture, then add the real route
        # methods that exercise the shared OpenAI-to-Anthropic request mapper.
        tree = ast.parse(SOURCE.read_text())
        definition = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Server')
        names = {'_compat2_chat', '_compat2_messages', '_handle_chat_completions'}
        methods = [node for node in definition.body if getattr(node, 'name', None) in names]
        namespace['openai_chat_to_anthropic_request'] = openai_chat_to_anthropic_request
        exec(compile(ast.fix_missing_locations(ast.Module(body=[
            ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
            *methods], type_ignores=[])), str(SOURCE), 'exec'), namespace)
        for name in names:
            setattr(type(server), name, namespace[name])
        if native:
            namespace['anthropic_to_openai'] = namespace['openai_chat_to_anthropic_request'] = lambda *a, **k: pytest.fail('native converter invoked')

        async def call(body):
            request = nvidia_request(body)
            if surface == 'messages':
                response = await server._handle_anthropic_messages(json.dumps(body).encode(), request)
            else:
                response = await server._handle_chat_completions(body, request, json.dumps(body).encode())
            captured.extend(record[1]['json'] for record in records)
            assert server._in_flight == 0 and all(key.in_flight == 0 for key in server.pool.keys)
            return response
        return call, captured

    namespace = _route_namespace(wrapper, anthropic=upstream_anthropic,
        response=copy.deepcopy(reply) if wrapper == 'nous' else JSONResponse(copy.deepcopy(reply)),
        captured=[])
    recorded = namespace['post_nous_with_retries' if wrapper == 'nous' else '_proxy_request']

    async def record(*args, **kwargs):
        captured.append(copy.deepcopy(args[0] if wrapper == 'nous' else args[2]))
        return await recorded(*args, **kwargs)

    namespace['post_nous_with_retries' if wrapper == 'nous' else '_proxy_request'] = record
    namespace.update({'anthropic_to_openai_response': anthropic_to_openai_response,
                      'openai_chat_to_anthropic_request': openai_chat_to_anthropic_request})
    if wrapper == 'nous':
        namespace.update(_load(ROOT / wrapper / 'src/main.py', {
            'anthropic_to_openai', '_validate_anthropic_chat_request', 'normalize_schema', 'openai_to_anthropic'},
            resolve_model=lambda model: model, strip_cache_control=strip_cache_control,
            _parse_dsml_from_text=lambda text: (text, [])))
    else:
        namespace.update(_load(ROOT / wrapper / 'src/main.py', {
            '_anthropic_to_openai', '_openai_to_anthropic_response'}, _strip_cache=strip_cache_control))
    if native:
        for name in ('anthropic_to_openai', '_anthropic_to_openai', 'openai_chat_to_anthropic_request'):
            namespace[name] = lambda *a, **k: pytest.fail('native converter invoked')

    async def call(body):
        response = await namespace['messages' if surface == 'messages' else 'chat_completions'](_request(body))
        return JSONResponse(response) if isinstance(response, dict) else response
    return call, captured


@pytest.mark.parametrize('wrapper', ACTIVE)
@pytest.mark.parametrize('surface', ['chat', 'messages'])
@pytest.mark.parametrize('strict', [True, False, 'absent'])
async def test_cross_routes_preserve_explicit_strict_and_leave_tools_unchanged(wrapper, surface, strict):
    body = _body(surface, strict)
    original = copy.deepcopy(body)
    route, captured = _route(wrapper, surface, upstream_anthropic=surface == 'chat')
    response = await route(body)
    assert response.status_code == 200 and len(captured) == 1
    sent = captured[0]
    assert sent['model'] == body['model'] and sent['max_tokens'] == body['max_tokens']
    tool = sent['tools'][0] if surface == 'chat' else sent['tools'][0]['function']
    _strict_equal(tool, strict)
    assert tool['input_schema' if surface == 'chat' else 'parameters'] == SCHEMA
    assert body == original
    # A converted schema belongs to the outbound request, not to the caller's
    # tool definition; later processing must not rewrite its original schema.
    tool['input_schema' if surface == 'chat' else 'parameters']['required'].append('future')
    assert body == original


@pytest.mark.parametrize('wrapper', ACTIVE)
@pytest.mark.parametrize('surface', ['chat', 'messages'])
@pytest.mark.parametrize('strict', [True, False, 'absent', {'future_version': [None, True]}])
async def test_native_routes_keep_strict_and_future_extensions_opaque(wrapper, surface, strict):
    body = _body(surface, strict)
    body['tools'][0]['future_parameter'] = {'opaque': [None, True]}
    original = copy.deepcopy(body)
    route, captured = _route(wrapper, surface, upstream_anthropic=surface == 'messages', native=True)
    response = await route(body)
    assert response.status_code == 200 and captured == [original]
    assert json.loads(response.body) == (MESSAGES_REPLY if surface == 'messages' else CHAT_REPLY)
    assert body == original


@pytest.mark.parametrize('wrapper', ACTIVE)
@pytest.mark.parametrize('strict', [True, False, 'absent'])
def test_cross_anthropic_converter_owns_outbound_tool_schema(wrapper, strict):
    if wrapper == 'nvidia-python':
        convert = COMPAT.anthropic_to_openai
    elif wrapper == 'nous':
        convert = _load(ROOT / wrapper / 'src/main.py', {
            'anthropic_to_openai', '_validate_anthropic_chat_request', 'normalize_schema'},
            resolve_model=lambda model: model, strip_cache_control=strip_cache_control)['anthropic_to_openai']
    else:
        convert = _load(ROOT / wrapper / 'src/main.py', {'_anthropic_to_openai'},
                        _strip_cache=strip_cache_control)['_anthropic_to_openai']
    body = _body('messages', strict)
    original = copy.deepcopy(body)
    converted = convert(body)
    tool = converted['tools'][0]['function']
    _strict_equal(tool, strict)
    assert body == original
    tool['parameters']['required'].append('future')
    assert body == original


@pytest.mark.parametrize('strict', [True, False, 'absent'])
def test_shared_cross_chat_converter_owns_outbound_tool_schema(strict):
    body = _body('chat', strict)
    original = copy.deepcopy(body)
    converted = openai_chat_to_anthropic_request(body)
    tool = converted['tools'][0]
    _strict_equal(tool, strict)
    assert body == original
    tool['input_schema']['required'].append('future')
    assert body == original


def _responses_body(strict, nested):
    tool = {'name': 'echo', 'description': 'Return the given value.', 'parameters': copy.deepcopy(SCHEMA)}
    if strict != 'absent':
        tool['strict'] = strict
    tool = {'type': 'function', 'function': tool} if nested else {'type': 'function', **tool}
    return {'model': 'exact/model', 'max_output_tokens': 256, 'input': 'Call echo with value OK.',
            'tools': [tool], 'tool_choice': {'type': 'function', 'name': 'echo'}}


def _adapter(wrapper):
    if wrapper == 'nvidia-python':
        return lambda body: RESPONSES.build_chat_body(body, body['model'], None)
    names = {'responses_to_chat'} | ({'normalize_schema'} if wrapper == 'nous' else set())
    return _load(ROOT / wrapper / 'src/main.py', names, resolve_model=lambda model: model,
                 repair_orphan_tool_messages=repair_orphan_tool_messages)['responses_to_chat']


@pytest.mark.parametrize('wrapper', ACTIVE)
@pytest.mark.parametrize('nested', [False, True])
@pytest.mark.parametrize('strict', [True, False, 'absent'])
def test_responses_adapter_preserves_strict_named_choice_and_schema_ownership(wrapper, nested, strict):
    body = _responses_body(strict, nested)
    original = copy.deepcopy(body)
    converted = _adapter(wrapper)(body)
    tool = converted['tools'][0]['function']
    _strict_equal(tool, strict)
    assert tool['parameters'] == SCHEMA
    assert converted['model'] == body['model'] and converted['max_tokens'] == body['max_output_tokens']
    assert converted['tool_choice'] == {'type': 'function', 'function': {'name': 'echo'}}
    assert body == original
    tool['parameters']['required'].append('future')
    assert body == original


@pytest.mark.parametrize('wrapper', ACTIVE)
@pytest.mark.parametrize('strict', [True, False, 'absent'])
async def test_responses_to_anthropic_route_keeps_strict_and_forced_tool_intent(wrapper, strict):
    body = _responses_body(strict, False)
    original = copy.deepcopy(body)
    reply = copy.deepcopy(MESSAGES_REPLY)
    reply.update({'content': [{'type': 'tool_use', 'id': 'call-echo', 'name': 'echo', 'input': {'value': 'OK'}}],
                  'stop_reason': 'tool_use'})
    captured = []
    if wrapper == 'nvidia-python':
        records = []
        server, namespace = _server([Peer(reply=reply)], records, native=True)
        namespace.update({'openai_chat_to_anthropic_request': openai_chat_to_anthropic_request,
                          'anthropic_to_openai_response': anthropic_to_openai_response,
                          '__name__': 'nvidia_controls_fixture.main', '__package__': 'nvidia_controls_fixture'})
        tree = ast.parse(SOURCE.read_text())
        definition = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Server')
        method = next(node for node in definition.body if getattr(node, 'name', None) == '_compat2_responses')
        exec(compile(ast.fix_missing_locations(ast.Module(body=[
            ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), method],
            type_ignores=[])), str(SOURCE), 'exec'), namespace)
        response = await namespace['_compat2_responses'](server, body, nvidia_request(body))
        captured = [record[1]['json'] for record in records]
        assert server._in_flight == 0 and all(key.in_flight == 0 for key in server.pool.keys)
    else:
        namespace = _route_namespace(wrapper, anthropic=True,
            response=reply if wrapper == 'nous' else JSONResponse(reply), captured=[])
        recorded = namespace['post_nous_with_retries' if wrapper == 'nous' else '_proxy_request']

        async def record(*args, **kwargs):
            captured.append(copy.deepcopy(args[0] if wrapper == 'nous' else args[2]))
            assert kwargs.get('path', args[1] if wrapper == 'openrouter' else '') in ('v1/messages', 'messages')
            return await recorded(*args, **kwargs)

        async def save(*args, **kwargs):
            pass

        namespace['post_nous_with_retries' if wrapper == 'nous' else '_proxy_request'] = record
        namespace.update({'responses_to_chat': _adapter(wrapper),
            'openai_chat_to_anthropic_request': openai_chat_to_anthropic_request,
            'anthropic_to_openai_response': anthropic_to_openai_response,
            '_extract_principal': lambda request: 'controlled-tenant',
            '_request_principal': lambda request: 'controlled-tenant',
            'store_conversation': save, '_remember_completed_response': lambda *args: None})
        namespace.update(_load(ROOT / wrapper / 'src/main.py', {'chat_to_responses'}))
        response = await namespace['responses'](_request(body))
        if isinstance(response, dict):
            response = JSONResponse(response)
    assert response.status_code == 200 and len(captured) == 1
    sent = captured[0]
    assert sent['model'] == body['model'] and sent['max_tokens'] == body['max_output_tokens']
    _strict_equal(sent['tools'][0], strict)
    assert sent['tools'][0]['input_schema'] == SCHEMA
    assert sent['tool_choice'] == {'type': 'tool', 'name': 'echo'}
    returned = json.loads(response.body)
    calls = [item for item in returned['output'] if item['type'] == 'function_call']
    assert len(calls) == 1 and json.loads(calls[0]['arguments']) == {'value': 'OK'}
    assert body == original
