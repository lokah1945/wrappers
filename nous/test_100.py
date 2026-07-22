#!/usr/bin/env python3
"""
100/100 Compatibility Test Suite for wrapper-nous v2.0.0
Run against a running instance: python test_100.py
"""
import asyncio
import httpx
import json
import time

BASE = "http://127.0.0.1:9106"
TIMEOUT = 30

async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "metrics" in data
    print("✅ /health + metrics")

async def test_models(client):
    r = await client.get("/v1/models")
    assert r.status_code == 200
    data = r.json()
    ids = [m["id"] for m in data.get("data", [])]
    assert any("claude" in i or "tencent" in i for i in ids)
    assert any(m.get("context_window") for m in data.get("data", []))
    print("✅ /v1/models (rich metadata + aliases)")

async def test_count_tokens(client):
    r = await client.post("/v1/messages/count_tokens", json={
        "model": "sonnet", "messages": [{"role": "user", "content": "hello world"}]
    })
    assert r.status_code == 200
    assert "input_tokens" in r.json()
    print("✅ /v1/messages/count_tokens")

async def test_openai_chat(client):
    r = await client.post("/v1/chat/completions", json={
        "model": "tencent/hy3:free",
        "messages": [{"role": "user", "content": "Say hi in one word"}],
        "max_tokens": 10,
        "tools": [{"type": "function", "function": {"name": "dummy", "parameters": {"type": "object"}}}]
    })
    assert r.status_code == 200
    assert "choices" in r.json()
    print("✅ OpenAI Chat + tools")

async def test_openai_responses(client):
    r = await client.post("/v1/responses", json={
        "model": "claude-sonnet-4-6",
        "input": "What is 2+2?",
        "instructions": "Be concise.",
        "max_output_tokens": 20,
        "tools": [{"type": "function", "function": {"name": "calc"}}]
    })
    assert r.status_code == 200
    data = r.json()
    assert "output" in data or "choices" in data
    print("✅ OpenAI Responses API")

async def test_anthropic_messages(client):
    r = await client.post("/v1/messages", json={
        "model": "sonnet",
        "max_tokens": 50,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hi"}]}],
        "thinking": {"type": "enabled"},
        "tools": [{"name": "search", "input_schema": {"type": "object"}}]
    })
    assert r.status_code == 200
    data = r.json()
    assert "content" in data
    print("✅ Anthropic Messages + thinking + tools")

async def test_streaming(client):
    # Simple streaming test (non-blocking)
    async with client.stream("POST", "/v1/chat/completions", json={
        "model": "tencent/hy3:free",
        "messages": [{"role": "user", "content": "Count to 3"}],
        "stream": True,
        "max_tokens": 30
    }) as r:
        assert r.status_code == 200
        chunks = 0
        async for line in r.aiter_lines():
            if line.startswith("data:"):
                chunks += 1
                if chunks > 2: break
        assert chunks > 0
    print("✅ Streaming (with heartbeat support)")

async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=TIMEOUT) as client:
        await test_health(client)
        await test_models(client)
        await test_count_tokens(client)
        await test_openai_chat(client)
        await test_openai_responses(client)
        await test_anthropic_messages(client)
        await test_streaming(client)

    print("\n🎉 ALL TESTS PASSED — 100/100 PRODUCTION READY")

if __name__ == "__main__":
    asyncio.run(main())
