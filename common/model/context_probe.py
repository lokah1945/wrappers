"""Background max-context probing that reinforces the existing refresh cycle.

Design contract (owner request 2026-10-15):
- This is a REINFORCEMENT of the existing auto-discovery/refresh mechanism,
  not a new feature: every wrapper already refreshes its model catalog on a
  schedule (NVIDIA MODEL_REFRESH_SEC=600 + registry on_refresh callbacks;
  NOUS/OpenRouter MODEL_CATALOG_REFRESH_SEC/TTL via CachedModelCatalog).
- After each catalog refresh the wrapper calls ``notify_catalog(entries)``;
  models whose advertised context is unknown (or whose probe evidence is
  stale) are enqueued and probed in the background, one at a time, through
  the wrapper's own request path — so key rotation, cooldowns, Retry-After
  and model-scoped-403 carve-outs apply automatically.
- Results are stored via the existing ``ModelStateStore.record_context_limit``
  table (account-scoped, never a global cap) and surfaced in /v1/models
  through ``merge_observed_context`` — published catalog values always win,
  empirical evidence only fills unknowns.
- Honesty rules (metadata.py contract): an accepted probe proves a FLOOR
  (verified_context_window), never a ceiling. A rejection parsed by
  common.context_recovery.parse_context_error can prove an exact window.
  Small-context models are therefore never over-labeled, and 1M-context
  models get their advertised capacity verified.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from common.context_recovery import parse_context_error
from common.model.metadata import positive_int

logger = logging.getLogger('context-probe')

_DEFAULT_LADDER = (4096, 32768, 131072, 262144, 524288, 1048576)


def _env_int(name: str, default: int, lo: int = 0) -> int:
    try:
        return max(lo, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: str = '1') -> bool:
    return os.environ.get(name, default).strip().lower() in ('1', 'true', 'yes', 'on')


def _parse_ladder() -> tuple[int, ...]:
    raw = os.environ.get('CONTEXT_PROBE_LADDER',
                         ','.join(str(v) for v in _DEFAULT_LADDER))
    values = []
    for part in raw.split(','):
        part = part.strip()
        if part.isdigit():
            value = int(part)
            if 0 < value <= 16_000_000:
                values.append(value)
    return tuple(sorted(set(values))) if values else _DEFAULT_LADDER


# Deterministic filler: one token-ish per ~4 chars of stable ASCII words.
_FILLER = ('context verification probe filler text '
           'measuring maximum accepted prompt length with stable words ')


def build_probe_messages(target_tokens: int, chars_per_token: float = 4.0) -> list[dict]:
    """Deterministic padding sized for a target token count.

    The exact token count is reported back by upstream usage, so the probe
    self-calibrates: the FIRST accepted response's usage.prompt_tokens
    recalibrates chars_per_token for subsequent rungs. No tokenizer guessing
    is ever persisted as metadata.
    """
    chars = max(64, int(target_tokens * chars_per_token))
    filler = (_FILLER * (chars // len(_FILLER) + 1))[:chars]
    return [{'role': 'user', 'content': filler.strip()[:chars]}]


@dataclass
class ProbeOutcome:
    status: str                     # 'accepted' | 'rejected' | 'error' | 'skipped'
    http_status: int = 0
    prompt_tokens: Optional[int] = None
    evidence: Optional[dict] = None  # parsed context-error evidence
    detail: str = ''


@dataclass
class ProbeResult:
    model_id: str
    verified_floor: Optional[int] = None
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    rungs_accepted: list = field(default_factory=list)
    outcome: str = 'not_probed'
    checked_at: float = 0.0


class ContextProbeService:
    """Per-wrapper background probe service (single instance per process)."""

    def __init__(self, provider: str, store, probe_fn: Callable[..., Awaitable[ProbeOutcome]],
                 *, busy_fn: Optional[Callable[[], bool]] = None,
                 credential: str = '', endpoint: str = ''):
        self.provider = provider
        self.store = store
        self.probe_fn = probe_fn
        self.busy_fn = busy_fn
        self.credential = credential or provider
        self.endpoint = endpoint
        self.ttl_sec = _env_int('CONTEXT_PROBE_TTL_SEC', 21600, lo=600)
        self.max_per_cycle = _env_int('CONTEXT_PROBE_MAX_PER_CYCLE', 16, lo=1)
        self.cycle_sec = _env_int('CONTEXT_PROBE_CYCLE_SEC', 240, lo=30)
        self.gap_sec = _env_int('CONTEXT_PROBE_GAP_SEC', 3, lo=0)
        self.timeout_sec = _env_int('CONTEXT_PROBE_TIMEOUT_SEC', 240, lo=10)
        self.ladder = _parse_ladder()
        self.failure_backoff_sec = _env_int('CONTEXT_PROBE_FAILURE_BACKOFF_SEC', 3600, lo=60)
        self.dead_backoff_sec = _env_int('CONTEXT_PROBE_DEAD_BACKOFF_SEC', 86400, lo=3600)
        self.drain_interval_sec = _env_int('CONTEXT_PROBE_DRAIN_INTERVAL_SEC', 300, lo=30)
        self._queue: list[tuple] = []   # (model_id, advertised_context | None)
        self._queued: set[str] = set()
        self._skip_until: dict[str, float] = {}
        self._inflight = False
        self._task: Optional[asyncio.Task] = None
        self._chars_per_token = 4.0
        self._last_outcome = ''

    # ---- queue management -------------------------------------------------
    def notify_catalog(self, entries: list[dict]) -> int:
        """Enqueue models needing context verification after a refresh."""
        if not _env_flag('CONTEXT_PROBE_ENABLED', '1'):
            return 0
        added = 0
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            model_id = str(entry.get('id') or '').strip()
            if not model_id:
                continue
            meta = (entry.get('context_metadata') or {})
            _advertised = positive_int(meta.get('context_window'))
            # Fresh probe evidence (exact window or verified floor) means the
            # model was verified recently: skip until the TTL lapses. Both
            # published and unknown models get verified once per TTL.
            if self._probe_fresh(model_id):
                continue
            if time.time() < self._skip_until.get(model_id, 0.0):
                continue
            if model_id in self._queued:
                continue
            self._queued.add(model_id)
            self._queue.append((model_id, _advertised))
            added += 1
        if added:
            logger.info('[context-probe:%s] queued %d model(s) for background '
                        'context verification (queue=%d)', self.provider, added, len(self._queue))
        self._wake()
        return added

    def _probe_fresh(self, model_id: str) -> bool:
        try:
            observations = self.store.context_observations(model_id)
        except Exception:
            return False
        now = time.time()
        for obs in observations or []:
            limits = obs.get('limits') or {}
            if positive_int(limits.get('context_window')) or positive_int(limits.get('input_tokens')):
                expiry = obs.get('expires_at')
                if isinstance(expiry, (int, float)) and expiry > now:
                    return True
        return False

    def _wake(self):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Called from a sync context without a loop: queueing still works;
            # the next notify from async context (refresh loop / callback)
            # starts the background drain.
            return
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._drain_safe())

    async def _drain_safe(self):
        if self._inflight:
            return
        self._inflight = True
        try:
            # Persistent drain: the queue empties at a bounded cadence even
            # when the owning wrapper's catalog refresh runs infrequently
            # (NOUS/OpenRouter refresh every 6h; NVIDIA every 600s).
            while self._queue:
                await self._run()
                if self._queue:
                    await asyncio.sleep(self.drain_interval_sec)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never take the wrapper down
            logger.warning('[context-probe:%s] drain error: %s', self.provider,
                           type(exc).__name__)
        finally:
            self._inflight = False

    # ---- probing ----------------------------------------------------------
    async def _run(self):
        started = time.monotonic()
        probed = 0
        while (self._queue and probed < self.max_per_cycle
               and time.monotonic() - started < self.cycle_sec):
            if self.busy_fn is not None:
                try:
                    if self.busy_fn():
                        # F7 parity: never compete with live traffic.
                        return
                except Exception:
                    pass
            model_id, advertised = self._queue.pop(0)
            self._queued.discard(model_id)
            try:
                result = await self._probe_model(model_id, advertised)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('[context-probe:%s] %s failed: %s', self.provider,
                               model_id, type(exc).__name__)
                continue
            probed += 1
            if result is not None:
                self._record(model_id, result)
                self._skip_until.pop(model_id, None)
            elif (result is None and self._last_outcome == 'dead'):
                # 404/410: terminally unavailable upstream — retry rarely.
                self._skip_until[model_id] = time.time() + self.dead_backoff_sec
            else:
                # inconclusive (error/timeout/gated): negative backoff so
                # unavailable models do not burn a request every cycle.
                self._skip_until[model_id] = time.time() + self.failure_backoff_sec
            if self.gap_sec and self._queue and probed < self.max_per_cycle:
                await asyncio.sleep(self.gap_sec)
        logger.info('[context-probe:%s] drain cycle: probed=%d remaining=%d',
                    self.provider, probed, len(self._queue))

    async def _probe_model(self, model_id: str,
                           advertised: int | None = None) -> Optional[ProbeResult]:
        """Ladder probe: ascending token targets until rejection or ladder top.

        When the catalog advertises a context window, probing starts at the
        highest ladder rung at-or-below it: one accepted request verifies the
        advertised capacity (floor), one rejection may reveal the exact
        window. Unknown-context models climb the full ladder.

        - Accepted rungs raise the VERIFIED FLOOR (never a ceiling).
        - A context rejection parsed by parse_context_error may reveal the
          exact window; the probe then stops.
        - Non-context errors abort the probe for this cycle (inconclusive).
        """
        result = ProbeResult(model_id=model_id, checked_at=time.time())
        ratio = self._chars_per_token
        accepted_tokens: Optional[int] = None
        rungs = list(self.ladder)
        verify_mode = False
        if positive_int(advertised):
            eligible = [r for r in rungs if r <= advertised]
            if eligible:
                rungs = [eligible[-1]]
                verify_mode = True
        for target in rungs:
            messages = build_probe_messages(target, chars_per_token=ratio)
            payload = {'model': model_id, 'messages': messages, 'max_tokens': 16,
                       'stream': False}
            try:
                outcome = await asyncio.wait_for(
                    self.probe_fn(payload), timeout=self.timeout_sec)
            except asyncio.TimeoutError:
                result.outcome = 'timeout'
                break
            except asyncio.CancelledError:
                raise
            if outcome.status == 'accepted':
                accepted_tokens = positive_int(outcome.prompt_tokens)
                if accepted_tokens:
                    # self-calibration for the next rung
                    ratio = len(messages[0]['content']) / accepted_tokens
                    self._chars_per_token = ratio
                    result.rungs_accepted.append(accepted_tokens)
                if verify_mode:
                    # Advertised capacity verified: one accepted request at
                    # (or near) the published window satisfies the contract.
                    result.outcome = 'advertised_verified'
                    break
                continue
            if outcome.status == 'rejected':
                evidence = outcome.evidence or {}
                result.context_window = positive_int(evidence.get('context_window'))
                result.max_output_tokens = positive_int(evidence.get('max_output_tokens'))
                result.outcome = 'rejected_exact' if result.context_window else 'rejected'
                break
            if outcome.status == 'dead':
                result.outcome = 'dead'
                break
            # error / skipped: abort — do not guess.
            result.outcome = outcome.status
            break
        result.verified_floor = accepted_tokens
        if result.outcome in ('not_probed',):
            result.outcome = 'accepted_floor' if accepted_tokens else 'inconclusive'
        self._last_outcome = result.outcome
        return result if (result.verified_floor or result.context_window) else None

    def _record(self, model_id: str, result: ProbeResult):
        limits = {}
        if result.context_window:
            limits['context_window'] = result.context_window
        if result.verified_floor and not result.context_window:
            # floor only: honest lower bound, never advertised as the window
            limits['input_tokens'] = result.verified_floor
        if result.max_output_tokens:
            limits['max_output_tokens'] = result.max_output_tokens
        if not limits:
            return
        try:
            self.store.record_context_limit(model_id, self.credential, self.endpoint,
                                            limits, ttl_sec=self.ttl_sec)
            logger.info('[context-probe:%s] %s recorded floor=%s window=%s',
                        self.provider, model_id, result.verified_floor,
                        result.context_window)
        except Exception as exc:
            logger.warning('[context-probe:%s] store failed for %s: %s',
                           self.provider, model_id, type(exc).__name__)

    def parse_rejection(self, http_status: int, body_text: str) -> Optional[dict]:
        """Parse a probe rejection body via the shared recovery parser."""
        evidence = parse_context_error(http_status, body_text)
        if evidence is None:
            return None
        return {'context_window': evidence.context_window,
                'input_tokens': evidence.input_tokens,
                'max_output_tokens': evidence.max_output_tokens}

    async def close(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
