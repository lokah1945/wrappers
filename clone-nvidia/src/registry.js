/**
 * registry.js — Dynamic model registry for wrapper-nvidia
 *
 * Authoritative context/max-output numbers come from NVIDIA's machine-readable
 * "featured-models" catalog (NGC assets), which is the same source OpenClaw
 * uses. We fetch it periodically, cache it locally, and fall back to the last
 * good cache (or a tiny static seed) if the network/endpoint is unavailable —
 * NEVER to silent hardcoded guesses.
 *
 * The full *list* of served models still comes from NIM /v1/models discovery
 * (performed in key_pool.js) plus the curated GENAI list (image/audio/video/
 * embedding/etc. that never appear in the chat catalog). This module only
 * contributes the authoritative context_window / max_output_tokens overrides.
 *
 * Usage:
 *   const { Registry } = require('./registry');
 *   const registry = new Registry();
 *   await registry.refresh(true);          // force first load
 *   registry.start();                       // background periodic refresh
 *   registry.getOfficialContext('deepseek-ai/deepseek-v4-pro'); // -> {context, maxOutput}
 */

const fs = require('fs');
const path = require('path');
const { fetch: undiciFetch, Agent } = require('undici');

const NGC_FEATURED_URL = (process.env.NGC_FEATURED_MODELS_URL ||
  'https://assets.ngc.nvidia.com/products/api-catalog/featured-models.json').replace(/\/+$/, '');

// How often to re-sync the NGC catalog (seconds). 1h is plenty — the featured
// list changes rarely and we always have a local cache as fallback.
const REGISTRY_REFRESH_SEC = parseInt(process.env.REGISTRY_REFRESH_SEC || '3600', 10);

// Local cache file so we survive restarts / NGC outages without re-fetching.
const CACHE_FILE = path.resolve(__dirname, '..', 'nvidia', 'ngc-featured-cache.json');

// Static seed used ONLY if both the live fetch AND the on-disk cache are
// missing (e.g. first-ever boot on an air-gapped host). Mirrors the values
// verified live on 2026-07-09 from NGC. This is a safety net, not the primary
// source — the live fetch always wins when reachable.
const STATIC_SEED = {
  'nvidia/nemotron-3-ultra-550b-a55b': { context: 1048576, maxOutput: 8192 },
  'nemotron-3-super-120b-a12b': { context: 1000000, maxOutput: 8192 },
  'z-ai/glm-5.2': { context: 202752, maxOutput: 8192 },
  'minimaxai/minimax-m3': { context: 196608, maxOutput: 8192 },
  'deepseek-ai/deepseek-v4-pro': { context: 262144, maxOutput: 16384 },
};

class Registry {
  constructor() {
    this._map = {};                 // id -> {context, maxOutput}
    this._source = 'empty';        // 'live' | 'cache' | 'seed' | 'empty'
    this._lastSync = 0;
    this._lastError = null;
    this._agent = null;
    this._timer = null;
  }

  setExternalAgent(agent) { this._agent = agent; }

  /**
   * Fetch + parse the NGC featured-models catalog.
   * Returns a map id -> {context, maxOutput}. Throws on any failure so the
   * caller can fall back to cache/seed.
   */
  async _fetchLive() {
    const resp = await undiciFetch(NGC_FEATURED_URL, {
      headers: { 'Accept': 'application/json' },
      dispatcher: this._agent || undefined,
      signal: AbortSignal.timeout(20000),
    });
    if (!resp.ok) {
      throw new Error(`NGC featured-models HTTP ${resp.status}`);
    }
    const body = await resp.json();
    const arr = body['featured-models'] || body.models || body.data || [];
    const map = {};
    for (const m of arr) {
      const id = m.model || m.id;
      if (!id) continue;
      const context = Number(m.context);
      const maxOutput = Number(m['max-output'] ?? m.max_output ?? m.maxOutput);
      if (!Number.isFinite(context) || context <= 0) continue;
      map[id] = {
        context,
        maxOutput: Number.isFinite(maxOutput) && maxOutput > 0 ? maxOutput : 4096,
      };
    }
    if (Object.keys(map).length === 0) {
      throw new Error('NGC featured-models returned no usable entries');
    }
    return map;
  }

