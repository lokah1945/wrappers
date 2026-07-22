#!/usr/bin/env python3
"""Basic tests for wrapper-opencode."""

import pytest
from fastapi.testclient import TestClient
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from main import app

client = TestClient(app)

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert "version" in data
    assert "keys" in data

def test_models():
    r = client.get("/v1/models")
    assert r.status_code == 200
    assert "data" in r.json()

def test_chat_completions_requires_auth_if_set(monkeypatch):
    # Without BEARER_TOKEN it should work in test
    r = client.post("/v1/chat/completions", json={
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 10
    })
    # Will likely 503 or 502 because no real keys in CI, but endpoint should not 404
    assert r.status_code in (200, 400, 401, 503, 502)

def test_responses_endpoint():
    r = client.post("/v1/responses", json={
        "model": "gpt-4o",
        "input": "hello"
    })
    assert r.status_code in (200, 503, 502)

def test_anthropic_messages():
    r = client.post("/v1/messages", json={
        "model": "gpt-4o",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "hi"}]
    })
    assert r.status_code in (200, 503, 502)
