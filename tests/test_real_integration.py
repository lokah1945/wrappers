#!/usr/bin/env python3
"""
Real Integration Test — SDK Compatibility Across All Wrappers

This test connects to ACTUAL running wrapper services and verifies:
1. OpenAI SDK compatibility
2. Anthropic SDK compatibility
3. Streaming responses
4. Tool calling
5. Thinking/reasoning modes
6. Error handling

Prerequisites:
  - All 4 wrappers must be running on ports 9101-9104
  - Model registry must be running on port 9200
  - Valid API keys configured in each wrapper

Usage:
  # Run against local wrappers
  python3 tests/test_real_integration.py

  # Run with custom ports
  python3 tests/test_real_integration.py --ports 9101,9102,9103,9104

  # Run specific wrappers
  python3 tests/test_real_integration.py --wrappers nvidia,nous

  # Skip if wrappers not running
  python3 tests/test_real_integration.py --skip-if-unavailable
"""

import asyncio
import json
import sys
import time
import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add project root to path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Try to import real SDKs
try:
    import openai
    HAS_OPENAI_SDK = True
except ImportError:
    HAS_OPENAI_SDK = False
    print("⚠️  OpenAI SDK not installed. Run: pip install openai")

try:
    import anthropic
    HAS_ANTHROPIC_SDK = True
except ImportError:
    HAS_ANTHROPIC_SDK = False
    print("⚠️  Anthropic SDK not installed. Run: pip install anthropic")

import aiohttp


# ============================================================================
# Configuration
# ============================================================================

@dataclass
class WrapperEndpoint:
    """Wrapper endpoint configuration."""
    name: str
    port: int
    base_url: str
    
    @property
    def openai_base_url(self) -> str:
        return f"{self.base_url}/v1"
    
    @property
    def health_url(self) -> str:
        return f"{self.base_url}/health"


# Default wrapper endpoints
DEFAULT_WRAPPERS = [
    WrapperEndpoint("nvidia-python", 9101, "http://127.0.0.1:9101"),
    WrapperEndpoint("nous", 9102, "http://127.0.0.1:9102"),
    WrapperEndpoint("opencode", 9103, "http://127.0.0.1:9103"),
    WrapperEndpoint("blackbox", 9104, "http://127.0.0.1:9104"),
]


# ============================================================================
# Test Cases
# ============================================================================

@dataclass
class IntegrationTestResult:
    """Result of a single test."""
    wrapper: str
    test_name: str
    success: bool
    message: str = ""
    duration_ms: float = 0
    
    def __str__(self):
        status = "✅ PASS" if self.success else "❌ FAIL"
        msg = f": {self.message}" if self.message else ""
        return f"{status} [{self.wrapper}] {self.test_name}{msg} ({self.duration_ms:.1f}ms)"


