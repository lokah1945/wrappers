#!/usr/bin/env python3
"""Tests for main.py — server routing and helpers."""

import os
import pytest
from src.main import (
    find_reasoning_config,
    translate_thinking_to_nim,
    apply_default_reasoning,
    request_requires_reasoning,
    guard_stream_unsupported,
    resolve_deprecated_redirect,
    get_deprecated_redirect_info,
    _strip_context_suffix,
    _is_valid_nim_alias_target,
    _norm_alias_key,
    resolve_target_model,
    route_upstream,
    model_from_path,
    generate_request_id,
    client_ip,
    enrich_model_metadata,
    resolve_base,
)


class TestFindReasoningConfig:
    def test_deepseek_v4(self):
        cfg = find_reasoning_config('deepseek-ai/deepseek-v4-pro')
        assert cfg is not None
        assert cfg['mechanism'] == 'chat_template_kwargs'
        assert cfg['requires_reasoning'] is True

    def test_qwen(self):
        cfg = find_reasoning_config('qwen/qwen-3')
        assert cfg is not None
        assert cfg['mechanism'] == 'chat_template_kwargs'

    def test_nemotron_ultra(self):
        cfg = find_reasoning_config('nvidia/nemotron-3-ultra-550b')
        assert cfg is not None
        assert cfg['mechanism'] == 'nemotron_chat_template'
        assert 'force_nonempty_content' in cfg['params']

    def test_generic_nemotron(self):
        cfg = find_reasoning_config('nvidia/nemotron-nano')
        assert cfg is not None
        assert cfg['mechanism'] == 'reasoning_effort'

    def test_no_match(self):
        cfg = find_reasoning_config('some/random-model')
        assert cfg is None

    def test_most_specific_match(self):
        cfg = find_reasoning_config('nvidia/nemotron-3-ultra-550b-a55b')
        assert cfg is not None
        assert cfg['mechanism'] == 'nemotron_chat_template'


class TestTranslateThinkingToNim:
    def test_chat_template_kwargs_enabled(self):
        body = {}
        translate_thinking_to_nim(body, 'deepseek-ai/deepseek-v4-pro', True)
        assert 'chat_template_kwargs' in body
        assert body['chat_template_kwargs']['enable_thinking'] is True

    def test_chat_template_kwargs_disabled(self):
        body = {}
        translate_thinking_to_nim(body, 'deepseek-ai/deepseek-v4-pro', False)
        assert 'chat_template_kwargs' in body
        assert body['chat_template_kwargs']['enable_thinking'] is False

    def test_reasoning_effort_enabled(self):
        body = {}
        translate_thinking_to_nim(body, 'gpt-oss/gpt-oss-20b', True)
        assert body.get('reasoning_effort') == 'high'

    def test_reasoning_effort_disabled(self):
        body = {}
        translate_thinking_to_nim(body, 'gpt-oss/gpt-oss-20b', False)
        assert body.get('reasoning_effort') == 'low'

    def test_nemotron_chat_template(self):
        body = {}
        translate_thinking_to_nim(body, 'nvidia/nemotron-3-ultra-550b', True)
        assert 'chat_template_kwargs' in body
        assert body['chat_template_kwargs']['enable_thinking'] is True
        assert body['chat_template_kwargs']['force_nonempty_content'] is True

    def test_none_thinking(self):
        body = {}
        translate_thinking_to_nim(body, 'deepseek-ai/deepseek-v4-pro', None)
        assert 'chat_template_kwargs' not in body

    def test_unknown_model(self):
        body = {}
        translate_thinking_to_nim(body, 'unknown/model', True)
        assert 'chat_template_kwargs' not in body


class TestApplyDefaultReasoning:
    def test_no_explicit_no_default(self):
        body = {}
        apply_default_reasoning(body, 'some/random-model')
        assert 'chat_template_kwargs' not in body

    def test_requires_reasoning_auto_inject(self):
        body = {}
        apply_default_reasoning(body, 'deepseek-ai/deepseek-v4-pro')
        assert 'chat_template_kwargs' in body
        assert body['chat_template_kwargs']['enable_thinking'] is True

    def test_explicit_prevents_inject(self):
        body = {'temperature': 0.7}
        apply_default_reasoning(body, 'deepseek-ai/deepseek-v4-pro')
        assert 'chat_template_kwargs' in body

    def test_explicit_chat_template_kwargs(self):
        body = {'chat_template_kwargs': {'enable_thinking': False}}
        apply_default_reasoning(body, 'deepseek-ai/deepseek-v4-pro')
        assert body['chat_template_kwargs']['enable_thinking'] is False


class TestRequestRequiresReasoning:
    def test_chat_template_kwargs(self):
        body = {'chat_template_kwargs': {'enable_thinking': True}}
        assert request_requires_reasoning(body, 'nvidia/llama') is True

    def test_reasoning_effort(self):
        body = {'reasoning_effort': 'high'}
        assert request_requires_reasoning(body, 'nvidia/llama') is True

    def test_extra_body_reasoning(self):
        body = {'extra_body': {'reasoning_budget': 1000}}
        assert request_requires_reasoning(body, 'nvidia/llama') is True

    def test_extended_thinking(self):
        body = {'extended_thinking': True}
        assert request_requires_reasoning(body, 'nvidia/llama') is True

    def test_thinking_block(self):
        body = {'thinking': {'type': 'enabled'}}
        assert request_requires_reasoning(body, 'nvidia/llama') is True

    def test_model_is_reasoning(self):
        body = {}
        assert request_requires_reasoning(body, 'deepseek-ai/deepseek-v4-pro') is True

    def test_no_reasoning(self):
        body = {'temperature': 0.7}
        assert request_requires_reasoning(body, 'nvidia/llama') is False


