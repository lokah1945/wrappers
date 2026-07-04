"""
wrapper-codex — FastAPI gateway for `codex exec --json` (codex-cli 0.141.0).

Exposes a small OpenAI-style surface + native /v1/runs (stream + async) so
agents (Hermes, etc.) can drive Codex CLI as if it were a local HTTP API.

Highlights:
  - Bounded concurrency (RunPool, MAX_CONCURRENT_RUNS).
  - Async-offloaded SQLite metrics (WAL, per-thread conn).
  - Subprocess kill on /cancel: a kill_fn registered at spawn time so the HTTP
    endpoint can terminate the CLI even if the asyncio Task is blocked in
    queue.get(). SIGTERM → 5s → SIGKILL on the process group.
  - Native /v1/runs stream (SSE) + 202 async fire-and-forget.
  - OpenAI-compatible /v1/chat/completions adapter (stream + non-stream) with
    approximate token counts.
  - Discovery probes answered locally (no upstream round-trip):
    /api/tags (ollama), /api/show, /props, /v1/props, /models, /api/v1/models,
    /version, /api/version, /favicon.ico.
  - Dashboard: themed, drill-down per model, hourly/daily chart, run history,
    cancel & reset.
  - Background prune loop (hourly, DATA_RETAIN_DAYS).
"""
import asyncio
import json
import os
import signal
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

import metrics as mx
from run_pool import RunPool, RunState
from runner import CodexRunSpec, build_command, extract_text, run_codex, validate_cwd

ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

PROVIDER = os.getenv("PROVIDER_NAME", "wrapper-codex")
CLI_BIN = os.getenv("CLI_BIN", "codex")
LISTEN_HOST = os.getenv("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "9103"))
MAX_CONCURRENT_RUNS = int(os.getenv("MAX_CONCURRENT_RUNS", "2"))
DEFAULT_TIMEOUT_SECONDS = int(os.getenv("DEFAULT_TIMEOUT_SECONDS", "1800"))
DEFAULT_CWD = os.getenv("DEFAULT_CWD", "/root")
ALLOWED_ROOTS = [p.strip() for p in os.getenv("ALLOWED_ROOTS", "/root").split(",") if p.strip()]
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "")
DEFAULT_SANDBOX = os.getenv("DEFAULT_SANDBOX", "workspace-write")
DEFAULT_APPROVAL_POLICY = os.getenv("DEFAULT_APPROVAL_POLICY", "never")
CODEX_SKIP_GIT_REPO_CHECK = os.getenv("CODEX_SKIP_GIT_REPO_CHECK", "true").lower() == "true"
CODEX_EPHEMERAL = os.getenv("CODEX_EPHEMERAL", "false").lower() == "true"
DATA_RETAIN_DAYS = int(os.getenv("DATA_RETAIN_DAYS", "30"))
MAX_EVENT_BUFFER = int(os.getenv("MAX_EVENT_BUFFER", "1000"))

VERSION = "1.1.0"

pool = RunPool(PROVIDER, MAX_CONCURRENT_RUNS)
_tasks: dict[str, asyncio.Task] = {}
_results: dict[str, dict] = {}
_events: dict[str, list[dict]] = {}
_dashboard_html = ""


# ── Helpers ──────────────────────────────────────────────────────────────

def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def _model_list() -> list[str]:
    """Static model hints (Codex picks the actual model at runtime)."""
    base = [DEFAULT_MODEL] if DEFAULT_MODEL else []
    return base + ["gpt-5-codex", "gpt-5", "oss-local"]


