#!/usr/bin/env python3
"""Wire ContextProbeService into the NOUS wrapper (9102).

- model_catalog_refresh_loop notifies the probe service after each refresh.
- probe_fn posts directly to the NOUS upstream with the configured key.
- get_model_meta merges account-scoped observations into discovery metadata
  BEFORE discovery_limits so probe-verified exact windows surface as
  context_length while floors stay as verified_context_window only.
"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

SRC = Path('/root/wrapper/nous/src/main.py')
STAMP = time.strftime('%Y%m%d-%H%M%S')


def die(msg):
    print(f'FATAL: {msg}')
    sys.exit(1)


def main():
    text = SRC.read_text(encoding='utf-8')
    if '_CONTEXT_PROBE' in text:
        die('already patched')
    backup = SRC.with_suffix('.py.bak-' + STAMP + '-probe')
    shutil.copy2(SRC, backup)
    print(f'backup: {backup}')

    def replace_once(old, new, what):
        nonlocal text
        count = text.count(old)
        if count != 1:
            die(f'anchor for {what} found {count} times')
        text = text.replace(old, new)
        print(f'patched: {what}')

    # ---- 1. import ----
    replace_once(
        "    from common.translations.think_tags import (\n"
        "        ThinkTagStreamFilter as _ThinkTagStreamFilter,\n"
        "        extract_inline_thinking as _extract_inline_thinking,\n"
        "        strip_trailing_think_tags as _strip_think_tags,\n"
        "    )\n",
        "    from common.translations.think_tags import (\n"
        "        ThinkTagStreamFilter as _ThinkTagStreamFilter,\n"
        "        extract_inline_thinking as _extract_inline_thinking,\n"
        "        strip_trailing_think_tags as _strip_think_tags,\n"
        "    )\n"
        "    from common.model.context_probe import ContextProbeService, ProbeOutcome\n"
        "    from common.model.metadata import merge_observed_context, positive_int as _probe_pos_int\n",
        'import')

    # ---- 2. surface merge in get_model_meta ----
    replace_once(
        "    base.update(upstream or {})\n"
        "    base.update(discovery_limits(upstream))\n",
        "    base.update(upstream or {})\n"
        "    # Empirical reinforcement (2026-10-15): account-scoped context\n"
        "    # evidence (background probe / rejection parser) fills gaps the\n"
        "    # published catalog leaves unknown. Published values always win;\n"
        "    # a verified floor never over-labels a small-context model.\n"
        "    try:\n"
        "        _observed = MODEL_STORE.context_observations(rooted)\n"
        "        if _observed:\n"
        "            base = merge_observed_context(base, _observed)\n"
        "    except Exception:\n"
        "        pass\n"
        "    base.update(discovery_limits(base))\n",
        'surface-merge')

    # ---- 3. probe_fn + service global + notify in refresh loop ----
    replace_once(
        "async def model_catalog_refresh_loop():\n",
        "_CONTEXT_PROBE = None\n"
        "\n"
        "\n"
        "async def _probe_context_upstream(payload: dict):\n"
        "    \"\"\"Probe a model's max context via the NOUS upstream chat surface.\"\"\"\n"
        "    key = (os.environ.get('NOUS_API_KEY', '').strip()\n"
        "           or os.environ.get('NOUS_API_KEY_1', '').strip())\n"
        "    if not key:\n"
        "        return ProbeOutcome(status='skipped', detail='no upstream key available')\n"
        "    url = f\"{NOUS_BASE}/v1/chat/completions\"\n"
        "    try:\n"
        "        timeout = aiohttp.ClientTimeout(total=300)\n"
        "        async with aiohttp.ClientSession(timeout=timeout) as session:\n"
        "            async with session.post(url, json=payload, headers={\n"
        "                'authorization': f'Bearer {key}',\n"
        "                'content-type': 'application/json',\n"
        "            }) as resp:\n"
        "                if resp.status == 200:\n"
        "                    try:\n"
        "                        body = await resp.json(content_type=None)\n"
        "                    except Exception:\n"
        "                        body = {}\n"
        "                    usage = (body or {}).get('usage') or {}\n"
        "                    return ProbeOutcome(status='accepted', http_status=200,\n"
        "                                        prompt_tokens=_probe_pos_int(usage.get('prompt_tokens')))\n"
        "                detail = (await resp.text())[:500]\n"
        "                if resp.status in (400, 413, 422):\n"
        "                    evidence = None\n"
        "                    if _CONTEXT_PROBE is not None:\n"
        "                        try:\n"
        "                            evidence = _CONTEXT_PROBE.parse_rejection(resp.status, detail)\n"
        "                        except Exception:\n"
        "                            evidence = None\n"
        "                    if evidence:\n"
        "                        return ProbeOutcome(status='rejected', http_status=resp.status,\n"
        "                                            evidence=evidence, detail=detail)\n"
        "                return ProbeOutcome(status='error', http_status=resp.status, detail=detail)\n"
        "    except asyncio.CancelledError:\n"
        "        raise\n"
        "    except Exception as exc:\n"
        "        return ProbeOutcome(status='error', detail=str(exc)[:300])\n"
        "\n"
        "\n"
        "async def model_catalog_refresh_loop():\n",
        'probe-fn')

    replace_once(
        "async def model_catalog_refresh_loop():\n"
        "    while True:\n"
        "        await refresh_model_catalog_once()\n",
        "async def model_catalog_refresh_loop():\n"
        "    global _CONTEXT_PROBE\n"
        "    if _CONTEXT_PROBE is None:\n"
        "        _CONTEXT_PROBE = ContextProbeService(\n"
        "            'nous', MODEL_STORE, _probe_context_upstream,\n"
        "            endpoint=f\"{NOUS_BASE}/v1/chat/completions\")\n"
        "    while True:\n"
        "        await refresh_model_catalog_once()\n"
        "        # Reinforcement: after each catalog refresh, enqueue models whose\n"
        "        # advertised context is unknown for background verification.\n"
        "        try:\n"
        "            _CONTEXT_PROBE.notify_catalog(MODEL_CATALOG.snapshot())\n"
        "        except Exception:\n"
        "            pass\n",
        'loop-notify')

    SRC.write_text(text, encoding='utf-8')
    print('written')
    r = subprocess.run([sys.executable, '-m', 'py_compile', str(SRC)],
                       capture_output=True, text=True)
    print('py_compile:', 'OK' if r.returncode == 0 else r.stderr)
    sys.exit(r.returncode)


if __name__ == '__main__':
    main()
