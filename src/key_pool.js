/**
 * key_pool.js v4.6.0 — Two-tier rate-limited API key pool for NVIDIA NIM.
 * Ported from Python key_pool.py — functionally identical with full pacing,
 * corroboration-based 429 classification, and FIFO admission queue.
 */

const { fetch: undiciFetch, Agent } = require('undici');

// ── Configuration ──────────────────────────────────────────────────────
const SOFT_LIMIT_RPM = parseInt(process.env.SOFT_LIMIT_RPM || '30', 10);
const HARD_LIMIT_RPM = parseInt(process.env.HARD_LIMIT_RPM || '40', 10);
const QUEUE_LIMIT_PER_KEY_PER_SEC = parseFloat(process.env.QUEUE_LIMIT_PER_KEY_PER_SEC || process.env.QUEUE_LIMIT || '1.0');
const MAX_QUEUE_SIZE = parseInt(process.env.MAX_QUEUE_SIZE || '100', 10);
const COOLDOWN_SEC = 65;
const KEY_RETRY_INTERVAL_MS = parseInt(process.env.KEY_RETRY_INTERVAL_MS || '5000', 10);
const MODEL_REFRESH_SEC = parseInt(process.env.MODEL_REFRESH_SEC || '600', 10);
const NVIDIA_BASE_URL = (process.env.NVIDIA_BASE_URL || 'https://integrate.api.nvidia.com').replace(/\/+$/, '');
const NVIDIA_GENAI_URL = (process.env.NVIDIA_GENAI_URL || 'https://ai.api.nvidia.com').replace(/\/+$/, '');
const NVIDIA_NVCF_URL = (process.env.NVIDIA_NVCF_URL || 'https://api.nvcf.nvidia.com').replace(/\/+$/, '');

const KEY_LEVEL_RPM_RATIO = parseFloat(process.env.KEY_LEVEL_RPM_RATIO || '0.8');
const CORROBORATION_WINDOW_S = parseInt(process.env.CORROBORATION_WINDOW_S || '60', 10);
const MODEL_BLOCK_CAP = parseInt(process.env.MODEL_BLOCK_CAP || '10', 10);
const KEY_BLOCK_CAP = parseInt(process.env.KEY_BLOCK_CAP || '30', 10);
const MODEL_BLOCK_DEFAULT_SECS = parseInt(process.env.MODEL_BLOCK_DEFAULT_SECS || '8', 10);

// (PATCH-002) selector scoring weights
const USE_SCORE_SELECTOR = (process.env.USE_SCORE_SELECTOR || 'true').toLowerCase() !== 'false';
const MODEL_PENALTY_WEIGHT = parseFloat(process.env.MODEL_PENALTY_WEIGHT || '0.20');
const PROVIDER_PENALTY_WEIGHT = parseFloat(process.env.PROVIDER_PENALTY_WEIGHT || '0.25');

const MODEL_429_HINTS = (process.env.MODEL_429_HINTS ||
  'for this model,per-model,per model,requests for model,model is rate,model rate limit,this model,model_rate_limit')
  .split(',').map(h => h.trim().toLowerCase()).filter(Boolean);

const KEY_429_HINTS = (process.env.KEY_429_HINTS ||
  'account,api key,api-key,apikey,organization,your key,credential')
  .split(',').map(h => h.trim().toLowerCase()).filter(Boolean);

// Simple Promise-based Mutex
class Mutex {
  constructor() {
    this._queue = [];
    this._locked = false;
  }
  async acquire() {
    if (!this._locked) {
      this._locked = true;
      return;
    }
    return new Promise(resolve => this._queue.push(resolve));
  }
  release() {
    if (this._queue.length > 0) {
      const next = this._queue.shift();
      next();
    } else {
      this._locked = false;
    }
  }
}

// ── Key Entry ──────────────────────────────────────────────────────────
class KeyEntry {
  constructor(label, apiKey) {
    this.label = label;
    this.apiKey = apiKey;
    this.softRpm = SOFT_LIMIT_RPM;
    this.hardRpm = HARD_LIMIT_RPM;
    this.timestamps = [];          // list of request timestamps (epoch seconds)
    this.hardBlockedUntil = 0.0;   // epoch seconds
    this.modelBlocks = {};         // model_name -> blocked_until epoch seconds
    this.detectedLimit = null;     // learned limit
    this.totalRequests = 0;
    this.total429s = 0;
    this.totalKey429s = 0;
    this.totalModel429s = 0;
    this.totalRotationsCaused = 0;
    this.lastUsed = 0.0;           // epoch seconds
    this.lastAdmit = 0.0;          // epoch seconds of last queue hand-off
    this.inFlight = 0;
  }

  get effectiveLoad() {
    return this.currentRpm() + this.inFlight;
  }

  incrementInFlight() {
    this.inFlight++;
  }

  decrementInFlight() {
    if (this.inFlight > 0) {
      this.inFlight--;
    }
  }

  admitReady(interval) {
    if (interval <= 0) return true;
    return (Date.now() / 1000 - this.lastAdmit) >= interval;
  }

