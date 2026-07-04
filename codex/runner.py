"""
runner.py — Codex CLI subprocess executor for wrapper-codex.

Spawns `codex exec --json` with strict sandboxing, captures stdout (stream-json
events) + stderr, enforces timeout via deadline polling, and supports clean
cancellation via SIGTERM→SIGKILL on the process group.

codex-cli 0.141.0 (verified) flag surface used here:
  --json, --color, -C/--cd, -s/--sandbox, -m/--model, --skip-git-repo-check,
  --ephemeral, --dangerously-bypass-approvals-and-sandbox,
  -c/--config (for approval_policy override)

NOTE: codex exec 0.141.0 dropped the legacy `-a <policy>` flag. Approval policy
is set via `-c 'approval_policy="..."'` (TOML-typed) or the
--dangerously-bypass-approvals-and-sandbox shorthand.
"""
import asyncio
import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional


# Approval policy values accepted by codex-cli 0.141.0 (verified).
# Anything outside this set falls back to "never" (matches old wrapper default).
VALID_APPROVAL_POLICIES = {"untrusted", "on-failure", "on-request", "never"}


@dataclass
class CodexRunSpec:
    run_id: str
    prompt: str
    cwd: str
    cli_bin: str
    model: str = ""
    sandbox: str = "workspace-write"
    approval_policy: str = "never"
    timeout_seconds: int = 1800
    skip_git_repo_check: bool = True
    ephemeral: bool = False


def validate_cwd(cwd: str, allowed_roots: list[str]) -> str:
    resolved = str(Path(cwd).expanduser().resolve())
    roots = [str(Path(r).expanduser().resolve()) for r in allowed_roots if r]
    if not roots:
        raise ValueError("ALLOWED_ROOTS is empty")
    if not any(resolved == r or resolved.startswith(r + os.sep) for r in roots):
        raise ValueError(f"cwd is outside allowed roots: {resolved}")
    if not Path(resolved).is_dir():
        raise ValueError(f"cwd does not exist or is not a directory: {resolved}")
    return resolved


def build_command(spec: CodexRunSpec) -> list[str]:
    """
    Build the `codex exec --json` argv.

    Approval policy handling (codex-cli 0.141.0):
      - "never" → use --dangerously-bypass-approvals-and-sandbox (no prompts ever)
        AND override approval_policy config for consistency.
      - other valid policies → override approval_policy via -c (TOML).

    We ALWAYS pass `--dangerously-bypass-approvals-and-sandbox` when
    approval_policy == "never" (the wrapper default), so the CLI never blocks
    on stdin for an interactive approval prompt. Otherwise we pass `-c
    'approval_policy="..."'` to let codex prompt as configured.
    """
    cmd: list[str] = [
        spec.cli_bin,
        "exec",
        "--json",
        "--color",
        "never",
        "-C",
        spec.cwd,
        "-s",
        spec.sandbox,
    ]
    if spec.model:
        cmd.extend(["-m", spec.model])
    if spec.skip_git_repo_check:
        cmd.append("--skip-git-repo-check")
    if spec.ephemeral:
        cmd.append("--ephemeral")

    # Approval policy: normalize to a known value, then emit the right flag combo.
    policy = (spec.approval_policy or "never").strip().lower()
    if policy not in VALID_APPROVAL_POLICIES:
        policy = "never"

    if policy == "never":
        # Skip all approval prompts — wrapper is non-interactive by design.
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
        # Also pin the config key so downstream logs agree.
        cmd.extend(["-c", 'approval_policy="never"'])
    else:
        cmd.extend(["-c", f'approval_policy="{policy}"'])

    # Prompt via stdin ("-") so we don't have to argv-escape.
    cmd.append("-")
    return cmd


# ── Stream-json event extraction ──────────────────────────────────────────
#
# Codex --json emits one JSON object per line. Event shapes we care about:
#   {"type":"thread.started",        "thread_id":"..."}
#   {"type":"turn.started"}
#   {"type":"item.started",          "item":{"type":"agent_message", ...}}
#   {"type":"item.updated",          "item":{"type":"agent_message", "text":"..."}}
#   {"type":"item.completed",        "item":{"type":"agent_message","text":"..."}}
#   {"type":"item.started",          "item":{"type":"reasoning","text":"..."}}
#   {"type":"item.completed",        "item":{"type":"command_execution",...}}
#   {"type":"item.completed",        "item":{"type":"file_change",...}}
#   {"type":"turn.completed",        "usage":{...}}
#   {"type":"thread.completed"}      or {"type":"error","message":"..."}
#
# `extract_text` returns the user-visible assistant text for a line, or "" if
# the line is a non-text event (thread start, command_execution, etc.).

