"""Regression tests for the active-wrapper Activity telemetry contract."""
import ast
import asyncio
import json
import os
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _class_from_source(path: Path, name: str, extra=None):
    tree = ast.parse(path.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    namespace = {
        'os': os, 'json': json, 'threading': threading, 'time': time,
        'Path': Path, '__file__': str(path),
    }
    if extra:
        namespace.update(extra)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace[name]


def _functions_from_nvidia(*names):
    path = ROOT / 'nvidia-python/src/main.py'
    tree = ast.parse(path.read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name in names]
    namespace = {'json': json}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


def test_nvidia_sse_usage_parser_handles_split_frames():
    ns = _functions_from_nvidia('_usage_pair', '_observe_sse_usage', '_sniff_stream_usage')
    sniff = ns['_sniff_stream_usage']

    async def source():
        yield b'data: {"usage":{"prompt_tokens":17,'
        yield b'"completion_tokens":9}}\n\n'
        yield b'data: [DONE]\n\n'

    async def run():
        state = {'prompt_tokens': 0, 'completion_tokens': 0}
        output = [chunk async for chunk in sniff(source(), state)]
        return output, state

    output, state = asyncio.run(run())
    assert output[-1].endswith(b'\n\n')
    assert state == {'prompt_tokens': 17, 'completion_tokens': 9}


def test_nvidia_sqlite_activity_preserves_tokens_and_latency(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / 'nvidia-python'))
    from src.metrics import Metrics

    async def run():
        metrics = Metrics(str(tmp_path / 'metrics.db'))
        await metrics.init()
        try:
            await metrics.record_request(
                model='test/model', key_label='test-key', status=200,
                latency_ms=1234, prompt_tokens=17, completion_tokens=9,
                total_tokens=26, streaming=True, path='/v1/chat/completions')
            rows = await metrics.recent_requests(1)
            assert rows[0]['prompt_tokens'] == 17
            assert rows[0]['completion_tokens'] == 9
            assert rows[0]['total_tokens'] == 26
            assert rows[0]['latency_ms'] == 1234
        finally:
            await metrics.close()

    asyncio.run(run())


def test_nous_stream_completion_updates_tokens_and_latency(tmp_path, monkeypatch):
    monkeypatch.setenv('METRICS_SNAPSHOT_FILE', str(tmp_path / 'nous.json'))
    Metrics = _class_from_source(ROOT / 'nous/src/main.py', 'Metrics')
    metrics = Metrics()
    metrics.record(model='test/model', key_label='test-key', status=200,
                   path='/v1/messages', streaming=True)
    metrics.record_stream_usage(model='test/model', key_label='test-key',
                                prompt_tokens=17, completion_tokens=9,
                                latency_ms=2345)
    row = metrics.recent_activity(1)[0]
    assert row['prompt_tokens'] == 17
    assert row['completion_tokens'] == 9
    assert row['latency_ms'] == 2345
    model = metrics.per_model_summary()[0]
    assert model['avg_latency_ms'] == 2345


def test_openrouter_stream_completion_updates_tokens_and_latency(tmp_path, monkeypatch):
    monkeypatch.setenv('METRICS_SNAPSHOT_FILE', str(tmp_path / 'openrouter.json'))
    Metrics = _class_from_source(ROOT / 'openrouter/src/metrics.py', 'Metrics')
    metrics = Metrics()

    async def run():
        await metrics.record_request(model='test/model', key_label='test-key',
                                     status_code=200, streaming=True,
                                     path='/v1/chat/completions')
        await metrics.record_tokens(model='test/model', key_label='test-key',
                                    prompt_tokens=17, completion_tokens=9,
                                    latency_ms=3456)

    asyncio.run(run())
    row = metrics.recent_activity(1)[0]
    assert row['prompt_tokens'] == 17
    assert row['completion_tokens'] == 9
    assert row['latency_ms'] == 3456
    model = metrics.per_model_summary()[0]
    assert model['avg_latency_ms'] == 3456


def test_openrouter_activity_endpoints_and_dashboard_latency_column():
    openrouter = (ROOT / 'openrouter/src/main.py').read_text()
    dashboard = (ROOT / 'dashboard/index.html').read_text()
    for route in ('/metrics/keys', '/metrics/activity', '/metrics/chart/hourly'):
        assert f'@app.get("{route}")' in openrouter
    assert "fmtMs(r.latency_ms)" in dashboard
    assert "fallback: 'unified_snapshot'" in dashboard
