"""
Key Pool Manager v1 — Blackbox AI Rate-Limit Proxy.
Adapted from wrapper-nvidia key_pool.py for Blackbox AI.

Two-tier rate limiting (KEY-level + MODEL-level).

Tier 1 — KEY-level (account / RPM cap): when a key is rate-limited the WHOLE
key is blocked; every model on it becomes unavailable until the block expires.
This is the proactive sliding-window cap (HARD_LIMIT_RPM) plus reactive 429s
classified as key-level.

Tier 2 — MODEL-level: a specific model is throttled while the key itself is fine
(e.g. gpt-5.5 limited but the key is at 5 rpm). Only the (key, model) pair is
blocked — other models keep using that key.

Classifying a 429 as KEY vs MODEL uses three signals, in precedence order:
  1. Explicit text in the 429 body (model name / key-account keywords).
  2. Cross-key corroboration (same model 429'd on multiple keys → model;
     one key 429'd across multiple models → key).
  3. Behavioural RPM heuristic (key near its own cap → key; key idle → model).

Both block tiers are time-based and auto-recover: once retry_after elapses the
key / (key, model) is reused automatically.
"""
import asyncio
import os
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("blackbox-proxy.pool")

# ── Classifier tunables (env-overridable) ────────────────────────────────
KEY_LEVEL_RPM_RATIO = float(os.getenv("KEY_LEVEL_RPM_RATIO", "0.8"))
MODEL_BLOCK_DEFAULT_SECS = int(os.getenv("MODEL_BLOCK_DEFAULT_SECS", "62"))
CORROBORATION_WINDOW_S = int(os.getenv("CORROBORATION_WINDOW_S", "60"))
MODEL_429_HINTS = [h.strip().lower() for h in os.getenv(
    "MODEL_429_HINTS",
    "for this model,per-model,per model,requests for model,model is rate,"
    "model rate limit,this model,model_rate_limit,model limit").split(",") if h.strip()]
KEY_429_HINTS = [h.strip().lower() for h in os.getenv(
    "KEY_429_HINTS",
    "account,api key,api-key,apikey,organization,your key,credential,quota,limit exceeded").split(",") if h.strip()]


@dataclass
class KeyState:
    key: str
    label: str
    # Sliding window: list of request timestamps within the last 60s
    timestamps: list = field(default_factory=list)
    # KEY-level hard block: whole key blocked until this epoch
    hard_blocked_until: float = 0.0
    # MODEL-level blocks: model_name -> blocked_until epoch
    model_blocks: dict = field(default_factory=dict)
    # Adaptive: detected actual KEY limit from a key-level 429 response
    detected_limit: Optional[int] = None
    # Counters
    total_requests: int = 0
    total_429s: int = 0           # all 429s (key + model)
    total_key_429s: int = 0
    total_model_429s: int = 0
    total_rotations_caused: int = 0
    last_used: float = 0.0

    def current_rpm(self, window: int = 60) -> int:
        now = time.time()
        self.timestamps = [t for t in self.timestamps if now - t < window]
        return len(self.timestamps)

    def effective_hard_limit(self, configured_hard: int) -> int:
        if self.detected_limit and self.detected_limit < configured_hard:
            return self.detected_limit
        return configured_hard

    def effective_soft_limit(self, configured_soft: int, configured_hard: int) -> int:
        hard = self.effective_hard_limit(configured_hard)
        adaptive_soft = max(1, int(hard * 0.75))
        return min(adaptive_soft, configured_soft)

    def is_hard_blocked(self) -> bool:
        return time.time() < self.hard_blocked_until

    def is_model_blocked(self, model: str) -> bool:
        if not model:
            return False
        until = self.model_blocks.get(model)
        if not until:
            return False
        if time.time() < until:
            return True
        self.model_blocks.pop(model, None)  # expired → prune
        return False

    def active_model_blocks(self) -> dict:
        """{model: remaining_seconds} for blocks still active (prunes expired)."""
        now = time.time()
        out = {}
        for m, until in list(self.model_blocks.items()):
            rem = until - now
            if rem > 0:
                out[m] = round(rem, 1)
            else:
                self.model_blocks.pop(m, None)
        return out

    def record(self):
        now = time.time()
        self.timestamps.append(now)
        self.total_requests += 1
        self.last_used = now

    def on_rate_limit(self, scope: str, model: Optional[str] = None,
                      retry_after: Optional[int] = None,
                      detected_limit: Optional[int] = None):
        """Apply a block. scope is 'key' or 'model' (decided by the classifier)."""
        now = time.time()
        self.total_429s += 1
        self.total_rotations_caused += 1
        block_secs = retry_after if retry_after else MODEL_BLOCK_DEFAULT_SECS

        if scope == "model" and model:
            self.model_blocks[model] = now + block_secs
            self.total_model_429s += 1
            log.warning("Key %s: MODEL '%s' rate-limited — (key, model) blocked %ds "
                        "(model 429 #%d)", self.label, model, block_secs, self.total_model_429s)
        else:
            self.hard_blocked_until = now + block_secs
            self.total_key_429s += 1
            if detected_limit:
                old = self.detected_limit
                self.detected_limit = detected_limit
                if old != detected_limit:
                    log.warning("Key %s: detected actual limit = %d rpm", self.label, detected_limit)
            log.warning("Key %s: KEY-LEVEL rate-limited — whole key blocked %ds "
                        "(key 429 #%d)", self.label, block_secs, self.total_key_429s)

    def stats(self, soft: int, hard: int) -> dict:
        rpm = self.current_rpm()
        eff_hard = self.effective_hard_limit(hard)
        eff_soft = self.effective_soft_limit(soft, hard)
        return {
            "label": self.label,
            "key_prefix": self.key[:16] + "...",
            "current_rpm": rpm,
            "configured_soft": soft,
            "configured_hard": hard,
            "effective_soft": eff_soft,
            "effective_hard": eff_hard,
            "hard_blocked": self.is_hard_blocked(),
            "hard_blocked_remaining_s": round(max(0, self.hard_blocked_until - time.time()), 1),
            "model_blocks": self.active_model_blocks(),
            "total_requests": self.total_requests,
            "total_429s": self.total_429s,
            "total_key_429s": self.total_key_429s,
            "total_model_429s": self.total_model_429s,
            "total_rotations_caused": self.total_rotations_caused,
            "detected_limit": self.detected_limit,
            "last_used_ago_s": round(time.time() - self.last_used, 1) if self.last_used else None,
        }


