#!/usr/bin/env python3
"""OR: notify the probe service after the INITIAL catalog load too."""
import subprocess
import sys
from pathlib import Path

P = Path('/root/wrapper/openrouter/src/main.py')
text = P.read_text(encoding='utf-8')

OLD = (
    "    try:\n"
    "        # Initial load\n"
    "        ids = await _refresh_models()\n"
    "        logger.info(f\"[openrouter] Initial model catalog: {len(ids)} models\")\n"
)
NEW = (
    "    try:\n"
    "        # Initial load\n"
    "        ids = await _refresh_models()\n"
    "        logger.info(f\"[openrouter] Initial model catalog: {len(ids)} models\")\n"
    "        # Reinforcement: enqueue models with unknown advertised context\n"
    "        # right away (the next refresh is hours away).\n"
    "        try:\n"
    "            _CONTEXT_PROBE.notify_catalog(MODEL_CATALOG.snapshot())\n"
    "        except Exception:\n"
    "            pass\n"
)
if NEW.strip() and 'right away (the next refresh is hours away)' in text:
    print('already patched')
    sys.exit(0)
assert text.count(OLD) == 1, f'count={text.count(OLD)}'
P.write_text(text.replace(OLD, NEW), encoding='utf-8')
print('patched')
r = subprocess.run([sys.executable, '-m', 'py_compile', str(P)], capture_output=True, text=True)
print('py_compile:', 'OK' if r.returncode == 0 else r.stderr)
sys.exit(r.returncode)
