#!/usr/bin/env python3
"""Safe VPS production audit runner for the wrapper monorepo.

Default mode is read-only preflight. Inference smoke/load calls are explicit
flags and always use the exact model supplied by the operator.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REDACT = re.compile(r"(?i)(bearer\s+|(?:nvapi|sk|ghp|github_pat)-)[A-Za-z0-9_\-.]+")
FORBIDDEN_RUNTIME_MARKERS = (
    "_build_fallback_candidates",
    "MODEL_FALLBACK_ENABLED",
    "MODEL_FALLBACK_MAX_HOPS",
    "select_fallback_model",
    "select_alternative_model",
)


class Audit:
    def __init__(self, repo: Path):
        self.repo = repo
        self.lines: list[str] = []
        self.failures = 0
        self.blocked = 0
        self.passes = 0

    def log(self, level: str, title: str, detail: str = "") -> None:
        clean = REDACT.sub(r"\1[REDACTED]", str(detail))
        self.lines.append(f"- **{level}** — {title}: {clean}")
        if level == "PASS":
            self.passes += 1
        elif level == "FAIL":
            self.failures += 1
        elif level == "BLOCKED":
            self.blocked += 1

    def command(self, cmd: list[str], timeout: int = 120) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.repo,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            return proc.returncode, REDACT.sub(r"\1[REDACTED]", proc.stdout[-6000:])
        except FileNotFoundError:
            return 127, f"command not found: {cmd[0]}"
        except subprocess.TimeoutExpired:
            return 124, "command timed out"

    def http(self, url: str, method: str = "GET", payload: dict | None = None,
             api_key: str | None = None, timeout: int = 10) -> tuple[int, float, dict | None, str]:
        headers = {"Accept": "application/json"}
        data = None
        if api_key is not None:
            headers["Authorization"] = f"Bearer {api_key}"
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode()
        req = Request(url, data=data, headers=headers, method=method)
        started = time.perf_counter()
        try:
            with urlopen(req, timeout=timeout) as response:
                raw = response.read(65536)
                status = int(response.status)
        except HTTPError as exc:
            raw = exc.read(65536)
            status = int(exc.code)
        except (URLError, TimeoutError, OSError) as exc:
            return 0, (time.perf_counter() - started) * 1000, None, str(exc)
        elapsed = (time.perf_counter() - started) * 1000
        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeDecodeError):
            parsed = None
        return status, elapsed, parsed if isinstance(parsed, dict) else None, ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", type=Path, default=None)
    parser.add_argument("--registry-url", default="http://127.0.0.1:9200")
    parser.add_argument("--wrapper-url", default=None, help="Wrapper /v1 base for explicit smoke/load")
    parser.add_argument("--model", default=None, help="Exact model for explicit smoke/load")
    parser.add_argument("--api-key-env", default=None, help="Env var containing local wrapper token")
    parser.add_argument("--run-tests", action="store_true")
    parser.add_argument("--run-smoke", action="store_true")
    parser.add_argument("--run-load", action="store_true")
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = (args.repo_dir or Path(__file__).resolve().parents[1]).resolve()
    audit = Audit(repo)
    timestamp = dt.datetime.now().astimezone().isoformat()

    audit.lines.extend([
        "# Production audit report",
        "",
        f"- Timestamp: `{timestamp}`",
        f"- Repository: `{repo}`",
        "- Mode: safe preflight plus explicitly requested tests/smoke/load",
        "",
        "## Results",
    ])

    if not (repo / "wrappers.json").is_file():
        audit.log("FAIL", "repository layout", "wrappers.json is missing")
    else:
        audit.log("PASS", "repository layout", "wrappers.json present")

    rc, out = audit.command(["git", "rev-parse", "HEAD"])
    audit.log("PASS" if rc == 0 else "FAIL", "deployed commit", out.strip())
    rc, out = audit.command(["git", "status", "--porcelain"])
    if rc == 0 and not out.strip():
        audit.log("PASS", "working tree", "clean")
    elif rc == 0:
        audit.log("BLOCKED", "working tree", "uncommitted changes are present")
    else:
        audit.log("BLOCKED", "working tree", out)

    install = repo / "install.sh"
    audit.log("PASS" if install.is_file() and os.access(install, os.X_OK) else "FAIL",
              "installer executable", str(install))

    for directory in ("common", "model-registry", "nvidia-python", "nous", "opencode", "blackbox"):
        path = repo / directory
        audit.log("PASS" if path.is_dir() else "FAIL", f"directory {directory}", str(path))

    runtime_files = []
    for directory in ("common", "model-registry", "nvidia-python", "nous", "opencode", "blackbox"):
        runtime_files.extend((repo / directory).rglob("*.py"))
    forbidden = []
    for path in runtime_files:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for marker in FORBIDDEN_RUNTIME_MARKERS:
            if marker in text:
                forbidden.append(f"{path.relative_to(repo)}:{marker}")
    audit.log("PASS" if not forbidden else "FAIL", "no model substitution markers", "; ".join(forbidden) or "none")

    config_names = [
        "nvidia-python/.env", "nous/.env", "opencode/.env", "blackbox/.env", "model-registry/.env"
    ]
    for name in config_names:
        path = repo / name
        audit.log("PASS" if path.is_file() else "BLOCKED", f"config presence {name}", "present" if path.is_file() else "missing")

    if shutil.which("systemctl"):
        units = [
            "wrapper-model-registry.service",
            "wrapper-nvidia-python.service",
            "wrapper-nous.service",
            "wrapper-opencode.service",
            "wrapper-blackbox.service",
        ]
        for unit in units:
            rc, out = audit.command(["systemctl", "is-active", unit], timeout=15)
            audit.log("PASS" if rc == 0 and out.strip() == "active" else "BLOCKED", f"systemd {unit}", out.strip() or "inactive")
    else:
        audit.log("BLOCKED", "systemd availability", "systemctl is not available")

    endpoints = {
        "registry": f"{args.registry_url.rstrip('/')}/health",
        "nvidia": "http://127.0.0.1:9101/health",
        "nous": "http://127.0.0.1:9102/health",
        "opencode": "http://127.0.0.1:9103/health",
        "blackbox": "http://127.0.0.1:9104/health",
    }
    for name, url in endpoints.items():
        status, ms, body, error = audit.http(url)
        ok = 200 <= status < 300
        detail = f"HTTP {status}, {ms:.1f} ms"
        if error:
            detail += f", {error}"
        elif isinstance(body, dict):
            detail += f", status={body.get('status', body.get('ok', 'unknown'))}"
        audit.log("PASS" if ok else "BLOCKED", f"endpoint {name}", detail)

    if args.run_tests:
        rc, out = audit.command([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], timeout=1800)
        audit.log("PASS" if rc == 0 else "FAIL", "repository tests", out)
        rc, out = audit.command([sys.executable, "tests/run_transparency_check.py"], timeout=300)
        audit.log("PASS" if rc == 0 else "FAIL", "cross-wrapper transparency", out)

    if args.run_smoke or args.run_load:
        if not args.wrapper_url or not args.model:
            audit.log("BLOCKED", "explicit model test", "--wrapper-url and --model are required")
        elif not args.api_key_env or not os.environ.get(args.api_key_env):
            audit.log("BLOCKED", "explicit model test", "--api-key-env is missing or empty")
        else:
            api_key = os.environ[args.api_key_env]
            if args.run_smoke:
                status, ms, body, error = audit.http(
                    f"{args.wrapper_url.rstrip('/')}/chat/completions",
                    method="POST",
                    payload={
                        "model": args.model,
                        "messages": [{"role": "user", "content": "Reply exactly OK."}],
                        "max_tokens": 8,
                        "stream": False,
                    },
                    api_key=api_key,
                    timeout=180,
                )
                returned_model = body.get("model") if isinstance(body, dict) else None
                identity_ok = returned_model in (None, args.model)
                ok = 200 <= status < 300 and identity_ok
                detail = f"HTTP {status}, {ms:.1f} ms, returned_model={returned_model or 'not-present'}"
                if error:
                    detail += f", {error}"
                audit.log("PASS" if ok else "FAIL", "exact-model smoke", detail)
            if args.run_load:
                env = {**os.environ, "API_KEY": api_key, "PYTHONDONTWRITEBYTECODE": "1"}
                cmd = [
                    sys.executable, "tests/perf/load_agent_sim.py",
                    "--base-url", args.wrapper_url,
                    "--model", args.model,
                    "--requests", str(args.requests),
                    "--concurrency", str(args.concurrency),
                ]
                try:
                    proc = subprocess.run(cmd, cwd=repo, env=env, text=True,
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          timeout=3600)
                    audit.log("PASS" if proc.returncode == 0 else "FAIL", "bounded load", proc.stdout[-6000:])
                except subprocess.TimeoutExpired:
                    audit.log("FAIL", "bounded load", "timed out")

    audit.lines.extend([
        "",
        "## Summary",
        f"- PASS: `{audit.passes}`",
        f"- FAIL: `{audit.failures}`",
        f"- BLOCKED: `{audit.blocked}`",
        "",
        "## Interpretation",
        "- BLOCKED means the VPS did not provide the required service/configuration or an explicit test flag was not supplied.",
        "- FAIL means an available component violated an acceptance criterion.",
        "- A production-ready decision requires zero FAIL and no unreviewed BLOCKED result.",
        "- The report intentionally does not include secrets or response bodies.",
    ])

    reports = repo / "productions" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    filename = reports / f"production-audit-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    filename.write_text("\n".join(audit.lines) + "\n")
    print(filename)
    print(f"PASS={audit.passes} FAIL={audit.failures} BLOCKED={audit.blocked}")
    return 1 if audit.failures else (2 if audit.blocked else 0)


if __name__ == "__main__":
    raise SystemExit(main())
