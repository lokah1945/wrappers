#!/usr/bin/env python3
"""Tests for responses_compat.py — OpenAI Responses API translation."""

import json
import pytest
from src.responses_compat import (
    input_to_messages,
    convert_tools,
    convert_usage,
    base_response,
    make_reasoning_item,
    respond_non_streaming,
    build_chat_body,
    http_status_from_error,
    _rand,
)


class TestRand:
    def test_rand_format(self):
        result = _rand('resp')
        assert result.startswith('resp_')
        assert len(result) > 5

    def test_rand_uniqueness(self):
        results = {_rand('x') for _ in range(100)}
        assert len(results) == 100


class TestInputToMessages:
    def test_bare_string_input(self):
        messages = input_to_messages("Hello, world!")
        assert len(messages) == 1
        assert messages[0]['role'] == 'user'
        assert messages[0]['content'] == 'Hello, world!'

    def test_string_with_instructions(self):
        messages = input_to_messages("Hello", "You are helpful")
        assert len(messages) == 2
        assert messages[0]['role'] == 'system'
        assert messages[0]['content'] == 'You are helpful'
        assert messages[1]['role'] == 'user'
        assert messages[1]['content'] == 'Hello'

    def test_message_items(self):
        input_val = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Hi there'},
        ]
        messages = input_to_messages(input_val)
        assert len(messages) == 2
        assert messages[0]['role'] == 'user'
        assert messages[1]['role'] == 'assistant'

    def test_developer_role_normalized_to_system(self):
        input_val = [{'role': 'developer', 'content': 'Be helpful'}]
        messages = input_to_messages(input_val)
        assert messages[0]['role'] == 'system'

    def test_function_call_item(self):
        input_val = [
            {'type': 'function_call', 'name': 'get_weather', 'arguments': '{"city":"NYC"}', 'call_id': 'call_1'},
        ]
        messages = input_to_messages(input_val)
        assert len(messages) == 1
        assert messages[0]['role'] == 'assistant'
        assert messages[0]['tool_calls'][0]['function']['name'] == 'get_weather'

    def test_function_call_output_item(self):
        input_val = [
            {'type': 'function_call_output', 'call_id': 'call_1', 'output': 'Sunny, 72F'},
        ]
        messages = input_to_messages(input_val)
        assert len(messages) == 1
        assert messages[0]['role'] == 'tool'
        assert messages[0]['content'] == 'Sunny, 72F'

    def test_input_text_content(self):
        input_val = [
            {'role': 'user', 'content': [{'type': 'input_text', 'text': 'Hello'}]},
        ]
        messages = input_to_messages(input_val)
        assert messages[0]['content'][0]['type'] == 'text'
        assert messages[0]['content'][0]['text'] == 'Hello'

    def test_input_image_content(self):
        input_val = [
            {'role': 'user', 'content': [{'type': 'input_image', 'image_url': {'url': 'data:image/png;base64,abc'}}]},
        ]
        messages = input_to_messages(input_val)
        assert messages[0]['content'][0]['type'] == 'image_url'
        assert messages[0]['content'][0]['image_url']['url'] == 'data:image/png;base64,abc'

    def test_empty_input(self):
        messages = input_to_messages(None)
        assert messages == []

    def test_non_object_item_in_array(self):
        messages = input_to_messages([None, "string item", 42])
        assert len(messages) == 1
        assert messages[0]['role'] == 'user'
        assert messages[0]['content'] == 'string item'


class TestConvertTools:
    def test_valid_tools(self):
        tools = [
            {'type': 'function', 'function': {'name': 'get_weather', 'description': 'Get weather', 'parameters': {}}},
        ]
        result = convert_tools(tools)
        assert result is not None
        assert len(result) == 1
        assert result[0]['function']['name'] == 'get_weather'

    def test_empty_tools(self):
        assert convert_tools([]) is None
        assert convert_tools(None) is None

    def test_non_function_tool(self):
        tools = [{'type': 'other', 'function': {}}]
        assert convert_tools(tools) is None


class TestConvertUsage:
    def test_full_usage(self):
        u = {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150}
        result = convert_usage(u)
        assert result['input_tokens'] == 100
        assert result['output_tokens'] == 50
        assert result['total_tokens'] == 150

    def test_missing_total(self):
        u = {'prompt_tokens': 100, 'completion_tokens': 50}
        result = convert_usage(u)
        assert result['total_tokens'] == 150

    def test_none_usage(self):
        assert convert_usage(None) is None


class TestBaseResponse:
    def test_base_response_structure(self):
        resp = base_response('resp_1', 'nvidia/llama', 'completed', [], None)
        assert resp['id'] == 'resp_1'
        assert resp['object'] == 'response'
        assert resp['model'] == 'nvidia/llama'
        assert resp['status'] == 'completed'
        assert resp['output'] == []
        assert resp['usage'] is None

    def test_with_output(self):
        output = [{'type': 'message', 'role': 'assistant', 'content': []}]
        resp = base_response('resp_2', 'nvidia/llama', 'completed', output, {'input_tokens': 10})
        assert resp['output'] == output
        assert resp['usage'] == {'input_tokens': 10}


