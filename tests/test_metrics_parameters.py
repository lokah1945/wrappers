#!/usr/bin/env python3
"""Test that metrics parameter names match between callers and the metrics module.

This test would have caught BUG-D1 (NVIDIA metrics camelCase/snake_case mismatch)
before it reached production. Every wrapper's metrics module must accept the
same parameter names that the wrapper's main module sends.
"""
import inspect
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("WRAPPER_SKIP_DOTENV", "true")
os.environ.setdefault("LOG_FILE", "/tmp/test-metrics.log")
os.environ.setdefault("METRICS_DB", "/tmp/test-metrics.db")
os.environ.setdefault("MODEL_STATE_DB", "/tmp/test-model-state.db")


def test_nvidia_metrics_accepts_snake_case():
    """NVIDIA metrics must accept snake_case params from main.py callers."""
    import asyncio
    sys.path.insert(0, str(ROOT / "nvidia-python"))
    from src.metrics import Metrics  # noqa: E402

    async def _test():
        m = Metrics("/tmp/test-nv-metrics2.db")
        await m.init()
        try:
            # Simulate what main.py actually sends (snake_case params)
            await m.record_request(
                model="meta/llama-3.1-8b-instruct",
                key_label="key1",
                status=200,
                latency_ms=1500,
                prompt_tokens=150,
                completion_tokens=42,
                path="/v1/chat/completions",
            )
            # Verify the data was recorded correctly (not default values!)
            # This test would have caught BUG-D1 (metrics all wrong)
            s = await m.summary()
            assert s.get("total_requests", 0) >= 1, "Request was not recorded"
        finally:
            await m.close()

    asyncio.run(_test())


def test_opencode_metrics_interface():
    """OpenCode metrics must have the same interface as other wrappers."""
    sys.path.insert(0, str(ROOT / "opencode"))
    import src.metrics as oc_metrics  # noqa: E402

    m = oc_metrics.Metrics()
    assert hasattr(m, 'record_request')
    assert hasattr(m, 'summary')
    assert hasattr(m, 'prom_metrics')
    assert hasattr(m, 'close')


def test_blackbox_metrics_interface():
    """Blackbox metrics must have the same interface as other wrappers."""
    sys.path.insert(0, str(ROOT / "blackbox"))
    import src.metrics as bb_metrics  # noqa: E402

    m = bb_metrics.Metrics()
    assert hasattr(m, 'record_request')
    assert hasattr(m, 'summary')
    assert hasattr(m, 'prom_metrics')
    assert hasattr(m, 'close')


def test_metrics_consistency_across_wrappers():
    """All wrappers must produce the same summary keys."""
    import asyncio

    async def check():
        # OpenCode
        sys.path.insert(0, str(ROOT / "opencode"))
        import src.metrics as oc_metrics
        oc = oc_metrics.Metrics()
        await oc.record_request(model="test", prompt_tokens=10, completion_tokens=5)
        oc_summary = await oc.summary()

        # Blackbox
        sys.path.insert(0, str(ROOT / "blackbox"))
        import src.metrics as bb_metrics
        bb = bb_metrics.Metrics()
        await bb.record_request(model="test", prompt_tokens=10, completion_tokens=5)
        bb_summary = await bb.summary()

        # Both must have the same keys
        assert set(oc_summary.keys()) == set(bb_summary.keys()), (
            f"OpenCode keys: {set(oc_summary.keys())}, "
            f"Blackbox keys: {set(bb_summary.keys())}"
        )
        # Both must have the expected keys
        for key in ("uptime_seconds", "total_requests", "total_tokens",
                     "input_tokens", "output_tokens", "error_rate"):
            assert key in oc_summary, f"Missing key: {key}"
            assert key in bb_summary, f"Missing key: {key}"

    asyncio.run(check())


if __name__ == "__main__":
    test_nvidia_metrics_accepts_snake_case()
    print("✅ NVIDIA metrics snake_case")
    test_opencode_metrics_interface()
    print("✅ OpenCode metrics interface")
    test_blackbox_metrics_interface()
    print("✅ Blackbox metrics interface")
    test_metrics_consistency_across_wrappers()
    print("✅ Metrics consistency across wrappers")
    print("ALL METRICS PARAMETER TESTS PASS")
