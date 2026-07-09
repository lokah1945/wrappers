#!/bin/bash

# Test exactly what Claude Code does - including auth headers
# Kill any running services first
pkill -f "node src/index.js" 2>/dev/null || true
pkill -f "claude.exe" 2>/dev/null || true
sleep 2

# Clear .env BEARER_TOKEN for Claude Code (this is how Claude Code typically works)
export BEARER_TOKEN=

# Start server with HOT RELOAD capability (should detect .env changes)
echo "Starting server..."
node src/index.js &
SERVER_PID=$!

# Wait for startup
sleep 5

# Check if server is running
if ! kill -0 $SERVER_PID 2>/dev/null; then
    echo "❌ Server failed to start"
    exit 1
fi

echo "✅ Server started successfully"

# Test 1: Exact Claude Code authentication flow
# Claude Code typically uses x-api-key and anthropic headers
TEST1_RESPONSE=$(curl -s -w "%{http_code}" -o /tmp/test1_body.txt -H "Content-Type: application/json" \
  -H "x-api-key: claude-auth-token" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: some-beta" \
  -X POST http://localhost:9910/v1/messages \
  -d '{"model":"llama-3.1-8b-instruct","messages":[{"role":"user","content":"hello"}],"max_tokens":50}')

echo "Test 1 - Claude Code Auth:"
echo "Status Code: $TEST1_RESPONSE"
echo "Body Preview:"
cat /tmp/test1_body.txt | head -20
echo ""

# Test 2: Multiple rapid requests (to test connection handling)
echo "=== Testing multiple rapid requests ==="
for i in {1..5}; do
    TEST_RESPONSE=$(curl -s -w "%{http_code}" -o /tmp/test_body_$i.txt -H "Content-Type: application/json" \
        -H "x-api-key: claude-auth-token" \
        -H "anthropic-version: 2023-06-01" \
        -X POST http://localhost:9910/v1/messages \
        -d "{\"model\": \"llama-3.1-8b-instruct\",\"messages\":[{\"role\": \"user\",\"content\":\"test $i\"}],\"max_tokens\":50}" 2>/dev/null)
    echo "Request $i: $TEST_RESPONSE"
    sleep 2
done

# Test 3: OpenAI compatibility test (what Claude Code does internally)
echo "=== Testing OpenAI compatibility ==="
HTTP_CODE=$(curl -s -o /tmp/test2_body.txt -w "%{http_code}" -H "Content-Type: application/json" \
  -H "Authorization: Bearer bearer-token-clone" \
  -X POST http://localhost:9910/v1/chat/completions \
  -d "{\"model\": \"llama-3.1-8b-instruct\",\"messages\":[{\"role\": \"user\",\"content\":\"hello from openai\"}],\"max_tokens\":50}")
echo "OpenAI Status Code: $HTTP_CODE"

# Test 4: Health endpoint
echo "=== Testing Health endpoint ==="
HEALTH_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:9910/health)
echo "Health Status Code: $HEALTH_CODE"

# Show server logs
echo "=== Server Logs ==="
cat /tmp/server.log

# Test streaming (what Claude Code uses)
echo "=== Testing Streaming ==="
curl -s -H "Content-Type: application/json" \
  -H "x-api-key: claude-auth-token" \
  -H "anthropic-version: 2023-06-01" \
  -X POST http://localhost:9910/v1/messages \
  -d '{"model":"llama-3.1-8b-instruct","messages":[{"role":"user","content":"streaming test"}],"stream":true,"max_tokens":50}' | head -50
echo ""
echo "=== Test Summary ==="
echo "If Claude Code was connecting successfully, you should see:"
echo "1. Claude Code auth requests (x-api-key) passing"
echo "2. OpenAI requests working"
echo "3. Health endpoint responsive"
echo "4. Streaming responses with SSE events"

# Kill server
kill $SERVER_PID 2>/dev/null || true

echo "Test completed. Check logs for details."
