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
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.wrapper_scope import (
    WrapperScopeError, frozen_wrappers, load_wrappers, production_wrappers, select_wrappers,
)

REDACT = re.compile(r"(?i)(bearer\s+|(?:nvapi|sk|ghp|github_pat)-)[A-Za-z0-9_\-.]+")
FORBIDDEN_RUNTIME_MARKERS = (
    "_build_fallback_candidates",
    "MODEL_FALLBACK_ENABLED",
    "MODEL_FALLBACK_MAX_HOPS",
    "select_fallback_model",
    "select_alternative_model",
)


def service_targets(repo: Path | None = None) -> dict[str, int]:
    entries = load_wrappers(repo)
    return {name: entries[name]["port"] for name in production_wrappers(repo)}


def reject_frozen_service_url(url: str | None, repo: Path | None = None) -> None:
    if not url:
        return
    target = urlparse(url)
    if target.hostname not in ("127.0.0.1", "localhost", "::1"):
        return
    entries = load_wrappers(repo)
    for name, config in entries.items():
        if config["status"] != "production" and target.port == config.get("port"):
            raise WrapperScopeError(f"{name}: pending wrapper is frozen; direct endpoint selection is excluded")


# Public default contract retained for tests and callers; main reloads metadata
# from --repo-dir so status changes cannot be bypassed by an imported snapshot.
SERVICE_TARGETS = service_targets()


def canonical_wrapper(name: str) -> str:
    return "nvidia-python" if name == "nvidia" else name