class RealIntegrationTestSuite:
    """Real integration test suite."""
    
    def __init__(self, wrappers: List[WrapperEndpoint], auth_token: str = "test-token"):
        self.wrappers = wrappers
        self.auth_token = auth_token
        self.results: List[IntegrationTestResult] = []
        self.cross_wrapper_bugs: Dict[str, List[str]] = {}
    
    async def check_wrapper_health(self, wrapper: WrapperEndpoint) -> bool:
        """Check if wrapper is healthy and running."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(wrapper.health_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    return resp.status == 200
        except Exception:
            return False
    
    async def test_openai_chat_completions(self, wrapper: WrapperEndpoint, streaming: bool = False, with_thinking: bool = False) -> IntegrationTestResult:
        """Test OpenAI SDK chat completions."""
        start_time = time.time()
        test_name = f"openai_chat_{'stream' if streaming else 'sync'}{'_thinking' if with_thinking else ''}"
        
        if not HAS_OPENAI_SDK:
            return IntegrationTestResult(wrapper.name, test_name, False, "OpenAI SDK not installed", 0)
        
        try:
            client = openai.AsyncOpenAI(
                base_url=wrapper.openai_base_url,
                api_key=self.auth_token,
            )
            
            extra_body = {}
            if with_thinking:
                extra_body["chat_template_kwargs"] = {"thinking": True}
            
            response = await client.chat.completions.create(
                model="test-model",
                messages=[{"role": "user", "content": "Hello"}],
                stream=streaming,
                extra_body=extra_body if extra_body else None,
            )
            
            if streaming:
                chunks = []
                async for chunk in response:
                    chunks.append(chunk)
                
                if not chunks:
                    return IntegrationTestResult(wrapper.name, test_name, False, "Empty stream", (time.time() - start_time) * 1000)
                
                # Check final chunk
                last_chunk = chunks[-1]
                if not last_chunk.choices or not last_chunk.choices[0].finish_reason:
                    return IntegrationTestResult(wrapper.name, test_name, False, "Stream missing finish_reason", (time.time() - start_time) * 1000)
            else:
                if not response.choices:
                    return IntegrationTestResult(wrapper.name, test_name, False, "Empty response", (time.time() - start_time) * 1000)
                
                if not response.choices[0].message.content:
                    return IntegrationTestResult(wrapper.name, test_name, False, "Empty message content", (time.time() - start_time) * 1000)
            
            duration_ms = (time.time() - start_time) * 1000
            return IntegrationTestResult(wrapper.name, test_name, True, "OK", duration_ms)
        
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            return IntegrationTestResult(wrapper.name, test_name, False, f"Exception: {e}", duration_ms)
    
    async def test_anthropic_messages(self, wrapper: WrapperEndpoint, streaming: bool = False, with_thinking: bool = False) -> IntegrationTestResult:
        """Test Anthropic SDK messages."""
        start_time = time.time()
        test_name = f"anthropic_{'stream' if streaming else 'sync'}{'_thinking' if with_thinking else ''}"
        
        if not HAS_ANTHROPIC_SDK:
            return IntegrationTestResult(wrapper.name, test_name, False, "Anthropic SDK not installed", 0)
        
        try:
            client = anthropic.AsyncAnthropic(
                base_url=wrapper.base_url,
                api_key=self.auth_token,
            )
            
            kwargs = {
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": streaming,
            }
            
            if with_thinking:
                kwargs["thinking"] = {"type": "enabled", "budget_tokens": 1024}
            
            response = await client.messages.create(**kwargs)
            
            if streaming:
                events = []
                async for event in response:
                    events.append(event)
                
                if not events:
                    return IntegrationTestResult(wrapper.name, test_name, False, "Empty event stream", (time.time() - start_time) * 1000)
                
                # Check for terminal event
                has_terminal = any(hasattr(e, "type") and e.type in ["message_stop"] for e in events)
                if not has_terminal:
                    return IntegrationTestResult(wrapper.name, test_name, False, "Event stream missing terminal event", (time.time() - start_time) * 1000)
            else:
                if not response.content:
                    return IntegrationTestResult(wrapper.name, test_name, False, "Empty content", (time.time() - start_time) * 1000)
                
                if with_thinking:
                    # Check for thinking block
                    has_thinking = any(block.type == "thinking" for block in response.content)
                    if not has_thinking:
                        return IntegrationTestResult(wrapper.name, test_name, False, "Missing thinking block", (time.time() - start_time) * 1000)
            
            duration_ms = (time.time() - start_time) * 1000
            return IntegrationTestResult(wrapper.name, test_name, True, "OK", duration_ms)
        
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            return IntegrationTestResult(wrapper.name, test_name, False, f"Exception: {e}", duration_ms)
    
    async def test_openai_responses(self, wrapper: WrapperEndpoint, streaming: bool = False) -> IntegrationTestResult:
        """Test OpenAI SDK responses API."""
        start_time = time.time()
        test_name = f"openai_responses_{'stream' if streaming else 'sync'}"
        
        if not HAS_OPENAI_SDK:
            return IntegrationTestResult(wrapper.name, test_name, False, "OpenAI SDK not installed", 0)
        
        try:
            client = openai.AsyncOpenAI(
                base_url=wrapper.openai_base_url,
                api_key=self.auth_token,
            )
            
            response = await client.responses.create(
                model="test-model",
                input="Hello",
                stream=streaming,
            )
            
            if streaming:
                events = []
                async for event in response:
                    events.append(event)
                
                if not events:
                    return IntegrationTestResult(wrapper.name, test_name, False, "Empty event stream", (time.time() - start_time) * 1000)
                
                # Check for terminal event
                has_terminal = any(hasattr(e, "type") and e.type in ["response.completed"] for e in events)
                if not has_terminal:
                    return IntegrationTestResult(wrapper.name, test_name, False, "Event stream missing terminal event", (time.time() - start_time) * 1000)
            else:
                if not response.output:
                    return IntegrationTestResult(wrapper.name, test_name, False, "Empty output", (time.time() - start_time) * 1000)
            
            duration_ms = (time.time() - start_time) * 1000
            return IntegrationTestResult(wrapper.name, test_name, True, "OK", duration_ms)
        
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            return IntegrationTestResult(wrapper.name, test_name, False, f"Exception: {e}", duration_ms)
    
    async def test_tool_calling(self, wrapper: WrapperEndpoint) -> IntegrationTestResult:
        """Test tool calling across all SDK types."""
        start_time = time.time()
        test_name = "tool_calling"
        
        if not HAS_OPENAI_SDK:
            return IntegrationTestResult(wrapper.name, test_name, False, "OpenAI SDK not installed", 0)
        
        try:
            client = openai.AsyncOpenAI(
                base_url=wrapper.openai_base_url,
                api_key=self.auth_token,
            )
            
            tools = [{
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather for a location",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "location": {"type": "string"}
                        },
                        "required": ["location"]
                    }
                }
            }]
            
            response = await client.chat.completions.create(
                model="test-model",
                messages=[{"role": "user", "content": "What's the weather in Tokyo?"}],
                tools=tools,
                stream=False,
            )
            
            # Check if response has tool calls
            if response.choices and response.choices[0].message.tool_calls:
                tool_calls = response.choices[0].message.tool_calls
                if len(tool_calls) == 0:
                    return IntegrationTestResult(wrapper.name, test_name, False, "Empty tool calls", (time.time() - start_time) * 1000)
                
                # Verify tool call structure
                tc = tool_calls[0]
                if not tc.function.name:
                    return IntegrationTestResult(wrapper.name, test_name, False, "Tool call missing name", (time.time() - start_time) * 1000)
                
                if not tc.function.arguments:
                    return IntegrationTestResult(wrapper.name, test_name, False, "Tool call missing arguments", (time.time() - start_time) * 1000)
            
            duration_ms = (time.time() - start_time) * 1000
            return IntegrationTestResult(wrapper.name, test_name, True, "OK", duration_ms)
        
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            return IntegrationTestResult(wrapper.name, test_name, False, f"Exception: {e}", duration_ms)
    
    async def test_error_handling(self, wrapper: WrapperEndpoint) -> IntegrationTestResult:
        """Test error handling for invalid requests."""
        start_time = time.time()
        test_name = "error_handling"
        
        if not HAS_OPENAI_SDK:
            return IntegrationTestResult(wrapper.name, test_name, False, "OpenAI SDK not installed", 0)
        
        try:
            client = openai.AsyncOpenAI(
                base_url=wrapper.openai_base_url,
                api_key=self.auth_token,
            )
            
            # Test invalid model
            try:
                response = await client.chat.completions.create(
                    model="invalid-model-that-does-not-exist",
                    messages=[{"role": "user", "content": "Hello"}],
                )
                return IntegrationTestResult(wrapper.name, test_name, False, "Should have raised error for invalid model", (time.time() - start_time) * 1000)
            except openai.APIError:
                # Expected
                pass
            except Exception as e:
                return IntegrationTestResult(wrapper.name, test_name, False, f"Wrong exception type: {e}", (time.time() - start_time) * 1000)
            
            # Test empty messages
            try:
                response = await client.chat.completions.create(
                    model="test-model",
                    messages=[],
                )
                return IntegrationTestResult(wrapper.name, test_name, False, "Should have raised error for empty messages", (time.time() - start_time) * 1000)
            except openai.APIError:
                # Expected
                pass
            except Exception as e:
                return IntegrationTestResult(wrapper.name, test_name, False, f"Wrong exception type: {e}", (time.time() - start_time) * 1000)
            
            duration_ms = (time.time() - start_time) * 1000
            return IntegrationTestResult(wrapper.name, test_name, True, "OK", duration_ms)
        
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            return IntegrationTestResult(wrapper.name, test_name, False, f"Exception: {e}", duration_ms)
    
    async def run_all_tests(self, skip_if_unavailable: bool = False) -> List[IntegrationTestResult]:
        """Run all tests across all wrappers."""
        print("\n" + "=" * 70)
        print("REAL INTEGRATION TEST SUITE")
        print("=" * 70)
        
        # Check wrapper health
        available_wrappers = []
        for wrapper in self.wrappers:
            healthy = await self.check_wrapper_health(wrapper)
            if healthy:
                print(f"✅ {wrapper.name} is healthy")
                available_wrappers.append(wrapper)
            else:
                print(f"⚠️  {wrapper.name} is not available")
                if not skip_if_unavailable:
                    print(f"   Use --skip-if-unavailable to skip unavailable wrappers")
                    return []
        
        if not available_wrappers:
            print("\n❌ No wrappers available for testing")
            return []
        
        print(f"\nRunning tests on {len(available_wrappers)} wrappers...\n")
        
        for wrapper in available_wrappers:
            print(f"\n--- Testing {wrapper.name} ---\n")
            
            # OpenAI Chat Completions
            for streaming in [False, True]:
                for with_thinking in [False, True]:
                    result = await self.test_openai_chat_completions(wrapper, streaming, with_thinking)
                    self.results.append(result)
                    print(result)
            
            # Anthropic Messages
            for streaming in [False, True]:
                for with_thinking in [False, True]:
                    result = await self.test_anthropic_messages(wrapper, streaming, with_thinking)
                    self.results.append(result)
                    print(result)
            
            # OpenAI Responses
            for streaming in [False, True]:
                result = await self.test_openai_responses(wrapper, streaming)
                self.results.append(result)
                print(result)
            
            # Tool Calling
            result = await self.test_tool_calling(wrapper)
            self.results.append(result)
            print(result)
            
            # Error Handling
            result = await self.test_error_handling(wrapper)
            self.results.append(result)
            print(result)
        
        return self.results
    
    def check_cross_wrapper_bugs(self):
        """Check for cross-wrapper bugs."""
        print("\n" + "=" * 70)
        print("CROSS-WRAPPER BUG CHECK")
        print("=" * 70)
        
        # Group failures by test type (excluding wrapper)
        failures_by_type: Dict[str, List[str]] = {}
        
        for result in self.results:
            if not result.success:
                # Create a type key without wrapper name
                type_key = result.test_name
                
                if type_key not in failures_by_type:
                    failures_by_type[type_key] = []
                failures_by_type[type_key].append(result.wrapper)
        
        # Check if same bug affects multiple wrappers
        for type_key, wrappers in failures_by_type.items():
            if len(wrappers) > 1:
                print(f"\n⚠️  CROSS-WRAPPER BUG: {type_key}")
                print(f"   Affected wrappers: {', '.join(wrappers)}")
                print(f"   ACTION: Fix in ALL wrappers, not just one!")
                self.cross_wrapper_bugs[type_key] = wrappers
            elif len(wrappers) == 1:
                print(f"\nℹ️  SINGLE-WRAPPER BUG: {type_key}")
                print(f"   Affected wrapper: {wrappers[0]}")
                print(f"   ACTION: Check other wrappers for similar bug")
    
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
        if total > 0:
            print(f"Pass rate:    {passed/total*100:.1f}%")
        print("=" * 70)
        
        if failed > 0:
            print("\nFailed tests:")
            for result in self.results:
                if not result.success:
                    print(f"  ❌ [{result.wrapper}] {result.test_name}: {result.message}")
        
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

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Real Integration Test Suite")
    parser.add_argument("--ports", type=str, default="9101,9102,9103,9104",
                       help="Comma-separated list of wrapper ports")
    parser.add_argument("--wrappers", type=str, default=None,
                       help="Comma-separated list of wrapper names to test")
    parser.add_argument("--skip-if-unavailable", action="store_true",
                       help="Skip wrappers that are not running")
    parser.add_argument("--auth-token", type=str, default="test-token",
                       help="Auth token for wrapper access")
    return parser.parse_args()


async def main():
    """Run the test suite."""
    args = parse_args()
    
    # Parse ports
    ports = [int(p.strip()) for p in args.ports.split(",")]
    
    # Build wrapper list
    if args.wrappers:
        wrapper_names = [w.strip() for w in args.wrappers.split(",")]
        wrappers = []
        for name in wrapper_names:
            for default_wrapper in DEFAULT_WRAPPERS:
                if default_wrapper.name == name:
                    wrappers.append(default_wrapper)
                    break
    else:
        wrappers = DEFAULT_WRAPPERS[:len(ports)]
        for i, wrapper in enumerate(wrappers):
            wrapper.port = ports[i]
            wrapper.base_url = f"http://127.0.0.1:{ports[i]}"
    
    # Run tests
    suite = RealIntegrationTestSuite(wrappers, args.auth_token)
    await suite.run_all_tests(skip_if_unavailable=args.skip_if_unavailable)
    suite.check_cross_wrapper_bugs()
    suite.print_summary()
    
    # Exit with error code if any tests failed
    failed = sum(1 for r in suite.results if not r.success)
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