def extract_text(line: str) -> str:
    try:
        data = json.loads(line)
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""

    item = data.get("item")
    if isinstance(item, dict):
        itype = item.get("type")
        # Agent message → text content
        if itype in ("agent_message", "assistant_message"):
            txt = item.get("text")
            if isinstance(txt, str):
                return txt
        # Reasoning → also useful as visible text (matches old behavior)
        if itype == "reasoning":
            txt = item.get("text")
            if isinstance(txt, str):
                return txt

    # OpenAI-compatible fallback: top-level "delta"/"text"/"content"/"message"
    for key in ("delta", "text", "content"):
        v = data.get(key)
        if isinstance(v, str) and v:
            return v
    msg = data.get("message")
    if isinstance(msg, dict):
        c = msg.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "".join(
                b.get("text", "") for b in c
                if isinstance(b, dict) and b.get("type") in ("text", None)
            )
    return ""


def extract_event_summary(line: str) -> str:
    """Return a short label (event type) for metrics, or '' on parse error."""
    try:
        data = json.loads(line)
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    t = data.get("type")
    if isinstance(t, str):
        return t
    item = data.get("item")
    if isinstance(item, dict):
        it = item.get("type")
        if isinstance(it, str):
            return f"item.{it}"
    return ""


async def kill_process(proc: asyncio.subprocess.Process):
    """SIGTERM the whole process group, escalate to SIGKILL after 5s."""
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
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


async def run_codex(spec: CodexRunSpec) -> AsyncIterator[dict]:
    """
    Spawn `codex exec --json`, stream events as dicts.

    Yielded event dicts always have an "event" key. Recognized values:
      - "process_started"   → {"event","pid","cmd"}
      - "output"            → {"event","stream","text"}   (raw stdout line)
      - "run_finished"      → {"event","status","exit_code","error","final_text","stderr"}

    The function never raises on subprocess errors; it converts them into a
    run_finished event with status="failed". asyncio.CancelledError propagates
    AFTER the subprocess is killed (so the parent can rely on the proc being
    gone on return).
    """
    cmd = build_command(spec)
    started = time.time()
    output_parts: list[str] = []
    stderr_parts: list[str] = []
    proc: Optional[asyncio.subprocess.Process] = None

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=spec.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid,  # own process group → SIGKILL the whole tree
        )
        yield {"event": "process_started", "pid": proc.pid, "cmd": cmd}

        assert proc.stdin is not None
        try:
            proc.stdin.write(spec.prompt.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        try:
            proc.stdin.close()
        except Exception:
            pass

        queue: asyncio.Queue[dict] = asyncio.Queue()

        async def read_stream(name: str, stream):
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
                await queue.put({"event": "output", "stream": name, "text": text})

        tasks = [
            asyncio.create_task(read_stream("stdout", proc.stdout)),
            asyncio.create_task(read_stream("stderr", proc.stderr)),
        ]
        wait_task = asyncio.create_task(proc.wait())
        deadline = started + spec.timeout_seconds

        while True:
            timeout = max(0.1, min(0.5, deadline - time.time()))
            if time.time() >= deadline:
                await kill_process(proc)
                yield {
                    "event": "run_finished",
                    "status": "failed",
                    "exit_code": proc.returncode,
                    "error": "timeout",
                    "final_text": "".join(output_parts).strip(),
                    "stderr": "\n".join(stderr_parts)[-8000:],
                }
                return
            try:
                item = await asyncio.wait_for(queue.get(), timeout=timeout)
                yield item
            except asyncio.TimeoutError:
                pass
            if wait_task.done() and queue.empty() and all(t.done() for t in tasks):
                break

        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            exit_code = await wait_task
        except Exception:
            exit_code = proc.returncode
        status = "completed" if exit_code == 0 else "failed"
        yield {
            "event": "run_finished",
            "status": status,
            "exit_code": exit_code,
            "error": "" if status == "completed" else "\n".join(stderr_parts)[-2000:],
            "final_text": "".join(output_parts).strip(),
            "stderr": "\n".join(stderr_parts)[-8000:],
        }
    except asyncio.CancelledError:
        if proc is not None:
            await kill_process(proc)
        # Drain whatever was already captured so callers see partial text
        raise
    except Exception as e:
        if proc is not None:
            await kill_process(proc)
        yield {
            "event": "run_finished",
            "status": "failed",
            "exit_code": proc.returncode if proc else None,
            "error": str(e),
            "final_text": "".join(output_parts).strip(),
            "stderr": "\n".join(stderr_parts)[-8000:],
        }
