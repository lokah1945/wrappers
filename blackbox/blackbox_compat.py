"""
blackbox_compat.py — Blackbox AI Native Format ⇄ OpenAI Chat Completions translation
for wrapper-blackbox. Handles Blackbox-specific request/response formats.

Blackbox AI uses OpenAI-compatible format for /chat/completions but has some
differences in model naming and additional features like web search.
"""
import json
from typing import Optional, Dict, Any, List


# ── Model ID Mapping ─────────────────────────────────────────────────────

# Blackbox model IDs that are known to be free
FREE_MODEL_HINTS = [
    "blackboxai/",
    "gemini-",
    "blackbox-search",
    "claude-3-haiku",
    "gpt-3.5-turbo",
    "llama-3",
    "mistral-",
    "mixtral-",
]

# Model ID normalization: Blackbox format → Internal format
def normalize_model_id(model: str) -> str:
    """Normalize Blackbox model ID to internal format."""
    # Already in provider/model format
    if "/" in model and not model.startswith("blackboxai/"):
        return model
    # Blackbox native models
    if model.startswith("blackboxai/"):
        return model
    # Known short names → full Blackbox IDs
    mapping = {
        "gpt-5.5": "blackboxai/openai/gpt-5.5",
        "gpt-5": "blackboxai/openai/gpt-5",
        "gpt-4o": "blackboxai/openai/gpt-4o",
        "gpt-4": "blackboxai/openai/gpt-4",
        "gemini-2.0-flash": "gemini-2.0-flash-001",
        "gemini-1.5-pro": "gemini-1.5-pro-001",
        "gemini-1.5-flash": "gemini-1.5-flash-001",
        "claude-3.5-sonnet": "blackboxai/anthropic/claude-3.5-sonnet",
        "claude-3-opus": "blackboxai/anthropic/claude-3-opus",
        "claude-3-haiku": "blackboxai/anthropic/claude-3-haiku",
        "llama-3.1-70b": "blackboxai/meta/llama-3.1-70b",
        "llama-3.1-8b": "blackboxai/meta/llama-3.1-8b",
        "mistral-large": "blackboxai/mistral/mistral-large",
        "mixtral-8x7b": "blackboxai/mistral/mixtral-8x7b",
        "blackbox-search": "blackbox-search",
    }
    return mapping.get(model, model)


def is_free_model(model: str) -> bool:
    """Check if a model is likely free based on known patterns."""
    model_lower = model.lower()
    return any(hint in model_lower for hint in FREE_MODEL_HINTS)


# ── Request Translation ──────────────────────────────────────────────────

def blackbox_to_openai(body: dict) -> dict:
    """Convert Blackbox native request to OpenAI format (mostly pass-through)."""
    oai = dict(body)
    
    # Normalize model ID
    if "model" in oai:
        oai["model"] = normalize_model_id(oai["model"])
    
    # Ensure stream_options for streaming
    if oai.get("stream") and "stream_options" not in oai:
        oai["stream_options"] = {"include_usage": True}
    
    return oai


def openai_to_blackbox(body: dict) -> dict:
    """Convert OpenAI request to Blackbox native format (mostly pass-through)."""
    # Blackbox accepts OpenAI format directly
    return dict(body)


# ── Response Translation ─────────────────────────────────────────────────

def openai_to_blackbox_response(resp: dict, model: str) -> dict:
    """Convert OpenAI response to Blackbox native format (mostly pass-through)."""
    # Blackbox returns OpenAI-compatible format
    return resp


# ── Web Search Handling ──────────────────────────────────────────────────

def is_web_search_model(model: str) -> bool:
    """Check if model is the web search model."""
    return model in ("blackbox-search", "blackboxai/search")


def extract_citations(response: dict) -> List[Dict[str, Any]]:
    """Extract citations from Blackbox web search response."""
    citations = []
    choices = response.get("choices", [])
    for choice in choices:
        message = choice.get("message", {})
        # Blackbox may include citations in annotations or tool_calls
        annotations = message.get("annotations", [])
        for ann in annotations:
            if ann.get("type") == "url_citation":
                citations.append({
                    "url": ann.get("url_citation", {}).get("url"),
                    "title": ann.get("url_citation", {}).get("title"),
                    "start_index": ann.get("url_citation", {}).get("start_index"),
                    "end_index": ann.get("url_citation", {}).get("end_index"),
                })
    return citations


# ── Tool Calling Support ─────────────────────────────────────────────────

def prepare_tools_for_blackbox(tools: List[dict]) -> List[dict]:
    """Prepare tools for Blackbox (OpenAI format works directly)."""
    # Blackbox uses OpenAI function calling format
    return tools


# ── Streaming Helpers ────────────────────────────────────────────────────

async def stream_blackbox_to_openai(resp, model: str, capture: dict):
    """Convert Blackbox SSE stream to OpenAI SSE stream (pass-through with capture)."""
    # Blackbox streams OpenAI-compatible SSE
    async for line in resp.aiter_lines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            yield f"data: [DONE]\n\n".encode()
            break
        try:
            chunk = json.loads(data)
            if chunk.get("usage"):
                capture["usage"] = chunk["usage"]
            # Pass through as-is
            yield f"data: {json.dumps(chunk)}\n\n".encode()
        except Exception:
            continue


# ── Error Handling ──────────────────────────────────────────────────────

def parse_blackbox_error(response: dict) -> tuple:
    """Parse Blackbox error response. Returns (status_code, error_message, error_type)."""
    error = response.get("error", {})
    message = error.get("message", "Unknown error")
    error_type = error.get("type", "api_error")
    status_code = response.get("status_code", 500)
    
    # Map error types
    if "rate limit" in message.lower() or "quota" in message.lower():
        error_type = "rate_limit_error"
        status_code = 429
    elif "invalid" in message.lower() or "bad request" in message.lower():
        error_type = "invalid_request_error"
        status_code = 400
    elif "unauthorized" in message.lower() or "authentication" in message.lower():
        error_type = "authentication_error"
        status_code = 401
    elif "not found" in message.lower():
        error_type = "not_found_error"
        status_code = 404
    
    return status_code, message, error_type


def is_rate_limit_error(response: dict) -> bool:
    """Check if response indicates rate limiting."""
    status_code, _, error_type = parse_blackbox_error(response)
    return status_code == 429 or error_type == "rate_limit_error"


def extract_retry_after(response: dict, headers: dict) -> Optional[int]:
    """Extract retry-after from response headers or body."""
    # Check headers first
    retry_after = headers.get("retry-after") or headers.get("Retry-After")
    if retry_after:
        try:
            return int(retry_after)
        except ValueError:
            pass
    
    # Check response body for retry info
    error = response.get("error", {})
    if isinstance(error, dict):
        retry_info = error.get("retry_after") or error.get("retry_after_seconds")
        if retry_info:
            try:
                return int(retry_info)
            except (ValueError, TypeError):
                pass
    
    return None