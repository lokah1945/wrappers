#!/usr/bin/env python3
"""Map HTTP 404/410 to ProbeOutcome status 'dead' in the three wrappers."""
import subprocess
import sys
from pathlib import Path

FILES = [
    Path('/root/wrapper/nvidia-python/src/main.py'),
    Path('/root/wrapper/nous/src/main.py'),
    Path('/root/wrapper/openrouter/src/main.py'),
]

OLD = (
    "                detail = (await resp.text())[:500]\n"
    "                if resp.status in (400, 413, 422):\n"
)
NEW = (
    "                detail = (await resp.text())[:500]\n"
    "                if resp.status in (404, 410):\n"
    "                    # EOL/retired upstream model: terminally unavailable.\n"
    "                    return ProbeOutcome(status='dead', http_status=resp.status, detail=detail)\n"
    "                if resp.status in (400, 413, 422):\n"
)

for path in FILES:
    text = path.read_text(encoding='utf-8')
    if "status='dead'" in text:
        print(f'skip (already applied): {path.name}')
        continue
    count = text.count(OLD)
    if count != 1:
        print(f'FATAL: anchor in {path.name} found {count} times')
        sys.exit(1)
    path.write_text(text.replace(OLD, NEW), encoding='utf-8')
    print(f'patched: {path.name}')
    r = subprocess.run([sys.executable, '-m', 'py_compile', str(path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print('COMPILE FAIL:', r.stderr)
        sys.exit(1)

print('all compiled OK')
