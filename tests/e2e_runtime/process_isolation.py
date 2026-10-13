"""Keep executable test gates away from production credentials and state."""
from __future__ import annotations

import atexit
import os
import shutil
import socket
import tempfile
from pathlib import Path

_STATE_DIRS: list[Path] = []


def isolated_environment() -> dict[str, str]:
    # An allowlist also excludes OAuth paths, proxy credentials and provider
    # settings whose names future wrappers may introduce.
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH", "VIRTUAL_ENV"}}
    state = Path(tempfile.mkdtemp(prefix="wrapper-e2e-"))
    _STATE_DIRS.append(state)
    env.update({
        "WRAPPER_SKIP_DOTENV": "true", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1", "VERIFY_ON_BOOT": "false",
        "VERIFY_PERIODIC_ENABLED": "false",
        "AUTH_PATH": str(state / "missing-auth.json"),
        "MODEL_REGISTRY_URL": "", "MODEL_REGISTRY_ADMIN_TOKEN": "",
        "MODEL_REGISTRY_STRICT": "false",
        "MODEL_STATE_DB": str(state / "model-state.db"),
        "MODEL_REGISTRY_DB": str(state / "registry.db"),
        "METRICS_DB": str(state / "metrics.db"),
        "METRICS_SNAPSHOT_FILE": str(state / "metrics-snapshot.json"),
        "CATALOG_DB": str(state / "missing-catalog.db"),
        "LOG_FILE": str(state / "wrapper.log"),
        "EVENTS_FILE": str(state / "events.jsonl"),
        "ALERT_OUTPUT": str(state / "alert-history.jsonl"),
        "LOKI_PUSH_URL": "http://127.0.0.1:0/disabled-loki",
        "NGC_CONTEXT_CACHE_FILE": str(state / "ngc-context.json"),
        "NGC_FEATURED_MODELS_URL": "http://127.0.0.1:0/disabled-ngc",
        "NVIDIA_BUILD_CONTEXT_ENABLED": "false",
    })
    return env


def assert_ports_free(*ports: int) -> None:
    """Fail before launching children if a requested port belongs to a service."""
    for port in ports:
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError as exc:
                raise RuntimeError(f"Test port {port} is already in use; existing service left untouched") from exc


def stop_process(process, timeout: float = 15) -> None:
    """Wait for our child to exit before reusing its port or deleting state."""
    import subprocess
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


@atexit.register
def _cleanup_state() -> None:
    for state in _STATE_DIRS:
        shutil.rmtree(state, ignore_errors=True)
