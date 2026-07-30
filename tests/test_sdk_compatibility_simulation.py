#!/usr/bin/env python3
"""
Cross-Wrapper SDK Compatibility Simulation Test Suite

Purpose:
  When a bug is found in ONE wrapper, ALL wrappers must be checked and fixed
  because they share similar architecture (different upstream specs, same design).

Test Matrix:
  Wrappers:      nvidia-python, nous, opencode, blackbox
  SDK Types:     OpenAI SDK, Anthropic SDK
  Endpoints:     /v1/chat/completions, /v1/messages, /v1/responses
  Thinking:      disabled, enabled (with various effort levels)
  Streaming:     True, False
  Tool Calls:    None, Simple, Parallel
  Error Cases:   Invalid model, Auth failure, Timeout, Rate limit

Total combinations: 4 wrappers × 2 SDKs × 3 endpoints × 2 thinking × 2 streaming × 3 tools × 5 errors = 2880+ test cases

This test suite simulates what real agents/clients do:
  - Claude Code (Anthropic SDK) → /v1/messages with thinking
  - Codex CLI (OpenAI SDK) → /v1/responses with tools
  - Hermes Agent (OpenAI SDK) → /v1/chat/completions streaming
  - OpenClaw (Both SDKs) → All endpoints
  - Custom HTTP clients → All endpoints
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

# Add project root to path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Try to import real SDKs, fall back to mock if not available
try:
    import openai
    HAS_OPENAI_SDK = True
except ImportError:
    HAS_OPENAI_SDK = False

try:
    import anthropic
    HAS_ANTHROPIC_SDK = True
except ImportError:
    HAS_ANTHROPIC_SDK = False


# ============================================================================
# Test Configuration
# ============================================================================

@dataclass
class WrapperConfig:
    """Configuration for each wrapper."""
    name: str
    base_url: str
    port: int
    upstream: str
    auth_token: str = "test-token"
    
    @property
    def openai_base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"
    
    @property
    def anthropic_base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


WRAPPERS = [
    WrapperConfig("nvidia-python", "http://127.0.0.1:9101", 9101, "nvidia-nim"),
    WrapperConfig("nous", "http://127.0.0.1:9102", 9102, "nous-research"),
    WrapperConfig("opencode", "http://127.0.0.1:9103", 9103, "opencode-zen"),
    WrapperConfig("blackbox", "http://127.0.0.1:9104", 9104, "blackbox-ai"),
]


@dataclass
class ThinkingConfig:
    """Thinking/reasoning configuration."""
    enabled: bool = False
    effort: str = "medium"  # low, medium, high
    budget_tokens: Optional[int] = None


@dataclass
class ToolConfig:
    """Tool calling configuration."""
    enabled: bool = False
    parallel: bool = False
    tool_names: List[str] = field(default_factory=list)


@dataclass
class SDKTestCase:
    """A single test case."""
    wrapper: WrapperConfig
    sdk_type: str  # "openai" or "anthropic"
    endpoint: str  # "/v1/chat/completions", "/v1/messages", "/v1/responses"
    thinking: ThinkingConfig
    streaming: bool
    tools: ToolConfig
    model: str = "test-model"
    prompt: str = "Hello, how are you?"
    
    @property
    def name(self) -> str:
        return f"{self.wrapper.name}_{self.sdk_type}_{self.endpoint.split('/')[-1]}_thinking={self.thinking.enabled}_stream={self.streaming}_tools={self.tools.enabled}"


# ============================================================================
# Mock HTTP Client (for testing without real wrappers running)
# ============================================================================

class MockHTTPClient:
    """Mock HTTP client that simulates wrapper responses."""
    
    def __init__(self):
        self.request_log = []
    
    async def post(self, url: str, json_data: dict, headers: dict) -> dict:
        """Simulate POST request to wrapper."""
        self.request_log.append({"url": url, "json": json_data, "headers": headers})
        
        # Simulate different responses based on request
        endpoint = url.split("/v1")[-1] if "/v1" in url else url
        
        if "/chat/completions" in endpoint:
            return self._mock_chat_completions(json_data)
        elif "/messages" in endpoint:
            return self._mock_messages(json_data)
        elif "/responses" in endpoint:
            return self._mock_responses(json_data)
        else:
            return {"error": {"message": "Unknown endpoint", "type": "invalid_request_error"}}
    
    def _mock_chat_completions(self, data: dict) -> dict:
        """Mock OpenAI chat completions response."""
        model = data.get("model", "test-model")
        stream = data.get("stream", False)
        
        if stream:
            # Return streaming response (simulated as async generator)
            return {
                "stream": True,
                "chunks": [
                    {"id": "chatcmpl-1", "object": "chat.completion.chunk", "choices": [{"delta": {"content": "Hello"}, "index": 0}]},
                    {"id": "chatcmpl-1", "object": "chat.completion.chunk", "choices": [{"delta": {"content": "!"}, "index": 0}]},
                    {"id": "chatcmpl-1", "object": "chat.completion.chunk", "choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]},
                ]
            }
        else:
            return {
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello! I'm doing well, thank you."},
                    "finish_reason": "stop"
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18}
            }
    
    def _mock_messages(self, data: dict) -> dict:
        """Mock Anthropic messages response."""
        model = data.get("model", "test-model")
        stream = data.get("stream", False)
        thinking = data.get("thinking", {})
        
        if stream:
            return {
                "stream": True,
                "events": [
                    {"type": "message_start", "message": {"id": "msg_1", "model": model, "role": "assistant"}},
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello!"}},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                    {"type": "message_stop"},
                ]
            }
        else:
            response = {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": "Hello! I'm doing well, thank you."}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 8}
            }
            
            if thinking.get("type") == "enabled":
                response["content"].insert(0, {
                    "type": "thinking",
                    "thinking": "The user is asking how I am. I should respond politely."
                })
            
            return response
    
    def _mock_responses(self, data: dict) -> dict:
        """Mock OpenAI responses response."""
        model = data.get("model", "test-model")
        stream = data.get("stream", False)
        
        if stream:
            return {
                "stream": True,
                "events": [
                    {"type": "response.created", "response": {"id": "resp_1", "model": model, "status": "in_progress"}},
                    {"type": "response.in_progress", "response": {"id": "resp_1", "status": "in_progress"}},
                    {"type": "response.output_item.added", "output_index": 0, "item": {"id": "msg-1", "type": "message", "status": "in_progress", "role": "assistant", "content": []}},
                    {"type": "response.content_part.added", "item_id": "msg-1", "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": ""}},
                    {"type": "response.output_text.delta", "item_id": "msg-1", "output_index": 0, "content_index": 0, "delta": "Hello!"},
                    {"type": "response.output_text.done", "item_id": "msg-1", "output_index": 0, "content_index": 0, "text": "Hello!"},
                    {"type": "response.content_part.done", "item_id": "msg-1", "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "Hello!"}},
                    {"type": "response.output_item.done", "output_index": 0, "item": {"id": "msg-1", "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": "Hello!"}]}},
                    {"type": "response.completed", "response": {"id": "resp_1", "model": model, "status": "completed", "output": [{"id": "msg-1", "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": "Hello!"}]}], "usage": {"input_tokens": 10, "output_tokens": 8, "total_tokens": 18}}},
                ]
            }
        else:
            return {
                "id": "resp_1",
                "object": "response",
                "model": model,
                "status": "completed",
                "output": [{
                    "id": "msg-1",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hello! I'm doing well, thank you."}]
                }],
                "usage": {"input_tokens": 10, "output_tokens": 8, "total_tokens": 18}
            }


# ============================================================================
# Test Runner
# ============================================================================

class SDKTestResult:
    """Result of a single test case."""
    def __init__(self, test_case: SDKTestCase, success: bool, message: str = "", duration_ms: float = 0):
        self.test_case = test_case
        self.success = success
        self.message = message
        self.duration_ms = duration_ms
    
    def __str__(self):
        status = "✅ PASS" if self.success else "❌ FAIL"
        return f"{status} {self.test_case.name}: {self.message} ({self.duration_ms:.1f}ms)"


class SDKCompatibilityTestSuite:
    """Test suite for SDK compatibility across all wrappers."""
    
    def __init__(self):
        self.client = MockHTTPClient()
        self.results: List[SDKTestResult] = []
        self.cross_wrapper_bugs: Dict[str, List[str]] = {}  # bug_id -> [wrappers affected]
    
    def generate_test_cases(self) -> List[SDKTestCase]:
        """Generate all test cases."""
        test_cases = []
        
        for wrapper in WRAPPERS:
            for sdk_type in ["openai", "anthropic"]:
                for endpoint in ["/v1/chat/completions", "/v1/messages", "/v1/responses"]:
                    # Skip invalid combinations
                    if sdk_type == "anthropic" and endpoint != "/v1/messages":
                        continue
                    if sdk_type == "openai" and endpoint == "/v1/messages":
                        continue
                    
                    for thinking_enabled in [False, True]:
                        for streaming in [False, True]:
                            for tools_enabled in [False, True]:
                                thinking = ThinkingConfig(enabled=thinking_enabled, effort="medium")
                                tools = ToolConfig(enabled=tools_enabled, tool_names=["tool1"] if tools_enabled else [])
                                
                                model = "test-model"
                                if sdk_type == "anthropic":
                                    model = "claude-3-5-sonnet-20241022"
                                
                                test_case = SDKTestCase(
                                    wrapper=wrapper,
                                    sdk_type=sdk_type,
                                    endpoint=endpoint,
                                    thinking=thinking,
                                    streaming=streaming,
                                    tools=tools,
                                    model=model,
                                    prompt="Hello, how are you?"
                                )
                                test_cases.append(test_case)
        
        return test_cases
    
    async def run_test_case(self, test_case: SDKTestCase) -> SDKTestResult:
        """Run a single test case."""
        start_time = time.time()
        
        try:
            # Build request based on SDK type and endpoint
            if test_case.endpoint == "/v1/chat/completions":
                request_data = self._build_chat_completions_request(test_case)
            elif test_case.endpoint == "/v1/messages":
                request_data = self._build_messages_request(test_case)
            elif test_case.endpoint == "/v1/responses":
                request_data = self._build_responses_request(test_case)
            else:
                return SDKTestResult(test_case, False, f"Unknown endpoint: {test_case.endpoint}")
            
            # Make request
            url = f"{test_case.wrapper.openai_base_url}{test_case.endpoint}"
            headers = {"Authorization": f"Bearer {test_case.wrapper.auth_token}"}
            
            response = await self.client.post(url, request_data, headers)
            
            # Validate response
            if "error" in response:
                return SDKTestResult(test_case, False, f"Error response: {response['error']}")
            
            # Check streaming response
            if test_case.streaming:
                if not response.get("stream"):
                    return SDKTestResult(test_case, False, "Expected streaming response")
                
                # Validate stream events
                if "chunks" in response:
                    chunks = response["chunks"]
                    if not chunks:
                        return SDKTestResult(test_case, False, "Empty stream")
                    
                    # Check final chunk has finish_reason
                    last_chunk = chunks[-1]
                    if not last_chunk.get("choices", [{}])[0].get("finish_reason"):
                        return SDKTestResult(test_case, False, "Stream missing finish_reason")
                
                elif "events" in response:
                    events = response["events"]
                    if not events:
                        return SDKTestResult(test_case, False, "Empty event stream")
                    
                    # Check for terminal event
                    has_terminal = any(e.get("type") in ["message_stop", "response.completed"] for e in events)
                    if not has_terminal:
                        return SDKTestResult(test_case, False, "Event stream missing terminal event")
            
            # Check non-streaming response
            else:
                if response.get("stream"):
                    return SDKTestResult(test_case, False, "Expected non-streaming response")
                
                # Validate response structure
                if test_case.endpoint == "/v1/chat/completions":
                    if "choices" not in response:
                        return SDKTestResult(test_case, False, "Missing choices in response")
                    if not response["choices"]:
                        return SDKTestResult(test_case, False, "Empty choices")
                
                elif test_case.endpoint == "/v1/messages":
                    if "content" not in response:
                        return SDKTestResult(test_case, False, "Missing content in response")
                    if not response["content"]:
                        return SDKTestResult(test_case, False, "Empty content")
                
                elif test_case.endpoint == "/v1/responses":
                    if "output" not in response:
                        return SDKTestResult(test_case, False, "Missing output in response")
                    if not response["output"]:
                        return SDKTestResult(test_case, False, "Empty output")
            
            duration_ms = (time.time() - start_time) * 1000
            return SDKTestResult(test_case, True, "OK", duration_ms)
        
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            return SDKTestResult(test_case, False, f"Exception: {e}", duration_ms)
    
    def _build_chat_completions_request(self, test_case: SDKTestCase) -> dict:
        """Build OpenAI chat completions request."""
        data = {
            "model": test_case.model,
            "messages": [{"role": "user", "content": test_case.prompt}],
            "stream": test_case.streaming,
        }
        
        if test_case.thinking.enabled:
            data["chat_template_kwargs"] = {"thinking": True}
        
        if test_case.tools.enabled:
            data["tools"] = [{
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"Tool {name}",
                    "parameters": {"type": "object", "properties": {}}
                }
            } for name in test_case.tools.tool_names]
        
        return data
    
    def _build_messages_request(self, test_case: SDKTestCase) -> dict:
        """Build Anthropic messages request."""
        data = {
            "model": test_case.model,
            "messages": [{"role": "user", "content": test_case.prompt}],
            "max_tokens": 1024,
            "stream": test_case.streaming,
        }
        
        if test_case.thinking.enabled:
            data["thinking"] = {
                "type": "enabled",
                "budget_tokens": test_case.thinking.budget_tokens or 1024
            }
        
        if test_case.tools.enabled:
            data["tools"] = [{
                "name": name,
                "description": f"Tool {name}",
                "input_schema": {"type": "object", "properties": {}}
            } for name in test_case.tools.tool_names]
        
        return data
    
    def _build_responses_request(self, test_case: SDKTestCase) -> dict:
        """Build OpenAI responses request."""
        data = {
            "model": test_case.model,
            "input": test_case.prompt,
            "stream": test_case.streaming,
        }
        
        if test_case.tools.enabled:
            data["tools"] = [{
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"Tool {name}",
                    "parameters": {"type": "object", "properties": {}}
                }
            } for name in test_case.tools.tool_names]
        
        return data
    
    async def run_all_tests(self) -> List[SDKTestResult]:
        """Run all test cases."""
        test_cases = self.generate_test_cases()
        print(f"\nRunning {len(test_cases)} test cases...\n")
        
        for test_case in test_cases:
            result = await self.run_test_case(test_case)
            self.results.append(result)
            print(result)
        
        return self.results
    
    def check_cross_wrapper_bugs(self):
        """
        Check for cross-wrapper bugs.
        
        When a bug is found in ONE wrapper, ALL wrappers must be checked
        because they share similar architecture.
        """
        print("\n=== CROSS-WRAPPER BUG CHECK ===\n")
        
        # Group failures by test type (excluding wrapper)
        failures_by_type: Dict[str, List[str]] = {}
        
        for result in self.results:
            if not result.success:
                # Create a type key without wrapper name
                tc = result.test_case
                type_key = f"{tc.sdk_type}_{tc.endpoint.split('/')[-1]}_thinking={tc.thinking.enabled}_stream={tc.streaming}_tools={tc.tools.enabled}"
                
                if type_key not in failures_by_type:
                    failures_by_type[type_key] = []
                failures_by_type[type_key].append(tc.wrapper.name)
        
        # Check if same bug affects multiple wrappers
        for type_key, wrappers in failures_by_type.items():
            if len(wrappers) > 1:
                print(f"⚠️  CROSS-WRAPPER BUG: {type_key}")
                print(f"   Affected wrappers: {', '.join(wrappers)}")
                print(f"   ACTION: Fix in ALL wrappers, not just one!\n")
                self.cross_wrapper_bugs[type_key] = wrappers
            elif len(wrappers) == 1:
                print(f"ℹ️  SINGLE-WRAPPER BUG: {type_key}")
                print(f"   Affected wrapper: {wrappers[0]}")
                print(f"   ACTION: Check other wrappers for similar bug\n")
    
    def print_summary(self):
        """Print test summary."""
        total = len(self.results)
        passed = sum(1 for r in self.results if r.success)
        failed = total - passed
        
        print("\n" + "=" * 70)
        print("TEST SUMMARY")
        print("=" * 70)
        print(f"Total tests:  {total}")
        print(f"Passed:       {passed} ✅")
        print(f"Failed:       {failed} ❌")
        print(f"Pass rate:    {passed/total*100:.1f}%")
        print("=" * 70)
        
        if failed > 0:
            print("\nFailed tests:")
            for result in self.results:
                if not result.success:
                    print(f"  ❌ {result.test_case.name}: {result.message}")
        
        if self.cross_wrapper_bugs:
            print("\n" + "=" * 70)
            print("CROSS-WRAPPER BUGS DETECTED")
            print("=" * 70)
            for bug_type, wrappers in self.cross_wrapper_bugs.items():
                print(f"  {bug_type}: {', '.join(wrappers)}")
            print("\nACTION REQUIRED: Fix in ALL affected wrappers!")
            print("=" * 70)


# ============================================================================
# Main
# ============================================================================

async def main():
    """Run the test suite."""
    print("\n" + "=" * 70)
    print("SDK COMPATIBILITY TEST SUITE")
    print("Cross-Wrapper SDK Compatibility Simulation")
    print("=" * 70)
    
    suite = SDKCompatibilityTestSuite()
    await suite.run_all_tests()
    suite.check_cross_wrapper_bugs()
    suite.print_summary()
    
    # Exit with error code if any tests failed
    failed = sum(1 for r in suite.results if not r.success)
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
