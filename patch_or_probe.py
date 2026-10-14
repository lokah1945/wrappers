#!/usr/bin/env python3
"""Wire ContextProbeService into the OpenRouter wrapper (9106).

- _refresh_models_loop notifies the probe service after each refresh.
- probe_fn posts directly to the OpenRouter upstream with a configured key.
- list_models / get_model_detail merge observations before discovery_limits.
"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

SRC = Path('/root/wrapper/openrouter/src/main.py')
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

    # ---- 2. probe_fn global ----
    replace_once(
        "async def _refresh_models_loop():\n",
        "_CONTEXT_PROBE = None\n"
        "\n"
        "\n"
        "async def _probe_context_upstream(payload: dict):\n"
        "    \"\"\"Probe a model's max context via the OpenRouter upstream.\"\"\"\n"
        "    key = (os.environ.get('OPENROUTER_API_KEY', '').strip()\n"
        "           or os.environ.get('OPENROUTER_API_KEY_1', '').strip())\n"
        "    if not key:\n"
        "        return ProbeOutcome(status='skipped', detail='no upstream key available')\n"
        "    url = f\"{OPENROUTER_BASE}/chat/completions\"\n"
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
        "async def _refresh_models_loop():\n",
        'probe-fn')

    # ---- 3. notify in refresh loop ----
    replace_once(
        "async def _refresh_models_loop():\n"
        "    \"\"\"Periodically refresh model catalog from upstream.\"\"\"\n",
        "async def _refresh_models_loop():\n"
        "    \"\"\"Periodically refresh model catalog from upstream.\"\"\"\n"
        "    global _CONTEXT_PROBE\n"
        "    if _CONTEXT_PROBE is None:\n"
        "        _CONTEXT_PROBE = ContextProbeService(\n"
        "            'openrouter', MODEL_STORE, _probe_context_upstream,\n"
        "            endpoint=f\"{OPENROUTER_BASE}/chat/completions\")\n",
        'loop-init')

    replace_once(
        "            ids = await _refresh_models()\n"
        "            logger.info(f\"[openrouter] Model catalog refreshed: {len(ids)} models\")\n",
        "            ids = await _refresh_models()\n"
        "            logger.info(f\"[openrouter] Model catalog refreshed: {len(ids)} models\")\n"
        "            # Reinforcement: enqueue models with unknown advertised\n"
        "            # context for background verification (follows the same\n"
        "            # refresh cadence as the catalog itself).\n"
        "            try:\n"
        "                _CONTEXT_PROBE.notify_catalog(MODEL_CATALOG.snapshot())\n"
        "            except Exception:\n"
        "                pass\n",
        'loop-notify')

    # ---- 4. surface merge in list_models / get_model_detail ----
    replace_once(
        "    return JSONResponse({'object': 'list', 'data': [{**m, **discovery_limits(m)} for m in entries]})\n",
        "    def _with_observations(m):\n"
        "        # Empirical reinforcement (2026-10-15): account-scoped context\n"
        "        # evidence fills gaps; published values always win; a verified\n"
        "        # floor never over-labels a small-context model.\n"
        "        try:\n"
        "            if isinstance(m, dict):\n"
        "                obs = MODEL_STORE.context_observations(str(m.get('id') or ''))\n"
        "                if obs:\n"
        "                    return merge_observed_context(m, obs)\n"
        "        except Exception:\n"
        "            pass\n"
        "        return m\n"
        "    return JSONResponse({'object': 'list',\n"
        "                        'data': [{**_with_observations(m), **discovery_limits(_with_observations(m))} for m in entries]})\n",
        'list-merge')

    replace_once(
        "    return JSONResponse({**model, **discovery_limits(model)})\n",
        "    try:\n"
        "        _obs = MODEL_STORE.context_observations(str(model.get('id') or ''))\n"
        "        if _obs:\n"
        "            model = merge_observed_context(model, _obs)\n"
        "    except Exception:\n"
        "        pass\n"
        "    return JSONResponse({**model, **discovery_limits(model)})\n",
        'detail-merge')

    SRC.write_text(text, encoding='utf-8')
    print('written')
    r = subprocess.run([sys.executable, '-m', 'py_compile', str(SRC)],
                       capture_output=True, text=True)
    print('py_compile:', 'OK' if r.returncode == 0 else r.stderr)
    sys.exit(r.returncode)


if __name__ == '__main__':
    main()
