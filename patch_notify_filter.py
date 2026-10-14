#!/usr/bin/env python3
"""Filter probe notify lists to models the wrapper actually serves.

NOUS: FREE_ONLY serves only 'free'-suffixed models.
OpenRouter: FREE_ONLY serves zero-priced models (pricing prompt+completion 0)
with the ':free' suffix as fallback.
Without this filter the background probe burns requests on paid/dead models
it would never route to (observed: 404/402 silent outcomes, zero records).
"""
import subprocess
import sys
from pathlib import Path

NOUS = Path('/root/wrapper/nous/src/main.py')
OR = Path('/root/wrapper/openrouter/src/main.py')

NOUS_OLD = (
    "        try:\n"
    "            _CONTEXT_PROBE.notify_catalog(MODEL_CATALOG.snapshot())\n"
    "        except Exception:\n"
    "            pass\n"
)
NOUS_NEW = (
    "        try:\n"
    "            _entries = MODEL_CATALOG.snapshot()\n"
    "            if free_only_enabled():\n"
    "                _entries = [m for m in _entries\n"
    "                            if isinstance(m, dict) and is_free_model(str(m.get('id') or ''))]\n"
    "            _CONTEXT_PROBE.notify_catalog(_entries)\n"
    "        except Exception:\n"
    "            pass\n"
)

OR_OLD = (
    "        try:\n"
    "            _CONTEXT_PROBE.notify_catalog(MODEL_CATALOG.snapshot())\n"
    "        except Exception:\n"
    "            pass\n"
)
OR_NEW = (
    "        try:\n"
    "            _entries = list(MODEL_CATALOG.snapshot())\n"
    "            if free_only_enabled():\n"
    "                _kept = []\n"
    "                for _m in _entries:\n"
    "                    if not isinstance(_m, dict):\n"
    "                        continue\n"
    "                    _pricing = _m.get('pricing') if isinstance(_m.get('pricing'), dict) else None\n"
    "                    _free = None\n"
    "                    if _pricing is not None:\n"
    "                        try:\n"
    "                            _free = (float(_pricing.get('prompt', '1') or '1') == 0.0\n"
    "                                     and float(_pricing.get('completion', '1') or '1') == 0.0)\n"
    "                        except (TypeError, ValueError):\n"
    "                            _free = None\n"
    "                    if _free is None:\n"
    "                        _free = is_free_model(_m.get('id', ''))\n"
    "                    if _free:\n"
    "                        _kept.append(_m)\n"
    "                _entries = _kept\n"
    "            _CONTEXT_PROBE.notify_catalog(_entries)\n"
    "        except Exception:\n"
    "            pass\n"
)


def patch(path, old, new, expected_count):
    text = path.read_text(encoding='utf-8')
    if new.strip() and new in text:
        print(f'skip (already applied): {path.name}')
        return
    count = text.count(old)
    if count != expected_count:
        print(f'FATAL: anchor in {path.name} found {count} times (expected {expected_count})')
        sys.exit(1)
    path.write_text(text.replace(old, new), encoding='utf-8')
    print(f'patched: {path.name} ({expected_count} site(s))')
    r = subprocess.run([sys.executable, '-m', 'py_compile', str(path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print('COMPILE FAIL:', r.stderr)
        sys.exit(1)


patch(NOUS, NOUS_OLD, NOUS_NEW, 1)

# OpenRouter: initial-load site (8-space indent) + in-loop site (12-space)
OR_OLD_INIT = (
    "        try:\n"
    "            _CONTEXT_PROBE.notify_catalog(MODEL_CATALOG.snapshot())\n"
    "        except Exception:\n"
    "            pass\n"
)
OR_LOOP_OLD = (
    "            try:\n"
    "                _CONTEXT_PROBE.notify_catalog(MODEL_CATALOG.snapshot())\n"
    "            except Exception:\n"
    "                pass\n"
)


def indent(block, spaces):
    pad = ' ' * spaces
    return ''.join(pad + line if line.strip() else line for line in block.splitlines(keepends=True))


# OR_NEW is written at 8-space base; derive the 12-space variant
OR_NEW_LOOP = indent(OR_NEW, 4)

text = OR.read_text(encoding='utf-8')
if OR_NEW.strip() in text or OR_NEW_LOOP in text:
    print('skip (already applied): openrouter main.py')
else:
    c1 = text.count(OR_OLD_INIT)
    c2 = text.count(OR_LOOP_OLD)
    if c1 != 1 or c2 != 1:
        print(f'FATAL: OR anchors init={c1} loop={c2}')
        sys.exit(1)
    text = text.replace(OR_OLD_INIT, OR_NEW).replace(OR_LOOP_OLD, OR_NEW_LOOP)
    OR.write_text(text, encoding='utf-8')
    print('patched: openrouter/src/main.py (2 sites)')
    r = subprocess.run([sys.executable, '-m', 'py_compile', str(OR)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print('COMPILE FAIL:', r.stderr)
        sys.exit(1)
print('all OK')