class KeyPool:
    def __init__(self, keys: list, soft_limit: int, hard_limit: int):
        if not keys:
            raise ValueError("KeyPool requires at least one API key")
        self.soft_limit = soft_limit
        self.hard_limit = hard_limit
        self._states = [KeyState(key=k, label=f"key{i+1}") for i, k in enumerate(keys)]
        self._lock = asyncio.Lock()
        self._recent_429 = []  # (ts, key_label, model) for cross-key corroboration

    # ── 429 Classification ──────────────────────────────────────────────
    def _classify_429(self, state: KeyState, model: Optional[str],
                      body_text: str, rpm_at_429: int, eff_hard: int) -> tuple:
        """Return (scope, reason). scope ∈ {'key', 'model'}."""
        body_lower = body_text.lower()

        # Signal 1 — explicit text in 429 body
        for hint in MODEL_429_HINTS:
            if hint in body_lower:
                return "model", f"body-hint:{hint}"
        for hint in KEY_429_HINTS:
            if hint in body_lower:
                return "key", f"body-hint:{hint}"

        # Signal 2 — cross-key corroboration (last 60s)
        now = time.time()
        recent = [(ts, kl, m) for (ts, kl, m) in self._recent_429 if now - ts < CORROBORATION_WINDOW_S]
        if model:
            # Same model 429'd on other keys → model-level
            other_keys_429_model = any(kl != state.label and m == model for (_, kl, m) in recent)
            if other_keys_429_model:
                return "model", "multi-key-for-model"
            # This key 429'd on multiple different models → key-level
            other_models_on_key = {m for (_, kl, m) in recent if kl == state.label and m != model}
            if len(other_models_on_key) >= 2:
                return "key", "multi-model-on-key"

        # Signal 3 — behavioural RPM heuristic (always available)
        if eff_hard and rpm_at_429 >= eff_hard * KEY_LEVEL_RPM_RATIO:
            return "key", f"rpm-near-cap({rpm_at_429}/{eff_hard})"
        return "model", f"rpm-low({rpm_at_429}/{eff_hard})"

    async def register_rate_limit(self, state: KeyState, model: Optional[str],
                                  retry_after: Optional[int], detected_limit: Optional[int],
                                  body_text: str = "") -> tuple:
        """Classify a 429 and apply the right block atomically. Returns (scope, reason)."""
        async with self._lock:
            rpm = state.current_rpm()
            eff_hard = state.effective_hard_limit(self.hard_limit)
            scope, reason = self._classify_429(state, model, body_text, rpm, eff_hard)
            state.on_rate_limit(scope=scope, model=model,
                                retry_after=retry_after, detected_limit=detected_limit)
            self._recent_429.append((time.time(), state.label, model))
            return scope, reason

    # ── Selection ────────────────────────────────────────────────────────
    async def get_best_key(self, model: Optional[str] = None) -> Optional[KeyState]:
        """
        Atomically pick AND reserve the lowest-RPM key that is neither
        KEY-blocked nor MODEL-blocked for `model`. Reserving under the same
        lock prevents the burst-concurrency race.
        """
        async with self._lock:
            available = [s for s in self._states
                         if not s.is_hard_blocked()
                         and not s.is_model_blocked(model)]
            if not available:
                return None

            rpm = {id(s): s.current_rpm() for s in available}
            below_soft = [s for s in available
                          if rpm[id(s)] < s.effective_soft_limit(self.soft_limit, self.hard_limit)]
            chosen = None
            if below_soft:
                chosen = min(below_soft, key=lambda s: rpm[id(s)])
            else:
                below_hard = [s for s in available
                              if rpm[id(s)] < s.effective_hard_limit(self.hard_limit)]
                if below_hard:
                    chosen = min(below_hard, key=lambda s: rpm[id(s)])
                    log.warning("All keys above soft limit — using %s @ %d rpm (hard=%d)",
                                chosen.label, rpm[id(chosen)], chosen.effective_hard_limit(self.hard_limit))
            if chosen is None:
                return None

            chosen.record()  # RESERVE atomically
            return chosen

    def retry_hint(self, model: Optional[str] = None) -> tuple:
        """When no key is available: (retry_after_s, scope) for the client response.
        scope: 'all_keys' (every key hard-blocked), 'model' (model blocked everywhere
        but keys alive), or 'capacity' (all at RPM cap)."""
        now = time.time()
        states = self._states
        if states and all(s.is_hard_blocked() for s in states):
            secs = min(s.hard_blocked_until - now for s in states)
            return max(1, round(secs)), "all_keys"

        # keys not hard-blocked but this model blocked on all of them?
        live = [s for s in states if not s.is_hard_blocked()]
        if model and live and all(s.is_model_blocked(model) for s in live):
            secs = min(s.model_blocks[model] - now for s in live if model in s.model_blocks)
            return max(1, round(secs)), "model"

        return MODEL_BLOCK_DEFAULT_SECS, "capacity"

    def peek_key(self) -> Optional[KeyState]:
        """Return a usable key WITHOUT recording (background/maintenance calls)."""
        for s in self._states:
            if not s.is_hard_blocked():
                return s
        return self._states[0] if self._states else None

    async def sync_keys(self, keys: list) -> bool:
        """Reconcile pool with a fresh key list, preserving live state for survivors."""
        if not keys:
            return False
        async with self._lock:
            existing = {s.key: s for s in self._states}
            new_states = []
            for i, k in enumerate(keys):
                st = existing.get(k)
                if st is None:
                    st = KeyState(key=k, label=f"key{i+1}")
                else:
                    st.label = f"key{i+1}"
                new_states.append(st)

            old_set = set(existing.keys())
            new_set = set(keys)
            if old_set == new_set and len(new_states) == len(self._states):
                self._states = new_states
                return False

            added = new_set - old_set
            removed = old_set - new_set
            self._states = new_states
            log.info("Key pool synced: +%d / -%d → %d total key(s)",
                     len(added), len(removed), len(new_states))
            return True

    def blocked_models(self) -> dict:
        """Aggregate active MODEL-level blocks across all keys:
        {model: {"keys": [labels], "retry_s": soonest_remaining}}."""
        now = time.time()
        agg = {}
        for s in self._states:
            for m, until in list(s.model_blocks.items()):
                rem = until - now
                if rem <= 0:
                    continue
                e = agg.setdefault(m, {"keys": [], "retry_s": rem})
                e["keys"].append(s.label)
                e["retry_s"] = min(e["retry_s"], rem)
        for m in agg:
            agg[m]["retry_s"] = round(agg[m]["retry_s"], 1)
        return agg

    async def reset_counters(self):
        """Reset cumulative per-key counters (for a metrics 'reset data' action).
        Live protection state (current RPM window, active blocks) is preserved."""
        async with self._lock:
            for s in self._states:
                s.total_requests = 0
                s.total_429s = 0
                s.total_key_429s = 0
                s.total_model_429s = 0
                s.total_rotations_caused = 0
            self._recent_429.clear()
        log.info("Per-key cumulative counters reset")

    def all_stats(self) -> list:
        return [s.stats(self.soft_limit, self.hard_limit) for s in self._states]

    def summary(self) -> dict:
        stats = self.all_stats()
        return {
            "total_keys": len(stats),
            "available_keys": sum(
                1 for s in self._states
                if not s.is_hard_blocked()
                and s.current_rpm() < s.effective_hard_limit(self.hard_limit)
            ),
            "blocked_models": self.blocked_models(),
            "keys": stats,
        }