"""Isolated on-host pacing admission acceptance without provider network traffic.

Run: ./.venv/bin/python -m scripts.engine_pacing_acceptance
Uses the real cross-process engine_pacing.reserve primitive, but only within
a private, automatically deleted temporary XDG_CONFIG_HOME. It never reads or
writes the operator's saved pacing policy and does not contact SearXNG.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import select
import stat
import subprocess
import sys
import tempfile

from engine_pacing import PaceUnavailable, reserve

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "lsm-engine-pacing-"
MARKER = ".acceptance-token"
MAX_CHILD_SECONDS = 6
ENGINE_A = "acceptance-engine-a"
ENGINE_B = "acceptance-engine-b"


def _valid_child_sandbox() -> bool:
    """Deny a direct worker invocation outside its owner's isolated sandbox."""
    raw = os.environ.get("_LSM_PACING_ACCEPTANCE_ROOT", "")
    config = os.environ.get("XDG_CONFIG_HOME", "")
    token = os.environ.get("_LSM_PACING_ACCEPTANCE_TOKEN", "")
    if not raw or raw != config or len(token) != 32:
        return False
    path = Path(raw)
    if not path.is_absolute() or not path.name.startswith(PREFIX):
        return False
    try:
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            return False
        marker = path / MARKER
        metadata = marker.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1):
            return False
        return secrets.compare_digest(marker.read_text(encoding="ascii"), token)
    except (OSError, UnicodeError, ValueError):
        return False


def _worker(engine: str, mode: str) -> int:
    if (not _valid_child_sandbox()
            or engine not in (ENGINE_A, ENGINE_B)
            or mode not in ("hold", "once")):
        print(json.dumps({"status": "sandbox_refused"}), flush=True)
        return 2
    gap = 30 if engine == ENGINE_A else 0
    try:
        with reserve(engine, gap):
            print(json.dumps({"status": "admitted"}), flush=True)
            if mode == "hold":
                # Parent must release or kill us; held flock proves mutual
                # exclusion between independent Python processes.
                if not sys.stdin.readline():
                    return 2
    except PaceUnavailable as exc:
        code = ("busy" if "active search" in str(exc) else
                "cooldown" if exc.retry_after_seconds > 0 else "unavailable")
        print(json.dumps({"status": code,
                          "retry_after_seconds": exc.retry_after_seconds}), flush=True)
    return 0


def _command(engine: str, mode: str) -> list[str]:
    return [sys.executable, "-m", "scripts.engine_pacing_acceptance",
            "--child", engine, mode]


def _run_once(engine: str, env: dict[str, str]) -> dict:
    completed = subprocess.run(
        _command(engine, "once"), cwd=ROOT, env=env, text=True,
        capture_output=True, check=False, timeout=MAX_CHILD_SECONDS)
    if completed.returncode != 0:
        raise RuntimeError("Isolated admission worker failed")
    try:
        result = json.loads(completed.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("Invalid isolated worker result") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Invalid isolated worker response")
    return result


def _handshake(worker: subprocess.Popen) -> dict:
    readable, _, _ = select.select([worker.stdout], [], [], MAX_CHILD_SECONDS)
    if not readable:
        raise RuntimeError("Isolated admission worker did not respond")
    line = worker.stdout.readline()
    try:
        status = json.loads(line)
    except ValueError as exc:
        raise RuntimeError("Invalid isolated worker greeting") from exc
    if not isinstance(status, dict):
        raise RuntimeError("Invalid isolated worker greeting")
    return status


def report() -> dict:
    """Exercise interprocess locks and restart-safe cooldown in ephemeral state."""
    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory(prefix=PREFIX) as base:
        root = Path(base)
        token = secrets.token_hex(16)
        fd = os.open(root / MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(token)
        # Do not pass caller credentials, inherited XDG state, Docker settings
        # or alternate network endpoints to the spawned worker processes.
        env = {
            "XDG_CONFIG_HOME": base,
            "_LSM_PACING_ACCEPTANCE_ROOT": base,
            "_LSM_PACING_ACCEPTANCE_TOKEN": token,
            "PYTHONUNBUFFERED": "1",
        }
        held = subprocess.Popen(
            _command(ENGINE_A, "hold"), cwd=ROOT, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            if _handshake(held).get("status") != "admitted":
                raise RuntimeError("First isolated worker was not admitted")
            busy = _run_once(ENGINE_A, env)
            checks["cross_process_same_engine_exclusion"] = (
                busy.get("status") == "busy"
                and busy.get("retry_after_seconds") == 1)
            distinct = _run_once(ENGINE_B, env)
            checks["independent_engine_admission"] = (
                distinct.get("status") == "admitted")
            if not all(checks.values()):
                raise RuntimeError("Isolated lock admission invariants failed")
            held.stdin.write("release\n")
            held.stdin.flush()
            held.stdin.close()
            if held.wait(timeout=MAX_CHILD_SECONDS) != 0:
                raise RuntimeError("Held admission worker did not exit cleanly")
        finally:
            if held.poll() is None:
                held.kill()
                held.wait(timeout=MAX_CHILD_SECONDS)
            for stream in (held.stdin, held.stdout, held.stderr):
                if stream is not None:
                    stream.close()
        cooling = _run_once(ENGINE_A, env)
        checks["cooldown_survives_process_exit"] = (
            cooling.get("status") == "cooldown"
            and type(cooling.get("retry_after_seconds")) is int
            and 1 <= cooling["retry_after_seconds"] <= 30)
        checks["zero_gap_engine_reusable"] = (
            _run_once(ENGINE_B, env).get("status") == "admitted")
    # TemporaryDirectory cleanup is complete before success is returned.
    if not all(checks.values()):
        raise RuntimeError("Isolated pacing acceptance failed")
    return {
        "status": "passed",
        "mode": "isolated_local_processes",
        "checks": checks,
        "checks_passed": len(checks),
        "provider_searches_sent": 0,
        "services_started": 0,
        "persisted_policy_modified": False,
        "sandbox_removed": True,
        "limitations": (
            "No real provider availability, upstream quota, request relevance, "
            "full MCP transport, background supervisor or reboot was tested."
        ),
    }


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--child":
        return _worker(sys.argv[2], sys.argv[3])
    if len(sys.argv) != 1:
        print(json.dumps({"status": "invalid_arguments"}))
        return 2
    try:
        print(json.dumps(report(), sort_keys=True, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        # Failures must be nonsecret; never print private paths, inherited
        # environment, worker stderr or temporary capabilities.
        print(json.dumps({"status": "failed",
                          "reason": "Isolated local pacing acceptance failed; inspect the host runtime."}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