  _loadCacheFile() {
    try {
      if (!fs.existsSync(CACHE_FILE)) return null;
      const raw = JSON.parse(fs.readFileSync(CACHE_FILE, 'utf8'));
      if (raw && raw.map && Object.keys(raw.map).length > 0) return raw.map;
    } catch (e) {
      console.warn(`[registry] cache read failed: ${e.message}`);
    }
    return null;
  }

  _saveCacheFile() {
    try {
      fs.writeFileSync(CACHE_FILE, JSON.stringify({
        source: 'live',
        syncedAt: new Date().toISOString(),
        map: this._map,
      }, null, 2));
    } catch (e) {
      console.warn(`[registry] cache write failed: ${e.message}`);
    }
  }

  /**
   * Refresh the authoritative context map.
   * Priority: live fetch -> on-disk cache -> static seed.
   * Never throws; worst case _map stays whatever it was (or seed on first boot).
   */
  async refresh(force = false) {
    const now = Date.now();
    if (!force && this._lastSync && (now - this._lastSync) < REGISTRY_REFRESH_SEC * 1000) {
      return this._map;
    }
    try {
      const live = await this._fetchLive();
      this._map = live;
      this._source = 'live';
      this._lastSync = now;
      this._lastError = null;
      this._saveCacheFile();
      console.log(`[registry] Synced NGC featured-models: ${Object.keys(live).length} models (live)`);
    } catch (e) {
      this._lastError = e.message;
      const cached = this._loadCacheFile();
      if (cached && Object.keys(cached).length > 0) {
        this._map = cached;
        this._source = 'cache';
        this._lastSync = now;
        console.warn(`[registry] Live NGC fetch failed (${e.message}); using on-disk cache (${Object.keys(cached).length} models)`);
      } else if (Object.keys(this._map).length === 0) {
        this._map = { ...STATIC_SEED };
        this._source = 'seed';
        this._lastSync = now;
        console.warn(`[registry] Live NGC fetch failed (${e.message}) and no cache; using static seed (${Object.keys(STATIC_SEED).length} models)`);
      } else {
        console.warn(`[registry] Live NGC fetch failed (${e.message}); keeping existing map (${Object.keys(this._map).length} models)`);
      }
    }
    return this._map;
  }

  start() {
    const tick = async () => {
      try { await this.refresh(); } catch {}
    };
    tick();
    this._timer = setInterval(tick, REGISTRY_REFRESH_SEC * 1000).unref();
  }

  stop() {
    if (this._timer) { clearInterval(this._timer); this._timer = null; }
  }

  /**
   * Look up authoritative context/max-output for a model id.
   * Tries exact id, then owner/model basename, then a few known alias forms.
   * Returns {context, maxOutput} or null when unknown.
   */
  getOfficialContext(modelId) {
    if (!modelId) return null;
    const candidates = new Set([modelId]);
    // owner/name -> name
    const slash = modelId.indexOf('/');
    if (slash >= 0) candidates.add(modelId.slice(slash + 1));
    for (const id of candidates) {
      if (this._map[id]) return this._map[id];
    }
    return null;
  }

  hasOfficialContext(modelId) {
    return this.getOfficialContext(modelId) != null;
  }

  /** Whole map (for diagnostics / /stats). */
  all() { return { ...this._map }; }

  status() {
    return {
      source: this._source,
      models: Object.keys(this._map).length,
      lastSync: this._lastSync ? new Date(this._lastSync).toISOString() : null,
      lastError: this._lastError,
      url: NGC_FEATURED_URL,
    };
  }
}

module.exports = { Registry, NGC_FEATURED_URL, CACHE_FILE, STATIC_SEED };