  secondsUntilAdmit(interval) {
    if (interval <= 0) return 0.0;
    return Math.max(0.0, interval - (Date.now() / 1000 - this.lastAdmit));
  }

  currentRpm(window = 60) {
    const now = Date.now() / 1000;
    this.timestamps = this.timestamps.filter(t => now - t < window);
    return this.timestamps.length;
  }

  effectiveHardLimit(configuredHard) {
    if (this.detectedLimit && this.detectedLimit < configuredHard) {
      return this.detectedLimit;
    }
    return configuredHard;
  }

  effectiveSoftLimit(configuredSoft, configuredHard) {
    const hard = this.effectiveHardLimit(configuredHard);
    return Math.max(1, Math.min(configuredSoft, hard - 1));
  }

  secondsUntilBelow(limit, window = 60) {
    const now = Date.now() / 1000;
    const ts = this.timestamps.filter(t => now - t < window).sort((a, b) => a - b);
    const rpm = ts.length;
    if (rpm < limit) return 0.0;
    const idx = rpm - limit;
    if (idx < 0 || idx >= ts.length) return 0.0;
    return Math.max(0.0, window - (now - ts[idx]));
  }

  isHardBlocked() {
    return (Date.now() / 1000) < this.hardBlockedUntil;
  }

  isModelBlocked(model) {
    if (!model) return false;
    const until = this.modelBlocks[model];
    if (!until) return false;
    if ((Date.now() / 1000) < until) return true;
    delete this.modelBlocks[model]; // expired
    return false;
  }

  activeModelBlocks() {
    const now = Date.now() / 1000;
    const out = {};
    for (const [m, until] of Object.entries(this.modelBlocks)) {
      const rem = until - now;
      if (rem > 0) {
        out[m] = Math.round(rem * 10) / 10;
      } else {
        delete this.modelBlocks[m];
      }
    }
    return out;
  }

  record() {
    const now = Date.now() / 1000;
    this.timestamps.push(now);
    this.totalRequests++;
    this.lastUsed = now;
  }

  recordRateLimit(retryAfterSec = 65) {
    this.hardBlockedUntil = (Date.now() / 1000) + retryAfterSec;
    this.total429s++;
    this.totalKey429s++;
    this.timestamps.push(Date.now() / 1000); // counts against RPM
  }

  onRateLimit(scope, model = null, retryAfter = null, detectedLimit = null) {
    const now = Date.now() / 1000;
    this.total429s++;
    this.totalRotationsCaused++;
    const rawSecs = retryAfter ? retryAfter : MODEL_BLOCK_DEFAULT_SECS;
    let blockSecs;
    if (scope === 'model' && model) {
      blockSecs = Math.min(rawSecs, MODEL_BLOCK_CAP);
      this.modelBlocks[model] = now + blockSecs;
      this.totalModel429s++;
      console.warn(`[wrapper-nvidia] Key ${this.label}: MODEL '${model}' rate-limited — blocked ${blockSecs}s`);
    } else {
      blockSecs = Math.min(rawSecs, KEY_BLOCK_CAP);
      this.hardBlockedUntil = now + blockSecs;
      this.totalKey429s++;
      if (detectedLimit) {
        const old = this.detectedLimit;
        this.detectedLimit = detectedLimit;
        if (old !== detectedLimit) {
          console.warn(`[wrapper-nvidia] Key ${this.label}: detected actual limit = ${detectedLimit} rpm`);
        }
      }
      console.warn(`[wrapper-nvidia] Key ${this.label}: KEY-LEVEL rate-limited — whole key blocked ${blockSecs}s`);
    }
  }

  stats(soft, hard) {
    const rpm = this.currentRpm();
    const effHard = this.effectiveHardLimit(hard);
    const effSoft = this.effectiveSoftLimit(soft, hard);
    const now = Date.now() / 1000;
    return {
      label: this.label,
      key_prefix: this.apiKey ? (this.apiKey.slice(0, 16) + '...') : 'unknown',
      current_rpm: rpm,
      in_flight: this.inFlight,
      effective_load: this.effectiveLoad,
      configured_soft: soft,
      configured_hard: hard,
      effective_soft: effSoft,
      effective_hard: effHard,
      detected_limit: this.detectedLimit,
      utilization_pct: effHard ? Math.round((rpm / effHard * 100) * 10) / 10 : 0,
      hard_blocked: this.isHardBlocked(),
      hard_blocked_remaining_s: Math.max(0, Math.round((this.hardBlockedUntil - now) * 10) / 10),
      model_blocks: this.activeModelBlocks(),
      total_requests: this.totalRequests,
      total_429s: this.total429s,
      total_key_429s: this.totalKey429s,
      total_model_429s: this.totalModel429s,
      total_rotations_caused: this.totalRotationsCaused,
      last_used_ago_s: this.lastUsed ? Math.round((now - this.lastUsed) * 10) / 10 : null,
      last_admit_ago_s: this.lastAdmit ? Math.round((now - this.lastAdmit) * 100) / 100 : null,
    };
  }
}

