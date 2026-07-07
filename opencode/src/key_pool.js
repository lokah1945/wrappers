const http = require('http');
const https = require('https');
const { URL } = require('url');

const DEFAULT_SOFT_RPM = parseInt(process.env.SOFT_LIMIT_RPM || '30', 10);
const DEFAULT_HARD_RPM = parseInt(process.env.HARD_LIMIT_RPM || '40', 10);
const COOLDOWN_SEC = 65;
const REQUEST_TIMEOUT_SEC = parseInt(process.env.REQUEST_TIMEOUT_SEC || '120', 10);
const INFLIGHT_SOFT_CAP = parseInt(process.env.INFLIGHT_SOFT_CAP || '50', 10);
const MODEL_BLOCK_DEFAULT_SECS = parseInt(process.env.MODEL_BLOCK_DEFAULT_SECS || '8', 10);
const MODEL_BLOCK_CAP = parseInt(process.env.MODEL_BLOCK_CAP || '10', 10);
const QUEUE_LIMIT = parseFloat(process.env.QUEUE_LIMIT || '1.0');

const FREE_MODELS = new Set([
  'deepseek-v4-flash-free',
  'mimo-v2.5-free',
  'north-mini-code-free',
  'nemotron-3-ultra-free',
  'big-pickle',
]);

class Mutex {
  constructor() {
    this._queue = [];
    this._locked = false;
  }
  acquire() {
    if (!this._locked) { this._locked = true; return; }
    return new Promise(resolve => this._queue.push(resolve));
  }
  release() {
    if (this._queue.length > 0) this._queue.shift()();
    else this._locked = false;
  }
}

class KeyEntry {
  constructor(label, apiKey) {
    this.label = label;
    this.apiKey = apiKey;
    this.softRpm = DEFAULT_SOFT_RPM;
    this.hardRpm = DEFAULT_HARD_RPM;
    this.timestamps = [];
    this.hardBlockedUntil = 0;
    this.modelBlocks = {};
    this.detectedLimit = null;
    this.totalRequests = 0;
    this.total429s = 0;
    this.totalKey429s = 0;
    this.totalModel429s = 0;
    this.inFlight = 0;
    this.lastUsed = 0;
    this.lastAdmit = 0;
  }

  get effectiveLoad() {
    return this.currentRpm() + this.inFlight;
  }

  incrementInFlight() { this.inFlight++; }
  decrementInFlight() { if (this.inFlight > 0) this.inFlight--; }

  admitReady(interval) {
    if (interval <= 0) return true;
    return (Date.now() / 1000 - this.lastAdmit) >= interval;
  }

  secondsUntilAdmit(interval) {
    if (interval <= 0) return 0;
    return Math.max(0, interval - (Date.now() / 1000 - this.lastAdmit));
  }

  currentRpm(window = 60) {
    const now = Date.now() / 1000;
    this.timestamps = this.timestamps.filter(t => now - t < window);
    return this.timestamps.length;
  }

  effectiveHardLimit(configuredHard) {
    const hard = configuredHard || DEFAULT_HARD_RPM;
    return this.detectedLimit && this.detectedLimit < hard ? this.detectedLimit : hard;
  }

  effectiveSoftLimit(configuredSoft, configuredHard) {
    const hard = this.effectiveHardLimit(configuredHard);
    const soft = configuredSoft || DEFAULT_SOFT_RPM;
    return Math.max(1, Math.min(soft, hard - 1));
  }

  secondsUntilBelow(limit, window = 60) {
    const now = Date.now() / 1000;
    const ts = this.timestamps.filter(t => now - t < window).sort((a, b) => a - b);
    const rpm = ts.length;
    if (rpm < limit) return 0;
    const idx = rpm - limit;
    if (idx < 0 || idx >= ts.length) return 0;
    return Math.max(0, window - (now - ts[idx]));
  }

  isHardBlocked() { return (Date.now() / 1000) < this.hardBlockedUntil; }

  isModelBlocked(model) {
    if (!model) return false;
    const until = this.modelBlocks[model];
    if (!until) return false;
    if ((Date.now() / 1000) < until) return true;
    delete this.modelBlocks[model];
    return false;
  }

