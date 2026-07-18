/**
 * key_pool.js v4.4.0 — Two-tier rate-limited API key pool for NVIDIA NIM.
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
const INFLIGHT_SOFT_CAP = parseInt(process.env.INFLIGHT_SOFT_CAP || '50', 10);

const MODEL_429_HINTS = (process.env.MODEL_429_HINTS ||
  'rate limit exceeded for model,model rate limit exceeded,per-model rate limit,requests for this model exceeded,model quota exceeded,model capacity exceeded')
  .split(',').map(h => h.trim().toLowerCase()).filter(Boolean);

const KEY_429_HINTS = (process.env.KEY_429_HINTS ||
  'account rate limit,api key rate limit,organization rate limit,your key rate limit,credential rate limit,key quota exceeded,account quota exceeded')
  .split(',').map(h => h.trim().toLowerCase()).filter(Boolean);

// Simple Promise-based Mutex — prevents double-release from waking multiple waiters.
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
    const hard = configuredHard || HARD_LIMIT_RPM;
    if (this.detectedLimit && this.detectedLimit < hard) {
      return this.detectedLimit;
    }
    return hard;
  }

  effectiveSoftLimit(configuredSoft, configuredHard) {
    const hard = this.effectiveHardLimit(configuredHard);
    const soft = configuredSoft || SOFT_LIMIT_RPM;
    return Math.max(1, Math.min(soft, hard - 1));
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
    // Count the 429 against RPM history (Bug K8). Without this, currentRpm()
    // is artificially low after a block expires, so the key is reused at full
    // RPM immediately → rapid re-429.
    this.timestamps.push(now);
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
    this.keys = [];
    this.softLimit = SOFT_LIMIT_RPM;
    this.hardLimit = HARD_LIMIT_RPM;
    this.pacing = true;
    this.pacingMaxWait = parseFloat(process.env.PACING_MAX_WAIT || '60');
    this.queueLimit = QUEUE_LIMIT_PER_KEY_PER_SEC;
    this.maxQueueSize = MAX_QUEUE_SIZE;
    this._admitInterval = this.queueLimit > 0 ? 1.0 / this.queueLimit : 0.0;
    // B10 FIX: cache version string at construction time instead of reading
    // package.json on every healthJson() call.
    this._version = '8.6.0-node';
    try {
      const pkg = require('./package.json');
      if (pkg && pkg.version) this._version = `${pkg.version}-node`;
    } catch { /* keep default */ }
    this._lock = new Mutex();
    this._recent429 = [];
    this._modelTs = {};
    this._modelLimit = {};
    this._keyModelLimit = {};
    this._modelTsByKey = {};
    this._rrIndex = 0;
    this._ticketSeq = 0;
    this._waiting = new Map();   // ticket -> requested model (for per-model pacing rank, Bug K6)

    this._idx = 0;
    this._modelsCache = [];
    this._modelsCacheTs = 0;
    this._initErrors = [];
    this._agent = null;
    this._ownsAgent = true;
  }

  setExternalAgent(extAgent) {
    if (this._agent && this._ownsAgent) {
      try { this._agent.close(); } catch {}
    }
    this._agent = extAgent;
    this._ownsAgent = false;
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
      envKeys.push(v.trim());
    }

    // PATCH-B: Allow 0 keys (discovery-only mode). Inference requests
    // will fail at acquire(), but /v1/models discovery still works via
    // anonymous upstream fetch.
    if (envKeys.length === 0) {
      console.warn('[wrapper-nvidia] No NVIDIA_API_KEY* found in environment ' +
        '--- running in discovery-only mode. Inference requests will be rejected.');
    }

    this.keys = envKeys.map((k, i) => new KeyEntry(`key${i + 1}`, k));
    console.info(`[wrapper-nvidia] Loaded ${this.keys.length} key(s) | soft=${this.softLimit} hard=${this.hardLimit} rpm`);
    return this;
  }

  /** Count helpers. */
  get totalKeys()       { return this.keys.length; }
  get availableKeys()   { return this.keys.filter(k => !k.isHardBlocked() && k.currentRpm() < k.effectiveHardLimit(this.hardLimit)).length; }
  // Model-aware availability (Bug K3): a key model-blocked for `model` is
  // counted as available by availableKeys (it's key-healthy + under RPM) yet
  // acquire(model) will reject it. Expose the true per-model count so callers
  // and /health can report accurate capacity for the model actually requested.
  availableForModel(model) {
    if (!model) return this.availableKeys;
    return this.keys.filter(k => !k.isHardBlocked() && !k.isModelBlocked(model) && k.currentRpm() < k.effectiveHardLimit(this.hardLimit)).length;
  }
  get blockedKeys()     { return this.keys.filter(k => k.isHardBlocked()).length; }
  get exhaustedCount()  { return this.keys.filter(k => k.currentRpm() >= k.effectiveHardLimit(this.hardLimit)).length; }

  get totalSoftCapacity() {
    return this.keys.reduce((sum, k) => {
      return sum + Math.max(0, k.effectiveSoftLimit(this.softLimit, this.hardLimit) - k.currentRpm());
    }, 0);
  }

  // ── Per-model rate pattern ───────────────────────────────────────────
  recordModel(model, keyLabel = null) {
    if (!model) return;
    const now = Date.now() / 1000;
    const window = 60;
    if (keyLabel) {
      const k = `${keyLabel}/${model}`;
      if (!this._modelTsByKey[k]) this._modelTsByKey[k] = [];
      this._modelTsByKey[k] = this._modelTsByKey[k].filter(t => now - t < window);
      this._modelTsByKey[k].push(now);
    }
    if (!this._modelTs[model]) this._modelTs[model] = [];
    this._modelTs[model] = this._modelTs[model].filter(t => now - t < window);
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

    // Signal 1 — prefer explicit KEY hints BEFORE the model-name substring
    // test. NVIDIA account/key-level 429 bodies frequently contain the model
    // identifier (e.g. "Rate limit reached for model meta/..."), so the
    // model-name check MUST NOT win or a true key-level 429 is misclassified
    // as model-level → key reused immediately at full speed → cascade 429s
    // (Bug K5).
    if (KEY_429_HINTS.some(h => txt.includes(h))) {
      return ['key', 'key-hint-in-body'];
    }
    if (MODEL_429_HINTS.some(h => txt.includes(h))) {
      return ['model', 'model-hint-in-body'];
    }
    if (model && txt.includes(model.toLowerCase())) {
      return ['model', 'model-name-in-body'];
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

    // B2 FIX: queue size check was OUTSIDE the Mutex lock while _waiting.add()
    // was INSIDE — two concurrent requests could both pass the check and both
    // add, exceeding maxQueueSize. Now the check is inside the lock (below,
    // right before _waiting.add) so it's atomic with the add.
    // Early-exit checks that don't touch _waiting stay outside the lock:
    // load shedding (inFlight cap) and signal abort.

    // Check in-flight soft cap (load shedding) — safe outside lock (reads only)
    if (process.env.LOAD_SHEDDING_ENABLED !== 'false') {
      const totalInFlight = this.keys.reduce((sum, k) => sum + k.inFlight, 0);
      if (totalInFlight >= INFLIGHT_SOFT_CAP) {
        console.warn(`[wrapper-nvidia] Load shedding: total in-flight ${totalInFlight} >= INFLIGHT_SOFT_CAP ${INFLIGHT_SOFT_CAP}. Rejecting with 503.`);
        return [null, 0.0];
      }
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
            // B2 FIX: queue size check is now INSIDE the Mutex lock, atomic
            // with _waiting.add(). Previously the check was outside the lock
            // so two concurrent requests could both pass and both add.
            if (this._waiting.size >= this.maxQueueSize) {
              console.warn(`[wrapper-nvidia] Queue backpressure load shed: waiting queue size ${this._waiting.size} exceeds max ${this.maxQueueSize}. Rejecting request.`);
              shouldSleep = false;
              break; // exits while(true) loop, cleanup in finally below
            }
            myTicket = this._ticketSeq++;
            this._waiting.set(myTicket, model);
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

            // Per-model pacing rank (Bug K6): a burst of requests for model A
            // must not starve a request for model B that has free capacity.
            // Count only waiters queued for the SAME requested model.
            const rank = Array.from(this._waiting.entries())
              .filter(([t, m]) => t < myTicket && (model ? m === model : true)).length;

            // P1-4 FIX: Per-model block should not trigger load shedding for other models
            // Only shed if ALL models on ALL keys are saturated
            const modelSpecificShed = model && avail.every(s => {
              const kml = this._keyModelLimit[`${s.label}/${model}`];
              return kml !== undefined && this.keyModelRpm(s.label, model) >= Math.max(1, Math.floor(kml * 0.9));
            });

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

            // Only apply model-specific wait if ALL keys have this model saturated
            // Otherwise, other models can still proceed
            if (modelSpecificShed) {
              wait = 1.0;
            } else if (avail.length === 0) {
              // No keys at all - shed
              wait = 1.0;
            } else {
              // Has available keys for other models - proceed normally
              // wait already set above
            }
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

  releaseRateLimited(key, retryAfterSec = 65) {
    if (key) {
      key.decrementInFlight();
      key.recordRateLimit(retryAfterSec);
    }
  }

  _pickKey(ready) {
    const load = {};
    for (const s of ready) {
      load[s.label] = s.effectiveLoad;
    }
    const minLoad = Math.min(...Object.values(load));
    const candidates = ready.filter(s => load[s.label] === minLoad);
    if (candidates.length === 1) {
      return candidates[0];
    }
    const labels = this.keys.map(s => s.label);
    const rrDistance = (s) => {
      const idx = labels.indexOf(s.label);
      if (idx === -1) return labels.length;
      return (idx - this._rrIndex + labels.length) % labels.length;
    };
    candidates.sort((a, b) => rrDistance(a) - rrDistance(b));
    const chosen = candidates[0];
    const chosenIdx = labels.indexOf(chosen.label);
    if (chosenIdx !== -1) {
      this._rrIndex = (chosenIdx + 1) % labels.length;
    } else {
      this._rrIndex = 0;
    }
    return chosen;
  }

  peekKey() {
    for (const s of this.keys) {
      if (!s.isHardBlocked() && s.inFlight < 5) return s;
    }
    for (const s of this.keys) {
      if (!s.isHardBlocked()) return s;
    }
    return this.keys[0] || null;
  }

  async syncKeys(keysList) {
    if (!keysList || keysList.length === 0) return false;
    await this._lock.acquire();
    try {
      const oldSet = new Set(this.keys.map(k => k.apiKey));
      const newSet = new Set(keysList);

      // Check if keys are identical (same keys, same order)
      let same = oldSet.size === newSet.size;
      if (same) {
        for (let i = 0; i < keysList.length; i++) {
          if (this.keys[i]?.apiKey !== keysList[i]) { same = false; break; }
        }
      }

      if (same) {
        return false; // No change needed
      }

      // Reuse existing KeyEntry objects keyed by apiKey so in-flight counters,
      // timestamps, detectedLimit, and modelBlocks survive a hot-reload.
      // Re-creating them orphans requests holding the old object and lets
      // concurrency be under-counted → 429 storms under agent load (Bug K1).
      const byApi = new Map(this.keys.map(k => [k.apiKey, k]));
      const newKeys = keysList.map((k, i) => {
        const ex = byApi.get(k);
        if (ex) { ex.label = `key${i + 1}`; return ex; }
        return new KeyEntry(`key${i + 1}`, k);
      });

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
      const now = Date.now() / 1000;
      for (const s of this.keys) {
        // Only heal if key has positive inFlight AND hasn't been used recently.
        // Default 600s (10min) — reasoning models (deepseek-v4-pro) can think
        // for 5+ minutes without sending data. The old 60s threshold caused
        // healInFlight to falsely reset inFlight=0 for actively-streaming keys
        // during long thinking pauses, enabling concurrent-request rate limit
        // violations on the same key.
        const HEAL_THRESHOLD_SEC = parseInt(process.env.HEAL_INFLIGHT_THRESHOLD_SEC || '600', 10);
        if (s.inFlight > 0 && s.lastUsed > 0 && (now - s.lastUsed) > HEAL_THRESHOLD_SEC) {
          console.warn(`[wrapper-nvidia] heal_in_flight: ${s.label} in_flight ${s.inFlight} stuck since lastUsed ${Math.round(now - s.lastUsed)}s ago -> 0`);
          s.inFlight = 0;
          totalFixed++;
        } else if (s.inFlight > 0 && s.lastUsed === 0) {
          // Never used but has inFlight? Stuck.
          console.warn(`[wrapper-nvidia] heal_in_flight: ${s.label} in_flight ${s.inFlight} with no lastUsed -> 0`);
          s.inFlight = 0;
          totalFixed++;
        }
      }
      if (totalFixed > 0) {
        console.info(`[wrapper-nvidia] heal_in_flight: ${totalFixed} key(s) corrected`);
      }
    } finally {
      this._lock.release();
    }
  }

  allStats() {
    return this.keys.map(s => s.stats(this.softLimit, this.hardLimit));
  }

  // keyDetails() is defined below with a richer schema for /stats

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

  get modelsMetadata() {
    return this._modelsMetadata || {};
  }

  async _fetchModels() {
    const agent = this._agent || undefined;

    // ── KEYLESS-FIRST model discovery ──────────────────────────────────────
    // NVIDIA's OpenAI-compatible catalog endpoint (/v1/models) is publicly
    // readable WITHOUT an API key. Prefer keyless so the catalog is available
    // even when every key is exhausted / unset. Fall back to keyed fetch if
    // keyless fails (future auth requirement, network partition, etc.).
    try {
      const resp = await undiciFetch(`${NVIDIA_BASE_URL}/v1/models`, {
        headers: { 'Accept': 'application/json' },
        dispatcher: agent,
        signal: AbortSignal.timeout(20000),
      });
      if (resp.ok) {
        const body = await resp.json();
        const modelsRaw = Array.isArray(body.data) ? body.data
          : Array.isArray(body.models) ? body.models : [];
        const parsed = [];
        this._modelsMetadata = this._modelsMetadata || {};
        for (const m of modelsRaw) {
          const id = typeof m === 'string' ? m : m.id;
          if (!id) continue;
          const cleanId = id.replace(/^(stg|dev|test)\//i, '');
          parsed.push(cleanId);
          if (typeof m === 'object') this._modelsMetadata[cleanId] = m;
        }
        if (parsed.length > 0) {
          parsed.sort();
          if (!this._keylessDiscoveryLogged) {
            console.info(`[wrapper-nvidia] Model catalog fetched KEYLESS from ${NVIDIA_BASE_URL}/v1/models (${parsed.length} models)`);
            this._keylessDiscoveryLogged = true;
          }
          return parsed;
        }
        console.warn(`[wrapper-nvidia] Keyless /v1/models returned 0 usable models; falling back to keyed fetch`);
      } else {
        console.warn(`[wrapper-nvidia] Keyless /v1/models returned HTTP ${resp.status}; falling back to keyed fetch`);
      }
    } catch (e) {
      console.warn(`[wrapper-nvidia] Keyless /v1/models failed (${e.message}); falling back to keyed fetch`);
    }

    // ── KEYED fallback ─────────────────────────────────────────────────────
    const key = this.peekKey();
    if (!key) {
      console.warn(`[wrapper-nvidia] No key available for model-discovery fallback; serving cached/empty list`);
      return [];
    }
    try {
      const resp = await undiciFetch(`${NVIDIA_BASE_URL}/v1/models`, {
        headers: { 'Authorization': `Bearer ${key.apiKey}`, 'Accept': 'application/json' },
        dispatcher: agent,
        signal: AbortSignal.timeout(20000),
      });
      if (!resp.ok) return [];
      const body = await resp.json();
      const modelsRaw = Array.isArray(body.data) ? body.data
        : Array.isArray(body.models) ? body.models : [];
      const parsedModels = [];
      this._modelsMetadata = this._modelsMetadata || {};
      for (const m of modelsRaw) {
        const id = typeof m === 'string' ? m : m.id;
        if (!id) continue;
        const cleanId = id.replace(/^(stg|dev|test)\//i, '');
        parsedModels.push(cleanId);
        if (typeof m === 'object') this._modelsMetadata[cleanId] = m;
      }
      parsedModels.sort();
      return parsedModels;
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

  /** Prune stale entries from rate-tracking maps to prevent memory leaks. */
  pruneStaleEntries() {
    const now = Date.now() / 1000;
    const window = 60;
    // Prune _modelTs — remove entries with no recent timestamps
    for (const model of Object.keys(this._modelTs)) {
      this._modelTs[model] = this._modelTs[model].filter(t => now - t < window);
      if (this._modelTs[model].length === 0) delete this._modelTs[model];
    }
    // Prune _modelTsByKey — same
    for (const k of Object.keys(this._modelTsByKey)) {
      this._modelTsByKey[k] = this._modelTsByKey[k].filter(t => now - t < window);
      if (this._modelTsByKey[k].length === 0) delete this._modelTsByKey[k];
    }
    // Prune _recent429 — keep only within corroboration window
    this._recent429 = this._recent429.filter(item => now - item.ts < CORROBORATION_WINDOW_S);
    // Prune _modelLimit — remove models with no recent activity in _modelTs
    for (const model of Object.keys(this._modelLimit)) {
      const ts = (this._modelTs[model] || []).filter(t => now - t < window * 10);
      if (ts.length === 0) delete this._modelLimit[model];
    }
    // Prune _keyModelLimit — remove entries with no recent activity
    for (const km of Object.keys(this._keyModelLimit)) {
      const [keyLabel, model] = km.split('/');
      const k = `${keyLabel}/${model}`;
      const ts = (this._modelTsByKey[k] || []).filter(t => now - t < window * 10);
      if (ts.length === 0) delete this._keyModelLimit[km];
    }
  }

  /** Start background model refresh. */
  startModelRefresh() {
    const refresh = async () => {
      try { await this.refreshModels(); } catch {}
      this.pruneStaleEntries();
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
      soft_limit_rpm: this.softLimit,
      hard_limit_rpm: this.hardLimit,
      queue_limit_per_key_per_sec: this.queueLimit,
      models_cached: this._modelsCache.length,
      version: this._version,
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
