#!/usr/bin/env python3
"""Agent Registry — universal agent compatibility layer."""

from __future__ import annotations

AGENTS = {
    "codex": {"protocols": ["openai"], "streaming": True, "tools": True, "structured_output": True},
    "claude_code": {"protocols": ["anthropic", "openai"], "streaming": True, "tools": True},
    "openclaw": {"protocols": ["openai", "anthropic"], "streaming": True, "tools": True},
    "hermes": {"protocols": ["openai"], "streaming": True},
    "opencode": {"protocols": ["openai"], "streaming": True, "tools": True},
    "cursor": {"protocols": ["openai"], "streaming": True},
    "windsurf": {"protocols": ["openai"], "streaming": True},
    "cline": {"protocols": ["openai"], "streaming": True},
    "continue": {"protocols": ["openai"], "streaming": True},
    "roo_code": {"protocols": ["openai"], "streaming": True, "tools": True},
    "aider": {"protocols": ["openai"], "streaming": False},
    "goose": {"protocols": ["openai"], "streaming": True},
    "gemini_cli": {"protocols": ["openai", "gemini"], "streaming": True, "multimodal": True},
}

CAPABILITIES = {
    "streaming": ["openai", "anthropic"],
    "structured_output": ["openai"],
    "multimodal": ["openai", "gemini"],
    "reasoning": ["openai", "anthropic", "gemini"],
}

class AgentRegistry:
    def detect_agent(self, headers: dict, user_agent: str = "") -> str:
        ua = (user_agent or "").lower()
        if "codex" in ua:
            return "codex"
        if "claude" in ua:
            return "claude_code"
        if "openclaw" in ua:
            return "openclaw"
        if "cursor" in ua:
            return "cursor"
        return "unknown"

    def get_capabilities(self, agent: str) -> dict:
        return AGENTS.get(agent, AGENTS["unknown"] if "unknown" in AGENTS else AGENTS["codex"])