class KeyPool {
  constructor() {
    // (PATCH-002) score-adjustment penalty maps (label|model) -> penalty score 0..1
    this._modelPenalty = new Map();    // "label/model" -> sum of recent model 429 penalties (decays)
    this._providerPenalty = new Map(); // "integrate.api.nvidia.com" -> provider-level penalty 0..1
    // raw counts (last 60s window) that drive the penalties
    this._modelPenaltyRaw = new Map();
    this._providerPenaltyRaw = new Map();
    this._lastPenaltyDecay = Date.now();
    this.ramping = {
      model: new Map(),
    };
    this.keys = [];
    this.softLimit = SOFT_LIMIT_RPM;
    this.hardLimit = HARD_LIMIT_RPM;
    this.pacing = true;
    // (BUGFIX audit-2026-06-30: pacingMaxWait was hardcoded to 60s — combined
    // with `pacing = true`, every code path that returned without an explicit
    // key (e.g. proxyOpenai returning before all keys admitted) would silently
    // stall for up to 60s waiting for ticket rotation. Now configurable
    // via env, default 30s. Lower bound 5s; still large enough for genuine
    // upstream batching but small enough for the wrapper to fail fast.)
    this.pacingMaxWait = parseFloat(process.env.PACING_MAX_WAIT || '30.0');
    if (this.pacingMaxWait < 5) this.pacingMaxWait = 5;
    this.queueLimit = QUEUE_LIMIT_PER_KEY_PER_SEC;
    this.maxQueueSize = MAX_QUEUE_SIZE;
    this._admitInterval = this.queueLimit > 0 ? 1.0 / this.queueLimit : 0.0;
    this._lock = new Mutex();
    this._recent429 = [];
    this._modelTs = {};
    this._modelLimit = {};
    this._keyModelLimit = {};
    this._modelTsByKey = {};
    this._rrIndex = 0;
    this._ticketSeq = 0;
    this._waiting = new Set();

    this._idx = 0;
    this._modelsCache = [];
    this._modelsCacheTs = 0;
    this._initErrors = [];
    this._agent = new Agent({ connections: 50, pipelining: 10 });
  }

  /** Load keys from environment. */
  loadFromEnv() {
    this.keys = [];
    this._initErrors = [];
    const keysSeen = new Set();
    const envKeys = [];
    for (const [k, v] of Object.entries(process.env)) {
      const m = k.match(/^NVIDIA_API_KEY(_\d+)?$/);
      if (!m) continue;
      if (!v || v.length < 10) {
        this._initErrors.push(`${k}: empty or too short, skipped`);
        continue;
      }
      if (keysSeen.has(v)) continue;
      keysSeen.add(v);
      envKeys.push(v);
    }

    if (envKeys.length === 0) {
      throw new Error('No NVIDIA_API_KEY* found in environment');
    }

    this.keys = envKeys.map((k, i) => new KeyEntry(`key${i + 1}`, k));
    console.info(`[wrapper-nvidia] Loaded ${this.keys.length} key(s) | soft=${this.softLimit} hard=${this.hardLimit} rpm`);
    return this;
  }

  /** Count helpers. */
  get totalKeys()       { return this.keys.length; }
  get availableKeys()   { return this.keys.filter(k => !k.isHardBlocked() && k.currentRpm() < k.effectiveHardLimit(this.hardLimit)).length; }
  get blockedKeys()     { return this.keys.filter(k => k.isHardBlocked()).length; }
  get exhaustedCount()  { return this.keys.filter(k => k.effectiveLoad >= k.effectiveHardLimit(this.hardLimit)).length; }

  get totalSoftCapacity() {
    return this.keys.reduce((sum, k) => {
      return sum + Math.max(0, k.effectiveSoftLimit(this.softLimit, this.hardLimit) - k.currentRpm());
    }, 0);
  }

  // ── Per-model rate pattern ───────────────────────────────────────────
  recordModel(model, keyLabel = null) {
    if (!model) return;
    const now = Date.now() / 1000;
    if (keyLabel) {
      const k = `${keyLabel}/${model}`;
      if (!this._modelTsByKey[k]) this._modelTsByKey[k] = [];
      this._modelTsByKey[k].push(now);
    }
    if (!this._modelTs[model]) this._modelTs[model] = [];
    this._modelTs[model].push(now);
  }

  modelRpm(model, window = 60) {
    if (!model) return 0;
    const now = Date.now() / 1000;
    const list = (this._modelTs[model] || []).filter(t => now - t < window);
    this._modelTs[model] = list;
    return list.length;
  }

  keyModelRpm(keyLabel, model, window = 60) {
    if (!keyLabel || !model) return 0;
    const now = Date.now() / 1000;
    const k = `${keyLabel}/${model}`;
    const list = (this._modelTsByKey[k] || []).filter(t => now - t < window);
    this._modelTsByKey[k] = list;
    return list.length;
  }

