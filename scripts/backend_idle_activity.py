"""Inert, thread-safe idle activity gate for future on-demand Docker management.

No Docker, systemd, firewall, background thread or timer is started. A caller
must explicitly arm a verified running backend and coordinate stop claims with
a future lifecycle controller. Only real MCP work may acquire work leases.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
import time
from typing import Callable, Iterator

BACKENDS = frozenset({"crawl4ai", "searxng"})
DEFAULT_IDLE_SECONDS = 30 * 60


class BackendBusy(RuntimeError):
    """A stop claim or active request prevents the requested transition."""


@dataclass
class _Activity:
    active: int = 0
    armed: bool = False
    idle_since: float | None = None
    stopping: bool = False


class IdleActivity:
    """Atomic activity leases and idle-stop claims, with no lifecycle effects."""

    def __init__(self, idle_seconds: int = DEFAULT_IDLE_SECONDS,
                 *, clock: Callable[[], float] = time.monotonic):
        if type(idle_seconds) is not int or not 1 <= idle_seconds <= 86400:
            raise ValueError("Idle threshold must be 1-86400 whole seconds")
        self.idle_seconds = idle_seconds
        self._clock = clock
        self._lock = Lock()
        self._backends = {name: _Activity() for name in BACKENDS}

    def _item(self, backend: str) -> _Activity:
        if backend not in BACKENDS:
            raise ValueError("Unknown project backend")
        return self._backends[backend]

    def arm_running(self, backend: str) -> None:
        """Start countdown only after verifying the backend is actually running."""
        with self._lock:
            item = self._item(backend)
            if item.stopping:
                raise BackendBusy("Idle stop is already claimed")
            # Revalidating an already-running backend must not restart its
            # quiet window; only a transition from disarmed starts the clock.
            if not item.armed:
                item.armed = True
                item.idle_since = self._clock() if item.active == 0 else None

    def begin_work(self, backend: str) -> None:
        """Explicit real MCP work; reject races with already-claimed idle stop."""
        with self._lock:
            item = self._item(backend)
            if item.stopping:
                raise BackendBusy("Idle stop is already claimed")
            item.active += 1
            item.idle_since = None

    def finish_work(self, backend: str) -> None:
        with self._lock:
            item = self._item(backend)
            if item.active <= 0:
                raise ValueError("No active MCP work lease")
            item.active -= 1
            if item.active == 0 and item.armed:
                item.idle_since = self._clock()

    @contextmanager
    def work(self, backend: str) -> Iterator[None]:
        self.begin_work(backend)
        try:
            yield
        finally:
            self.finish_work(backend)

    def claim_idle_stop(self, backend: str) -> bool:
        """Atomically reserve one stop. Ownership/security rechecks still needed."""
        with self._lock:
            item = self._item(backend)
            if (not item.armed or item.stopping or item.active
                    or item.idle_since is None
                    or self._clock() - item.idle_since < self.idle_seconds):
                return False
            item.stopping = True
            return True

    def finish_stop(self, backend: str, *, stopped: bool) -> None:
        """Release a claim. A failed stop restarts the entire idle interval."""
        if type(stopped) is not bool:
            raise ValueError("Stopped must be boolean")
        with self._lock:
            item = self._item(backend)
            if not item.stopping:
                raise ValueError("No idle-stop claim")
            item.stopping = False
            item.armed = not stopped
            item.idle_since = None if stopped else self._clock()

    def disarm(self, backend: str) -> None:
        """Mark explicit full shutdown / disabled mode as ineligible for sleep."""
        with self._lock:
            item = self._item(backend)
            if item.stopping or item.active:
                raise BackendBusy("Cannot disarm while work or stop is in progress")
            item.armed = False
            item.idle_since = None

    def snapshot(self, backend: str) -> dict:
        """Read-only counters; polling never refreshes the activity timestamp."""
        with self._lock:
            item = self._item(backend)
            return {
                "armed": item.armed,
                "active_requests": item.active,
                "idle_seconds": (None if item.idle_since is None else
                                 max(0.0, self._clock() - item.idle_since)),
                "stop_claimed": item.stopping,
            }
