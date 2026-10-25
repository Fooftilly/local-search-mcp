#!/usr/bin/env python3
"""Rehearse historical state readers inside an offline, private Linux sandbox.

No host services, checkout, packages or credentials are changed. This is NOT
permission to activate a historical release or restore a running installation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import stat
import subprocess
import sys
import tempfile

if __package__:
    from . import backup_state, rollback_candidate, rollback_release_audit
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import backup_state, rollback_candidate, rollback_release_audit

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_TOOL = Path("/usr/bin/bwrap")
_LIMIT = 2048
# This executes staged historical code only in bwrap's new user, mount, PID,
# IPC and network namespaces. Its stdout is a bounded fixed JSON summary.
WORKER = r"""
import json
import sys
sys.path.insert(0, '/app')
try:
    import domain_policy
    import history_index
    domain_policy.initialize()
    policy = domain_policy.counts()
    history_index.sync_history()
    history = history_index.search_history(limit=25)
    if not isinstance(history, list):
        raise ValueError('invalid indexed history')
    if not isinstance(policy, dict):
        raise ValueError('invalid policy counts')
    print(json.dumps({'historical_policy_reader': True,
                      'historical_history_reader': True,
                      'recent_history_count': len(history)}))
except BaseException:
    # Never disclose private history or policy values from this subprocess.
    print(json.dumps({'historical_policy_reader': False,
                      'historical_history_reader': False,
                      'recent_history_count': 0}))
    raise SystemExit(2)
