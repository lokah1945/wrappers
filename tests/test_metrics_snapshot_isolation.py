"""Metrics fixtures must never read or overwrite a production snapshot."""
import ast
import asyncio
import json
import os
import threading
import time
from pathlib import Path

import pytest

from common.wrapper_scope import select_wrappers

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('provider', [p for p in select_wrappers(include_registry=False)
                                      if p in ('nous', 'openrouter')])
async def test_explicit_snapshot_isolates_load_save_and_restart(tmp_path, monkeypatch, provider):
    source = ROOT / provider / 'src' / ('main.py' if provider == 'nous' else 'metrics.py')
    tree = ast.parse(source.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Metrics')
    fake_source = tmp_path / 'deployment' / 'src' / 'main.py'
    fake_source.parent.mkdir(parents=True)
    deployed_snapshot = fake_source.parent.parent / 'metrics-snapshot.json'
    original = json.dumps({'requests': 99, 'tokens_in': 500, 'tokens_out': 30, 'errors': 2})
    deployed_snapshot.write_text(original)
    isolated = tmp_path / 'fixture-metrics.json'
    monkeypatch.setenv('METRICS_SNAPSHOT_FILE', str(isolated))
    namespace = dict(os=os, json=json, threading=threading, time=time, Path=Path,
                     __file__=str(fake_source))
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    metrics_class = namespace['Metrics']
    metrics = metrics_class()
    assert metrics.requests == 0
    if provider == 'nous':
        metrics.record(prompt=7, completion=3)
    else:
        await metrics.record_request(prompt_tokens=7, completion_tokens=3)
    metrics._persist()
    restored = metrics_class()
    assert (restored.requests, restored.tokens_in, restored.tokens_out) == (1, 7, 3)
    assert deployed_snapshot.read_text() == original
    monkeypatch.delenv('METRICS_SNAPSHOT_FILE')
    assert metrics_class().requests == 99  # Default deployment path is unchanged.


@pytest.mark.parametrize('provider', [p for p in select_wrappers(include_registry=False)
                                      if p in ('nvidia-python', 'nous')])
def test_skip_dotenv_prevents_watcher_registration(monkeypatch, provider):
    source = ROOT / provider / 'src/main.py'
    node = next(n for n in ast.parse(source.read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name == 'start_env_watcher')
    monkeypatch.setenv('WRAPPER_SKIP_DOTENV', 'true')
    # No watcher objects or dotenv loader are supplied: any attempt to consult
    # them, even before instantiation, fails the isolation contract.
    namespace = {'os': os}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    namespace['start_env_watcher']()


async def test_nvidia_periodic_probe_can_be_disabled_and_never_runs_at_boot():
    assert 'nvidia-python' in select_wrappers(include_registry=False)
    source = ROOT / 'nvidia-python/src/main.py'
    node = next(n for n in ast.parse(source.read_text()).body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == 'verify_loop')
    namespace = {'_env_flag': lambda *args: False, 'VERIFY_INTERVAL': 1800000}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    await namespace['verify_loop'](object())  # No asyncio/probe access while disabled.
    calls = []
    async def first_sleep(seconds):
        calls.append(seconds)
        raise asyncio.CancelledError()
    from types import SimpleNamespace
    namespace.update(_env_flag=lambda *args: True,
                     asyncio=SimpleNamespace(sleep=first_sleep, CancelledError=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await namespace['verify_loop'](object())
    assert calls == [1800]