  activeModelBlocks() {
    const now = Date.now() / 1000;
    const out = {};
    for (const [m, until] of Object.entries(this.modelBlocks)) {
      const rem = until - now;
      if (rem > 0) out[m] = Math.round(rem * 10) / 10;
      else delete this.modelBlocks[m];
    }
    return out;
  }

  record() {
    this.timestamps.push(Date.now() / 1000);
    this.totalRequests++;
    this.lastUsed = Date.now() / 1000;
  }

  onRateLimit(scope, model = null, retryAfter = null, detectedLimit = null) {
    const now = Date.now() / 1000;
    this.total429s++;
    const rawSecs = retryAfter || MODEL_BLOCK_DEFAULT_SECS;
    if (scope === 'model' && model) {
      const blockSecs = Math.min(rawSecs, MODEL_BLOCK_CAP);
      this.modelBlocks[model] = now + blockSecs;
      this.totalModel429s++;
      console.warn(`[wrapper-zen] Key ${this.label}: model '${model}' blocked ${blockSecs}s`);
    } else {
      const blockSecs = Math.min(rawSecs, 30);
      this.hardBlockedUntil = now + blockSecs;
      this.totalKey429s++;
      if (detectedLimit) {
        this.detectedLimit = detectedLimit;
      }
      console.warn(`[wrapper-zen] Key ${this.label}: KEY-LEVEL rate-limited — blocked ${blockSecs}s`);
    }
  }

  stats(soft, hard) {
    const rpm = this.currentRpm();
    const now = Date.now() / 1000;
    return {
      label: this.label,
      key_prefix: this.apiKey.slice(0, 16) + '...',
      current_rpm: rpm,
      in_flight: this.inFlight,
      effective_load: this.effectiveLoad,
      configured_soft: soft,
      configured_hard: hard,
      effective_soft: this.effectiveSoftLimit(soft, hard),
      effective_hard: this.effectiveHardLimit(hard),
      detected_limit: this.detectedLimit,
      utilization_pct: this.effectiveHardLimit(hard) ? Math.round((rpm / this.effectiveHardLimit(hard) * 100) * 10) / 10 : 0,
      hard_blocked: this.isHardBlocked(),
      hard_blocked_remaining_s: Math.max(0, Math.round((this.hardBlockedUntil - now) * 10) / 10),
      model_blocks: this.activeModelBlocks(),
      total_requests: this.totalRequests,
      total_429s: this.total429s,
      total_key_429s: this.totalKey429s,
      total_model_429s: this.totalModel429s,
      last_used_ago_s: this.lastUsed ? Math.round((now - this.lastUsed) * 10) / 10 : null,
    };
  }
}

class KeyPool {
  constructor() {
    this.keys = [];
    this.softLimit = DEFAULT_SOFT_RPM;
    this.hardLimit = DEFAULT_HARD_RPM;
    this.freeOnly = process.env.FREE_ONLY?.toLowerCase() === 'true';
    this._rrIndex = 0;
    this._modelsCache = [];
    this._modelsCacheTs = 0;
    this._fetchLock = false;
    this._lock = new Mutex();
    this._admitInterval = QUEUE_LIMIT > 0 ? 1.0 / QUEUE_LIMIT : 0;
    this._modelTs = {};
    this._modelLimit = {};
    this._recent429s = [];
  }

  get totalKeys() { return this.keys.length; }

  get availableKeys() {
    return this.keys.filter(k => !k.isHardBlocked() && k.effectiveLoad < k.effectiveHardLimit(this.hardLimit)).length;
  }

  loadFromEnv() {
    this.keys = [];
    const seenKeys = new Set();
    const raw = [];

    for (const [k, v] of Object.entries(process.env)) {
      const m = k.match(/^OPENCODE-ZEN_API_KEY(_\d+)?$/);
      if (!m) continue;
      if (!v || v.length < 5) continue;
      if (seenKeys.has(v)) continue;
      seenKeys.add(v);
      raw.push(v);
    }

    if (raw.length === 0) {
      console.warn('[wrapper-zen] No OPENCODE-ZEN_API_KEY* found in environment');
      return this;
    }

    this.keys = raw.map((k, i) => new KeyEntry(`key${i + 1}`, k));
    console.info(`[wrapper-zen] Loaded ${this.keys.length} key(s) | soft=${this.softLimit} hard=${this.hardLimit} rpm`);
    return this;
  }