  noteModel429(model, observedRpm, keyLabel = null) {
    if (!model) return;
    const val = Math.max(1, Math.floor(observedRpm));
    if (keyLabel) {
      const k = `${keyLabel}/${model}`;
      const cur = this._keyModelLimit[k];
      this._keyModelLimit[k] = cur ? Math.min(cur, val) : val;
    }
    const curG = this._modelLimit[model];
    this._modelLimit[model] = curG ? Math.min(curG, val) : val;
  }

  modelLimit(model, keyLabel = null) {
    if (keyLabel) {
      const k = `${keyLabel}/${model}`;
      if (this._keyModelLimit[k] !== undefined) {
        return this._keyModelLimit[k];
      }
      return null;
    }
    return this._modelLimit[model] || null;
  }

  // ── Classification ───────────────────────────────────────────────────
  _classify429(state, model, bodyText, rpmAt429, effHard) {
    const txt = (bodyText || '').toLowerCase();

    // Signal 1
    if (model && txt.includes(model.toLowerCase())) {
      return ['model', 'model-name-in-body'];
    }
    if (MODEL_429_HINTS.some(h => txt.includes(h))) {
      return ['model', 'model-hint-in-body'];
    }
    if (KEY_429_HINTS.some(h => txt.includes(h))) {
      return ['key', 'key-hint-in-body'];
    }

    // Signal 2
    const now = Date.now() / 1000;
    this._recent429 = this._recent429.filter(item => now - item.ts < CORROBORATION_WINDOW_S);
    const otherKeysForModel = new Set();
    const otherModelsForKey = new Set();
    for (const item of this._recent429) {
      if (item.model === model && item.keyLabel !== state.label) {
        otherKeysForModel.add(item.keyLabel);
      }
      if (item.keyLabel === state.label && item.model !== model) {
        otherModelsForKey.add(item.model);
      }
    }
    if (otherModelsForKey.size >= 1) {
      return ['key', 'multi-model-on-key'];
    }
    if (otherKeysForModel.size >= 1) {
      return ['model', 'multi-key-for-model'];
    }

    // Signal 3
    if (effHard && rpmAt429 >= effHard * KEY_LEVEL_RPM_RATIO) {
      return ['key', `rpm-near-cap(${rpmAt429}/${effHard})`];
    }
    return ['model', `rpm-low(${rpmAt429}/${effHard})`];
  }

  async registerRateLimit(state, model, retryAfter, detectedLimit, bodyText = '') {
    await this._lock.acquire();
    try {
      const rpm = state.currentRpm();
      const effHard = state.effectiveHardLimit(this.hardLimit);
      const [scope, reason] = this._classify429(state, model, bodyText, rpm, effHard);
      state.onRateLimit(scope, model, retryAfter, detectedLimit);
      this._recent429.push({ ts: Date.now() / 1000, keyLabel: state.label, model });
      if (scope === 'model' && model) {
        this.noteModel429(model, this.modelRpm(model), state.label);
      }
      return [scope, reason];
    } finally {
      this._lock.release();
    }
  }

  // ── Selection & Pacing Queue ─────────────────────────────────────────

  async acquire(model = null, signal = null) {
    const start = Date.now();
    const [chosen, waitedS] = await this.acquireSlot(model, signal);
    return {
      key: chosen,
      waitedMs: Math.round(waitedS * 1000)
    };
  }