def _request_to_spec(run_id: str, body: dict) -> CodexRunSpec:
    cwd = validate_cwd(body.get("cwd") or DEFAULT_CWD, ALLOWED_ROOTS)
    timeout = int(body.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    timeout = max(1, min(timeout, DEFAULT_TIMEOUT_SECONDS))
    return CodexRunSpec(
        run_id=run_id,
        prompt=str(body.get("prompt") or ""),
        cwd=cwd,
        cli_bin=CLI_BIN,
        model=str(body.get("model") or DEFAULT_MODEL),
        sandbox=str(body.get("sandbox") or DEFAULT_SANDBOX),
        approval_policy=str(body.get("approval_policy") or DEFAULT_APPROVAL_POLICY),
        timeout_seconds=timeout,
        skip_git_repo_check=bool(body.get("skip_git_repo_check", CODEX_SKIP_GIT_REPO_CHECK)),
        ephemeral=bool(body.get("ephemeral", CODEX_EPHEMERAL)),
    )


async def _record_event(run_id: str, item: dict):
    buf = _events.setdefault(run_id, [])
    buf.append(item)
    if len(buf) > MAX_EVENT_BUFFER:
        del buf[:-MAX_EVENT_BUFFER]
    await asyncio.to_thread(
        mx.record_event,
        run_id,
        item.get("event", ""),
        item.get("stream", ""),
        len(item.get("text", "") or ""),
    )


async def _sigterm_proc(proc: asyncio.subprocess.Process):
    """SIGTERM the process group, escalate to SIGKILL after 5s."""
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except Exception:
            return
    except Exception:
        try:
            proc.terminate()
        except Exception:
            return
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            await proc.wait()
        except Exception:
            pass


async def _run_subprocess(spec: CodexRunSpec, kill_event: asyncio.Event,
                          proc_ref: dict) -> tuple[dict, list[str], list[str]]:
    """
    Spawn codex, read stdout/stderr concurrently, watch for kill_event.
    Returns (final_dict, output_parts, stderr_parts).
    """
    cmd = build_command(spec)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=spec.cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=os.setsid,
    )
    proc_ref["proc"] = proc

    try:
        proc.stdin.write(spec.prompt.encode("utf-8"))
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    try:
        proc.stdin.close()
    except Exception:
        pass

    output_parts: list[str] = []
    stderr_parts: list[str] = []

    async def read_one(name: str, stream) -> list[dict]:
        events: list[dict] = []
        while True:
            try:
                raw = await stream.readline()
            except Exception:
                return events
            if not raw:
                return events
            text = raw.decode("utf-8", "replace").rstrip("\n")
            if name == "stdout":
                extracted = extract_text(text)
                if extracted:
                    output_parts.append(extracted)
            else:
                stderr_parts.append(text)
            events.append({"event": "output", "stream": name, "text": text})
        return events  # unreachable, satisfies linters

    stdout_task = asyncio.create_task(read_one("stdout", proc.stdout))
    stderr_task = asyncio.create_task(read_one("stderr", proc.stderr))
    wait_task = asyncio.create_task(proc.wait())
    deadline = time.time() + spec.timeout_seconds

    # Always wait for the process itself to finish (or be killed) before
    # deciding the final status. Read tasks finishing first just means the
    # pipes closed — the process may still be cleaning up.
    try:
        exit_code = await asyncio.wait_for(wait_task, timeout=max(0.1, deadline - time.time()))
    except asyncio.TimeoutError:
        await _sigterm_proc(proc)
        try:
            exit_code = await asyncio.wait_for(wait_task, timeout=10)
        except asyncio.TimeoutError:
            exit_code = -1
    except Exception:
        exit_code = proc.returncode if proc.returncode is not None else -1

    # Drain reader tasks (graceful close).
    for t in (stdout_task, stderr_task):
        if not t.done():
            try:
                await asyncio.wait_for(t, timeout=2.0)
            except asyncio.TimeoutError:
                t.cancel()

    final_text = "".join(output_parts).strip()
    stderr_joined = "\n".join(stderr_parts)[-8000:]

    if kill_event.is_set():
        status = "cancelled"
        err = "cancelled by API"
    elif exit_code is None:
        # Process didn't exit cleanly → treat as failed (likely killed/timed out).
        status = "failed"
        err = "\n".join(stderr_parts)[-2000:] or "aborted"
    elif exit_code == 0:
        status = "completed"
        err = ""
    else:
        status = "failed"
        err = "\n".join(stderr_parts)[-2000:]

    return (
        {"status": status, "exit_code": exit_code, "error": err,
         "final_text": final_text, "stderr": stderr_joined},
        output_parts, stderr_parts,
    )


