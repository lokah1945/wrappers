"""
run_pool.py — Concurrency limiter for wrapper-codex.

Tracks active + historical RunState objects, reserves slots atomically, and
exposes a cancel(kill_proc) helper so the HTTP cancel endpoint can request
process termination as soon as the subprocess spawns.

State is in-process — single uvicorn worker is required (matches wrapper-nvidia).
"""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional


@dataclass
class RunState:
    run_id: str
    provider: str
    model: str
    cwd: str
    status: str = "starting"
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    error: str = ""
    output_chars: int = 0
    stderr_chars: int = 0
    request_bytes: int = 0
    final_text_chars: int = 0

    def as_dict(self) -> dict:
        now = time.time()
        return {
            "run_id": self.run_id,
            "provider": self.provider,
            "model": self.model,
            "cwd": self.cwd,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round((self.finished_at or now) - (self.started_at or self.created_at), 3),
            "pid": self.pid,
            "exit_code": self.exit_code,
            "error": self.error,
            "output_chars": self.output_chars,
            "stderr_chars": self.stderr_chars,
            "request_bytes": self.request_bytes,
            "final_text_chars": self.final_text_chars,
        }


KillFn = Callable[[], Awaitable[None]]


class RunPool:
    """
    Bounded-concurrency run pool. `max_concurrent` slots total.

    Each RunState carries an optional `kill_fn` callback set by the spawner;
    the HTTP `/v1/runs/{id}/cancel` endpoint invokes it (after canceling the
    asyncio.Task) to ensure the subprocess is killed even if the task is in
    the middle of an `await queue.get()`.
    """

    def __init__(self, provider: str, max_concurrent: int):
        self.provider = provider
        self.max_concurrent = max(1, max_concurrent)
        self._lock = asyncio.Lock()
        self._active: dict[str, RunState] = {}
        self._history: dict[str, RunState] = {}
        self._kill_fns: dict[str, KillFn] = {}

    async def reserve(self, state: RunState) -> bool:
        async with self._lock:
            if len(self._active) >= self.max_concurrent:
                return False
            state.status = "running"
            state.started_at = time.time()
            self._active[state.run_id] = state
            self._history[state.run_id] = state
            return True

    async def attach_pid(self, run_id: str, pid: int):
        async with self._lock:
            if run_id in self._active:
                self._active[run_id].pid = pid

    async def register_kill_fn(self, run_id: str, kill_fn: KillFn):
        """Register a subprocess-kill callback (called from HTTP cancel)."""
        async with self._lock:
            self._kill_fns[run_id] = kill_fn

    async def finish(self, run_id: str, status: str, exit_code: Optional[int], error: str = ""):
        async with self._lock:
            st = self._history.get(run_id)
            if st:
                st.status = status
                st.exit_code = exit_code
                st.error = error
                st.finished_at = time.time()
            self._active.pop(run_id, None)
            self._kill_fns.pop(run_id, None)

    async def add_output(self, run_id: str, stream: str, n: int):
        async with self._lock:
            st = self._history.get(run_id)
            if not st:
                return
            if stream == "stderr":
                st.stderr_chars += n
            else:
                st.output_chars += n

    async def set_final_text(self, run_id: str, n: int):
        async with self._lock:
            st = self._history.get(run_id)
            if st:
                st.final_text_chars = n

    async def cancel(self, run_id: str) -> bool:
        """Cancel a running task: invoke its kill_fn (if registered) → subprocess dies.

        Returns True if a kill_fn was found and invoked, False otherwise.
        """
        async with self._lock:
            kill_fn = self._kill_fns.get(run_id)
        if kill_fn is None:
            return False
        try:
            await kill_fn()
            return True
        except Exception:
            return False

    def get(self, run_id: str) -> Optional[RunState]:
        return self._history.get(run_id)

    def summary(self) -> dict:
        return {
            "provider": self.provider,
            "max_concurrent_runs": self.max_concurrent,
            "active_runs": len(self._active),
            "available_slots": max(0, self.max_concurrent - len(self._active)),
            "runs": [s.as_dict() for s in self._active.values()],
        }
