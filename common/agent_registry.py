#!/usr/bin/env python3
"""Agent Registry — universal agent compatibility layer.

This module is INTENTIONALLY advisory-only. It NEVER blocks, gates, or
modifies requests based on agent identity. Every AI client/agent (Claude
Code, Codex, OpenCode, Hermes, OpenClaw, Cursor, Windsurf, Cline, Aider,
Goose, Gemini CLI, Continue, Roo Code, or any unknown agent) is allowed
to use the wrapper. The registry only labels the agent for logging and
metrics — it does not enforce any allowlist or denylist.
"""

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
    "unknown": {"protocols": ["openai", "anthropic"], "streaming": True, "tools": True},
}

CAPABILITIES = {
    "streaming": ["openai", "anthropic"],
    "structured_output": ["openai"],
    "multimodal": ["openai", "gemini"],
    "reasoning": ["openai", "anthropic", "gemini"],
}


class AgentRegistry:
    """Detect the calling agent from request headers.

    Detection is based on User-Agent substring matching. Unknown agents
    return 'unknown' and are STILL served — the wrapper is universally
    compatible and does not gate on agent identity.
    """

    def detect_agent(self, headers: dict, user_agent: str = "") -> str:
        ua = (user_agent or "").lower()
        # Also peek at common client-identifying headers for SDKs that don't
        # set a distinctive User-Agent (e.g. openai-python, anthropic-python).
        x_lib = (headers or {}).get('x-stainless-lang', '') or (headers or {}).get('x-library', '')
        x_lib = x_lib.lower()

        if "codex" in ua:
            return "codex"
        if "claude" in ua or "claude" in x_lib or "anthropic" in x_lib:
            return "claude_code"
        if "openclaw" in ua:
            return "openclaw"
        if "hermes" in ua:
            return "hermes"
        if "opencode" in ua:
            return "opencode"
        if "cursor" in ua:
            return "cursor"
        if "windsurf" in ua:
            return "windsurf"
        if "cline" in ua or "cline" in x_lib:
            return "cline"
        if "continue" in ua or "continue" in x_lib:
            return "continue"
        if "roo" in ua or "roo_code" in ua:
            return "roo_code"
        if "aider" in ua:
            return "aider"
        if "goose" in ua:
            return "goose"
        if "gemini" in ua or "gemini" in x_lib:
            return "gemini_cli"
        return "unknown"

    def get_capabilities(self, agent: str) -> dict:
        """Return capability dict for the agent. Unknown agents get a permissive default."""
        return AGENTS.get(agent, AGENTS["unknown"])