  async acquireSlot(model = null, signal = null) {
    const start = Date.now() / 1000;
    const soft = this.softLimit;
    const hard = this.hardLimit;
    const interval = this._admitInterval;
    let myTicket = null;
    let onAbort = null;
    let abortPromise = null;

    if (this._waiting.size >= this.maxQueueSize) {
      console.warn(`[wrapper-nvidia] Queue backpressure load shed: waiting queue size ${this._waiting.size} exceeds max ${this.maxQueueSize}. Rejecting request.`);
      return [null, 0.0];
    }

    // (PATCH-004) provider circuit breaker — short-circuit if upstream is OPEN
    if (typeof acquireProviderCircuitCheck === 'function' && acquireProviderCircuitCheck()) {
      return [null, 0.0];
    }

    if (signal) {
      if (signal.aborted) {
        return [null, 0.0];
      }
      abortPromise = new Promise((resolve) => {
        onAbort = () => resolve(true);
        signal.addEventListener('abort', onAbort);
      });
    }

    try {
      while (true) {
        // Check if connection is aborted/destroyed
        if (signal && signal.aborted) {
          break;
        }

        await this._lock.acquire();
        let sleepDuration = 0;
        let shouldSleep = true;
        try {
          if (myTicket === null) {
            myTicket = this._ticketSeq++;
            this._waiting.add(myTicket);
          }
          const now = Date.now() / 1000;
          const avail = this.keys.filter(s => !s.isHardBlocked() && !s.isModelBlocked(model));

          // Saturation checks
          let modelSaturated = false;
          if (model) {
            let allSaturated = true;
            for (const s of avail) {
              const kml = this._keyModelLimit[`${s.label}/${model}`];
              if (kml !== undefined && this.keyModelRpm(s.label, model) >= Math.max(1, Math.floor(kml * 0.9))) {
                continue;
              } else {
                allSaturated = false;
                break;
              }
            }
            if (avail.length > 0 && allSaturated) {
              modelSaturated = true;
            }
          }

          let chosen = null;
          let wait = null;

          if (avail.length > 0 && !modelSaturated) {
            const IDLE_RPM = 3;
            const rpmOk = (s) => {
              const current = s.currentRpm();
              if (current < IDLE_RPM) return true;
              const lim = this.pacing ? s.effectiveSoftLimit(soft, hard) : s.effectiveHardLimit(hard);
              return current < lim;
            };

            const admitOk = (s) => s.admitReady(interval);

            let ready = avail.filter(s => rpmOk(s) && admitOk(s));

            if (model) {
              ready = ready.filter(s => {
                const kml = this._keyModelLimit[`${s.label}/${model}`];
                if (kml !== undefined && this.keyModelRpm(s.label, model) >= Math.max(1, Math.floor(kml * 0.9))) {
                  return false;
                }
                return true;
              });
            }

            const rank = Array.from(this._waiting).filter(t => t < myTicket).length;

            if (ready.length > 0 && (interval <= 0 || rank < ready.length)) {
              chosen = this._pickKey(ready);
              chosen.record();
              chosen.incrementInFlight();
              chosen.lastAdmit = now;
              this.recordModel(model, chosen.label);
              this._waiting.delete(myTicket);
              myTicket = null;
              shouldSleep = false;
              return [chosen, (Date.now() / 1000) - start];
            }

            const waits = [];
            for (const s of avail) {
              const rpmW = s.secondsUntilBelow(s.effectiveSoftLimit(soft, hard));
              const admW = s.secondsUntilAdmit(interval);
              waits.push(Math.max(rpmW, admW));
            }
            wait = waits.length > 0 ? Math.min(...waits) : 1.0;
          } else if (modelSaturated) {
            wait = 1.0;
          } else {
            const [secs] = this.retryHint(model);
            wait = secs;
          }

          wait = Math.max(0.02, Math.min(wait !== null ? wait : 1.0, 5.0));

          const elapsed = (Date.now() / 1000) - start;
          if (elapsed >= this.pacingMaxWait) {
            shouldSleep = false;
            break;
          }

          sleepDuration = Math.min(wait, this.pacingMaxWait - elapsed + 0.01);
        } finally {
          this._lock.release();
        }

        if (shouldSleep) {
          if (abortPromise) {
            let timeoutId;
            const sleepPromise = new Promise(resolve => {
              timeoutId = setTimeout(() => resolve(false), sleepDuration * 1000);
            });
            const wasAborted = await Promise.race([sleepPromise, abortPromise]);
            clearTimeout(timeoutId);
            if (wasAborted) {
              break;
            }
          } else {
            await new Promise(resolve => setTimeout(resolve, sleepDuration * 1000));
          }
        }
      }

      if (myTicket !== null) {
        this._waiting.delete(myTicket);
      }
      return [null, (Date.now() / 1000) - start];
    } finally {
      if (signal && onAbort) {
        signal.removeEventListener('abort', onAbort);
      }
    }
  }

  retryHint(model = null) {
    const now = Date.now() / 1000;
    if (this.keys.length > 0 && this.keys.every(s => s.isHardBlocked())) {
      const secs = Math.min(...this.keys.map(s => s.hardBlockedUntil - now));
      return [Math.max(1, Math.round(secs)), 'all_keys'];
    }

    const live = this.keys.filter(s => !s.isHardBlocked());
    if (model && live.length > 0 && live.every(s => s.isModelBlocked(model))) {
      const secs = Math.min(...live.map(s => s.modelBlocks[model] - now).filter(rem => rem > 0));
      return [Math.max(1, Math.round(secs)), 'model'];
    }

    return [MODEL_BLOCK_DEFAULT_SECS, 'capacity'];
  }

  releaseSuccess(key) {
    if (key) key.decrementInFlight();
  }

  releaseFailure(key) {
    if (key) key.decrementInFlight();
  }

  releaseRateLimited(key, retryAfterSec = 65) {
    if (key) {
      key.decrementInFlight();
      key.recordRateLimit(retryAfterSec);
    }
  }

  _pickKey(ready) {
    const labels = this.keys.map(s => s.label);
    const rrDistance = (s) => {
      const idx = labels.indexOf(s.label);
      if (idx === -1) return labels.length;
      return (idx - this._rrIndex + labels.length) % labels.length;
    };

    // Sort ready keys by:
    // (PATCH-002) composite score when USE_SCORE_SELECTOR=true, else legacy effectiveLoad.
    // Score = availability_signal - (model_penalty * MODEL_PENALTY_WEIGHT) - (provider_penalty * PROVIDER_PENALTY_WEIGHT).
    // Lower score = worse candidate; we pick the LOWEST score (= most available).
    this._decayPenalties();
    const candidates = [...ready].sort((a, b) => {
      if (USE_SCORE_SELECTOR) {
        const sa = this._scoreFor(a);
        const sb = this._scoreFor(b);
        if (sa !== sb) return sa - sb;
      } else {
        if (a.effectiveLoad !== b.effectiveLoad) {
          return a.effectiveLoad - b.effectiveLoad;
        }
      }
      if (a.totalRequests !== b.totalRequests) {
        return a.totalRequests - b.totalRequests;
      }
      return rrDistance(a) - rrDistance(b);
    });

    const chosen = candidates[0];
    if (chosen) {
      const chosenIdx = labels.indexOf(chosen.label);
      if (chosenIdx !== -1) {
        this._rrIndex = (chosenIdx + 1) % labels.length;
      }
    }
    return chosen;
  }

