const BASE_URL = 'http://127.0.0.1:9910';
const AUTH_HEADER = 'Bearer bearer-token-clone';

async function testDeepSeekFlash() {
  console.log('[TEST] POST /v1/messages using deepseek-ai/deepseek-v4-flash...');
  
  const payload = {
    model: "deepseek-ai/deepseek-v4-flash",
    max_tokens: 100,
    messages: [
      {
        role: "user",
        content: "hello, please respond with hello back."
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
      console.log('✔ PASS: deepseek-ai/deepseek-v4-flash responded successfully!');
      console.log('Response:', text);
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

testDeepSeekFlash();
