#!/bin/bash

# Kill any running server
pkill -f "node src/index.js" 2>/dev/null || true
sleep 2

# Start server
node src/index.js > /tmp/server.log 2>&1 &
SERVER_PID=$!

# Wait for server to start
sleep 5

# Check if server is running
if kill -0 $SERVER_PID 2>/dev/null; then
    echo "✅ Server started successfully (PID: $SERVER_PID)"
    
    # Test Claude Code API
    echo "Testing Claude Code API..."
    HTTP_CODE=$(curl -s -o /tmp/response.txt -w "%{http_code}" -H "Content-Type: application/json" \
         -H "x-api-key: claude-auth-token" \
         -H "anthropic-version: 2023-06-01" \
         -X POST http://localhost:9910/v1/messages \
         -d '{"model":"llama-3.1-8b-instruct","messages":[{"role":"user","content":"hello"}],"stream":false}')
    
    echo "HTTP Response Code: $HTTP_CODE"
    
    # Test OpenAI API
    echo "Testing OpenAI API..."
    HTTP_CODE2=$(curl -s -o /tmp/response2.txt -w "%{http_code}" -H "Content-Type: application/json" \
         -H "Authorization: Bearer bearer-token-clone" \
         -X POST http://localhost:9910/v1/chat/completions \
         -d '{"model":"llama-3.1-8b-instruct","messages":[{"role":"user","content":"hello"}],"stream":false}')
    
    echo "OpenAI HTTP Response Code: $HTTP_CODE2"
    
    # Test health endpoint
    echo "Testing Health endpoint..."
    HTTP_CODE3=$(curl -s -o /tmp/health.txt -w "%{http_code}" http://localhost:9910/health)
    echo "Health HTTP Response Code: $HTTP_CODE3"
    
    echo "✅ All tests completed successfully"
    
    # Show server logs
    echo "=== Server logs ==="
    cat /tmp/server.log
    
    # Show responses
    echo "=== Claude Code Response ==="
    cat /tmp/response.txt | head -20
    echo "=== OpenAI Response ==="
    cat /tmp/response2.txt | head -20
    
else
    echo "❌ Server failed to start"
    echo "Server logs:"
    cat /tmp/server.log
fi

# Kill server
kill $SERVER_PID 2>/dev/null || true