async def _execute(spec: CodexRunSpec, state: RunState, request_bytes: int):
    """Async fire-and-forget execution. Records run + events."""
    t0 = time.time()
    final: dict = {"status": "failed", "exit_code": None, "error": "no result", "final_text": ""}
    kill_event = asyncio.Event()
    proc_ref: dict = {}

    async def _kill():
        kill_event.set()
        proc = proc_ref.get("proc")
        if proc is not None:
            await _sigterm_proc(proc)

    await pool.register_kill_fn(spec.run_id, _kill)

    output_parts: list[str] = []
    stderr_parts: list[str] = []

    try:
        # Emit process_started BEFORE async-spawning so consumers see the PID.
        # We can't get the PID before spawn, so use a synthetic record via a
        # spawn helper. We use _run_subprocess directly.
        final, output_parts, stderr_parts = await _run_subprocess(spec, kill_event, proc_ref)
        # Emit buffered output events (best-effort replay for /events endpoint).
        # We don't replay every line — only mark the run as finished.
        await _record_event(spec.run_id, {"event": "run_finished", **final})
    except asyncio.CancelledError:
        final = {"status": "cancelled", "exit_code": None, "error": "cancelled", "final_text": ""}
        await _record_event(spec.run_id, {"event": "run_finished", **final})
    except Exception as e:
        final = {"status": "failed", "exit_code": None, "error": str(e), "final_text": ""}
        await _record_event(spec.run_id, {"event": "run_finished", **final})
    finally:
        await pool.finish(spec.run_id, final["status"], final.get("exit_code"), final.get("error", ""))
        await pool.set_final_text(spec.run_id, len(final.get("final_text", "") or ""))
        st = pool.get(spec.run_id)
        _results[spec.run_id] = final
        await asyncio.to_thread(
            mx.record_run,
            spec.run_id,
            PROVIDER,
            spec.model,
            spec.cwd,
            final["status"],
            final.get("exit_code"),
            (time.time() - t0) * 1000,
            st.output_chars if st else 0,
            st.stderr_chars if st else 0,
            request_bytes,
            len(final.get("final_text", "") or ""),
            final.get("error", ""),
        )


# ── Lifespan ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _dashboard_html
    html_path = Path(__file__).parent / "dashboard.html"
    _dashboard_html = html_path.read_text(encoding="utf-8") if html_path.exists() \
        else f"<h1>{PROVIDER}</h1>"
    prune_task = asyncio.create_task(_prune_loop())
    try:
        yield
    finally:
        prune_task.cancel()
        # Best-effort kill any straggler subprocess.
        for run_id in list(pool._kill_fns.keys()):
            kfn = pool._kill_fns.get(run_id)
            if kfn:
                try:
                    await kfn()
                except Exception:
                    pass
        for task in list(_tasks.values()):
            task.cancel()


async def _prune_loop():
    while True:
        await asyncio.sleep(3600)
        await asyncio.to_thread(mx.prune_old_data, DATA_RETAIN_DAYS)


app = FastAPI(title="Codex CLI Wrapper", version=VERSION, lifespan=lifespan)


# ── Health & stats ───────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "provider": PROVIDER,
        "version": VERSION,
        "cli_bin": CLI_BIN,
        "port": LISTEN_PORT,
        "max_concurrent_runs": MAX_CONCURRENT_RUNS,
        **pool.summary(),
    }


