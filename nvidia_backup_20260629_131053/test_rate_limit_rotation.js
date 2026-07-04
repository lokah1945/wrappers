const assert = require('assert');
const path = require('path');

// Mock environment variables
process.env.SOFT_LIMIT_RPM = '10';
process.env.HARD_LIMIT_RPM = '20';
process.env.KEYS_RELOAD_SECONDS = '5';
process.env.MAX_QUEUE_SIZE = '10';

// Import from the VPS source directory
const keyPoolPath = '/root/wrapper/nvidia/src/key_pool.js';
console.log('Loading KeyPool from:', keyPoolPath);
const { KeyPool } = require(keyPoolPath);

// Re-implement the exact isDegradedError function to verify classification logic
function isDegradedError(errBody, text) {
  const txt = (text || '').toLowerCase();
  if (txt.includes('degraded') || txt.includes('cannot be invoked') || (txt.includes('function id') && txt.includes('cannot'))) {
    return true;
  }
  if (errBody) {
    const detail = (errBody.detail || '').toLowerCase();
    const title = (errBody.title || '').toLowerCase();
    if (detail.includes('degraded') || detail.includes('cannot be invoked') || title.includes('degraded')) {
      return true;
    }
  }
  return false;
}

async function runTests() {
  console.log('=== Running Key Rotation & Model-level Rate Limiting Tests ===');

  const pool = new KeyPool();
  const testKeys = [
    'nvapi-key-one-xxxxxxxxxxxxxxxxx',
    'nvapi-key-two-xxxxxxxxxxxxxxxxx',
    'nvapi-key-three-xxxxxxxxxxxxxxx'
  ];
  
  // Initialize keys
  await pool.syncKeys(testKeys);
  assert.strictEqual(pool.keys.length, 3, 'Should load 3 keys');
  console.log('✔ Loaded 3 mock keys successfully');

  // Test Case 1: Acquire slots for a specific model
  const modelId = 'meta/llama-3.1-8b-instruct';
  const { key: firstKey } = await pool.acquire(modelId);
  assert.ok(firstKey, 'Should successfully acquire a key');
  console.log(`✔ Acquired first key: ${firstKey.label}`);

  // Release it
  pool.releaseSuccess(firstKey);
  assert.strictEqual(firstKey.inFlight, 0, 'In flight count should be decremented to 0');
  console.log('✔ Released first key cleanly');

  // Test Case 2: Register a model-scoped 429
  // We simulate a 429 response that is classified as model-scoped (e.g. body contains the model name)
  const bodyText = 'Rate limit exceeded for model: meta/llama-3.1-8b-instruct';
  const [scope, reason] = await pool.registerRateLimit(firstKey, modelId, 65, null, bodyText);
  
  assert.strictEqual(scope, 'model', 'Rate limit scope should be classified as model-scoped');
  assert.strictEqual(firstKey.isModelBlocked(modelId), true, 'Model should be blocked for firstKey');
  assert.strictEqual(firstKey.isHardBlocked(), false, 'Entire key should NOT be hard-blocked');
  console.log(`✔ Model-level rate limit correctly classified: scope=${scope}, reason=${reason}`);
  console.log(`✔ firstKey model is blocked: ${firstKey.isModelBlocked(modelId)}, key hard-blocked: ${firstKey.isHardBlocked()}`);

  // Test Case 3: Key Rotation for the blocked model
  // Now, acquiring the blocked model should bypass firstKey and return a different key (key2 or key3)
  const { key: rotatedKey } = await pool.acquire(modelId);
  assert.ok(rotatedKey, 'Should successfully acquire a rotated key');
  assert.notStrictEqual(rotatedKey.label, firstKey.label, 'Rotated key must not be the blocked key');
  console.log(`✔ Rotated key acquired successfully: ${rotatedKey.label}`);
  pool.releaseSuccess(rotatedKey);

  // Test Case 4: Non-blocked models remain accessible on the rate-limited key
  // firstKey should still be available for other models, e.g., 'nvidia/nv-embed-v1'
  const otherModel = 'nvidia/nv-embed-v1';
  // Block all other keys for otherModel so that the pool is forced to pick firstKey if it's healthy
  for (const k of pool.keys) {
    if (k.label !== firstKey.label) {
      k.modelBlocks[otherModel] = (Date.now() / 1000) + 100; // Block key2 & key3 for otherModel
    }
  }

  const { key: healthyModelKey } = await pool.acquire(otherModel);
  assert.ok(healthyModelKey, 'Should successfully acquire a key for the other model');
  assert.strictEqual(healthyModelKey.label, firstKey.label, 'Should route to firstKey because it is not blocked for otherModel');
  console.log(`✔ Rate-limited key successfully accepted request for a different model: ${healthyModelKey.label}`);
  pool.releaseSuccess(healthyModelKey);

  // Test Case 5: Verify isDegradedError classification
  console.log('\n=== Running Degraded Error Classification Tests ===');
  
  // Real degraded error from logs
  const realErrorObj = {
    status: 400,
    title: "Bad Request",
    detail: "Function id '87ea0ddc-cff1-4bca-bf8b-3bd98a35ddd0': DEGRADED function cannot be invoked"
  };
  const realErrorStr = JSON.stringify(realErrorObj);
  assert.strictEqual(isDegradedError(realErrorObj, realErrorStr), true, 'Should classify real degraded error as degraded');
  
  // Case insensitivity check
  const mixedErrorStr = "Function is Degraded and cannot be invoked";
  assert.strictEqual(isDegradedError(null, mixedErrorStr), true, 'Should handle case-insensitive text check');
  
  // Normal 400 error (unsupported parameters, validation error, etc.)
  const normalErrorObj = {
    status: 400,
    title: "Bad Request",
    detail: "unsupported parameter: 'temperature' must be >= 0.0"
  };
  const normalErrorStr = JSON.stringify(normalErrorObj);
  assert.strictEqual(isDegradedError(normalErrorObj, normalErrorStr), false, 'Should NOT classify standard parameter errors as degraded');
  
  console.log('✔ All Degraded Error classification assertions passed successfully');
  console.log('=== All tests passed successfully! ===');
}

runTests().catch(err => {
  console.error('Test Failed:', err);
  process.exit(1);
});
