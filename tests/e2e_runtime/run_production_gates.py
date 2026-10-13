#!/usr/bin/env python3
"""Run the release checks against local mocks, without live inference."""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from process_isolation import isolated_environment

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from common.wrapper_scope import frozen_wrappers, select_wrappers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--soak-seconds", type=int, default=15)
    parser.add_argument("--concurrency", type=int, default=6)
    args = parser.parse_args()
    if args.soak_seconds < 1 or args.concurrency < 1:
        parser.error("--soak-seconds and --concurrency must be positive")
    output = args.output_dir or Path(tempfile.mkdtemp(prefix="wrapper-release-gates-"))
    output.mkdir(parents=True, exist_ok=True)
    print(f"Evidence directory: {output.resolve()}", flush=True)
    selected = select_wrappers(repo=ROOT)
    scope = {"production": selected, "pending_excluded": frozen_wrappers(ROOT),
             "native_production_defaults": "acceptance", "explicit_legacy_chat_adapter_fixtures": ["runtime", "sdk", "matrix", "compat", "soak"]}
    import json
    (output / "scope.json").write_text(json.dumps(scope, indent=2) + "\n")
    print(f"Production scope: {', '.join(selected)}; frozen providers excluded: {', '.join(scope['pending_excluded'])}", flush=True)
    env = isolated_environment()
    env.update({"BEARER_TOKEN": "", "FREE_ONLY": "no"})
    commands = [
        ("runtime", ["tests/e2e_runtime/run_runtime_e2e.py"]),
        ("sdk", ["tests/e2e_runtime/sdk_codex_compat.py"]),
        ("matrix", ["tests/e2e_runtime/full_matrix_audit.py", "--output", str(output.resolve() / "matrix.json")]),
        ("compat", ["tests/e2e_runtime/compat_layer_e2e.py"]),
        ("acceptance", ["tests/e2e_runtime/active_production_acceptance.py", "--output", str(output.resolve() / "acceptance.json")]),
        ("soak", ["tests/e2e_runtime/soak.py", "--seconds", str(args.soak_seconds), "--concurrency", str(args.concurrency)]),
        ("transparency", ["tests/run_transparency_check.py"]),
        ("unit", ["-m", "pytest", "tests/", "nvidia-python/tests/", "-q", "-rs", "-p", "no:cacheprovider", "--tb=short"]),
    ]
    for name, command in commands:
        print(f"Running {name} gate…", flush=True)
        started = time.monotonic()
        log = output / f"{name}.log"
        with log.open("w") as stream:
            result = subprocess.run([sys.executable, *command], cwd=ROOT, env=env,
                                    stdout=stream, stderr=subprocess.STDOUT)
        elapsed = time.monotonic() - started
        if result.returncode:
            print(f"FAIL {name} (exit {result.returncode}, {elapsed:.1f}s); evidence: {log}", flush=True)
            print(log.read_text(errors="replace")[-6000:], flush=True)
            return result.returncode
        print(f"PASS {name} ({elapsed:.1f}s); evidence: {log}", flush=True)
    print("All production gates passed against isolated local mocks.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