def isolated_test_environment(state: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH", "VIRTUAL_ENV"}}
    env.update({
        "WRAPPER_SKIP_DOTENV": "true", "PYTHONDONTWRITEBYTECODE": "1",
        "VERIFY_ON_BOOT": "false", "BEARER_TOKEN": "", "FREE_ONLY": "no",
        "VERIFY_PERIODIC_ENABLED": "false",
        "AUTH_PATH": str(state / "missing-auth.json"),
        "MODEL_REGISTRY_URL": "", "MODEL_REGISTRY_ADMIN_TOKEN": "",
        "MODEL_REGISTRY_STRICT": "false",
        "MODEL_STATE_DB": str(state / "model-state.db"),
        "MODEL_REGISTRY_DB": str(state / "registry.db"),
        "METRICS_DB": str(state / "metrics.db"),
        "METRICS_SNAPSHOT_FILE": str(state / "metrics-snapshot.json"),
        "CATALOG_DB": str(state / "missing-catalog.db"),
        "LOG_FILE": str(state / "test.log"), "EVENTS_FILE": str(state / "events.jsonl"),
        "ALERT_OUTPUT": str(state / "alert-history.jsonl"),
        "LOKI_PUSH_URL": "http://127.0.0.1:0/disabled-loki",
        "NGC_CONTEXT_CACHE_FILE": str(state / "ngc-context.json"),
        "NGC_FEATURED_MODELS_URL": "http://127.0.0.1:0/disabled-ngc",
        "NVIDIA_BUILD_CONTEXT_ENABLED": "false",
    })
    return env


def readiness_ok(status: int, body: dict | None) -> bool:
    # Several wrappers deliberately return HTTP 200 with ready=false.
    return 200 <= status < 300 and isinstance(body, dict) and body.get("ready") is True


class Audit:
    def __init__(self, repo: Path):
        self.repo = repo
        self.lines: list[str] = []
        self.failures = 0
        self.blocked = 0
        self.passes = 0

    def log(self, level: str, title: str, detail: str = "") -> None:
        clean = REDACT.sub(r"\1[REDACTED]", str(detail))
        clean = re.sub(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", clean)
        self.lines.append(f"- **{level}** — {title}: {clean}")
        if level == "PASS":
            self.passes += 1
        elif level == "FAIL":
            self.failures += 1
        elif level == "BLOCKED":
            self.blocked += 1

    def command(self, cmd: list[str], timeout: int = 120, isolated: bool = False) -> tuple[int, str]:
        state = tempfile.TemporaryDirectory(prefix="wrapper-audit-tests-") if isolated else None
        env = isolated_test_environment(Path(state.name)) if state else {
            **os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.repo,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                env=env,
            )
            return proc.returncode, REDACT.sub(r"\1[REDACTED]", proc.stdout[-6000:])
        except FileNotFoundError:
            return 127, f"command not found: {cmd[0]}"
        except subprocess.TimeoutExpired:
            return 124, "command timed out"
        finally:
            if state:
                state.cleanup()

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
    parser.add_argument("--api-key-env", default="WRAPPER_API_KEY", help="Env var containing local wrapper token (default: WRAPPER_API_KEY)")
    parser.add_argument("--surface", action="append", choices=["chat", "responses", "messages"],
                        help="Surface to smoke (repeatable): chat=/v1/chat/completions, responses=/v1/responses, messages=/v1/messages")
    parser.add_argument("--run-tests", action="store_true")
    parser.add_argument("--report-dir", type=Path, default=None, help="Report output directory")
    parser.add_argument("--run-smoke", action="store_true")
    parser.add_argument("--smoke-all", action="store_true",
                        help="Smoke every required inference wrapper with the explicit --model")
    parser.add_argument("--run-load", action="store_true")
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--required-wrapper", action="append", type=canonical_wrapper,
                        default=None,
                        help="Production wrapper required to be ready; repeatable (default: enabled services and registry). Pending selections are rejected.")
    parser.add_argument("--require-registry", action="store_true")
    args = parser.parse_args()
    try:
        args.required_wrapper = list(select_wrappers(args.required_wrapper, args.repo_dir))
        reject_frozen_service_url(args.wrapper_url, args.repo_dir)
        reject_frozen_service_url(args.registry_url, args.repo_dir)
    except WrapperScopeError as exc:
        parser.error(str(exc))
    if args.requests < 1 or args.concurrency < 1:
        parser.error("--requests and --concurrency must be positive")
    if args.smoke_all and not (args.run_smoke or args.run_load):
        parser.error("--smoke-all requires --run-smoke or --run-load")
    return args


def error_summary(body: dict | None) -> str:
    if not isinstance(body, dict):
        return "error_type=unstructured"
    error = body.get("error") if isinstance(body.get("error"), dict) else body
    if not isinstance(error, dict):
        return "error_type=unknown"
    error_type = error.get("type") or body.get("type") or "unknown"
    code = error.get("code") or error.get("provider_code") or "none"
    return f"error_type={error_type},error_code={code}"


def _run_load(audit, repo, base_url, model, api_key, requests, concurrency) -> None:
    """Bounded load test against one wrapper; records p50/p95/p99/TTFT to report."""
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH", "VIRTUAL_ENV"}}
    env.update({"API_KEY": api_key, "PYTHONDONTWRITEBYTECODE": "1"})
    cmd = [
        sys.executable, "tests/perf/load_agent_sim.py",
        "--base-url", base_url,
        "--model", model,
        "--requests", str(requests),
        "--concurrency", str(concurrency),
    ]
    try:
        proc = subprocess.run(cmd, cwd=repo, env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=3600)
        out = proc.stdout or ""
        # Parse load metrics: ok=X/Y error=Z and error_class=...
        import re as _re
        m = _re.search(r"ok=(\d+)/(\d+)\s+error=(\d+)", out)
        ok_count = int(m.group(1)) if m else 0
        total = int(m.group(2)) if m else 0
        err_count = int(m.group(3)) if m else 0
        cls = "unknown"
        cm = _re.search(r"error_class=(\w+)", out)
        if cm:
            cls = cm.group(1)
        # SLA thresholds (M-04 / agent finding #4)
        # Publish metrics only, never the simulator's sample response body.
        metrics = "\n".join(line for line in out.splitlines()
                            if line.startswith(("ok=", "latency_ms ", "ttft_ms ")))
        if proc.returncode == 0 and total == requests and ok_count == total and err_count == 0:
            level = "PASS"
            detail = metrics
        elif cls == "external_outage":
            # upstream 503/502 majority => external, not wrapper defect
            level = "BLOCKED"
            detail = f"external provider outage ({err_count}/{total} errored): {metrics}"
        else:
            # provider/runtime reliability error => real FAIL
            level = "FAIL"
            detail = f"load reliability error ({err_count}/{total}), exit={proc.returncode}: {metrics}"
        audit.log(level, f"bounded load [{base_url} {model}]", detail)
    except subprocess.TimeoutExpired:
        audit.log("FAIL", f"bounded load [{base_url} {model}]", "timed out")


def main() -> int:
    args = parse_args()
    repo = (args.repo_dir or Path(__file__).resolve().parents[1]).resolve()
    audit = Audit(repo)
    timestamp = dt.datetime.now().astimezone().isoformat()
    try:
        targets_by_service = service_targets(repo)
        args.required_wrapper = list(select_wrappers(args.required_wrapper, repo))
        reject_frozen_service_url(args.wrapper_url, repo)
        reject_frozen_service_url(args.registry_url, repo)
    except WrapperScopeError as exc:
        print(f"[audit][ERROR] {exc}", file=sys.stderr)
        return 2

    audit.lines.extend([
        "# Production audit report",
        "",
        f"- Timestamp: `{timestamp}`",
        f"- Repository: `{repo}`",
        "- Mode: safe preflight plus explicitly requested tests/smoke/load",
        "",
        "## Results",
    ])
    for name in frozen_wrappers(repo):
        audit.log("PENDING", f"provider {name}",
                  "frozen by wrappers.json; implementation, configuration, service, logs and endpoints excluded")

    if not (repo / "wrappers.json").is_file():
        audit.log("FAIL", "repository layout", "wrappers.json is missing")
    else:
        audit.log("PASS", "repository layout", "wrappers.json present")

    rc, out = audit.command(["git", "rev-parse", "HEAD"])
    checkout_commit = out.strip() if rc == 0 else ""
    audit.log("PASS" if checkout_commit else "BLOCKED", "checkout commit", checkout_commit or "unknown")
    expected_commits = {}
    for name in targets_by_service:
        marker = repo / "runtime" / f"{name}.commit"
        expected_commits[name] = marker.read_text().strip() if marker.is_file() else checkout_commit
    rc, branch = audit.command(["git", "branch", "--show-current"])
    audit.log("PASS" if rc == 0 and branch.strip() else "BLOCKED", "git branch", branch.strip() or "unknown")
    rc, origin = audit.command(["git", "remote", "get-url", "origin"])
    audit.log("PASS" if rc == 0 and origin.strip() else "BLOCKED", "git origin", "configured" if origin.strip() else "missing")
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

    for directory in ("common", *targets_by_service):
        path = repo / directory
        audit.log("PASS" if path.is_dir() else "FAIL", f"directory {directory}", str(path))

    runtime_files = []
    for directory in ("common", *targets_by_service):
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

    config_names = [f"{name}/.env" for name in targets_by_service]
    for name in config_names:
        path = repo / name
        audit.log("PASS" if path.is_file() else "BLOCKED", f"config presence {name}", "present" if path.is_file() else "missing")

    unit_states = {}
    if shutil.which("systemctl"):
        units = [f"wrapper-{name}.service" for name in targets_by_service]
        for unit in units:
            rc, out = audit.command(["systemctl", "--user", "is-active", unit], timeout=15)
            state = out.strip() or "inactive"
            unit_states[unit.removesuffix(".service").removeprefix("wrapper-")] = state
            service = unit.removesuffix(".service").removeprefix("wrapper-")
            required = service in args.required_wrapper or (service == "model-registry" and args.require_registry)
            level = "PASS" if rc == 0 and state == "active" else ("FAIL" if required else "BLOCKED")
            audit.log(level, f"systemd {unit}", state)
    else:
        audit.log("FAIL" if args.required_wrapper or args.require_registry else "BLOCKED", "systemd availability", "systemctl is not available")

    for name, port in targets_by_service.items():
        base = args.registry_url.rstrip("/") if name == "model-registry" else f"http://127.0.0.1:{port}"
        required = name in args.required_wrapper or (name == "model-registry" and args.require_registry)
        status, ms, body, error = audit.http(f"{base}/health")
        healthy = 200 <= status < 300 and isinstance(body, dict) and body.get("status") == "ok"
        detail = f"HTTP {status}, {ms:.1f} ms, status={body.get('status', 'unknown') if body else 'unknown'}"
        if error:
            detail += f", {error}"
        audit.log("PASS" if healthy else ("FAIL" if required else "BLOCKED"), f"endpoint {name}", detail)
        if healthy and unit_states.get(name) not in (None, "active"):
            audit.log("FAIL", f"orphan runtime {name}", "endpoint is healthy but its systemd unit is not active")
        runtime_commit = body.get("git_commit") if isinstance(body, dict) else None
        expected_commit = expected_commits[name]
        if runtime_commit and expected_commit and runtime_commit != expected_commit:
            audit.log("FAIL", f"runtime commit {name}", f"runtime={runtime_commit} deployed={expected_commit}")
        elif runtime_commit and expected_commit:
            audit.log("PASS", f"runtime commit {name}", f"runtime={runtime_commit} deployed={expected_commit}")
        elif required:
            audit.log("BLOCKED", f"runtime commit {name}", "health response or deployment marker has no build identity")
        if runtime_commit and checkout_commit and runtime_commit != checkout_commit:
            audit.log("BLOCKED", f"checkout revision {name}", "running service differs from checkout; review deployed revision before release")
        if name != "model-registry":
            api_key = os.environ.get(args.api_key_env) or None
            ready_status, ready_ms, ready_body, ready_error = audit.http(f"{base}/ready", api_key=api_key)
            ready = readiness_ok(ready_status, ready_body)
            level = "PASS" if ready else ("FAIL" if required else "BLOCKED")
            if ready_status in (401, 403) and not api_key:
                level = "BLOCKED"
            detail = f"HTTP {ready_status}, {ready_ms:.1f} ms, ready={ready}"
            if ready_status in (401, 403):
                detail += f", local bearer authentication required via --api-key-env {args.api_key_env}"
            if ready_error:
                detail += f", {ready_error}"
            audit.log(level, f"readiness {name}", detail)

    if args.run_tests:
        rc, out = audit.command([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], timeout=1800, isolated=True)
        audit.log("PASS" if rc == 0 else "FAIL", "repository tests", out)
        rc, out = audit.command([sys.executable, "tests/run_transparency_check.py"], timeout=300, isolated=True)
        audit.log("PASS" if rc == 0 else "FAIL", "cross-wrapper transparency", out)

    if args.run_smoke or args.run_load:
        api_key = os.environ.get(args.api_key_env, "")
        if not args.model:
            audit.log("BLOCKED", "explicit model test", "--model is required; no default model will be selected")
        elif not api_key:
            audit.log("BLOCKED", "explicit model test", "--api-key-env is missing or empty")
        elif not args.smoke_all and not args.wrapper_url:
            audit.log("BLOCKED", "explicit model test", "--wrapper-url is required")
        else:
            targets = ([f"http://127.0.0.1:{targets_by_service[name]}/v1"
                        for name in args.required_wrapper if name != "model-registry"]
                       if args.smoke_all else [args.wrapper_url])
            if not targets:
                audit.log("BLOCKED", "explicit model test", "no inference wrapper selected")
            for base in targets:
                if args.run_smoke:
                    for surface in args.surface or ["chat"]:
                        if surface == "chat":
                            path, payload = "/chat/completions", {"model": args.model, "messages": [{"role": "user", "content": "Reply exactly OK."}], "max_tokens": 8, "stream": False}
                        elif surface == "responses":
                            path, payload = "/responses", {"model": args.model, "input": "Reply exactly OK.", "max_output_tokens": 8}
                        else:
                            path, payload = "/messages", {"model": args.model, "max_tokens": 8, "messages": [{"role": "user", "content": "Reply exactly OK."}]}
                        status, ms, body, error = audit.http(
                            f"{base.rstrip('/')}{path}", method="POST", payload=payload,
                            api_key=api_key, timeout=180)
                        returned_model = body.get("model") if isinstance(body, dict) else None
                        ok = 200 <= status < 300 and isinstance(body, dict) and returned_model in (None, args.model)
                        detail = (f"wrapper={base}, model={args.model}, surface={surface}, "
                                  f"HTTP {status}, {ms:.1f} ms, returned_model={returned_model or 'not-present'}, "
                                  f"{error_summary(body)}")
                        if error:
                            detail += f", transport_error={error}"
                        audit.log("PASS" if ok else "FAIL", f"exact-model smoke [{surface}]", detail)
                if args.run_load:
                    _run_load(audit, repo, base, args.model, api_key,
                              args.requests, args.concurrency)

    audit.lines.extend([
        "",
        "## Summary",
        f"- PASS: `{audit.passes}`",
        f"- FAIL: `{audit.failures}`",
        f"- BLOCKED: `{audit.blocked}`",
        "",
        "## Interpretation",
        "- PENDING providers are frozen and excluded from production judgment; they are never inspected or probed.",
        "- BLOCKED means the VPS did not provide the required service/configuration or an explicit test flag was not supplied.",
        "- FAIL means an available component violated an acceptance criterion.",
        "- A production-ready decision requires zero FAIL and no unreviewed BLOCKED result.",
        "- The bounded load section reports ok/error count, latency p50/p95/p99, and TTFT p50/p95/p99.",
        "- The report intentionally does not include secrets or response bodies.",
    ])

    reports = args.report_dir or repo / "productions" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    filename = reports / f"production-audit-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    filename.write_text("\n".join(audit.lines) + "\n")
    print(filename)
    print(f"PASS={audit.passes} FAIL={audit.failures} BLOCKED={audit.blocked}")
    return 1 if audit.failures else (2 if audit.blocked else 0)


if __name__ == "__main__":
    raise SystemExit(main())