  /** Initialize the totalRequests count from the SQLite database at boot/reload. */
  initializeKeyRequests(counts) {
    if (!counts) return;
    for (const s of this.keys) {
      if (counts[s.label] !== undefined) {
        s.totalRequests = counts[s.label];
      }
    }
  }


  peekKey() {
    for (const s of this.keys) {
      if (!s.isHardBlocked()) return s;
    }
    return this.keys[0] || null;
  }

  // ── PATCH-002 scoring helpers ──────────────────────────────────────────
  /** Increment penalty for a single (keyLabel, model) failure record. */
  recordModelPenalty(keyLabel, model) {
    if (!keyLabel || !model) return;
    const k = `${keyLabel}/${model}`;
    this._modelPenaltyRaw.set(k, (this._modelPenaltyRaw.get(k) || 0) + 1);
  }
  /** Increment provider-level penalty for provider-wide failures (CLASS C). */
  recordProviderPenalty(provider) {
    if (!provider) return;
    this._providerPenaltyRaw.set(provider, (this._providerPenaltyRaw.get(provider) || 0) + 1);
  }
  /** Decay raw counters every 5s; recompute normalized penalties [0,1]. */
  _decayPenalties() {
    const now = Date.now();
    if (now - this._lastPenaltyDecay < 5000) return;
    this._lastPenaltyDecay = now;
    const halfLife = 60_000; // raw halves every 60s
    const factor = Math.pow(0.5, (now - (this._lastPenaltyDecay)) / halfLife);
    // model penalties: max raw across map -> 1.0 normalizer
    let maxM = 0;
    const normM = new Map();
    for (const [k, v] of this._modelPenaltyRaw.entries()) {
      const decayed = v * factor;
      this._modelPenaltyRaw.set(k, decayed);
      normM.set(k, decayed);
      if (decayed > maxM) maxM = decayed;
    }
    this._modelPenalty = new Map();
    if (maxM > 0) {
      for (const [k, v] of normM.entries()) this._modelPenalty.set(k, Math.min(1, v / maxM));
    }
    let maxP = 0;
    const normP = new Map();
    for (const [k, v] of this._providerPenaltyRaw.entries()) {
      const decayed = v * factor;
      this._providerPenaltyRaw.set(k, decayed);
      normP.set(k, decayed);
      if (decayed > maxP) maxP = decayed;
    }
    this._providerPenalty = new Map();
    if (maxP > 0) {
      for (const [k, v] of normP.entries()) this._providerPenalty.set(k, Math.min(1, v / maxP));
    }
  }
  /** Lower = better. effectiveLoad baseline + penalty weight. */
  _scoreFor(keyObj) {
    if (!USE_SCORE_SELECTOR) return keyObj.effectiveLoad;
    const mPenalty = this._modelPenalty.get(keyObj.label) || 0;
    const pPenalty = this._providerPenalty.get('integrate.api.nvidia.com') || 0;
    return keyObj.effectiveLoad
      + mPenalty * MODEL_PENALTY_WEIGHT * 100
      + pPenalty * PROVIDER_PENALTY_WEIGHT * 100;
  }

  async syncKeys(keysList) {
    if (!keysList || keysList.length === 0) return false;
    await this._lock.acquire();
    try {
      const existing = {};
      for (const s of this.keys) {
        existing[s.apiKey] = s;
      }
      const newKeys = [];
      for (let i = 0; i < keysList.length; i++) {
        const k = keysList[i];
        let st = existing[k];
        if (!st) {
          st = new KeyEntry(`key${i + 1}`, k);
        } else {
          st.label = `key${i + 1}`;
        }
        newKeys.push(st);
      }

      const oldSet = new Set(Object.keys(existing));
      const newSet = new Set(keysList);

      let same = oldSet.size === newSet.size;
      if (same) {
        for (const k of newSet) {
          if (!oldSet.has(k)) { same = false; break; }
        }
      }

      if (same && newKeys.length === this.keys.length) {
        this.keys = newKeys;
        return false;
      }

      const added = keysList.filter(k => !oldSet.has(k));
      const removed = Array.from(oldSet).filter(k => !newSet.has(k));
      this.keys = newKeys;
      console.info(`[wrapper-nvidia] Key pool synced: +${added.length} / -${removed.length} -> ${this.keys.length} total key(s)`);
      return true;
    } finally {
      this._lock.release();
    }
  }

