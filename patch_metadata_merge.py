#!/usr/bin/env python3
"""Append merge_observed_context to common/model/metadata.py."""
import subprocess
import sys
from pathlib import Path

P = Path('/root/wrapper/common/model/metadata.py')
text = P.read_text(encoding='utf-8')

if 'def merge_observed_context' in text:
    print('already patched')
    sys.exit(0)

addition = '''

def merge_observed_context(entry: dict | None, observations: list | None) -> dict:
    """Fill unknown context fields with account-scoped empirical evidence.

    Published catalog values always win; observations (background context
    probe or upstream rejection evidence) only fill gaps. An accepted probe
    proves a verified floor — exposed as verified_context_window, never as
    context_window — so small-context models are never over-labeled.
    """
    result = copy.deepcopy(entry) if isinstance(entry, dict) else {}
    if not isinstance(observations, list) or not observations:
        return result
    metadata = context_metadata(result)
    if positive_int(metadata.get('context_window')):
        # Advertised capacity is authoritative; keep the discovery output
        # stable and do not mix account-scoped evidence into it.
        return result
    now = time.time()
    best_exact = None
    best_floor = None
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        expiry = obs.get('expires_at')
        if not isinstance(expiry, (int, float)) or expiry <= now:
            continue
        limits = obs.get('limits') or {}
        checked = obs.get('checked_at') if isinstance(obs.get('checked_at'), (int, float)) else 0
        exact = positive_int(limits.get('context_window'))
        floor = positive_int(limits.get('input_tokens'))
        if exact and (best_exact is None or checked > (best_exact.get('checked_at') or 0)):
            best_exact = {'context_window': exact, 'checked_at': checked,
                          'expires_at': expiry,
                          'max_output_tokens': positive_int(limits.get('max_output_tokens'))}
        if floor and (best_floor is None or floor > (best_floor.get('input_tokens') or 0)):
            best_floor = {'input_tokens': floor, 'checked_at': checked, 'expires_at': expiry}
    chosen = best_exact or best_floor
    if chosen is None:
        return result
    merged = dict(metadata)
    if best_exact:
        merged['context_window'] = best_exact['context_window']
        if best_exact.get('max_output_tokens') and not positive_int(merged.get('max_output_tokens')):
            merged['max_output_tokens'] = best_exact['max_output_tokens']
        merged['status'] = 'probe_verified'
    else:
        merged['verified_context_window'] = best_floor['input_tokens']
        merged['status'] = 'observed_floor'
    merged['source'] = 'empirical_probe'
    merged['scope'] = 'account_endpoint'
    for key in ('checked_at', 'expires_at'):
        if chosen.get(key) is not None:
            merged[key] = chosen[key]
    merged['stale'] = False
    result['context_metadata'] = merged
    return result
'''

P.write_text(text + addition, encoding='utf-8')
print('appended')
r = subprocess.run([sys.executable, '-m', 'py_compile', str(P)], capture_output=True, text=True)
print('py_compile:', 'OK' if r.returncode == 0 else r.stderr)
sys.exit(r.returncode)