  async syncKeys(keysList) {
    if (!keysList || keysList.length === 0) return false;
    await this._lock.acquire();
    try {
      const oldSet = new Set(this.keys.map(k => k.apiKey));
      const newSet = new Set(keysList);
      let same = oldSet.size === newSet.size;
      if (same) {
        for (let i = 0; i < keysList.length; i++) {
          if (this.keys[i]?.apiKey !== keysList[i]) { same = false; break; }
        }
      }
      if (same) return false;
      const newKeys = keysList.map((k, i) => new KeyEntry(`key${i + 1}`, k));
      const added = keysList.filter(k => !oldSet.has(k));
      const removed = Array.from(oldSet).filter(k => !newSet.has(k));
      this.keys = newKeys;
      console.info(`[wrapper-zen] Key pool synced: +${added.length} / -${removed.length} -> ${this.keys.length} total`);
      return true;
    } finally {
      this._lock.release();
    }
  }

  get freeModelIds() { return Array.from(FREE_MODELS); }

  isFreeModel(modelId) {
    if (!modelId) return false;
    return FREE_MODELS.has(modelId.toLowerCase().trim());
  }

  filterModels(models) {
    if (!this.freeOnly) return models;
    return models.filter(m => this.isFreeModel(m.id || m));
  }

  _recordModelTs(model, keyLabel) {
    if (!model) return;
    const now = Date.now() / 1000;
    if (!this._modelTs[model]) this._modelTs[model] = [];
    this._modelTs[model] = this._modelTs[model].filter(t => now - t < 60);
    this._modelTs[model].push(now);
  }

  modelRpm(model) {
    if (!model) return 0;
    const now = Date.now() / 1000;
    const list = (this._modelTs[model] || []).filter(t => now - t < 60);
    this._modelTs[model] = list;
    return list.length;
  }

  _classify429(state, model, bodyText, rpmAt429, effHard) {
    const txt = (bodyText || '').toLowerCase();

    if (model && txt.includes(model.toLowerCase())) return ['model', 'model-name-in-body'];
    if (txt.includes('rate limit for model') || txt.includes('per-model')) return ['model', 'model-hint'];
    if (txt.includes('account rate limit') || txt.includes('key rate limit')) return ['key', 'key-hint'];

    const now = Date.now() / 1000;
    this._recent429s = this._recent429s.filter(item => now - item.ts < 60);
    const otherModels = new Set();
    for (const item of this._recent429s) {
      if (item.keyLabel === state.label && item.model !== model) otherModels.add(item.model);
    }
    if (otherModels.size >= 1) return ['key', 'multi-model-on-key'];

    if (effHard && rpmAt429 >= effHard * 0.8) return ['key', `rpm-near-cap(${rpmAt429}/${effHard})`];
    return ['model', `rpm-low(${rpmAt429}/${effHard})`];
  }

  async registerRateLimit(state, model, retryAfter, detectedLimit, bodyText) {
    await this._lock.acquire();
    try {
      const rpm = state.currentRpm();
      const effHard = state.effectiveHardLimit(this.hardLimit);
      const [scope, reason] = this._classify429(state, model, bodyText, rpm, effHard);
      state.onRateLimit(scope, model, retryAfter, detectedLimit);
      this._recent429s.push({ ts: Date.now() / 1000, keyLabel: state.label, model });
      if (scope === 'model' && model) {
        const observed = this.modelRpm(model);
        const val = Math.max(1, Math.floor(observed));
        const cur = this._modelLimit[model];
        this._modelLimit[model] = cur ? Math.min(cur, val) : val;
      }
      return [scope, reason];
    } finally {
      this._lock.release();
    }
  }

