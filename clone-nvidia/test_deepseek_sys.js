const BASE_URL = 'https://integrate.api.nvidia.com/v1/chat/completions';
const fs = require('fs');
const dotenv = require('dotenv');

const envContent = fs.readFileSync('.env', 'utf8');
const env = dotenv.parse(envContent);
const key = env.NVIDIA_API_KEY_1;

async function testScenario(name, messages) {
  console.log(`[TEST] Scenario: ${name}`);
  const payload = {
    model: "deepseek-ai/deepseek-v4-pro",
    max_tokens: 20,
    messages
  };

  const start = Date.now();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000); // 20s timeout

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
    console.log(`[Scenario ${name}] Status = ${status}, Latency = ${Date.now() - start}ms`);
    console.log(`Response: ${text.slice(0, 300)}`);
  } catch (e) {
    clearTimeout(timer);
    console.log(`[Scenario ${name}] Error = ${e.message}, Latency = ${Date.now() - start}ms`);
  }
}

async function run() {
  await testScenario("A (User only)", [{ role: "user", content: "hello" }]);
  console.log('---');
  await testScenario("B (System + User)", [
    { role: "system", content: "You are a helpful assistant" },
    { role: "user", content: "hello" }
  ]);
}

run();
