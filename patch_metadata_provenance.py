#!/usr/bin/env python3
"""Enhance merge_observed_context: attach probe provenance for published models too.

Published catalog values still win (never overridden), but fresh empirical
evidence is attached as provenance: verified_context_window (accepted floor)
and/or observed_context_window (exact, from a parsed rejection). This makes
the reinforcement visible in /v1/models for every model, per the owner's
'results go into metadata' contract.
"""
import subprocess
import sys
from pathlib import Path

P = Path('/root/wrapper/common/model/metadata.py')
text = P.read_text(encoding='utf-8')

OLD = (
    "    result = copy.deepcopy(entry) if isinstance(entry, dict) else {}\n"
    "    if not isinstance(observations, list) or not observations:\n"
    "        return result\n"
    "    metadata = context_metadata(result)\n"
    "    if positive_int(metadata.get('context_window')):\n"
    "        # Advertised capacity is authoritative; keep the discovery output\n"
    "        # stable and do not mix account-scoped evidence into it.\n"
    "        return result\n"
    "    now = time.time()\n"
)
NEW = (
    "    result = copy.deepcopy(entry) if isinstance(entry, dict) else {}\n"
    "    if not isinstance(observations, list) or not observations:\n"
    "        return result\n"
    "    metadata = context_metadata(result)\n"
    "    published = positive_int(metadata.get('context_window'))\n"
    "    now = time.time()\n"
)

if 'published = positive_int(metadata.get' in text:
    print('already patched')
    sys.exit(0)
assert text.count(OLD) == 1, f'anchor count={text.count(OLD)}'
text = text.replace(OLD, NEW)

OLD2 = (
    "    chosen = best_exact or best_floor\n"
    "    if chosen is None:\n"
    "        return result\n"
    "    merged = dict(metadata)\n"
    "    if best_exact:\n"
)
NEW2 = (
    "    chosen = best_exact or best_floor\n"
    "    if chosen is None:\n"
    "        return result\n"
    "    if published:\n"
    "        # Advertised capacity is authoritative and is never overridden,\n"
    "        # but fresh empirical evidence is attached as provenance so the\n"
    "        # verification is visible: the exact window observed on this\n"
    "        # account/route and/or the verified floor actually accepted.\n"
    "        provenance = {}\n"
    "        if best_exact:\n"
    "            provenance['observed_context_window'] = best_exact['context_window']\n"
    "        if best_floor:\n"
    "            provenance['verified_context_window'] = best_floor['input_tokens']\n"
    "        if provenance:\n"
    "            provenance['source'] = 'empirical_probe'\n"
    "            provenance['scope'] = 'account_endpoint'\n"
    "            for key in ('checked_at', 'expires_at'):\n"
    "                if chosen.get(key) is not None:\n"
    "                    provenance[key] = chosen[key]\n"
    "            merged = dict(metadata)\n"
    "            merged['probe'] = provenance\n"
    "            result['context_metadata'] = merged\n"
    "        return result\n"
    "    merged = dict(metadata)\n"
    "    if best_exact:\n"
)
assert text.count(OLD2) == 1, f'anchor2 count={text.count(OLD2)}'
text = text.replace(OLD2, NEW2)

P.write_text(text, encoding='utf-8')
print('patched')
r = subprocess.run([sys.executable, '-m', 'py_compile', str(P)], capture_output=True, text=True)
print('py_compile:', 'OK' if r.returncode == 0 else r.stderr)
sys.exit(r.returncode)
