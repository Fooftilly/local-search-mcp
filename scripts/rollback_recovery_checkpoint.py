#!/usr/bin/env python3
"""Create or inspect a private, verified current-state recovery checkpoint.

This command is strictly pre-cutover. It does not stop/start services, change
Git, install software, restore live files, or authorize an old release.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

if __package__:
    from . import (backup_state, maintenance_readiness, restart_on_update,
                   restore_smoke, rollback_candidate, rollback_preflight)
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import (backup_state, maintenance_readiness, restart_on_update,
                         restore_smoke, rollback_candidate, rollback_preflight)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "local-search-mcp/recovery-checkpoint/v1"
ARCHIVE = "current-state.zip"
MANIFEST = "checkpoint.json"
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _private_dir(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("Expected an owner-only recovery directory")


def _read_file(path: Path, *, limit: int) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | NOFOLLOW)
    except OSError as exc:
        raise ValueError("Private recovery file unavailable") from exc
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > limit):
            raise ValueError("Recovery file is not a private regular file")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Recovery file exceeds size limit")
        after = os.fstat(stream.fileno())
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                info.st_size, info.st_mtime_ns, info.st_ctime_ns):
            raise ValueError("Recovery file changed during inspection")
        return data


def _candidate_identity(candidate: Path) -> tuple[str, str, str]:
    rollback_candidate.inspect(candidate)
    raw = _read_file(candidate / "candidate.json", limit=1_000_000)
    meta = json.loads(raw)
    if (type(meta) is not dict
            or meta.get("schema") != "local-search-mcp/rollback-candidate/v2"
            or meta.get("activation_performed") is not False
            or meta.get("ready_for_live_use") is not False):
        raise ValueError("Expected an intact offline candidate")
    source, target = meta.get("source_commit"), meta.get("target_commit")
    if (type(source) is not str or type(target) is not str
            or not SHA.fullmatch(source) or not SHA.fullmatch(target)):
        raise ValueError("Missing candidate Git identity")
    return source, target, hashlib.sha256(raw).hexdigest()


def _checkout_unchanged(root: Path, source: str) -> None:
    restart_on_update.verify_checkout(root, read_only=True)
    if (restart_on_update.head(root, read_only=True) != source
            or restart_on_update.busy(root, read_only=True)
            or not rollback_preflight._clean_checkout(root)):
        raise ValueError("Current checkout no longer matches candidate source")


def _maintenance_ready(candidate: Path) -> None:
    status = maintenance_readiness.inspect(candidate=candidate)
    if (status["ready_for_offline_maintenance"] is not True
            or status["live_rollback_authorized"] is not False
            or status["services_changed"] is not False):
        raise ValueError("All project-owned services must be quiescent")


def _verify_effective_environment(*, state_dir: Path | None = None,
                                  config_dir: Path | None = None) -> None:
    """Refuse checkpoints when the shell and systemd service paths may differ.

    Only the root-owned systemctl binary is used. Environment output is
    inspected in memory and never logged; arbitrary service/unit env overrides
    are unsupported until explicitly handled by a future activation workflow.
    """
    trusted = None
    for entry in ("/usr/bin/systemctl", "/bin/systemctl"):
        try:
            info = Path(entry).stat()
            if (stat.S_ISREG(info.st_mode) and info.st_uid == 0
                    and not info.st_mode & 0o022):
                trusted = entry
                break
        except OSError:
            continue
    if trusted is None:
        raise ValueError("Trusted systemd user manager tool unavailable")

    def show(*args: str) -> str:
        result = subprocess.run([trusted, "--user", *args], capture_output=True,
                                check=False, timeout=15)
        if result.returncode or len(result.stdout) > 131_072:
            raise ValueError("Systemd user environment unavailable")
        return result.stdout.decode("utf-8", "strict")

    # Check that no per-unit Environment/EnvironmentFile overrides the user
    # manager state paths. As a safety gate, refuse all unit-level overrides.
    for name in ("local-search-mcp-bridge.service", "local-search-mcp-tunnel.service"):
        text = show("show", name, "--property=Environment",
                    "--property=EnvironmentFiles", "--property=PassEnvironment",
                    "--property=UnsetEnvironment")
        values = {}
        for line in text.splitlines():
            key, eq, value = line.partition("=")
            if not eq or key in values or key not in {
                    "Environment", "EnvironmentFiles",
                    "PassEnvironment", "UnsetEnvironment"}:
                raise ValueError("Unrecognized unit environment response")
            values[key] = value
        if (set(values) != {"Environment", "EnvironmentFiles",
                            "PassEnvironment", "UnsetEnvironment"}
                or any(values.values())):
            raise ValueError("Unit has unverified environment overrides")

    # The shell may inherit a *different* XDG_STATE_HOME, history file or HOME.
    # Do not silently archive an unrelated but valid database in that case.
    values = {}
    for line in show("show-environment").splitlines():
        key, eq, value = line.partition("=")
        if not eq:
            raise ValueError("Unrecognized user manager environment response")
        if key in ("HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME",
                   "LOCAL_SEARCH_MCP_HISTORY_FILE"):
            if key in values:
                raise ValueError("Duplicate service state environment")
            values[key] = value
    import pwd
    manager_home = Path(values.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir)
    shell_home = Path.home()
    if manager_home != shell_home:
        raise ValueError("Shell home differs from the user manager home")
    for key in ("XDG_STATE_HOME", "XDG_CONFIG_HOME",
                "LOCAL_SEARCH_MCP_HISTORY_FILE"):
        if (values.get(key) or "") != (os.environ.get(key) or ""):
            raise ValueError("Shell and user service state paths differ")
    state = (Path(values["XDG_STATE_HOME"]) if values.get("XDG_STATE_HOME")
             else manager_home / ".local" / "state") / "local-search-mcp"
    config = (Path(values["XDG_CONFIG_HOME"]) if values.get("XDG_CONFIG_HOME")
              else manager_home / ".config") / "local-search-mcp"
    if ((state_dir is not None and state_dir.resolve() != state.resolve())
            or (config_dir is not None and config_dir.resolve() != config.resolve())):
        raise ValueError("Selected backup directories differ from the service")
    history = values.get("LOCAL_SEARCH_MCP_HISTORY_FILE")
    if history and not Path(history).is_absolute():
        raise ValueError("Historical service audit path is not absolute")


def _archive_identity(path: Path) -> tuple[str, int, int]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | NOFOLLOW)
    except OSError as exc:
        raise ValueError("Private recovery file unavailable") from exc
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or not 0 < info.st_size <= backup_state.MAX_ARCHIVE):
            raise ValueError("Recovery archive is not private or is oversized")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        # Keep the original validated file descriptor open across the ZIP
        # validation by pathname; then re-hash the *same inode* and check
        # that its pathname, size and modification metadata still match.
        # Reject concurrent replacement rather than binding the manifest to
        # different bytes than those passed through the archive validator.
        payload = backup_state.verified_payload(path)
        stream.seek(0)
        after_digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
        named = path.lstat()
        if (digest != after_digest
                or (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino)
                or (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino)
                or (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError("Recovery archive changed during validation")
    return digest, info.st_size, len(payload)


def inspect(directory: Path, *, candidate: Path | None = None) -> dict[str, object]:
    """Require sealed metadata and revalidate the private backup and candidate."""
    _private_dir(directory)
    if {entry.name for entry in directory.iterdir()} != {ARCHIVE, MANIFEST}:
        raise ValueError("Recovery checkpoint is incomplete or contains extra files")
    meta = json.loads(_read_file(directory / MANIFEST, limit=4096))
    if (type(meta) is not dict
            or set(meta) != {
                "schema", "source_commit", "target_commit",
                "candidate_sha256", "archive_sha256", "archive_size",
                "state_files", "complete", "activation_performed",
                "ready_for_live_use"}
            or meta["schema"] != SCHEMA
            or meta["complete"] is not True
            or meta["activation_performed"] is not False
            or meta["ready_for_live_use"] is not False
            or any(type(meta[key]) is not str or not SHA.fullmatch(meta[key])
                   for key in ("source_commit", "target_commit"))
            or any(type(meta[key]) is not str
                   or not re.fullmatch(r"[0-9a-f]{64}", meta[key])
                   for key in ("candidate_sha256", "archive_sha256"))
            or type(meta["archive_size"]) is not int
            or type(meta["state_files"]) is not int):
        raise ValueError("Unsupported or incomplete recovery checkpoint")
    digest, size, files = _archive_identity(directory / ARCHIVE)
    if (meta["archive_sha256"] != digest
            or meta["archive_size"] != size
            or meta["state_files"] != files):
        raise ValueError("Recovery checkpoint no longer matches archive")
    if candidate is not None:
        source, target, candidate_digest = _candidate_identity(candidate)
        if (source != meta["source_commit"]
                or target != meta["target_commit"]
                or candidate_digest != meta["candidate_sha256"]):
            raise ValueError("Recovery checkpoint targets another candidate")
    return {
        "checkpoint_verified": True,
        "archive_integrity_verified": True,
        "candidate_binding_verified": candidate is not None,
        "state_files": files,
        "activation_performed": False,
        "ready_for_live_use": False,
        "services_changed": False,
    }


def create(candidate: Path, destination: Path, *,
           root: Path = ROOT, state_dir: Path | None = None,
           config_dir: Path | None = None) -> dict[str, object]:
    """Seal a new checkpoint only after all independent recovery checks pass.

    A process interrupted before publishing checkpoint.json leaves an
    intentionally *unsealed* destination that inspect() always rejects.
    Existing destinations are never replaced, including incomplete ones.
    """
    if os.geteuid() == 0:
        raise ValueError("Run recovery checkpoint as the unprivileged operator")
    if destination.name in ("", ".", "..") or destination.exists() or destination.is_symlink():
        raise ValueError("Recovery destination must be new")
    _private_dir(destination.parent)
    # Prevent private output being placed inside a tracked source, candidate,
    # or running state directory. Such placement would change what is audited.
    parent = destination.parent.resolve(strict=True)
    # Optional live state/settings roots need not exist on a fresh install;
    # resolving without strict=True still lets us exclude nested destinations.
    roots = (root.resolve(strict=True), candidate.resolve(strict=True),
             (state_dir or backup_state.state_directory()).resolve(),
             (config_dir or backup_state.settings_directory()).resolve())
    target = parent / destination.name
    if any(target == base or base in target.parents for base in roots):
        raise ValueError("Recovery checkpoint must be outside live and candidate paths")
    source, target, candidate_digest = _candidate_identity(candidate)
    _checkout_unchanged(root, source)
    _maintenance_ready(candidate)
    _verify_effective_environment(state_dir=state_dir, config_dir=config_dir)
    destination.mkdir(mode=0o700)
    # Intentionally do not remove a partial directory on failure. An interrupted
    # invocation is never mistaken for a valid checkpoint or silently reused.
    backup_state.backup(destination / ARCHIVE,
                        state_dir=state_dir, config_dir=config_dir)
    digest, size, files = _archive_identity(destination / ARCHIVE)
    # A staged candidate inventory alone does not establish that its target
    # still exists locally or is a strict ancestor. Reuse the hardened,
    # no-network Git preflight against this exact newly archived checkpoint.
    rollback_preflight.preflight(target, destination / ARCHIVE, root=root)
    restore_smoke.smoke(destination / ARCHIVE)
    _maintenance_ready(candidate)
    _verify_effective_environment(state_dir=state_dir, config_dir=config_dir)
    _checkout_unchanged(root, source)
    _, _, after = _candidate_identity(candidate)
    if after != candidate_digest:
        raise ValueError("Candidate changed during recovery checkpoint")
    record = {
        "schema": SCHEMA,
        "source_commit": source, "target_commit": target,
        "candidate_sha256": candidate_digest,
        "archive_sha256": digest, "archive_size": size, "state_files": files,
        "complete": True, "activation_performed": False,
        "ready_for_live_use": False,
    }
    data = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(data) > 4096:
        raise ValueError("Recovery manifest exceeds size limit")
    fd = os.open(destination / MANIFEST, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    directory_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return inspect(destination, candidate=candidate)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--inspect", dest="inspect_path", type=Path)
    args = parser.parse_args(argv)
    if args.inspect_path is not None:
        if args.destination is not None:
            parser.error("--destination cannot be combined with --inspect")
    elif args.candidate is None or args.destination is None:
        parser.error("Select either --inspect or both --candidate and --destination")
    try:
        report = (inspect(args.inspect_path, candidate=args.candidate)
                  if args.inspect_path else create(args.candidate, args.destination))
    except (OSError, ValueError, KeyError, TypeError, RecursionError,
            subprocess.SubprocessError):
        print("Private recovery checkpoint incomplete or unavailable; review local "
              "maintenance state and checkpoint directory", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
