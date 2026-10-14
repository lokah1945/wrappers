"""Regression tests for the background max-context probe reinforcement.

Covers the ContextProbeService ladder logic and the
merge_observed_context honesty rules:
- accepted rungs raise a VERIFIED FLOOR, never a ceiling
- exact windows come only from parsed rejections
- published catalog values always win over empirical evidence
- floors never set context_length aliases (no over-labeling)
"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.model.context_probe import (  # noqa: E402
    ContextProbeService, ProbeOutcome, build_probe_messages)
from common.model.metadata import merge_observed_context, annotate_catalog  # noqa: E402


class FakeStore:
    def __init__(self):
        self.recorded = {}
        self._observations = {}

    def context_observations(self, model_id):
        return self._observations.get(model_id, [])

    def record_context_limit(self, model_id, credential, endpoint, limits, ttl_sec=3600):
        self.recorded[model_id] = dict(limits or {})
        now = time.time()
        self._observations[model_id] = [{
            'limits': dict(limits or {}), 'checked_at': now,
            'expires_at': now + ttl_sec, 'source': 'upstream_context_error',
            'scope': 'account_endpoint'}]
        return None


def make_service(store, outcomes_by_target):
    calls = []

    async def probe_fn(payload):
        target = len(payload['messages'][0]['content'])
        calls.append({'model': payload['model'], 'chars': target})
        for threshold, outcome in outcomes_by_target:
            if target <= threshold:
                return outcome
        return outcomes_by_target[-1][1]

    return ContextProbeService('test', store, probe_fn), calls


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_accepted_ladder_records_floor_only():
    store = FakeStore()
    outcomes = [
        (20000, ProbeOutcome(status='accepted', http_status=200, prompt_tokens=4096)),
        (200000, ProbeOutcome(status='accepted', http_status=200, prompt_tokens=131072)),
    ]
    svc, calls = make_service(store, outcomes)
    result = run(svc._probe_model('m/known'))
    assert result.verified_floor == 131072 and result.context_window is None
    assert result.outcome == 'accepted_floor'
    svc._record('m/known', result)
    # floor stored as input_tokens, never as context_window
    assert store.recorded['m/known'] == {'input_tokens': 131072}


def test_rejection_with_exact_window_stops_ladder():
    store = FakeStore()
    outcomes = [
        (20000, ProbeOutcome(status='accepted', http_status=200, prompt_tokens=4096)),
        (200000, ProbeOutcome(status='rejected', http_status=400,
                              evidence={'context_window': 8192, 'input_tokens': 9000})),
    ]
    svc, _ = make_service(store, outcomes)
    result = run(svc._probe_model('m/small'))
    assert result.context_window == 8192
    assert result.outcome == 'rejected_exact'
    svc._record('m/small', result)
    assert store.recorded['m/small']['context_window'] == 8192


def test_error_aborts_without_guessing():
    store = FakeStore()
    outcomes = [(20000, ProbeOutcome(status='error', http_status=500, detail='boom'))]
    svc, _ = make_service(store, outcomes)
    result = run(svc._probe_model('m/broken'))
    assert result is None  # inconclusive: nothing recorded
    assert store.recorded == {}


def test_notify_catalog_enqueues_all_non_fresh():
    store = FakeStore()
    async def noop(payload):
        return ProbeOutcome(status='error')
    svc = ContextProbeService('test', store, noop)
    entries = [
        {'id': 'a/unknown', 'context_metadata': {'status': 'unknown'}},
        {'id': 'b/known', 'context_metadata': {'status': 'known', 'context_window': 65536}},
    ]
    # Owner contract: EVERY model gets background metadata verification once
    # per TTL (published values verified, unknowns discovered).
    added = svc.notify_catalog(entries)
    assert added == 2
    assert {m for m, _ in svc._queue} == {'a/unknown', 'b/known'}
    # fresh observations short-circuit re-enqueue
    now = time.time()
    store._observations['a/unknown'] = [{
        'limits': {'input_tokens': 1000}, 'checked_at': now,
        'expires_at': now + 3600, 'source': 'probe', 'scope': 'account'}]
    svc._queue.clear()
    svc._queued.clear()
    assert svc.notify_catalog(entries) == 1  # only b/known
    assert [m for m, _ in svc._queue] == ['b/known']


def test_advertised_verify_single_rung():
    store = FakeStore()
    # one accepted outcome for any request
    outcomes = [(10**9, ProbeOutcome(status='accepted', http_status=200, prompt_tokens=524288))]
    svc, calls = make_service(store, outcomes)
    result = run(svc._probe_model('m/big', advertised=1048576))
    # verify mode: exactly ONE request at the highest rung <= advertised
    assert len(calls) == 1
    assert result.outcome == 'advertised_verified'
    assert result.verified_floor == 524288
    svc._record('m/big', result)
    assert store.recorded['m/big'] == {'input_tokens': 524288}


def test_merge_observed_context_floor_no_alias():
    entry = annotate_catalog({'id': 'x'}, 'upstream')  # unknown status
    obs = [{'limits': {'input_tokens': 131072}, 'checked_at': time.time(),
            'expires_at': time.time() + 3600, 'source': 'probe', 'scope': 'account'}]
    merged = merge_observed_context(entry, obs)
    meta = merged['context_metadata']
    assert meta.get('verified_context_window') == 131072
    assert 'context_window' not in meta or not meta.get('context_window')
    assert meta.get('status') == 'observed_floor'


def test_merge_observed_context_exact_window():
    entry = annotate_catalog({'id': 'x'}, 'upstream')
    obs = [{'limits': {'context_window': 8192}, 'checked_at': time.time(),
            'expires_at': time.time() + 3600}]
    merged = merge_observed_context(entry, obs)
    assert merged['context_metadata'].get('context_window') == 8192
    assert merged['context_metadata'].get('status') == 'probe_verified'


def test_merge_published_wins():
    entry = annotate_catalog({'id': 'x', 'context_length': 1048576}, 'upstream')
    obs = [{'limits': {'context_window': 8192}, 'checked_at': time.time(),
            'expires_at': time.time() + 3600}]
    merged = merge_observed_context(entry, obs)
    assert merged['context_metadata'].get('context_window') == 1048576


def test_merge_published_attaches_probe_provenance():
    entry = annotate_catalog({'id': 'x', 'context_length': 1000000}, 'upstream')
    obs = [{'limits': {'input_tokens': 517013}, 'checked_at': time.time(),
            'expires_at': time.time() + 3600, 'source': 'probe'}]
    merged = merge_observed_context(entry, obs)
    meta = merged['context_metadata']
    # published value never overridden; verification attached as provenance
    assert meta.get('context_window') == 1000000
    assert meta['probe']['verified_context_window'] == 517013
    assert meta['probe']['source'] == 'empirical_probe'
    assert 'verified_context_window' not in meta or meta.get('context_window') == 1000000


def test_merge_stale_ignored():
    entry = annotate_catalog({'id': 'x'}, 'upstream')
    obs = [{'limits': {'context_window': 8192}, 'checked_at': 1.0, 'expires_at': 2.0}]
    merged = merge_observed_context(entry, obs)
    assert 'context_window' not in merged['context_metadata']


def test_probe_messages_deterministic():
    msgs = build_probe_messages(4096)
    assert len(msgs) == 1 and msgs[0]['role'] == 'user'
    again = build_probe_messages(4096)
    assert msgs == again


if __name__ == '__main__':
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f'PASS {name}')
            except AssertionError as exc:
                fails += 1
                print(f'FAIL {name}: {exc}')
    print('ALL PASS' if not fails else f'{fails} FAILURES')
    sys.exit(1 if fails else 0)