  async acquire(model = null) {
    const start = Date.now() / 1000;
    const interval = this._admitInterval;
    let myTicket = null;

    const totalInFlight = this.keys.reduce((sum, k) => sum + k.inFlight, 0);
    if (totalInFlight >= INFLIGHT_SOFT_CAP) {
      console.warn(`[wrapper-zen] Load shedding: in-flight ${totalInFlight} >= ${INFLIGHT_SOFT_CAP}`);
      return { key: null, retryAfter: 5 };
    }

    while (true) {
      await this._lock.acquire();
      let sleepDuration = 0;
      let shouldSleep = true;
      try {
        if (myTicket === null) myTicket = Date.now() + Math.random();

        const avail = this.keys.filter(s => !s.isHardBlocked() && !(model && s.isModelBlocked(model)));

        let modelSaturated = false;
        if (model && avail.length > 0) {
          const modelLimit = this._modelLimit[model];
          if (modelLimit && avail.every(s => {
            const km = `${s.label}/${model}`;
            return false;
          })) {
            const rpm = this.modelRpm(model);
            if (rpm >= modelLimit * 0.9) modelSaturated = true;
          }
        }

        if (avail.length > 0 && !modelSaturated) {
          const loads = avail.map(k => ({ key: k, load: k.effectiveLoad }));
          loads.sort((a, b) => a.load - b.load);
          const minLoad = loads[0].load;
          const candidates = loads.filter(l => l.load === minLoad);

          const rpmOk = (s) => s.currentRpm() < (this.pacing ? s.effectiveSoftLimit(this.softLimit, this.hardLimit) : s.effectiveHardLimit(this.hardLimit));
          const admitOk = (s) => s.admitReady(interval);
          const ready = candidates.filter(({ key }) => rpmOk(key) && admitOk(key));

          if (ready.length > 0) {
            let chosen = ready[0].key;
            if (ready.length > 1) {
              const labels = this.keys.map(k => k.label);
              ready.sort((a, b) => {
                const aDist = (labels.indexOf(a.key.label) - this._rrIndex + labels.length) % labels.length;
                const bDist = (labels.indexOf(b.key.label) - this._rrIndex + labels.length) % labels.length;
                return aDist - bDist;
              });
              chosen = ready[0].key;
              const chosenIdx = labels.indexOf(chosen.label);
              if (chosenIdx !== -1) this._rrIndex = (chosenIdx + 1) % labels.length;
            }

            chosen.record();
            chosen.incrementInFlight();
            chosen.lastAdmit = Date.now() / 1000;
            this._recordModelTs(model, chosen.label);
            return { key: chosen, retryAfter: 0 };
          }

          const waits = avail.map(s => Math.max(s.secondsUntilBelow(s.effectiveSoftLimit(this.softLimit, this.hardLimit)), s.secondsUntilAdmit(interval)));
          sleepDuration = Math.max(0.02, Math.min(Math.min(...waits), 2.0));
        } else {
          sleepDuration = 1.0;
        }

        const elapsed = (Date.now() / 1000) - start;
        if (elapsed >= 30) { shouldSleep = false; break; }
        sleepDuration = Math.min(sleepDuration, 30 - elapsed + 0.01);
      } finally {
        this._lock.release();
      }

      if (shouldSleep) {
        await new Promise(r => setTimeout(r, sleepDuration * 1000));
      }
    }

    return { key: null, retryAfter: Math.ceil(Math.min(...this.keys.map(k => k.secondsUntilCooldown()))) };
  }

  release(key) {
    if (key) key.decrementInFlight();
  }

  retryHint(model) {
    const now = Date.now() / 1000;
    if (this.keys.every(s => s.isHardBlocked())) {
      const secs = Math.min(...this.keys.map(s => s.hardBlockedUntil - now));
      return [Math.max(1, Math.round(secs)), 'all_keys'];
    }
    const live = this.keys.filter(s => !s.isHardBlocked());
    if (model && live.every(s => s.isModelBlocked(model))) {
      const secs = Math.min(...live.map(s => s.modelBlocks[model] - now).filter(rem => rem > 0));
      return [Math.max(1, Math.round(secs)), 'model'];
    }
    return [MODEL_BLOCK_DEFAULT_SECS, 'capacity'];
  }

  async fetchModels() {
    const key = this.keys.find(k => !k.isHardBlocked());
    if (!key) return [];

    try {
      const res = await this._request('https://opencode.ai/zen/v1/models', 'GET', null, key);
      const body = res.data;
      if (!body || !Array.isArray(body.data)) return [];
      return body.data.map(m => ({ id: m.id, ...m }));
    } catch (e) {
      return [];
    }
  }

  async refreshModels(force = false) {
    const now = Date.now();
    if (!force && now - this._modelsCacheTs < 300000 && this._modelsCache.length > 0) return this._modelsCache;
    if (this._fetchLock) return this._modelsCache;
    this._fetchLock = true;
    try {
      const models = await this.fetchModels();
      if (models.length > 0) { this._modelsCache = models; this._modelsCacheTs = now; }
    } finally { this._fetchLock = false; }
    return this._modelsCache;
  }

