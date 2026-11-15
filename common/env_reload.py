"""Shared runtime .env reload service for active wrappers (final audit 2026-11-15).

Ownership and precedence
------------------------
* At boot the wrappers keep their existing ``load_dotenv`` semantics: a real
  OS-provided variable wins over the same variable in the file.
* After startup, the watched .env file is the *authoritative runtime source*
  for the variables it defines. A variable defined by the file is owned by
  the file: later edits apply with override semantics, and removing the line
  removes the variable from ``os.environ``.
* Variables that were never part of any file snapshot are never touched; they
  belong to other sources (systemd ``EnvironmentFile``, shell export, control
  API).
* A missing or valid-but-empty file is a legitimate zero-key state: every
  file-owned variable is removed and credential pools may go to zero eligible
  keys without a restart.
* An unreadable or corrupt file keeps the last-known-good snapshot; the failed
  reload is visible in :meth:`EnvReloadService.status`. Nothing partial is
  ever applied.

Event model
-----------
* With ``watchdog`` installed, the service watches the env file's directory
  (non-recursive) and reacts to ``on_modified`` (in-place write),
  ``on_created`` (re-created file), ``on_moved`` (atomic rename onto the
  watched path — the destination path is checked), and ``on_deleted``
  (treated as an empty snapshot).
* Editor event bursts are debounced (default 0.25 s) into a single reload.
* Without ``watchdog`` a bounded polling thread (default 1 s interval,
  mtime+size then content hash) keeps hot swap functional; the active mode is
  exposed in the service status so operators are never left guessing.
* Reloads always execute on the service event loop, captured once at
  :meth:`EnvReloadService.start`; watcher threads schedule work with
  ``asyncio.run_coroutine_threadsafe`` — never ``asyncio.get_event_loop()``
  from a foreign thread.

Lifecycle
---------
``start()`` after the application lifespan is running; ``stop()`` is
idempotent, stops and joins the observer/poller. ``WRAPPER_SKIP_DOTENV``
disables the service entirely (kept for test isolation; the live hot-swap
acceptance harness must NOT set it and instead points ``WRAPPER_ENV_FILE``
at an isolated temporary file).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("wrapper.env_reload")

TRUTHY = ("1", "true", "yes", "on")


def skip_dotenv_requested() -> bool:
    return os.environ.get("WRAPPER_SKIP_DOTENV", "").strip().lower() in TRUTHY


def resolve_env_path(default_path: Path | str) -> Path:
    """Deterministic env file: ``WRAPPER_ENV_FILE`` overrides the provider default."""
    override = os.environ.get("WRAPPER_ENV_FILE", "").strip()
    return Path(override).expanduser() if override else Path(default_path)


def parse_env_file(path: Path) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """Parse a full .env snapshot.

    Returns ``(snapshot, None)`` on success — including a missing file, which
    parses as an empty (valid zero-variable) snapshot — or ``(None, error)``
    when the file must not be applied (read error / binary corruption); the
    caller keeps the last-known-good state in that case.
    """
    try:
        if not path.exists():
            return {}, None
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"read error: {exc}"
    if b"\x00" in raw:
        return None, "binary/corrupt content (NUL byte)"
    snapshot: Dict[str, str] = {}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "undecodable content (non-UTF-8)"
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.lower().startswith("export "):
            stripped = stripped[7:].lstrip()
        if "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        name = name.strip()
        if not name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            comment = value.find(" #")
            if comment >= 0:
                value = value[:comment].rstrip()
        snapshot[name] = value
    return snapshot, None


def reconcile_snapshot(snapshot: Dict[str, str], previous: Dict[str, str]) -> Dict[str, object]:
    """Apply file-owned variables to ``os.environ`` and report the changes.

    ``snapshot`` is the new file content, ``previous`` the last applied file
    snapshot. Variables present in the new snapshot are set (override);
    variables that the previous snapshot owned but the new one no longer
    defines are deleted unless some other source changed their value in the
    meantime. Variables absent from both snapshots are never touched.
    """
    changed: Dict[str, str] = {}
    removed: List[str] = []
    for name, value in snapshot.items():
        if os.environ.get(name) != value:
            os.environ[name] = value
            changed[name] = value
    for name in previous:
        if name in snapshot:
            continue
        current = os.environ.get(name)
        # Delete only while the value still matches what this source last
        # applied; a foreign owner may have taken the variable over.
        if current is not None and current == previous[name]:
            del os.environ[name]
            removed.append(name)
    return {"set": changed, "removed": removed}


def scan_pool_keys(snapshot: Dict[str, str], pattern: str, *, min_length: int = 10) -> List[str]:
    """Ordered, de-duplicated credential values owned by ``pattern``.

    ``pattern`` is an anchored regex such as ``^NVIDIA_API_KEY(_\\d+)?$``.
    Order follows sorted variable names so reordering duplicate values or
    renaming indices cannot churn pool identity.
    """
    import re
    regex = re.compile(pattern)
    keys: List[str] = []
    seen = set()
    for name in sorted(snapshot):
        if not regex.match(name):
            continue
        value = (snapshot.get(name) or "").strip()
        if value and len(value) >= min_length and value not in seen:
            seen.add(value)
            keys.append(value)
    return keys


class EnvReloadService:
    """Owns the watched env file, reconciliation and provider apply hook."""

    def __init__(self, *, env_path: Path | str, apply: Callable[[Dict[str, str]], Awaitable[None]],
                 debounce: float = 0.25, poll_interval: float = 1.0):
        self.env_path = Path(env_path)
        self._apply = apply
        self._debounce = float(debounce)
        self._poll_interval = float(poll_interval)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._observer = None
        self._poll_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pending = threading.Event()
        self._reload_lock = asyncio.Lock()
        self._previous: Dict[str, str] = {}
        self._boot_recorded = False
        # status fields (mutated on the service loop or under GIL-atomic writes)
        self.generation = 0
        self.last_result = "not_run"
        self.last_error: Optional[str] = None
        self.last_event_at: Optional[float] = None
        self.last_applied_at: Optional[float] = None
        self.last_reload_ms: Optional[float] = None
        self.watcher_mode = "stopped"
        self._fingerprint = ""

    # ── boot ──────────────────────────────────────────────────────────────

    def initialize(self) -> bool:
        """Record the boot snapshot without applying it (boot semantics win).

        Synchronous on purpose: it must complete before the watcher can fire
        its first event. Returns False when the file cannot be parsed; the
        service then keeps an empty ownership baseline and stays honest in
        :meth:`status`.
        """
        snapshot, error = parse_env_file(self.env_path)
        if snapshot is None:
            self.last_result = "failed"
            self.last_error = f"boot parse: {error}"
            logger.warning("[env-reload] %s: %s", self.env_path, error)
            return False
        self._previous = snapshot
        self._fingerprint = self._hash(snapshot)
        self._boot_recorded = True
        return True

    # ── reload ────────────────────────────────────────────────────────────

    async def reload_now(self, *, trigger: str = "manual") -> bool:
        """Parse, reconcile and apply the current file content.

        Runs on the service loop (scheduled thread-safely by the watcher);
        a concurrent pair of triggers coalesce under ``_reload_lock``.
        """
        async with self._reload_lock:
            started = time.monotonic()
            snapshot, error = parse_env_file(self.env_path)
            if snapshot is None:
                self.last_result = "failed"
                self.last_error = error
                logger.warning("[env-reload] keeping last-known-good (%s): %s", trigger, error)
                return False
            try:
                changes = reconcile_snapshot(snapshot, self._previous)
                await self._apply(snapshot)
            except Exception as exc:  # provider hook failure must not corrupt state
                self.last_result = "failed"
                self.last_error = f"apply failed: {exc}"
                logger.warning("[env-reload] apply failed (%s): %s", trigger, exc)
                return False
            self._previous = snapshot
            self._fingerprint = self._hash(snapshot)
            self.generation += 1
            self.last_result = "ok"
            self.last_error = None
            self.last_applied_at = time.time()
            self.last_reload_ms = (time.monotonic() - started) * 1000.0
            logger.info("[env-reload] generation %d applied (%s): set=%d removed=%d keys_in_file=%d",
                        self.generation, trigger, len(changes["set"]), len(changes["removed"]), len(snapshot))
            return True

    # ── watcher wiring ────────────────────────────────────────────────────

    def start(self) -> bool:
        """Capture the running loop and start the best available watcher."""
        if skip_dotenv_requested():
            self.watcher_mode = "disabled"
            logger.info("[env-reload] WRAPPER_SKIP_DOTENV set; hot reload disabled")
            return False
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self.watcher_mode = "disabled"
            logger.warning("[env-reload] start() called outside a running loop; hot reload disabled")
            return False
        self._stop.clear()
        if self._start_watchdog():
            self.watcher_mode = "watchdog"
            logger.info("[env-reload] watching %s (watchdog, debounce=%.2fs)", self.env_path, self._debounce)
            return True
        self._start_polling()
        self.watcher_mode = "polling"
        logger.info("[env-reload] watching %s (polling every %.1fs; watchdog unavailable)",
                    self.env_path, self._poll_interval)
        return True

    def _start_watchdog(self) -> bool:
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            return False
        service = self

        class Handler(FileSystemEventHandler):
            def _relevant(self, path: Optional[str]) -> bool:
                try:
                    return bool(path) and Path(path).resolve() == service.env_path.resolve()
                except OSError:
                    return False

            def _schedule(self, event_kind: str) -> None:
                service.last_event_at = time.time()
                logger.debug("[env-reload] event %s on %s", event_kind, service.env_path)
                service._pending.set()

            def on_modified(self, event):
                if not event.is_directory and self._relevant(getattr(event, "src_path", None)):
                    self._schedule("modified")

            def on_created(self, event):
                if not event.is_directory and self._relevant(getattr(event, "src_path", None)):
                    self._schedule("created")

            def on_moved(self, event):
                # Atomic save (editors, installers): the destination matters.
                if not event.is_directory and self._relevant(getattr(event, "dest_path", None)):
                    self._schedule("moved")

            def on_deleted(self, event):
                if not event.is_directory and self._relevant(getattr(event, "src_path", None)):
                    self._schedule("deleted")

        try:
            observer = Observer()
            observer.schedule(Handler(), path=str(self.env_path.parent), recursive=False)
            observer.daemon = True
            observer.start()
            self._observer = observer
        except Exception as exc:  # pragma: no cover - platform specific
            logger.warning("[env-reload] watchdog start failed (%s); falling back to polling", exc)
            return False
        self._start_debounce_thread()
        return True

    def _start_polling(self) -> None:
        self._start_debounce_thread()

    def _start_debounce_thread(self) -> None:
        thread = threading.Thread(target=self._debounce_loop, name="env-reload-debounce", daemon=True)
        thread.start()
        self._poll_thread = thread

    def _debounce_loop(self) -> None:
        """Coalesce event bursts; poll fallback also drives from here."""
        watchdog_mode = self._observer is not None
        last_poll = 0.0
        while not self._stop.wait(self._debounce if watchdog_mode else 0.05):
            if watchdog_mode:
                if self._pending.is_set():
                    # Drain the burst window: editors emit several events per
                    # save; anything arriving during the sleep re-arms pending
                    # and the next iteration coalesces it too.
                    time.sleep(self._debounce)
                    if self._stop.is_set():
                        break
                    self._pending.clear()
                else:
                    continue
            else:
                now = time.monotonic()
                if now - last_poll < self._poll_interval:
                    continue
                last_poll = now
                if not self._poll_changed():
                    continue
            self._schedule_reload()

    def _poll_changed(self) -> bool:
        """Polling fallback: reload when the parsed content hash changes."""
        try:
            snapshot, _ = parse_env_file(self.env_path)
        except OSError:
            return False
        if snapshot is None:
            # Unreadable right now (mid-write): retry next tick; never treat a
            # transient read error as an empty snapshot.
            return False
        return self._hash(snapshot) != self._fingerprint

    def _schedule_reload(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.warning("[env-reload] service loop unavailable; reload dropped")
            return
        try:
            asyncio.run_coroutine_threadsafe(self.reload_now(trigger="file-event"), loop)
        except RuntimeError as exc:
            logger.warning("[env-reload] scheduling failed: %s", exc)

    def stop(self) -> None:
        """Idempotently stop and join watcher threads."""
        self._stop.set()
        self._pending.set()
        observer, self._observer = self._observer, None
        if observer is not None:
            try:
                observer.stop()
                observer.join(timeout=2.0)
            except Exception:  # pragma: no cover
                pass
        thread, self._poll_thread = self._poll_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self.watcher_mode = "stopped" if not skip_dotenv_requested() else "disabled"

    # ── introspection ─────────────────────────────────────────────────────

    def status(self) -> Dict[str, object]:
        return {
            "source": str(self.env_path),
            "watcher": self.watcher_mode,
            "generation": self.generation,
            "last_result": self.last_result,
            "last_error": self.last_error,
            "last_event_at": self.last_event_at,
            "last_applied_at": self.last_applied_at,
            "last_reload_ms": self.last_reload_ms,
            "boot_snapshot_recorded": self._boot_recorded,
            "owned_variables": len(self._previous),
        }

    @staticmethod
    def _hash(snapshot: Dict[str, str]) -> str:
        return hashlib.sha256(json_dumps(snapshot).encode("utf-8")).hexdigest()


def json_dumps(value: object) -> str:
    import json
    return json.dumps(value, sort_keys=True, ensure_ascii=True)
