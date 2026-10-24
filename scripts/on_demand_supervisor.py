"""Optional in-process idle supervisor; disabled unless private config opts in.

Never grants arbitrary Docker commands to an MCP client. The crawler requires
a separately installed fixed egress-probe user unit whose ExecStart invokes
a narrowly allowed root-owned checker.
"""
from __future__ import annotations

from contextlib import contextmanager
from http.client import HTTPException
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Iterator

from crawler_security import verify_health, local_container_warning
from health_cli import get_json, searxng_ok
from scripts import on_demand_docker as docker
from scripts import on_demand_settings as settings
from scripts.backend_idle_activity import IdleActivity

PROBE_UNIT = "local-search-mcp-guard-probe.service"
# A short verification lease coalesces wake storms without permanently trusting
# a firewall or Docker state that may change while the bridge stays running.
VERIFY_SECONDS = 10
_supervisor = None
_start_lock = threading.Lock()


class BackendUnavailable(RuntimeError):
    """No result can be reported when waking a backend fails."""


def _healthy(service: str) -> bool:
    if service == "searxng":
        return searxng_ok()
    try:
        # Recheck unsafe settings after a cold start, not only at bridge import.
        if local_container_warning():
            return False
        return verify_health(get_json(11235, "/health")) is None
    except (OSError, ValueError, TypeError, HTTPException):
        return False


def _probe_crawler() -> bool:
    """Request the fixed privileged probe through a distinct user systemd unit.

    Unlike running sudo from the bridge, this separate oneshot process does
    not inherit the bridge's NoNewPrivileges=true restriction.
    """
    try:
        p = subprocess.run(
            ["systemctl", "--user", "start", PROBE_UNIT],
            capture_output=True, text=True, timeout=72, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


class Supervisor:
    """Serialized per-backend start/stop and active MCP work leases."""

    def __init__(self, idle_minutes: int):
        self.tracker = IdleActivity(idle_seconds=idle_minutes * 60)
        self._locks = {name: threading.Lock() for name in ("crawl4ai", "searxng")}
        self._state = {"crawl4ai": "sleeping", "searxng": "sleeping"}
        self._verified_at = {"crawl4ai": 0.0, "searxng": 0.0}
        self._stop_event = threading.Event()
        # Do not start Docker here, only arm verified already-running backends.
        for name in self._locks:
            try:
                target = docker._safe_target(name)
                if target.running:
                    self.tracker.arm_running(name)
                    self._state[name] = "running"
            except docker.LifecycleError:
                self._state[name] = "failed"
        self._worker = threading.Thread(
            target=self._idle_loop, name="local-search-on-demand-idle", daemon=True)
        self._worker.start()

    def _enabled(self) -> bool:
        try:
            return settings.read()["enabled"]
        except (OSError, ValueError):
            return False

    @contextmanager
    def lease(self, service: str) -> Iterator[None]:
        lock = self._locks[service]
        with lock:
            if not self._enabled():
                # Caller will re-check settings below and bypass in disabled
                # mode rather than trying to start any Docker containers.
                raise BackendUnavailable("On-demand mode was disabled; retry the request")
            try:
                self.tracker.begin_work(service)
            except Exception as exc:
                raise BackendUnavailable("Backend busy; retry the request") from exc
            try:
                current = self.tracker.snapshot(service)
                needs_verification = (
                    self._state[service] != "running" or not current["armed"]
                    or time.monotonic() - self._verified_at[service] >= VERIFY_SECONDS)
                if needs_verification:
                    self._state[service] = "waking"
                    docker.start(service, health=_healthy,
                                 crawler_probe=_probe_crawler if service == "crawl4ai" else None)
                    self.tracker.arm_running(service)
                    self._verified_at[service] = time.monotonic()
                self._state[service] = "running"
            except (docker.LifecycleError, OSError, ValueError) as exc:
                self.tracker.finish_work(service)
                self._state[service] = "failed"
                raise BackendUnavailable(
                    "Backend could not be verified or started; check local Docker and egress guard") from exc
        try:
            yield
        finally:
            self.tracker.finish_work(service)

    def _idle_loop(self) -> None:
        while not self._stop_event.wait(10.0):
            self.idle_once()

    def idle_once(self) -> None:
        """One testable idle sweep; no timer or Docker work while disabled."""
        for service, lock in self._locks.items():
            if not lock.acquire(blocking=False):
                continue
            try:
                if not self._enabled():
                    try:
                        self.tracker.disarm(service)
                    except Exception:
                        pass
                    continue
                if not self.tracker.claim_idle_stop(service):
                    continue
                self._state[service] = "stopping"
                stopped = False
                try:
                    # A full stop or settings edit can disable the mode
                    # after claim but before Docker mutation.
                    if self._enabled():
                        docker.stop(service)
                        stopped = True
                    else:
                        self._state[service] = "running"
                except (docker.LifecycleError, OSError):
                    self._state[service] = "failed"
                else:
                    if stopped:
                        self._state[service] = "sleeping"
                        self._verified_at[service] = 0.0
                finally:
                    self.tracker.finish_stop(service, stopped=stopped)
            finally:
                lock.release()

    def snapshot(self) -> dict:
        output = {}
        for name in self._locks:
            info = self.tracker.snapshot(name)
            elapsed = info["idle_seconds"]
            output[name] = {
                "state": self._state[name],
                "armed": info["armed"],
                "active_requests": info["active_requests"],
                "last_activity_ago_seconds": elapsed,
                "next_idle_stop_seconds": (
                    max(0.0, self.tracker.idle_seconds - elapsed)
                    if elapsed is not None and info["armed"] else None),
                "stop_claimed": info["stop_claimed"],
            }
        return output

    def shutdown(self) -> None:
        self._stop_event.set()
        # Shutdown does not stop Docker: full stop is a separate action.


def _manager() -> Supervisor | None:
    global _supervisor
    try:
        config = settings.read()
    except (OSError, ValueError) as exc:
        raise BackendUnavailable("On-demand configuration is unsafe or unavailable") from exc
    if not config["enabled"]:
        return None
    with _start_lock:
        if _supervisor is None:
            _supervisor = Supervisor(config["idle_minutes"])
        elif _supervisor.tracker.idle_seconds != config["idle_minutes"] * 60:
            raise BackendUnavailable("On-demand timeout changed; restart the bridge to apply it")
        return _supervisor


@contextmanager
def lease(service: str) -> Iterator[None]:
    """Cache misses call this; cache hits and dashboard polls never wake Docker."""
    manager = _manager()
    if manager is None:
        yield
        return
    with manager.lease(service):
        yield


def local_status() -> dict:
    """Read-only in-process state. Never initializes the supervisor."""
    try:
        desired = settings.read()
    except (OSError, ValueError):
        return {"mode": "unavailable", "enabled": False}
    manager = _supervisor
    return {
        "mode": "on-demand" if desired["enabled"] else "disabled",
        "enabled": desired["enabled"],
        "idle_minutes": desired["idle_minutes"],
        "backends": manager.snapshot() if manager is not None else None,
    }