  get cachedModels() { return this._modelsCache; }

  _request(url, method, body, key, stream = false) {
    return new Promise((resolve, reject) => {
      const urlObj = new URL(url);
      const mod = urlObj.protocol === 'https:' ? https : http;
      const headers = { 'Content-Type': 'application/json' };
      if (stream) {
        headers['Accept'] = 'text/event-stream';
      } else {
        headers['Accept'] = 'application/json';
      }
      if (key) headers['Authorization'] = `Bearer ${key.apiKey}`;

      const options = {
        hostname: urlObj.hostname,
        port: urlObj.port || 443,
        path: urlObj.pathname + urlObj.search,
        method: method || 'GET',
        headers,
        timeout: REQUEST_TIMEOUT_SEC * 1000,
      };

      const req = mod.request(options, (res) => {
        if (stream) {
          return resolve({ status: res.statusCode, headers: res.headers, stream: res });
        }
        const chunks = [];
        res.on('data', c => chunks.push(c));
        res.on('end', () => {
          const raw = Buffer.concat(chunks).toString();
          try {
            const parsed = JSON.parse(raw);
            resolve({ status: res.statusCode, headers: res.headers, data: parsed, raw });
          } catch {
            resolve({ status: res.statusCode, headers: res.headers, data: null, raw });
          }
        });
      });

      req.on('error', reject);
      req.on('timeout', () => { req.destroy(); reject(new Error('Request timeout')); });
      if (body) req.write(JSON.stringify(body));
      req.end();
    });
  }

  async proxyChat(body, key) {
    const url = 'https://opencode.ai/zen/v1/chat/completions';
    const isStream = !!body.stream;

    const result = await this._request(url, 'POST', body, key, isStream);

    if (result.status === 429) {
      const ra = parseInt(result.headers['retry-after'] || '65', 10);
      return { status: 429, retryAfter: ra };
    }

    if (isStream && result.status === 200) {
      return { status: 200, stream: result.stream, key };
    }

    return { status: result.status, data: result.data };
  }

  addRateLimitHeaders(headers, keyLabel) {
    const key = this.keys.find(k => k.label === keyLabel);
    if (key) {
      const hardLimit = key.effectiveHardLimit(this.hardLimit);
      const remaining = Math.max(0, hardLimit - key.currentRpm());
      headers['X-RateLimit-Limit'] = hardLimit;
      headers['X-RateLimit-Remaining'] = remaining;
      headers['X-RateLimit-Reset'] = Math.ceil(Date.now() / 1000) + 60;
    }
  }

  allStats() {
    return this.keys.map(k => k.stats(this.softLimit, this.hardLimit));
  }

  blockedModels() {
    const now = Date.now() / 1000;
    const agg = {};
    for (const s of this.keys) {
      for (const [m, until] of Object.entries(s.modelBlocks)) {
        const rem = until - now;
        if (rem <= 0) continue;
        if (!agg[m]) agg[m] = { keys: [], retry_s: rem };
        agg[m].keys.push(s.label);
        agg[m].retry_s = Math.min(agg[m].retry_s, rem);
      }
    }
    for (const m of Object.keys(agg)) agg[m].retry_s = Math.round(agg[m].retry_s * 10) / 10;
    return agg;
  }

  healthJson() {
    return {
      status: this.availableKeys > 0 ? 'ok' : 'degraded',
      total_keys: this.totalKeys,
      available_keys: this.availableKeys,
      free_only: this.freeOnly,
      soft_limit_rpm: this.softLimit,
      hard_limit_rpm: this.hardLimit,
      models_cached: this._modelsCache.length,
      version: '2.0.0',
    };
  }

  resetCounters() {
    for (const s of this.keys) {
      s.totalRequests = 0;
      s.total429s = 0;
      s.totalKey429s = 0;
      s.totalModel429s = 0;
      s.inFlight = 0;
    }
  }

  healInFlight() {
    for (const s of this.keys) {
      if (s.inFlight > 0) {
        console.warn(`[wrapper-zen] Heal: ${s.label} in_flight ${s.inFlight} -> 0`);
        s.inFlight = 0;
      }
    }
  }
}

module.exports = { KeyPool, FREE_MODELS };