"""


def _trusted_bwrap() -> Path:
    info = _TOOL.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or info.st_mode & 0o022 or not info.st_mode & 0o111):
        raise ValueError("Trusted sandbox executable not installed")
    return _TOOL


def _copy_candidate_state(candidate: Path, scratch: Path) -> int:
    """Copy allowlisted private state into disposable XDG dirs without links."""
    manifest = candidate / "candidate.json"
    fd = os.open(manifest, os.O_RDONLY | _NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 1_000_000):
            raise ValueError("Candidate manifest is unsafe")
        raw = stream.read(1_000_001)
    meta = json.loads(raw)
    inventory = meta["inventory"]
    names = [key[len("state/"):] for key, value in inventory.items()
             if key.startswith("state/") and value.startswith("F:")]
    if (not names or set(names) - set(backup_state.ALLOWED_FILES)
            or backup_state.DB_NAME not in names):
        raise ValueError("Unexpected staged state names")
    state = scratch / "state" / "local-search-mcp"
    config = scratch / "config" / "local-search-mcp"
    state.mkdir(mode=0o700, parents=True)
    config.mkdir(mode=0o700, parents=True)
    copied = 0
    state_fd = os.open(candidate / "state", os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
    try:
        for name in sorted(names):
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | _NOFOLLOW,
                         dir_fd=state_fd)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_nlink != 1):
                    raise ValueError("Staged state changed after inspection")
                target = (config if name in backup_state.SETTINGS_NAMES else state) / name
                output_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                    _NOFOLLOW, 0o600)
                digest = hashlib.sha256()
                length = 0
                with os.fdopen(output_fd, "wb") as output:
                    while data := stream.read(1_048_576):
                        length += len(data)
                        if length > backup_state.MAX_TOTAL:
                            raise ValueError("Staged state exceeds size bounds")
                        digest.update(data)
                        output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                after = os.fstat(stream.fileno())
                if (after.st_size != info.st_size or
                        after.st_mtime_ns != info.st_mtime_ns or
                        after.st_ctime_ns != info.st_ctime_ns):
                    raise ValueError("Staged state changed while copying")
                if inventory.get("state/" + name) != "F:0600:" + digest.hexdigest():
                    raise ValueError("Staged state digest changed before rehearsal")
            copied += 1
    finally:
        os.close(state_fd)
    return copied


def _sandbox_command(candidate: Path, python: Path, scratch: Path) -> list[str]:
    # No inherited HOME, config, API tokens, proxy, PATH or network. The only
    # host paths mounted are OS libraries, the explicitly selected venv and
    # the already verified historical source; scratch is disposable.
    executable = "/venv/bin/" + python.name
    return [
        str(_trusted_bwrap()), "--die-with-parent", "--new-session",
        "--unshare-all", "--clearenv",
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        "--dir", "/venv", "--ro-bind", str(python.parent.parent), "/venv",
        "--dir", "/app", "--ro-bind", str(candidate / "source"), "/app",
        "--dir", "/work", "--bind", str(scratch), "/work",
        "--tmpfs", "/tmp", "--dev", "/dev", "--proc", "/proc",
        "--setenv", "HOME", "/work",
        "--setenv", "XDG_STATE_HOME", "/work/state",
        "--setenv", "XDG_CONFIG_HOME", "/work/config",
        "--setenv", "ALLOWED_CRAWL_DOMAINS", "",
        "--setenv", "PYTHONNOUSERSITE", "1",
        "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "PATH", "/usr/bin:/bin",
        "--chdir", "/app", executable, "-I", "-B", "-c", WORKER,
    ]


def _limit_child() -> None:
    # An untrusted historical Python runtime must not flood captured logs.
    resource.setrlimit(resource.RLIMIT_FSIZE, (_LIMIT, _LIMIT))
    resource.setrlimit(resource.RLIMIT_CPU, (25, 25))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))


def run(candidate: Path, *, python: Path) -> dict[str, object]:
    if os.geteuid() == 0:
        raise ValueError("Run historical rehearsal as an unprivileged operator")
    # Fail closed unless BOTH v2 integrity and separately prepared environment
    # verification succeed. The audit never executes historical Python.
    gate = rollback_release_audit.audit(candidate, python=python)
    if not gate["offline_checks_passed"]:
        raise ValueError("Historical source or dependency audit did not pass")
    with tempfile.TemporaryDirectory(prefix="mcp-historical-restore-") as folder:
        scratch = Path(folder)
        files = _copy_candidate_state(candidate, scratch)
        # Detect candidate changes after the private snapshot was copied.
        rollback_candidate.inspect(candidate)
        command = _sandbox_command(candidate, python, scratch)
        with tempfile.TemporaryFile(mode="w+b") as output:
            result = subprocess.run(
                command, stdin=subprocess.DEVNULL, stdout=output,
                stderr=subprocess.DEVNULL, timeout=40, check=False,
                close_fds=True, env={"PATH": "/usr/bin:/bin"},
                preexec_fn=_limit_child,
            )
            output.seek(0)
            raw = output.read(_LIMIT + 1)
    if result.returncode != 0 or len(raw) > _LIMIT:
        raise ValueError("Historical reader sandbox could not finish successfully")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("Historical reader returned an invalid summary") from exc
    if (type(data) is not dict
            or set(data) != {"historical_policy_reader",
                             "historical_history_reader", "recent_history_count"}
            or data["historical_policy_reader"] is not True
            or data["historical_history_reader"] is not True
            or type(data["recent_history_count"]) is not int
            or not 0 <= data["recent_history_count"] <= 25):
        raise ValueError("Historical reader failed compatibility checks")
    return {
        "sandboxed_old_state_readers_passed": True,
        "staged_files": files,
        "recent_history_count": data["recent_history_count"],
        "network_access_enabled": False,
        "live_state_changed": False,
        "services_changed": False,
        "credentials_provided": False,
        "ready_for_live_use": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--python", required=True, type=Path,
                        help="Independent prepared historical venv interpreter")
    args = parser.parse_args(argv)
    try:
        report = run(args.candidate, python=args.python)
    except (OSError, ValueError, RecursionError, subprocess.SubprocessError):
        print("Historical reader rehearsal unavailable or incompatible; inspect "
              "private candidate, environment and sandbox locally",
              file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
