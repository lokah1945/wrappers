#!/usr/bin/env python3
"""Wire ContextProbeService into the NVIDIA wrapper (9101).

Reinforcement of the existing refresh cycle:
- _sync_context_catalog (fires on every model refresh + registry refresh)
  now also notifies the probe service -> models with unknown advertised
  context get background-verified.
- probe_fn posts directly to the upstream /v1/chat/completions using a key
  from the existing pool (peek_key) -> cooldowns/rotation semantics apply.
- busy_fn skips probing while live requests wait on the pool (F7 parity).
- _context_catalog_entry merges account-scoped observations into discovery
  metadata (published values always win, floors never over-label).
"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

SRC = Path('/root/wrapper/nvidia-python/src/main.py')
STAMP = time.strftime('%Y%m%d-%H%M%S')


def die(msg):
    print(f'FATAL: {msg}')
    sys.exit(1)


def main():
    text = SRC.read_text(encoding='utf-8')
    if '_context_probe' in text:
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

    # ---- 1. import at top (after the common try/except fallback block) ----
    replace_once(
        "try:\n"
        "    from watchdog.observers import Observer\n"
        "    from watchdog.events import FileSystemEventHandler\n"
        "    HAS_WATCHDOG = True\n",
        "from common.model.context_probe import ContextProbeService, ProbeOutcome\n"
        "from common.model.metadata import merge_observed_context, positive_int\n"
        "\n"
        "try:\n"
        "    from watchdog.observers import Observer\n"
        "    from watchdog.events import FileSystemEventHandler\n"
        "    HAS_WATCHDOG = True\n",
        'import')

    # ---- 2. surface observations in _context_catalog_entry ----
    replace_once(
        "        raw['context_metadata'] = merge_context_metadata({'context_metadata': endpoint}, official)\n",
        "        raw['context_metadata'] = merge_context_metadata({'context_metadata': endpoint}, official)\n"
        "        # Empirical reinforcement (2026-10-15): account-scoped context\n"
        "        # evidence from the background probe / rejection parser fills\n"
        "        # gaps the published catalogs leave unknown. Published values\n"
        "        # always win; a verified floor never over-labels a model.\n"
        "        try:\n"
        "            observed = self.model_state.context_observations(model_id)\n"
        "            if observed:\n"
        "                raw = merge_observed_context(raw, observed)\n"
        "        except Exception:\n"
        "            pass\n",
        'surface-merge')

    # ---- 3. notify after each catalog sync ----
    replace_once(
        "        if entries:\n"
        "            self.model_state.upsert_catalog(entries, source='nvidia:cached-upstream-metadata')\n"
        "            MODEL_REGISTRY.register_catalog(entries, revision='runtime-catalog')\n"
        "            MODEL_REGISTRY_CLIENT.schedule_catalog('nvidia', entries, 'runtime-catalog')\n",
        "        if entries:\n"
        "            self.model_state.upsert_catalog(entries, source='nvidia:cached-upstream-metadata')\n"
        "            MODEL_REGISTRY.register_catalog(entries, revision='runtime-catalog')\n"
        "            MODEL_REGISTRY_CLIENT.schedule_catalog('nvidia', entries, 'runtime-catalog')\n"
        "            # Reinforcement: enqueue models needing context verification.\n"
        "            probe = getattr(self, '_context_probe', None)\n"
        "            if probe is not None:\n"
        "                try:\n"
        "                    probe.notify_catalog(entries)\n"
        "                except Exception:\n"
        "                    pass\n",
        'notify-sync')

    # ---- 4. probe_fn method + service init ----
    replace_once(
        "    async def init(self):\n"
        "        await MODEL_REGISTRY_CLIENT.start()\n",
        "    async def _probe_context_upstream(self, payload: dict):\n"
        "        \"\"\"Probe a model's max context via the upstream chat surface.\n"
        "\n"
        "        Uses a key from the existing pool (peek_key): rotation,\n"
        "        cooldown and model-scoped carve-out semantics all apply.\n"
        "        \"\"\"\n"
        "        peek = getattr(self.pool, 'peek_key', None)\n"
        "        key_entry = peek() if callable(peek) else None\n"
        "        api_key = getattr(key_entry, 'api_key', None) or os.environ.get('NVIDIA_API_KEY', '')\n"
        "        if not api_key:\n"
        "            return ProbeOutcome(status='skipped', detail='no upstream key available')\n"
        "        url = BASE_LLM.rstrip('/') + '/v1/chat/completions'\n"
        "        try:\n"
        "            timeout = aiohttp.ClientTimeout(total=300)\n"
        "            async with self._session.post(url, json=payload, headers={\n"
        "                'authorization': f'Bearer {api_key}',\n"
        "                'content-type': 'application/json',\n"
        "                'accept': 'application/json',\n"
        "            }, timeout=timeout) as resp:\n"
        "                if resp.status == 200:\n"
        "                    try:\n"
        "                        body = await resp.json(content_type=None)\n"
        "                    except Exception:\n"
        "                        body = {}\n"
        "                    usage = (body or {}).get('usage') or {}\n"
        "                    return ProbeOutcome(status='accepted', http_status=200,\n"
        "                                        prompt_tokens=positive_int(usage.get('prompt_tokens')))\n"
        "                detail = (await resp.text())[:500]\n"
        "                if resp.status in (400, 413, 422):\n"
        "                    evidence = None\n"
        "                    probe = getattr(self, '_context_probe', None)\n"
        "                    if probe is not None:\n"
        "                        try:\n"
        "                            evidence = probe.parse_rejection(resp.status, detail)\n"
        "                        except Exception:\n"
        "                            evidence = None\n"
        "                    if evidence:\n"
        "                        return ProbeOutcome(status='rejected', http_status=resp.status,\n"
        "                                            evidence=evidence, detail=detail)\n"
        "                return ProbeOutcome(status='error', http_status=resp.status, detail=detail)\n"
        "        except asyncio.CancelledError:\n"
        "            raise\n"
        "        except Exception as exc:\n"
        "            return ProbeOutcome(status='error', detail=str(exc)[:300])\n"
        "\n"
        "    async def init(self):\n"
        "        await MODEL_REGISTRY_CLIENT.start()\n",
        'probe-fn')

    replace_once(
        "        self.registry.on_refresh = self._sync_context_catalog\n"
        "        self.pool.on_models_refresh = self._sync_context_catalog\n",
        "        self.registry.on_refresh = self._sync_context_catalog\n"
        "        self.pool.on_models_refresh = self._sync_context_catalog\n"
        "        # Reinforcement (2026-10-15): background max-context probing that\n"
        "        # follows the existing refresh cycle. Probes go through the\n"
        "        # upstream chat surface with pool keys; the service skips work\n"
        "        # while live requests are waiting (F7 parity).\n"
        "        self._context_probe = ContextProbeService(\n"
        "            'nvidia', self.model_state, self._probe_context_upstream,\n"
        "            busy_fn=lambda: bool(getattr(self.pool, '_waiting', None)),\n"
        "            endpoint=BASE_LLM.rstrip('/') + '/v1/chat/completions')\n",
        'service-init')

    SRC.write_text(text, encoding='utf-8')
    print('written')
    r = subprocess.run([sys.executable, '-m', 'py_compile', str(SRC)],
                       capture_output=True, text=True)
    print('py_compile:', 'OK' if r.returncode == 0 else r.stderr)
    sys.exit(r.returncode)


if __name__ == '__main__':
    main()