  async syncLimits({ soft, hard, queueLimit, maxQueueSize } = {}) {
    await this._lock.acquire();
    try {
      if (soft !== undefined && !isNaN(soft) && soft !== this.softLimit) {
        console.info(`[wrapper-nvidia] Soft limit synced: ${this.softLimit} -> ${soft} RPM`);
        this.softLimit = soft;
      }
      if (hard !== undefined && !isNaN(hard) && hard !== this.hardLimit) {
        console.info(`[wrapper-nvidia] Hard limit synced: ${this.hardLimit} -> ${hard} RPM`);
        this.hardLimit = hard;
      }
      if (queueLimit !== undefined && !isNaN(queueLimit) && queueLimit !== this.queueLimit) {
        console.info(`[wrapper-nvidia] Queue limit synced: ${this.queueLimit} -> ${queueLimit} QPS`);
        this.queueLimit = queueLimit;
        this._admitInterval = queueLimit > 0 ? 1.0 / queueLimit : 0.0;
      }
      if (maxQueueSize !== undefined && !isNaN(maxQueueSize) && maxQueueSize !== this.maxQueueSize) {
        console.info(`[wrapper-nvidia] Max queue size synced: ${this.maxQueueSize} -> ${maxQueueSize}`);
        this.maxQueueSize = maxQueueSize;
      }
    } finally {
      this._lock.release();
    }
  }

  blockedModels() {
    const now = Date.now() / 1000;
    const agg = {};
    for (const s of this.keys) {
      for (const [m, until] of Object.entries(s.modelBlocks)) {
        const rem = until - now;
        if (rem <= 0) continue;
        if (!agg[m]) {
          agg[m] = { keys: [], retry_s: rem };
        }
        agg[m].keys.push(s.label);
        agg[m].retry_s = Math.min(agg[m].retry_s, rem);
      }
    }
    for (const m of Object.keys(agg)) {
      agg[m].retry_s = Math.round(agg[m].retry_s * 10) / 10;
    }
    return agg;
  }

  async resetCounters() {
    await this._lock.acquire();
    try {
      for (const s of this.keys) {
        s.totalRequests = 0;
        s.total429s = 0;
        s.totalKey429s = 0;
        s.totalModel429s = 0;
        s.totalRotationsCaused = 0;
        s.inFlight = 0;
      }
      this._recent429 = [];
      console.info('[wrapper-nvidia] Per-key cumulative counters reset');
    } finally {
      this._lock.release();
    }
  }

  async healInFlight() {
    let totalFixed = 0;
    await this._lock.acquire();
    try {
      for (const s of this.keys) {
        if (s.inFlight > 0) {
          console.warn(`[wrapper-nvidia] heal_in_flight: ${s.label} in_flight ${s.inFlight} -> 0`);
          s.inFlight = 0;
          totalFixed++;
        }
      }
      console.info(`[wrapper-nvidia] heal_in_flight: ${totalFixed} key(s) corrected`);
    } finally {
      this._lock.release();
    }
  }

  allStats() {
    return this.keys.map(s => s.stats(this.softLimit, this.hardLimit));
  }

  summary() {
    const stats = this.allStats();
    const learnedKeyModelLimitsFormatted = {};
    for (const [k, v] of Object.entries(this._keyModelLimit)) {
      learnedKeyModelLimitsFormatted[k] = v;
    }
    return {
      total_keys: stats.length,
      available_keys: this.keys.filter(s => !s.isHardBlocked() && s.currentRpm() < s.effectiveHardLimit(this.hardLimit)).length,
      blocked_models: this.blockedModels(),
      learned_model_limits: this._modelLimit,
      learned_key_model_limits: learnedKeyModelLimitsFormatted,
    };
  }

  // ── Model Cache ─────────────────────────────────────────────────────

  async _fetchModels() {
    const key = this.peekKey();
    if (!key) return [];

    try {
      const resp = await undiciFetch(`${NVIDIA_BASE_URL}/v1/models`, {
        headers: { 'Authorization': `Bearer ${key.apiKey}`, 'Accept': 'application/json' },
        dispatcher: this._agent,
      });

      let respText = '';
      try { respText = await resp.text(); } catch {}

      if (!resp.ok) {
        this.releaseRateLimited(key, resp.status === 429 ? 65 : 10);
        return [];
      }
      this.releaseSuccess(key);

      let body;
      try { body = JSON.parse(respText); } catch {}
      const models = (body && body.data || []).map(m => m.id).filter(Boolean).sort();
      return models;
    } catch (e) {
      return [];
    }
  }

  async refreshModels(force = false) {
    const now = Date.now();
    if (!force && now - this._modelsCacheTs < MODEL_REFRESH_SEC * 1000 && this._modelsCache.length > 0) {
      return this._modelsCache;
    }
    const models = await this._fetchModels();
    if (models.length > 0) {
      this._modelsCache = models;
      this._modelsCacheTs = now;
    }
    return this._modelsCache;
  }

  get modelsCached() { return this._modelsCache; }

