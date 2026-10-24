"""Scoped Docker start/stop operations for a future opt-in supervisor.

Never import this module as a reason to start containers. Every operation
re-checks a complete project inventory, service identity and private Compose
files. A crawler wake requires a caller-provided *live* security probe.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import time
from typing import Callable

from scripts import backend_inventory as inv

_ALLOWED = frozenset({"crawl4ai", "searxng"})
_ALLOWED_POLICIES = frozenset({"no", "unless-stopped"})
_TIMEOUT = 30


class LifecycleError(RuntimeError):
    """Fail closed. Never embed Docker's potentially sensitive stderr."""


@dataclass(frozen=True)
class Target:
    service: str
    ident: str
    running: bool
    policy: str


def _safe_target(service: str, *, config_base: Path | None = None) -> Target:
    """Select exactly one project-owned full ID; never use Docker names as targets."""
    if service not in _ALLOWED:
        raise LifecycleError("Unrecognized managed backend")
    snapshot = inv.report(config_base=config_base)
    if (snapshot.get("state") not in ("running", "partially_running", "stopped")
            or snapshot.get("other_published_crawlers") != 0
            or snapshot.get("unverified_project_containers") != 0
            or snapshot.get("verified_services") != {"crawl4ai": 1, "searxng": 1}
            or snapshot.get("managed_running", -1) +
               snapshot.get("managed_stopped", -1) != 2):
        raise LifecycleError("Dedicated project ownership could not be verified")
    ids = inv._ids("--filter", "label=com.docker.compose.project=" + inv.PROJECT)
    if ids is None or len(ids) != 2:
        raise LifecycleError("Managed container list changed during verification")
    if config_base is None:
        config_base = Path(os.environ.get("XDG_CONFIG_HOME") or
                           str(Path.home() / ".config")).expanduser()
    private = inv._private_files(config_base)
    if private is None:
        raise LifecycleError("Private Compose files are unavailable")
    expected_files, overlay = private
    crawler = next((p for p in expected_files if p.endswith("/compose.yaml")), None)
    searx = next((p for p in expected_files if p.endswith("/searxng.compose.yaml")), None)
    if crawler is None or not overlay or searx is None:
        raise LifecycleError("Both private service definitions are required")
    found: dict[str, Target] = {}
    for ident in ids:
        details = inv._inspect(ident)
        if details is None:
            raise LifecycleError("Managed container changed during verification")
        labels, running, policy = details
        kind = labels.get("com.docker.compose.service")
        files = labels.get("com.docker.compose.project.config_files")
        if (labels.get("com.docker.compose.project") != inv.PROJECT
                or kind not in _ALLOWED or kind in found
                or type(files) is not str):
            raise LifecycleError("Managed service identity is ambiguous")
        parts = files.split(",")
        names = set(parts)
        if (len(parts) != len(names) or crawler not in names
                or not names.issubset(expected_files)
                or kind == "searxng" and searx not in names):
            raise LifecycleError("Managed service has untrusted Compose sources")
        if policy not in _ALLOWED_POLICIES:
            # Always/on-failure policies need an explicit separate shutdown
            # and recovery plan. Never change them silently during idle sleep.
            raise LifecycleError("Restart policy is unsafe for transient sleep")
        found[kind] = Target(kind, ident, running == "true", policy)
    if set(found) != _ALLOWED:
        raise LifecycleError("Expected exactly one of each managed service")
    return found[service]


def _run(service: str, ident: str, *, stopping: bool) -> None:
    """Only fixed Docker verbs, pinned local socket and verified full IDs."""
    if service not in _ALLOWED or len(ident) != 64 or any(
            c not in "0123456789abcdef" for c in ident):
        raise LifecycleError("Unverified Docker target")
    env = os.environ.copy()
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
                "COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES"):
        env.pop(key, None)
    args = [*inv.DOCKER, "stop", "--time", "10", ident] if stopping else [
        *inv.DOCKER, "start", ident]
    try:
        output = subprocess.run(args, timeout=_TIMEOUT, check=False,
                                capture_output=True, text=True, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LifecycleError("Managed Docker action unavailable or timed out") from exc
    if output.returncode:
        raise LifecycleError("Managed Docker action failed; retry after checking local Docker")


def start(service: str, *, config_base: Path | None = None,
          health: Callable[[str], bool],
          crawler_probe: Callable[[], bool] | None = None,
          timeout: float = 45.0,
          clock: Callable[[], float] = time.monotonic,
          sleep: Callable[[float], None] = time.sleep) -> bool:
    """Wake only one verified backend. Probe crawler before allowing work.

    Returns True if newly started. Health/probe failures never report success;
    a newly started crawler is stopped if its security probe fails.
    """
    if not 1 <= timeout <= 120:
        raise LifecycleError("Invalid readiness timeout")
    target = _safe_target(service, config_base=config_base)
    if service == "crawl4ai" and crawler_probe is None:
        raise LifecycleError("Crawler security probe not configured")
    started = not target.running
    if started:
        # Recheck the exact ID immediately before mutation; Docker cannot
        # reuse a full ID if the container is removed between checks.
        again = _safe_target(service, config_base=config_base)
        if again.ident != target.ident or again.running:
            raise LifecycleError("Managed target changed before startup")
        _run(service, target.ident, stopping=False)
    deadline = clock() + timeout
    try:
        while True:
            fresh = _safe_target(service, config_base=config_base)
            if fresh.ident != target.ident:
                raise LifecycleError("Managed target changed during wake")
            if fresh.running and health(service):
                if service == "crawl4ai" and not crawler_probe():
                    raise LifecycleError("Live crawler egress probe failed")
                return started
            if clock() >= deadline:
                raise LifecycleError("Backend startup timed out; retry after inspecting service")
            sleep(0.5)
    except Exception:
        # If the runtime itself started the crawler but cannot prove a live
        # security gate, do not leave that new crawler running unattended.
        if started and service == "crawl4ai":
            try:
                fresh = _safe_target(service, config_base=config_base)
                if fresh.ident == target.ident and fresh.running:
                    _run(service, target.ident, stopping=True)
            except (LifecycleError, OSError):
                pass
        raise


def stop(service: str, *, config_base: Path | None = None) -> bool:
    """Transiently stop a verified backend; retain its original restart policy."""
    target = _safe_target(service, config_base=config_base)
    if not target.running:
        return False
    again = _safe_target(service, config_base=config_base)
    if again.ident != target.ident or not again.running:
        raise LifecycleError("Managed target changed before idle stop")
    _run(service, target.ident, stopping=True)
    final = _safe_target(service, config_base=config_base)
    if final.ident != target.ident or final.running or final.policy != target.policy:
        raise LifecycleError("Managed target changed after idle stop")
    return True
