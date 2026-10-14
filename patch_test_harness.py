#!/usr/bin/env python3
"""Fix test harness namespaces for the anti-trace think_tags imports.

The AST-extraction harness (_load and load_openrouter_translator) execs
slices of main.py without module-level imports, so the new think_tags names
must be injected explicitly (same pattern as the DASH metrics fix).
"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path('/root/wrapper')
STAMP = time.strftime('%Y%m%d-%H%M%S')


def die(msg):
    print(f'FATAL: {msg}')
    sys.exit(1)


def patch(path, old, new, what):
    text = path.read_text(encoding='utf-8')
    if new.strip() and new in text:
        print(f'skip (already applied): {what} in {path.name}')
        return
    count = text.count(old)
    if count != 1:
        die(f'anchor for {what} in {path.name} found {count} times')
    backup = path.with_suffix(path.suffix + '.bak-' + STAMP)
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(text.replace(old, new), encoding='utf-8')
    print(f'patched: {what} in {path.name}')


# 1. shared _load in test_active_native_passthrough.py
p1 = ROOT / 'tests/test_active_native_passthrough.py'
patch(
    p1,
    "from common.translations import (\n"
    "    anthropic_to_openai_response, is_retriable_status, parse_retry_after,\n"
    "    should_cooldown_key, strip_cache_control, normalize_upstream_error,\n"
    ")\n",
    "from common.translations import (\n"
    "    anthropic_to_openai_response, is_retriable_status, parse_retry_after,\n"
    "    should_cooldown_key, strip_cache_control, normalize_upstream_error,\n"
    ")\n"
    "from common.translations.think_tags import (\n"
    "    ThinkTagStreamFilter, extract_inline_thinking, strip_trailing_think_tags,\n"
    ")\n",
    'think-import')

patch(
    p1,
    "    namespace = dict(\n"
    "        asyncio=asyncio, json=json, os=os, random=random, time=time,\n"
    "        logger=logging.getLogger(__name__), aiohttp=aiohttp,\n"
    "        JSONResponse=JSONResponse, Response=Response,\n"
    "        StreamingResponse=StreamingResponse, close_stream=close_stream, **globals_,\n"
    "    )\n",
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
    "    )\n",
    'load-namespace')

# _load needs parse_dsml_from_text imported too
patch(
    p1,
    "from common.translations.think_tags import (\n"
    "    ThinkTagStreamFilter, extract_inline_thinking, strip_trailing_think_tags,\n"
    ")\n",
    "from common.translations.think_tags import (\n"
    "    ThinkTagStreamFilter, extract_inline_thinking, strip_trailing_think_tags,\n"
    ")\n"
    "from common.translations import parse_dsml_from_text\n",
    'dsml-import')

# 2. load_openrouter_translator in test_sse_streaming_regressions.py
p2 = ROOT / 'tests/test_sse_streaming_regressions.py'
patch(
    p2,
    "    ns = {\n"
    "        'json': json, 'time': __import__('time'), 'asyncio': asyncio,\n"
    "        'HEARTBEAT_MS': 100000,\n",
    "    from common.translations.think_tags import (\n"
    "        ThinkTagStreamFilter, extract_inline_thinking, strip_trailing_think_tags,\n"
    "    )\n"
    "    ns = {\n"
    "        'json': json, 'time': __import__('time'), 'asyncio': asyncio,\n"
    "        'HEARTBEAT_MS': 100000,\n"
    "        '_ThinkTagStreamFilter': ThinkTagStreamFilter,\n"
    "        '_extract_inline_thinking': extract_inline_thinking,\n"
    "        '_strip_think_tags': strip_trailing_think_tags,\n",
    'or-translator-ns')

print('done')
r = subprocess.run([sys.executable, '-m', 'py_compile', str(p1), str(p2)],
                   capture_output=True, text=True)
print('py_compile:', 'OK' if r.returncode == 0 else r.stderr)
sys.exit(r.returncode)
