const BASE_URL = 'https://integrate.api.nvidia.com/v1/chat/completions';
const fs = require('fs');
const dotenv = require('dotenv');

// Read keys from env
const envContent = fs.readFileSync('.env', 'utf8');
const env = dotenv.parse(envContent);

const keys = [
  env.NVIDIA_API_KEY_1,
  env.NVIDIA_API_KEY_2,
  env.NVIDIA_API_KEY_3,
  env.NVIDIA_API_KEY_4,
  env.NVIDIA_API_KEY_5
].filter(Boolean);

async function probeKey(key, idx) {
  console.log(`[PROBE] Probing Key ${idx + 1} with model deepseek-ai/deepseek-v4-pro...`);
  
  const payload = {
    model: "deepseek-ai/deepseek-v4-flash",
    max_tokens: 1,
    messages: [
      {
        role: "user",
        content: "hello"
      }
    ]
  };

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000); // 15 seconds timeout

  try {
    const res = await fetch(BASE_URL, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${key}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify(payload),
      signal: controller.signal
    });

    clearTimeout(timer);
    const status = res.status;
    const text = await res.text();

    console.log(`[RESULT] Key ${idx + 1}: Status = ${status}, Response = ${text.slice(0, 200)}`);
  } catch (e) {
    clearTimeout(timer);
    console.log(`[RESULT] Key ${idx + 1}: Error = ${e.message}`);
  }
}

async function run() {
  for (let i = 0; i < keys.length; i++) {
    await probeKey(keys[i], i);
    await new Promise(r => setTimeout(r, 1000));
  }
}

run();