class TestMakeReasoningItem:
    def test_reasoning_item(self):
        item = make_reasoning_item('My reasoning')
        assert item['type'] == 'reasoning'
        assert item['status'] == 'completed'
        assert item['text'] == 'My reasoning'
        assert 'id' in item


class TestRespondNonStreaming:
    def test_text_response(self):
        data = {
            'model': 'nvidia/llama',
            'choices': [{'message': {'content': 'Hello!'}}],
            'usage': {'prompt_tokens': 5, 'completion_tokens': 2},
        }
        result = respond_non_streaming(data, 'nvidia/llama')
        assert result['status'] == 'completed'
        assert result['model'] == 'nvidia/llama'
        assert len(result['output']) == 1
        assert result['output'][0]['type'] == 'message'
        assert result['output'][0]['content'][0]['text'] == 'Hello!'

    def test_tool_call_response(self):
        data = {
            'model': 'nvidia/llama',
            'choices': [{'message': {
                'tool_calls': [{'id': 'call_1', 'function': {'name': 'get_weather', 'arguments': '{"city":"NYC"}'}}]
            }}],
            'usage': {},
        }
        result = respond_non_streaming(data, 'nvidia/llama')
        assert len(result['output']) == 1
        assert result['output'][0]['type'] == 'function_call'
        assert result['output'][0]['name'] == 'get_weather'

    def test_reasoning_extracted(self):
        data = {
            'model': 'nvidia/llama',
            'choices': [{'message': {
                'content': 'Thinking... \nFinal answer',
                'reasoning_content': 'My internal reasoning'
            }}],
            'usage': {},
        }
        result = respond_non_streaming(data, 'nvidia/llama')
        assert len(result['output']) == 2
        assert result['output'][0]['type'] == 'reasoning'
        assert result['output'][0]['text'] == 'My internal reasoning'


class TestBuildChatBody:
    def test_basic_chat_body(self):
        body = {'input': 'Hello', 'temperature': 0.7}
        chat_body = build_chat_body(body, 'nvidia/llama', lambda b, m, t: None)
        assert chat_body['model'] == 'nvidia/llama'
        assert chat_body['messages'][0]['role'] == 'user'
        assert chat_body['messages'][0]['content'] == 'Hello'
        assert chat_body['temperature'] == 0.7
        assert chat_body['stream'] is False

    def test_max_output_tokens(self):
        body = {'input': 'Hello', 'max_output_tokens': 100}
        chat_body = build_chat_body(body, 'nvidia/llama', lambda b, m, t: None)
        assert chat_body['max_tokens'] == 100

    def test_tool_choice(self):
        body = {
            'input': 'Hello',
            'tools': [{'type': 'function', 'function': {'name': 'f1'}}],
            'tool_choice': {'type': 'function', 'name': 'f1'},
        }
        chat_body = build_chat_body(body, 'nvidia/llama', lambda b, m, t: None)
        assert chat_body['tools'] is not None
        assert chat_body['tool_choice'] == {'type': 'function', 'function': {'name': 'f1'}}

    def test_tool_choice_required(self):
        body = {'input': 'Hello', 'tool_choice': {'type': 'required'}}
        chat_body = build_chat_body(body, 'nvidia/llama', lambda b, m, t: None)
        assert chat_body['tool_choice'] == 'required'

    def test_reasoning_translation(self):
        captured = {}
        def mock_translate(chat_body, model, thinking):
            captured['called'] = True
            captured['thinking'] = thinking
        body = {'input': 'Hello', 'reasoning': {'type': 'enabled'}}
        build_chat_body(body, 'nvidia/llama', mock_translate)
        assert captured['called']
        assert captured['thinking'] == {'type': 'enabled'}


class TestHttpStatusFromError:
    def test_rate_limit_error(self):
        assert http_status_from_error({'type': 'rate_limit_error'}) == 429

    def test_invalid_request_error(self):
        assert http_status_from_error({'type': 'invalid_request_error'}) == 400

    def test_authentication_error(self):
        assert http_status_from_error({'type': 'authentication_error'}) == 401

    def test_permission_error(self):
        assert http_status_from_error({'type': 'permission_error'}) == 403

    def test_not_found_error(self):
        assert http_status_from_error({'type': 'not_found_error'}) == 404

    def test_request_too_large(self):
        assert http_status_from_error({'type': 'request_too_large'}) == 413

    def test_unprocessable_entity(self):
        assert http_status_from_error({'type': 'unprocessable_entity_error'}) == 422

    def test_status_field_preference(self):
        assert http_status_from_error({'type': 'rate_limit_error', 'status': 429}) == 429

    def test_default_500(self):
        assert http_status_from_error({'type': 'unknown'}) == 500
        assert http_status_from_error({}) == 500
        assert http_status_from_error(None) == 500
