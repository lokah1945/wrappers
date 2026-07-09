const BASE_URL = 'http://127.0.0.1:9910';
const AUTH_HEADER = 'Bearer bearer-token-clone';

async function testToolUseTranslation() {
  console.log('[TEST] POST /v1/messages with multi-turn tool use...');
  
  const payload = {
    model: "mistralai/mistral-large-3-675b-instruct-2512",
    max_tokens: 100,
    messages: [
      {
        role: "user",
        content: [
          { type: "text", text: "hallo" }
        ]
      },
      {
        role: "assistant",
        content: [
          {
            type: "tool_use",
            id: "toolu_01",
            name: "get_weather",
            input: {}
          }
        ]
      },
      {
        role: "user",
        content: [
          {
            type: "tool_result",
            tool_use_id: "toolu_01",
            content: "Sunny, 22C"
          }
        ]
      }
    ],
    tools: [
      {
        name: "get_weather",
        description: "Get the current weather",
        input_schema: {
          type: "object",
          properties: {}
        }
      }
    ]
  };

  try {
    const res = await fetch(`${BASE_URL}/v1/messages`, {
      method: 'POST',
      headers: {
        'Authorization': AUTH_HEADER,
        'Content-Type': 'application/json',
        'anthropic-version': '2023-06-01'
      },
      body: JSON.stringify(payload)
    });

    const status = res.status;
    const text = await res.text();

    if (status === 200) {
      console.log('✔ PASS: Server responded with 200 SUCCESS!');
      console.log('Response:', text.slice(0, 500));
      process.exit(0);
    } else {
      console.error(`❌ FAIL: Expected 200, got ${status}`);
      console.error('Response:', text);
      process.exit(1);
    }
  } catch (e) {
    console.error(`❌ FAIL: Exception: ${e.message}`);
    process.exit(1);
  }
}

testToolUseTranslation();