@app.get("/stats")
async def stats():
    return {
        **pool.summary(),
        "allowed_roots": ALLOWED_ROOTS,
        "default_cwd": DEFAULT_CWD,
        "default_model": DEFAULT_MODEL,
        "default_sandbox": DEFAULT_SANDBOX,
        "default_approval_policy": DEFAULT_APPROVAL_POLICY,
        "default_timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
    }


# ── Dashboard ────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return _dashboard_html


# ── Metrics API ──────────────────────────────────────────────────────────

@app.get("/metrics")
async def metrics(window: str = Query("24h")):
    return {
        **await asyncio.to_thread(mx.get_summary, window),
        "live": pool.summary(),
    }


@app.get("/metrics/tokens")
async def metrics_tokens(window: str = Query("24h")):
    s = await asyncio.to_thread(mx.get_summary, window)
    return {
        "window": window,
        "output_chars": s["output_chars"],
        "stderr_chars": s["stderr_chars"],
        "final_text_chars": s["final_text_chars"],
        "request_bytes": s["request_bytes"],
        "approx_input_tokens": s["request_bytes"] // 4,
        "approx_output_tokens": s["final_text_chars"] // 4,
    }


@app.get("/metrics/models")
async def metrics_models(window: str = Query("24h")):
    return {
        "window": window,
        "models": await asyncio.to_thread(mx.get_per_model, window),
    }


@app.get("/metrics/activity")
async def metrics_activity(limit: int = Query(50), offset: int = Query(0)):
    rows = await asyncio.to_thread(mx.get_activity_log, limit, offset)
    return {"limit": limit, "offset": offset, "count": len(rows), "rows": rows}


@app.get("/metrics/recent")
async def metrics_recent(limit: int = Query(20)):
    return {"limit": limit, "rows": await asyncio.to_thread(mx.get_recent_runs, limit)}


@app.get("/metrics/chart/hourly")
async def metrics_chart_hourly(hours: int = Query(24)):
    return {"hours": hours, "data": await asyncio.to_thread(mx.get_hourly_chart, hours)}


@app.get("/metrics/chart/daily")
async def metrics_chart_daily(days: int = Query(30)):
    return {"days": days, "data": await asyncio.to_thread(mx.get_daily_chart, days)}


@app.get("/metrics/totals")
async def metrics_totals():
    return await asyncio.to_thread(mx.get_total_counts)


@app.post("/metrics/reset")
async def metrics_reset():
    removed = await asyncio.to_thread(mx.reset_all)
    return {"status": "ok", "reset": removed}


# ── Model listing (static, since Codex picks at runtime) ─────────────────

@app.get("/v1/models")
async def models():
    data = [
        {"id": m, "object": "model", "owned_by": "codex"}
        for m in _model_list()
    ]
    return {"object": "list", "data": data}


# ── Native run API ───────────────────────────────────────────────────────

