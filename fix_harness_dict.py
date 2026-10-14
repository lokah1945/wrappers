#!/usr/bin/env python3
"""Fix duplicate-keyword conflict: use setdefault + update instead of dict literal."""
import subprocess
import sys
from pathlib import Path

P = Path('/root/wrapper/tests/test_active_native_passthrough.py')
text = P.read_text(encoding='utf-8')

old = (
    "    namespace = dict(\n"
    "        asyncio=asyncio, json=json, os=os, random=random, time=time,\n"
    "        logger=logging.getLogger(__name__), aiohttp=aiohttp,\n"
    "        JSONResponse=JSONResponse, Response=Response,\n"
    "        StreamingResponse=StreamingResponse, close_stream=close_stream,\n"
    "        _ThinkTagStreamFilter=ThinkTagStreamFilter,\n"
    "        _extract_inline_thinking=extract_inline_thinking,\n"
    "        _strip_think_tags=strip_trailing_think_tags,\n"
    "        _parse_dsml_from_text=parse_dsml_from_text,\n"
    "        **globals_,\n"
    "    )\n"
)
new = (
    "    namespace = dict(\n"
    "        asyncio=asyncio, json=json, os=os, random=random, time=time,\n"
    "        logger=logging.getLogger(__name__), aiohttp=aiohttp,\n"
    "        JSONResponse=JSONResponse, Response=Response,\n"
    "        StreamingResponse=StreamingResponse, close_stream=close_stream,\n"
    "    )\n"
    "    # Anti-trace helpers: setdefault so callers that re-pass a previously\n"
    "    # built namespace (as **globals_) never hit duplicate keywords, and\n"
    "    # explicit caller overrides always win.\n"
    "    namespace.setdefault('_ThinkTagStreamFilter', ThinkTagStreamFilter)\n"
    "    namespace.setdefault('_extract_inline_thinking', extract_inline_thinking)\n"
    "    namespace.setdefault('_strip_think_tags', strip_trailing_think_tags)\n"
    "    namespace.setdefault('_parse_dsml_from_text', parse_dsml_from_text)\n"
    "    namespace.update(globals_)\n"
)
if new in text:
    print('already fixed')
    sys.exit(0)
assert text.count(old) == 1, f'anchor count={text.count(old)}'
P.write_text(text.replace(old, new), encoding='utf-8')
print('fixed')
r = subprocess.run([sys.executable, '-m', 'py_compile', str(P)], capture_output=True, text=True)
print('py_compile:', 'OK' if r.returncode == 0 else r.stderr)
sys.exit(r.returncode)
