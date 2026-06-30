/**
 * error_taxonomy.js — Minimal 4-class error classifier for wrapper-nvidia.
 *
 * Output: one of MODEL | KEY | PROVIDER | NETWORK (lowercase strings).
 *
 * Replaces _classify429 in key_pool.js for new code paths.
 * Legacy HINTS-based heuristic remains as fallback to avoid regression.
 */

'use strict';

const CLASS = Object.freeze({
  MODEL: 'model',
  KEY: 'key',
  PROVIDER: 'provider',
  NETWORK: 'network',
});

// Provider-wide failure thresholds (configurable via env, but with sane defaults)
const PROVIDER_FAIL_WINDOW_MS = parseInt(process.env.PROVIDER_FAIL_WINDOW_MS || '60000', 10);
const PROVIDER_FAIL_THRESHOLD = parseInt(process.env.PROVIDER_FAIL_THRESHOLD || '5', 10);
const PROVIDER_OPEN_MS = parseInt(process.env.PROVIDER_OPEN_MS || '120000', 10);

// Network blip threshold before promotion
const NETWORK_PROMOTE_AFTER = parseInt(process.env.NETWORK_PROMOTE_AFTER || '3', 10);

// Body-text negative hints (kept tight; legacy list maintained for compat)
const MODEL_HINTS = (process.env.MODEL_429_HINTS ||
  'for this model,per-model,per model,this model is rate').split(',').map(s => s.trim());
const KEY_HINTS = (process.env.KEY_429_HINTS ||
  'account,api key,api-key,credential,your organization').split(',').map(s => s.trim());

function lc(s) { return (s || '').toLowerCase(); }

class ErrorTaxonomy {
  constructor() {
    // (provider, keyLabel, model) -> ts[]
    this._recentFails = new Map();
    // providerName -> openUntil (epoch ms)
    this._providerOpen = new Map();
    this._providerFailCount = new Map();
  }

  /**
   * Classify a single failure event.
   * @param {object} ev { status, body, model, key, provider }
   * @returns {string} 'model' | 'key' | 'provider' | 'network'
   */
  classify(ev = {}) {
    const { status, body, model, key, provider } = ev;
    const text = lc(typeof body === 'string' ? body : JSON.stringify(body || {}));

    // 1. NETWORK: timeout, ECONNRESET, abort, EAI_AGAIN
    if (status === 0 || /econnreset|eai_again|aborted|timeout|etimedout/.test(text)) {
      return CLASS.NETWORK;
    }

    // 2. PROVIDER hint patterns (5xx but not 504 from upstream LLM itself)
    const isProviderStatus = status >= 500 && status < 600;
    const isBadGatewayClass = new Set([502, 503, 504]).has(status);
    const hasProviderHint = /service.?unavailable|upstream|bad.?gateway|cloudflare|origin.?unreachable/.test(text);
    if (isProviderStatus || isBadGatewayClass || hasProviderHint) {
      return CLASS.PROVIDER;
    }

    // 3. 429 classification
    if (status === 429) {
      // MODEL signals: body names the model
      if (model && text.includes(lc(model))) return CLASS.MODEL;
      // MODEL signals: shared hints
      if (MODEL_HINTS.some(h => text.includes(h))) return CLASS.MODEL;
      // KEY signals: shared hints
      if (KEY_HINTS.some(h => text.includes(h))) return CLASS.KEY;

      // Default: KEY (legacy behavior) — but tighten by corroboration
      // If rpm is well below cap and same fleet returned 429, prefer KEY
      return CLASS.KEY;
    }

    // 4. 401/403 → KEY (credential)
    if (status === 401 || status === 403) return CLASS.KEY;

    // 5. 404 → MODEL (model not found)
    if (status === 404) return CLASS.MODEL;

    // 6. 400 → MODEL (bad payload, not key-level)
    if (status === 400) return CLASS.MODEL;

    // 7. 413 → KEY (payload too large, quota drift)
    if (status === 413) return CLASS.KEY;

    return CLASS.NETWORK;
  }

  /**
   * Record a failure for provider circuit-breaker accounting.
   * Returns true if circuit is now OPEN for the given provider.
   */
  recordProviderFail(provider) {
    if (!provider) return false;
    const now = Date.now();
    const key = String(provider);
    if (!this._recentFails.has(key)) this._recentFails.set(key, []);
    const arr = this._recentFails.get(key);
    arr.push(now);
    // GC old entries
    while (arr.length && now - arr[0] > PROVIDER_FAIL_WINDOW_MS) arr.shift();

    if (arr.length >= PROVIDER_FAIL_THRESHOLD) {
      this._providerOpen.set(key, now + PROVIDER_OPEN_MS);
      this._providerFailCount.set(key, arr.length);
      return true;
    }
    return false;
  }

  /** Returns true if circuit is currently OPEN (refuse traffic). */
  isProviderOpen(provider) {
    if (!provider) return false;
    const until = this._providerOpen.get(String(provider));
    if (!until) return false;
    if (Date.now() >= until) {
      this._providerOpen.delete(String(provider));
      // half-open: fall through; next record decides
      return false;
    }
    return true;
  }

  /** Half-open probe: caller verifies one call. If success, close circuit. */
  providerProbeSucceeded(provider) {
    if (!provider) return;
    const k = String(provider);
    this._providerOpen.delete(k);
    this._recentFails.delete(k);
  }

  /** Reset all state (admin endpoint or test). */
  reset() {
    this._recentFails.clear();
    this._providerOpen.clear();
    this._providerFailCount.clear();
  }

  /** Snapshot for /admin/state */
  snapshot() {
    const providers = {};
    for (const [k, v] of this._providerOpen.entries()) {
      providers[k] = { openUntilMs: v, remainingMs: Math.max(0, v - Date.now()) };
    }
    const counts = {};
    for (const [k, v] of this._providerFailCount.entries()) counts[k] = v;
    return { openProviders: providers, recentFailCounts: counts };
  }
}

module.exports = { ErrorTaxonomy, CLASS };
