#!/usr/bin/env python3
"""Tests for reasoning extraction (think tags) — critical for Claude Code."""

from src.anthropic_compat import extract_internal_reasoning


class TestExtractInternalReasoning:
    def test_reasoning_content_field(self):
        msg = {"content": "answer", "reasoning_content": "thoughts"}
        r = extract_internal_reasoning(msg)
        assert r["reasoning"] == "thoughts"
        assert r["content"] == "answer"

    def test_think_tags(self):
        msg = {"content": "<think>step by step</think>\nFinal"}
        r = extract_internal_reasoning(msg)
        assert r["reasoning"] == "step by step"
        assert r["content"] == "Final"

    def test_thinking_tags(self):
        msg = {"content": "<thinking>plan</thinking>\nDone"}
        r = extract_internal_reasoning(msg)
        assert r["reasoning"] == "plan"
        assert r["content"] == "Done"

    def test_no_reasoning(self):
        msg = {"content": "plain answer"}
        r = extract_internal_reasoning(msg)
        assert r["reasoning"] == ""
        assert r["content"] == "plain answer"

    def test_list_content(self):
        msg = {"content": [{"type": "text", "text": "hi"}], "reasoning": "r"}
        r = extract_internal_reasoning(msg)
        assert r["reasoning"] == "r"
        assert r["content"] == "hi"