  /** Start background model refresh. */
  startModelRefresh() {
    const refresh = async () => {
      try { await this.refreshModels(); } catch {}
    };
    refresh();
    setInterval(refresh, MODEL_REFRESH_SEC * 1000);
  }

  // ── Prometheus Metrics ───────────────────────────────────────────────

  promMetrics() {
    const lines = [];
    lines.push(`# HELP wrapper_nvidia_keys_total Total API keys loaded`);
    lines.push(`# TYPE wrapper_nvidia_keys_total gauge`);
    lines.push(`wrapper_nvidia_keys_total ${this.totalKeys}`);
    lines.push(`# HELP wrapper_nvidia_keys_available Keys with soft RPM capacity`);
    lines.push(`# TYPE wrapper_nvidia_keys_available gauge`);
    lines.push(`wrapper_nvidia_keys_available ${this.availableKeys}`);
    lines.push(`# HELP wrapper_nvidia_keys_blocked Keys blocked (exhausted/cooldown)`);
    lines.push(`# TYPE wrapper_nvidia_keys_blocked gauge`);
    lines.push(`wrapper_nvidia_keys_blocked ${this.blockedKeys}`);
    lines.push(`# HELP wrapper_nvidia_rpm_total Total virtual RPM capacity across all keys`);
    lines.push(`# TYPE wrapper_nvidia_rpm_total gauge`);
    lines.push(`wrapper_nvidia_rpm_total ${this.totalSoftCapacity}`);
    lines.push(`# HELP wrapper_nvidia_in_flight_total Currently processing requests`);
    lines.push(`# TYPE wrapper_nvidia_in_flight_total gauge`);
    lines.push(`wrapper_nvidia_in_flight_total ${this._inFlight || 0}`);
    lines.push(`# HELP wrapper_nvidia_avg_latency_ms_24h Average latency last 24h`);
    lines.push(`# TYPE wrapper_nvidia_avg_latency_ms_24h gauge`);
    lines.push(`wrapper_nvidia_avg_latency_ms_24h ${this._avgLatency24h || 0}`);
    lines.push(`# HELP wrapper_nvidia_exhaustions_total_24h Key exhaustion events last 24h`);
    lines.push(`# TYPE wrapper_nvidia_exhaustions_total_24h gauge`);
    lines.push(`wrapper_nvidia_exhaustions_total_24h ${this._exhaust24h || 0}`);
    lines.push(`# HELP wrapper_nvidia_models_cached Number of cached model IDs`);
    lines.push(`# TYPE wrapper_nvidia_models_cached gauge`);
    lines.push(`wrapper_nvidia_models_cached ${this._modelsCache.length}`);
    // Per-key metrics (like Python)
    const stats = this.allStats();
    for (const s of stats) {
      const label = s.label || 'unknown';
      lines.push(`wrapper_nvidia_key_rpm{key="${label}"} ${s.current_rpm || 0}`);
      lines.push(`wrapper_nvidia_key_in_flight{key="${label}"} ${s.in_flight || 0}`);
      lines.push(`wrapper_nvidia_key_hard_blocked{key="${label}"} ${s.hard_blocked ? 1 : 0}`);
      lines.push(`wrapper_nvidia_key_unused_429_total{key="${label}"} ${s.total_429s || 0}`);
    }
    return lines.join('\n');
  }

  // ── Health JSON ──────────────────────────────────────────────────────

  healthJson() {
    return {
      status: this.availableKeys > 0 ? 'ok' : 'degraded',
      total_keys: this.totalKeys,
      available_keys: this.availableKeys,
      blocked_keys: this.blockedKeys,
      soft_limit_rpm: SOFT_LIMIT_RPM,
      hard_limit_rpm: HARD_LIMIT_RPM,
      queue_limit_per_key_per_sec: QUEUE_LIMIT_PER_KEY_PER_SEC,
      models_cached: this._modelsCache.length,
      version: '4.6.0-node',
    };
  }

  /** Key details for /stats. */
  keyDetails() {
    const now = Date.now() / 1000;
    return this.keys.map(k => {
      return {
        label: k.label,
        soft_rpm: k.softRpm,
        hard_rpm: k.hardRpm,
        soft_used: k.currentRpm(),
        hard_used: k.currentRpm(),
        soft_available: k.effectiveLoad < k.effectiveSoftLimit(this.softLimit, this.hardLimit),
        hard_available: k.effectiveLoad < k.effectiveHardLimit(this.hardLimit),
        ready: !k.isHardBlocked(),
        pacing_ready: k.admitReady(this._admitInterval),
        cooldown_until: k.hardBlockedUntil ? new Date(k.hardBlockedUntil * 1000).toISOString() : null,
        exhaustions: k.totalKey429s,
        total_requests: k.totalRequests,
      };
    });
  }
}

module.exports = { KeyPool, KeyEntry, SOFT_LIMIT_RPM, HARD_LIMIT_RPM, QUEUE_LIMIT_PER_KEY_PER_SEC, NVIDIA_BASE_URL, NVIDIA_GENAI_URL, NVIDIA_NVCF_URL };
