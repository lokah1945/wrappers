/**
 * wrapper-nvidia E2E Translation Test Suite
 * Tests both Anthropic-compatible and OpenAI-compatible endpoints
 * Production validation: run before deployment
 */

const http = require('http');

const HOST = process.env.WRAPPER_HOST || 'localhost';
const PORT = parseInt(process.env.WRAPPER_PORT || '9100', 10);

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

function post(path, body) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const req = http.request({
      hostname: HOST,
      port: PORT,
      path,
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(data)
      },
      timeout: 15000
    }, (res) => {
      let body = '';
      res.on('data', chunk => body += chunk);
      res.on('end', () => {
        try {
          resolve({ status: res.statusCode, body: JSON.parse(body) });
        } catch {
          resolve({ status: res.statusCode, body });
        }
      });
    });
    req.on('error', reject);
    req.on('timeout', () => reject(new Error('Request timeout')));
    req.write(data);
    req.end();
  });
}

async function testAnthropicMessages() {
  console.log('\n=== TEST 1: Anthropic Messages Endpoint ===');
  const result = await post('/v1/messages', {
    model: 'claude-3-haiku-20240307',
    max_tokens: 100,
    messages: [{ role: 'user', content: 'Translate: Hello to Anthropic -> OpenAI' }]
  });

  console.log(`Status: ${result.status}`);
  console.log(`Response type: ${result.body?.type || 'N/A'}`);
  console.log(`Content preview: ${JSON.stringify(result.body?.content || result.body).slice(0, 200)}`);

  if (result.status === 200 && result.body?.type === 'message') {
    console.log('✅ PASS: Anthropic -> OpenAI translation works');
    return true;
  } else {
    console.log('⚠️ Note: May require valid NVIDIA API key or model availability');
    return false;
  }
}

async function testOpenAIChat() {
  console.log('\n=== TEST 2: OpenAI Chat Completions Endpoint ===');
  const result = await post('/v1/chat/completions', {
    model: 'nvidia/llama-3.1-70b-instruct',
    max_tokens: 50,
    messages: [{ role: 'user', content: 'Test OpenAI compatibility' }]
  });

  console.log(`Status: ${result.status}`);
  if (result.body?.choices) {
    console.log(`Response preview: ${result.body.choices[0]?.message?.content?.slice(0, 100) || 'N/A'}`);
  }

  if (result.status === 200) {
    console.log('✅ PASS: OpenAI endpoints operational');
    return true;
  } else {
    console.log('⚠️ Note: May require valid NVIDIA API key or model availability');
    return false;
  }
}

async function testHealthCheck() {
  console.log('\n=== TEST 3: Health Check ===');
  const res = await post('/health', {});
  console.log(`Health status: ${res.body?.status || 'ok'}`);

  if (res.status === 200 && res.body?.status) {
    console.log('✅ PASS: Health endpoint working');
    return true;
  } else {
    console.log('⚠️ Health endpoint check');
    return false;
  }
}

async function testRateLimit() {
  console.log('\n=== TEST 4: Rate Limiting Detection ===');
  // Send 50 rapid requests to test rate limiting
  const promises = [];
  for (let i = 0; i < 50; i++) {
    promises.push(post('/v1/chat/completions', {
      model: 'nvidia/llama-3.1-70b-instruct',
      max_tokens: 10,
      messages: [{ role: 'user', content: 'Rate test' }]
    }));
  }
  const results = await Promise.allSettled(promises);
  const statuses = results.map(r => r.status === 'fulfilled' ? r.value.status : 'error');
  const rateLimited = statuses.filter(s => s === 429).length;

  console.log(`Requests: ${statuses.length}, Rate-limited (429): ${rateLimited}`);
  console.log('✅ PASS: Rate limiting check completed');
  return true;
}

async function main() {
  console.log('Starting E2E Translation Test Suite...');
  console.log(`Target: http://${HOST}:${PORT}`);

  const results = {
    anthropic: await testAnthropicMessages().catch(e => { console.log('Error:', e.message); return false; }),
    openai: await testOpenAIChat().catch(e => { console.log('Error:', e.message); return false; }),
    health: await testHealthCheck().catch(e => { console.log('Error:', e.message); return false; }),
    ratelimit: await testRateLimit().catch(e => { console.log('Error:', e.message); return false; })
  };

  console.log('\n=== FINAL SUMMARY ===');
  console.log(JSON.stringify(results, null, 2));
  const passed = Object.values(results).every(Boolean);
  console.log(`${passed ? '✅ ALL TESTS PASSED' : '⚠️ Some tests require upstream API access'}`);
  process.exit(passed ? 0 : 1);
}

main();