class TestGuardStreamUnsupported:
    def test_chat_model_allows_stream(self):
        body = {'stream': True}
        result = guard_stream_unsupported(body, 'nvidia/llama-3.3-nemotron-super-49b-v1.5')
        assert result is None

    def test_non_streaming(self):
        body = {'stream': False}
        result = guard_stream_unsupported(body, 'nvidia/llama')
        assert result is None

    def test_no_stream_field(self):
        body = {}
        result = guard_stream_unsupported(body, 'nvidia/llama')
        assert result is None


class TestResolveDeprecatedRedirect:
    def test_known_redirect(self):
        result = resolve_deprecated_redirect('minimaxai/minimax-m2.5')
        assert result == 'minimaxai/minimax-m2.7'

    def test_case_insensitive(self):
        result = resolve_deprecated_redirect('MiniMaxAI/MiniMax-M2.5')
        assert result == 'minimaxai/minimax-m2.7'

    def test_no_redirect(self):
        assert resolve_deprecated_redirect('nvidia/llama-3.3-nemotron-super-49b-v1.5') is None

    def test_empty(self):
        assert resolve_deprecated_redirect('') is None
        assert resolve_deprecated_redirect(None) is None


class TestGetDeprecatedRedirectInfo:
    def test_disabled_by_default(self):
        os.environ.pop('DEPRECATED_MODEL_REDIRECT_ERROR', None)
        assert get_deprecated_redirect_info('minimaxai/minimax-m2.5') is None

    def test_enabled(self):
        os.environ['DEPRECATED_MODEL_REDIRECT_ERROR'] = '1'
        result = get_deprecated_redirect_info('minimaxai/minimax-m2.5')
        assert result is not None
        assert result['from'] == 'minimaxai/minimax-m2.5'
        assert result['to'] == 'minimaxai/minimax-m2.7'
        os.environ.pop('DEPRECATED_MODEL_REDIRECT_ERROR', None)


class TestStripContextSuffix:
    def test_strips_suffix(self):
        assert _strip_context_suffix('nvidia/llama [1m]') == 'nvidia/llama'

    def test_no_suffix(self):
        assert _strip_context_suffix('nvidia/llama') == 'nvidia/llama'

    def test_empty(self):
        assert _strip_context_suffix('') is None or _strip_context_suffix('') == ''


class TestIsValidNimAliasTarget:
    def test_valid(self):
        assert _is_valid_nim_alias_target('nvidia/llama-3.3') is True

    def test_invalid_colon(self):
        assert _is_valid_nim_alias_target('tencent/hy3:free') is False

    def test_invalid_no_slash(self):
        assert _is_valid_nim_alias_target('llama') is False

    def test_empty(self):
        assert _is_valid_nim_alias_target('') is False
        assert _is_valid_nim_alias_target(None) is False


class TestNormAliasKey:
    def test_normalizes(self):
        assert _norm_alias_key('  Haiku  ') == 'haiku'

    def test_empty(self):
        assert _norm_alias_key('') == ''
        assert _norm_alias_key(None) == ''


class TestRouteUpstream:
    def test_chat_routes_to_llm(self):
        assert route_upstream('/v1/chat/completions') == 'https://integrate.api.nvidia.com'

    def test_images_routes_to_genai(self):
        assert route_upstream('/v1/images/generations') == 'https://ai.api.nvidia.com'

    def test_ranking_routes_to_genai(self):
        assert route_upstream('/v1/ranking') == 'https://ai.api.nvidia.com'

    def test_infer_routes_to_genai(self):
        assert route_upstream('/v1/infer') == 'https://ai.api.nvidia.com'


class TestModelFromPath:
    def test_single_segment(self):
        assert model_from_path('v1/models') == 'models'

    def test_multi_segment(self):
        assert model_from_path('v1/models/nvidia/llama') == 'llama'

    def test_empty(self):
        assert model_from_path('') == ''


class TestGenerateRequestId:
    def test_format(self):
        rid = generate_request_id()
        assert rid.startswith('req_')
        assert len(rid) > 5

    def test_uniqueness(self):
        rids = {generate_request_id() for _ in range(100)}
        assert len(rids) == 100


class TestEnrichModelMetadata:
    def test_enrichment(self):
        desc = {'id': 'nvidia/llama', 'type': 'chat', 'context_window': 4096}
        status = {'nvidia/llama': {'ok': True, 'last_status': 200, 'reason': 'ok', 'verified': True}}
        result = enrich_model_metadata('nvidia/llama', desc, status)
        assert result['last_status'] == 200
        assert result['ok'] is True
        assert result['verified'] is True

    def test_no_status(self):
        desc = {'id': 'nvidia/llama', 'type': 'chat'}
        result = enrich_model_metadata('nvidia/llama', desc, {})
        assert result['ok'] is True
        assert result['last_status'] == 0


class TestResolveBase:
    def test_chat_model(self):
        assert resolve_base('/v1/chat/completions') == 'https://integrate.api.nvidia.com'

    def test_image_model(self):
        assert resolve_base('/v1/images/generations') == 'https://ai.api.nvidia.com'