@app.post("/v1/runs")
async def create_run(request: Request):
    raw = await request.body()
    try:
        body = json.loads(raw) if raw else {}
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})

    run_id = body.get("run_id") or str(uuid.uuid4())
    try:
        spec = _request_to_spec(run_id, body)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    if not spec.prompt.strip():
        return JSONResponse(status_code=400, content={"error": "prompt is required"})

    state = RunState(
        run_id=run_id, provider=PROVIDER, model=spec.model,
        cwd=spec.cwd, request_bytes=len(raw),
    )
    if not await pool.reserve(state):
        return JSONResponse(status_code=429, content={"error": "all run slots are busy", **pool.summary()})

    stream = bool(body.get("stream", False))

    if stream:
        async def gen():
            t0 = time.time()
            kill_event = asyncio.Event()
            proc_ref: dict = {}
            final: dict = {"status": "failed", "exit_code": None, "error": "no result", "final_text": ""}
            stdout_buf: list[dict] = []
            stderr_buf: list[dict] = []

            async def _kill():
                kill_event.set()
                proc = proc_ref.get("proc")
                if proc is not None:
                    await _sigterm_proc(proc)

            await pool.register_kill_fn(spec.run_id, _kill)
            yield _sse("run_started", state.as_dict())

            try:
                # Spawn codex directly.
                cmd = build_command(spec)
                proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=spec.cwd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    preexec_fn=os.setsid,
                )
                proc_ref["proc"] = proc
                await pool.attach_pid(spec.run_id, proc.pid)
                await _record_event(spec.run_id, {"event": "process_started", "pid": proc.pid, "cmd": cmd})
                yield _sse("process_started", {"event": "process_started", "pid": proc.pid, "cmd": cmd})

                try:
                    proc.stdin.write(spec.prompt.encode("utf-8"))
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                try:
                    proc.stdin.close()
                except Exception:
                    pass

                output_parts: list[str] = []
                stderr_parts: list[str] = []

                async def read_one(name: str, stream, buf: list[dict]):
                    while True:
                        try:
                            raw = await stream.readline()
                        except Exception:
                            return
                        if not raw:
                            return
                        text = raw.decode("utf-8", "replace").rstrip("\n")
                        if name == "stdout":
                            extracted = extract_text(text)
                            if extracted:
                                output_parts.append(extracted)
                        else:
                            stderr_parts.append(text)
                        ev = {"event": "output", "stream": name, "text": text}
                        buf.append(ev)
                        await pool.add_output(spec.run_id, name, len(text))
                        await _record_event(spec.run_id, ev)
                        yield ev

                async def collect(name: str, stream, buf: list[dict]):
                    async for ev in read_one(name, stream, buf):
                        pass

                stdout_task = asyncio.create_task(collect("stdout", proc.stdout, stdout_buf))
                stderr_task = asyncio.create_task(collect("stderr", proc.stderr, stderr_buf))
                wait_task = asyncio.create_task(proc.wait())
                deadline = time.time() + spec.timeout_seconds

                # Loop: yield buffered events while waiting for proc exit.
                last_yield_count = 0
                while True:
                    if kill_event.is_set():
                        await _sigterm_proc(proc)
                        break
                    if wait_task.done():
                        break
                    if time.time() >= deadline:
                        await _sigterm_proc(proc)
                        final = {"status": "failed", "exit_code": None, "error": "timeout",
                                  "final_text": "".join(output_parts).strip(),
                                  "stderr": "\n".join(stderr_parts)[-8000:]}
                        break
                    # Yield any newly buffered events.
                    all_buf = stdout_buf + stderr_buf
                    while last_yield_count < len(all_buf):
                        ev = all_buf[last_yield_count]
                        last_yield_count += 1
                        yield _sse(ev.get("event", "event"), ev)
                    await asyncio.sleep(0.05)

                # Wait for proc to fully exit + drain remaining events.
                try:
                    exit_code = await asyncio.wait_for(wait_task, timeout=10)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except Exception:
                        pass
                    exit_code = -1

                # Drain reader tasks (graceful close).
                for t in (stdout_task, stderr_task):
                    if not t.done():
                        try:
                            await asyncio.wait_for(t, timeout=2.0)
                        except asyncio.TimeoutError:
                            t.cancel()
                # Yield any remaining buffered events.
                all_buf = stdout_buf + stderr_buf
                while last_yield_count < len(all_buf):
                    ev = all_buf[last_yield_count]
                    last_yield_count += 1
                    yield _sse(ev.get("event", "event"), ev)

                # Build final if not already set (timeout branch).
                if not final.get("status") or final.get("status") == "failed" and final.get("error") == "no result":
                    status = ("cancelled" if kill_event.is_set()
                              else "completed" if exit_code == 0 else "failed")
                    final = {
                        "status": status,
                        "exit_code": exit_code,
                        "error": "" if status == "completed" else (
                            "cancelled by API" if status == "cancelled"
                            else "\n".join(stderr_parts)[-2000:]),
                        "final_text": "".join(output_parts).strip(),
                        "stderr": "\n".join(stderr_parts)[-8000:],
                    }

                await _record_event(spec.run_id, {"event": "run_finished", **final})
                yield _sse("run_finished", final)

            except asyncio.CancelledError:
                final = {"status": "cancelled", "exit_code": None, "error": "cancelled", "final_text": ""}
                await _record_event(spec.run_id, {"event": "run_finished", **final})
                yield _sse("run_finished", final)
            finally:
                await pool.finish(spec.run_id, final["status"], final.get("exit_code"), final.get("error", ""))
                await pool.set_final_text(spec.run_id, len(final.get("final_text", "") or ""))
                st = pool.get(spec.run_id)
                _results[spec.run_id] = final
                await asyncio.to_thread(
                    mx.record_run, spec.run_id, PROVIDER, spec.model, spec.cwd,
                    final["status"], final.get("exit_code"),
                    (time.time() - t0) * 1000,
                    st.output_chars if st else 0, st.stderr_chars if st else 0,
                    len(raw), len(final.get("final_text", "") or ""),
                    final.get("error", ""),
                )

        return StreamingResponse(gen(), media_type="text/event-stream")

    # Async (non-streaming): 202 Accepted.
    task = asyncio.create_task(_execute(spec, state, len(raw)))
    _tasks[run_id] = task
    task.add_done_callback(lambda _: _tasks.pop(run_id, None))
    return JSONResponse(status_code=202, content={"run_id": run_id, "status": "running"})


