const BASE_URL = 'http://127.0.0.1:9910';
const AUTH_HEADER = 'Bearer bearer-token-clone';

async function probeContext(numTokens) {
  // Approximate character count for numTokens (using 4 chars per token)
  const textLength = numTokens * 4;
  const dummyText = 'a '.repeat(numTokens);
  
  console.log(`[PROBE] Sending request with ~${numTokens} tokens...`);
  
  const payload = {
    model: "mistralai/mistral-large-3-675b-instruct-2512",
    max_tokens: 100,
    messages: [
      {
        role: "user",
        content: dummyText
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
      console.log(`✔ SUCCESS for ${numTokens} tokens`);
      return true;
    } else {
      console.log(`❌ FAILED for ${numTokens} tokens. Status: ${status}, Response: ${text}`);
      return false;
    }
  } catch (e) {
    console.error(`❌ EXCEPTION for ${numTokens} tokens: ${e.message}`);
    return false;
  }
}

async function run() {
  // Test a few sizes: 8000, 16000, 24000, 32000
  const sizes = [8000, 16000, 24000, 32000];
  for (const size of sizes) {
    await probeContext(size);
    await new Promise(r => setTimeout(r, 1000)); // avoid rate limit
  }
}

run();
