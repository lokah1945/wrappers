const BASE_URL = 'http://127.0.0.1:9910';
const AUTH_HEADER = 'Bearer bearer-token-clone';

async function testPruning() {
  console.log('[TEST] POST /v1/messages with 80,000 tokens of history (should auto-prune to stay under the 32k model limit)...');
  
  // We send 8 user-assistant turns, each user turn has ~10,000 tokens of text (approx 20,000 characters)
  const turns = [];
  for (let i = 1; i <= 8; i++) {
    turns.push({
      role: 'user',
      content: `Turn ${i}: ` + 'a '.repeat(10000)
    });
    turns.push({
      role: 'assistant',
      content: `I received turn ${i} and acknowledged it.`
    });
  }
  
  // The final user turn (turn 9)
  turns.push({
    role: 'user',
    content: 'Hello, please reply with exactly the word "acknowledged".'
  });

  const payload = {
    model: "mistralai/mistral-large-3-675b-instruct-2512",
    max_tokens: 100,
    messages: turns
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

testPruning();