@app.get("/v1/runs/{run_id}")
async def get_run(run_id: str):
    st = pool.get(run_id)
    if not st:
        return JSONResponse(status_code=404, content={"error": "run not found"})
    return {**st.as_dict(), "result": _results.get(run_id)}


@app.get("/v1/runs/{run_id}/events")
async def get_run_events(run_id: str, limit: int = Query(200)):
    if run_id not in _events and not pool.get(run_id):
        return JSONResponse(status_code=404, content={"error": "run not found"})
    events = _events.get(run_id, [])
    return {"run_id": run_id, "count": len(events),
            "events": events[-limit:] if limit else events}


@app.post("/v1/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    """
    Cancel a running task:
      1. asyncio.Task.cancel() (CancelledError propagates in the worker).
      2. Invoke the registered kill_fn → SIGTERM the subprocess group →
         SIGKILL after 5s. Guarantees the CLI dies even if the task is
         blocked in queue.get().
    """
    task = _tasks.get(run_id)
    killed = False
    if task is not None and not task.done():
        task.cancel()
    killed = await pool.cancel(run_id)
    if task is None and not killed:
        return JSONResponse(status_code=404, content={"error": "active run not found"})
    return {"run_id": run_id, "status": "cancelling", "kill_invoked": killed}


# ── OpenAI-compatible adapter ────────────────────────────────────────────
#
# Maps OpenAI Chat Completions → Codex CLI run. Streams the final_text as
# the assistant message. Token counts are approximated (~4 chars/token)
# because Codex CLI doesn't surface usage in non-interactive mode reliably.

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    raw = await request.body()
    try:
        body = json.loads(raw) if raw else {}
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})

    messages = body.get("messages") or []
    if not messages:
        return JSONResponse(status_code=400, content={"error": "messages is required"})
    prompt = "\n".join(f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages)

    run_body = {
        "prompt": prompt,
        "cwd": body.get("cwd") or DEFAULT_CWD,
        "model": body.get("model") or DEFAULT_MODEL,
        "timeout_seconds": body.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS,
        "approval_policy": body.get("approval_policy") or DEFAULT_APPROVAL_POLICY,
    }
    run_id = str(uuid.uuid4())
    try:
        spec = _request_to_spec(run_id, run_body)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    state = RunState(
        run_id=run_id, provider=PROVIDER, model=spec.model, cwd=spec.cwd,
        request_bytes=len(raw),
    )
    if not await pool.reserve(state):
        return JSONResponse(status_code=429, content={"error": "all run slots are busy", **pool.summary()})

    streaming = bool(body.get("stream", False))

    if streaming:
        async def gen():
            t0 = time.time()
            kill_event = asyncio.Event()
            proc_ref: dict = {}
            final: dict = {"status": "failed", "exit_code": None, "error": "no result", "final_text": ""}

            async def _kill():
                kill_event.set()
                proc = proc_ref.get("proc")
                if proc is not None:
                    await _sigterm_proc(proc)

            await pool.register_kill_fn(spec.run_id, _kill)

            try:
                cmd = build_command(spec)
                proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=spec.cwd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    preexec_fn=os.setsid,
                )
                proc_ref["proc"] = proc
                await pool.attach_pid(spec.run_id, proc.pid)

                try:
                    proc.stdin.write(spec.prompt.encode("utf-8"))
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                try:
                    proc.stdin.close()
                except Exception:
                    pass

                # First chunk: role announcement.
                yield _sse("", {
                    "id": f"chatcmpl-{run_id}",
                    "object": "chat.completion.chunk",
                    "model": spec.model or "codex-default",
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                })

                output_parts: list[str] = []
                stderr_parts: list[str] = []
                last_text_len = 0

                async def read_stdout():
                    while True:
                        try:
                            raw = await proc.stdout.readline()
                        except Exception:
                            return
                        if not raw:
                            return
                        text = raw.decode("utf-8", "replace").rstrip("\n")
                        extracted = extract_text(text)
                        if extracted:
                            output_parts.append(extracted)

                async def read_stderr():
                    while True:
                        try:
                            raw = await proc.stderr.readline()
                        except Exception:
                            return
                        if not raw:
                            return
                        stderr_parts.append(raw.decode("utf-8", "replace").rstrip("\n"))

                stdout_task = asyncio.create_task(read_stdout())
                stderr_task = asyncio.create_task(read_stderr())
                wait_task = asyncio.create_task(proc.wait())
                deadline = time.time() + spec.timeout_seconds

                # Stream new text as deltas; race with kill_event / timeout / exit.
                while True:
                    if kill_event.is_set():
                        await _sigterm_proc(proc)
                        final = {"status": "cancelled", "exit_code": None,
                                  "error": "cancelled by API", "final_text": "".join(output_parts).strip()}
                        break
                    if time.time() >= deadline:
                        await _sigterm_proc(proc)
                        final = {"status": "failed", "exit_code": None, "error": "timeout",
                                  "final_text": "".join(output_parts).strip()}
                        break
                    if wait_task.done():
                        break
                    # Emit incremental text.
                    cur_text = "".join(output_parts).strip()
                    if len(cur_text) > last_text_len:
                        delta_text = cur_text[last_text_len:]
                        last_text_len = len(cur_text)
                        yield _sse("", {
                            "id": f"chatcmpl-{run_id}",
                            "object": "chat.completion.chunk",
                            "model": spec.model or "codex-default",
                            "choices": [{"index": 0, "delta": {"content": delta_text}, "finish_reason": None}],
                        })
                    await asyncio.sleep(0.05)

                # Final flush.
                cur_text = "".join(output_parts).strip()
                if len(cur_text) > last_text_len:
                    delta_text = cur_text[last_text_len:]
                    yield _sse("", {
                        "id": f"chatcmpl-{run_id}",
                        "object": "chat.completion.chunk",
                        "model": spec.model or "codex-default",
                        "choices": [{"index": 0, "delta": {"content": delta_text}, "finish_reason": None}],
                    })

                try:
                    exit_code = await asyncio.wait_for(wait_task, timeout=10)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except Exception:
                        pass
                    exit_code = -1

                for t in (stdout_task, stderr_task):
                    if not t.done():
                        try:
                            await asyncio.wait_for(t, timeout=2.0)
                        except asyncio.TimeoutError:
                            t.cancel()

                if not final.get("final_text"):
                    final["final_text"] = "".join(output_parts).strip()
                if final.get("status") == "failed" and final.get("error") == "no result":
                    status = ("cancelled" if kill_event.is_set()
                              else "completed" if exit_code == 0 else "failed")
                    final = {
                        "status": status, "exit_code": exit_code,
                        "error": "" if status == "completed" else (
                            "cancelled by API" if status == "cancelled"
                            else "\n".join(stderr_parts)[-2000:]),
                        "final_text": "".join(output_parts).strip(),
                    }

                yield _sse("", {
                    "id": f"chatcmpl-{run_id}",
                    "object": "chat.completion.chunk",
                    "model": spec.model or "codex-default",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                })
                yield "data: [DONE]\n\n".encode()

            except asyncio.CancelledError:
                final = {"status": "cancelled", "exit_code": None, "error": "cancelled", "final_text": ""}
            finally:
                await pool.finish(spec.run_id, final["status"], final.get("exit_code"), final.get("error", ""))
                await pool.set_final_text(spec.run_id, len(final.get("final_text", "") or ""))
                st = pool.get(spec.run_id)
                _results[spec.run_id] = final
                await asyncio.to_thread(
                    mx.record_run, spec.run_id, PROVIDER, spec.model, spec.cwd,
                    final["status"], final.get("exit_code"),
                    (time.time() - t0) * 1000,
                    st.output_chars if st else 0, st.stderr_chars if st else 0,
                    len(raw), len(final.get("final_text", "") or ""),
                    final.get("error", ""),
                )

        return StreamingResponse(gen(), media_type="text/event-stream")

    # Non-streaming
    await _execute(spec, state, len(raw))
    result = _results.get(run_id, {})
    final_text = result.get("final_text", "")
    pt = mx.estimate_tokens(prompt)
    ct = mx.estimate_tokens(final_text)
    return {
        "id": f"chatcmpl-{run_id}",
        "object": "chat.completion",
        "model": spec.model or "codex-default",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": final_text},
            "finish_reason": "stop" if result.get("status") == "completed" else "error",
        }],
        "usage": {
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
        },
        "_wrapper": {
            "provider": PROVIDER,
            "run_id": run_id,
            "status": result.get("status"),
            "exit_code": result.get("exit_code"),
            "X-Wrapper-Token-Count": "approximate",
        },
    }


# ── Discovery probes (answered locally — no upstream round-trip) ────────
#
# Many clients (Ollama, llama.cpp, Hermes, etc.) probe these endpoints to
# detect the provider. For a local-CLI wrapper there is no remote catalog to
# forward to, and a 404 would just confuse clients. Answer with a minimal
# valid payload shaped like the canonical probe.

@app.get("/version")
@app.get("/api/version")
async def _probe_version():
    return {"version": f"{PROVIDER}-{VERSION}"}


@app.get("/api/tags")
async def _probe_ollama_tags():
    models = [
        {"name": m, "model": m, "modified_at": "1970-01-01T00:00:00Z",
         "size": 0, "digest": "",
         "details": {"family": m.split("/")[0] if "/" in m else m,
                     "parameter_size": "", "quantization_level": ""}}
        for m in _model_list()
    ]
    return {"models": models}


@app.get("/api/v1/models")
@app.get("/models")
async def _probe_models_alias():
    return await models()


@app.get("/props")
@app.get("/v1/props")
async def _probe_props():
    return {"system_prompt": "", "default_generation_settings": {}, "total_slots": MAX_CONCURRENT_RUNS}


@app.api_route("/api/show", methods=["GET", "POST"])
async def _probe_show():
    return {"license": "", "modelfile": "", "parameters": "", "template": "", "details": {}}


@app.get("/favicon.ico")
async def _favicon():
    return Response(status_code=204)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT)
