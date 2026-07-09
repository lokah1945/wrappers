const BASE_URL = 'http://127.0.0.1:9910';

async function testEndpoint(path, expectedType = 'application/json') {
  const url = `${BASE_URL}${path}`;
  console.log(`[TEST] GET ${path}...`);
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(5000) });
    if (res.status !== 200) {
      console.error(`❌ FAIL: Expected 200, got ${res.status}`);
      return false;
    }
    const contentType = res.headers.get('content-type') || '';
    if (!contentType.includes(expectedType)) {
      console.error(`❌ FAIL: Expected Content-Type ${expectedType}, got ${contentType}`);
      return false;
    }

    if (expectedType === 'application/json') {
      const data = await res.json();
      console.log(`✔ PASS (JSON keys: ${Object.keys(data).join(', ')})`);
    } else {
      const text = await res.text();
      console.log(`✔ PASS (${text.length} chars)`);
    }
    return true;
  } catch (e) {
    console.error(`❌ FAIL: Exception: ${e.message}`);
    return false;
  }
}

async function testSSE() {
  console.log(`[TEST] GET /events (SSE)...`);
  try {
    const controller = new AbortController();
    const res = await fetch(`${BASE_URL}/events`, { signal: controller.signal });
    if (res.status !== 200) {
      console.error(`❌ FAIL: Expected 200, got ${res.status}`);
      return false;
    }
    const contentType = res.headers.get('content-type') || '';
    if (!contentType.includes('text/event-stream')) {
      console.error(`❌ FAIL: Expected Content-Type text/event-stream, got ${contentType}`);
      return false;
    }

    // Abort connection to test cleanup
    controller.abort();
    console.log(`✔ PASS (SSE connection established and aborted successfully)`);
    return true;
  } catch (e) {
    if (e.name === 'AbortError') {
      console.log(`✔ PASS (SSE connection established and aborted successfully)`);
      return true;
    }
    console.error(`❌ FAIL: Exception: ${e.message}`);
    return false;
  }
}

async function main() {
  console.log('=== Running Dashboard API Tests ===\n');

  const tests = [
    testEndpoint('/dashboard.html', 'text/html'),
    testEndpoint('/metrics?window=24h'),
    testEndpoint('/metrics/tokens?window=24h'),
    testEndpoint('/metrics/models?window=24h'),
    testEndpoint('/metrics/models/timeseries?model=meta/llama-3.1-8b-instruct&hours=24'),
    testEndpoint('/metrics/keys?window=24h'),
    testEndpoint('/metrics/activity?limit=50'),
    testEndpoint('/metrics/rate-limits?limit=100&window=24h'),
    testEndpoint('/metrics/chart/hourly?hours=24'),
    testEndpoint('/metrics/chart/daily?days=30'),
    testSSE()
  ];

  const results = await Promise.all(tests);
  const allPass = results.every(r => r === true);

  console.log('\n=== RESULTS ===');
  if (allPass) {
    console.log('🎉 ALL DASHBOARD TESTS PASSED SUCCESSFULLY!');
    process.exit(0);
  } else {
    console.error('❌ SOME DASHBOARD TESTS FAILED!');
    process.exit(1);
  }
}

main();
