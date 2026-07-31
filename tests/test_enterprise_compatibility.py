#!/usr/bin/env python3
"""Integration tests covering agent compatibility, protocol translation, key routing."""
import pytest, asyncio
from common.agent_registry import AgentRegistry
from common.key_intelligence_engine import KeyRouter
from common.protocol_translation_engine import ProtocolTranslator

def test_agent_registry_detect():
    reg = AgentRegistry()
    assert reg.detect_agent({"user-agent":"codex/1.0"}, "codex/1.0") == "codex"

def test_key_router_never_blocks():
    router = KeyRouter()
    router.add_key("test-key")
    # Phase 7.1 fix: remove tautological assertion 'or True'
    assert router.select_key() is not None

def test_protocol_translation():
    pt = ProtocolTranslator()
    result = pt.translate_request({"model":"x"}, "openai", "anthropic")
    assert result["translated"] is True
