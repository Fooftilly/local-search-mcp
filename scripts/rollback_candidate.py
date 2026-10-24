#!/usr/bin/env python3
"""Prepare an offline, isolated rollback candidate; NEVER activate live rollback.

After the existing strict read-only rollback preflight, validate state through
the current application's fresh-install smoke test. Copy the exact local
ancestor's tracked raw Git blobs into a NEW private directory and stage a verified state restore. No old code runs, no services
restart, and the current Git HEAD/index, secrets and live files stay intact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tempfile

if __package__:
    from . import backup_state, rollback_preflight, restore_smoke
    from . import restart_on_update as update
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import backup_state, rollback_preflight, restore_smoke
    from scripts import restart_on_update as update

ROOT = Path(__file__).resolve().parents[1]
MAX_TOTAL_SOURCE = 128 * 1024 * 1024
MAX_FILES = 4000
MAX_INDIVIDUAL = 32 * 1024 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _private_parent(path: Path) -> None:
    parent = path.parent
    info = parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError("Rollback candidate parent must be owned and private (0700)")


def _valid_member(name: str) -> tuple[str, ...]:
    if type(name) is not str or len(name) > 1024:
        raise ValueError("Invalid archived source name")
    rel = PurePosixPath(name)
    parts = rel.parts
    if (not parts or rel.is_absolute() or any(
            p in ("", ".", "..") or "\\" in p or "\x00" in p for p in parts)
            or name.startswith("./") or name.endswith("//")):
        raise ValueError("Unsafe archive member path")
    return parts


def _git(root: Path, *args: str) -> bytes:
    """Raw local Git plumbing only; neither checkout nor content filters."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, check=False,
            env=update._inspection_env(), timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Cannot read local Git object") from exc
    if result.returncode:
        raise ValueError("Required local Git object unavailable")
    return result.stdout


_TREE_ENTRY = re.compile(rb"(100644|100755) blob ([0-9a-f]{40}|[0-9a-f]{64})")


def _copy_git_blobs(root: Path, target: str, destination: Path) -> int:
    """Copy exact tracked blobs, NEVER triggering Git smudge or archive filters."""
    listing = _git(root, "ls-tree", "-r", "-z", "--full-tree", target)
    if len(listing) > 512_000:
        raise ValueError("Rollback source listing too large")
    seen = set()
    total = 0
    count = 0
    for record in listing.split(b"\0"):
        if not record:
            continue
        header, sep, pathname = record.partition(b"\t")
        match = _TREE_ENTRY.fullmatch(header) if sep else None
        if match is None:
            # This intentionally rejects Git symlinks (120000), gitlinks
            # (160000), and future unknown file modes.
            raise ValueError("Rollback source has unsupported tracked file type")
        try:
            name = pathname.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ValueError("Rollback source uses unsupported filename encoding") from exc
        parts = _valid_member(name)
        if parts in seen:
            raise ValueError("Duplicate tracked source")
        seen.add(parts)
        oid = match.group(2).decode("ascii")
        raw_size = _git(root, "cat-file", "-s", oid)
        if not re.fullmatch(rb"[0-9]{1,10}\n?", raw_size):
            raise ValueError("Invalid tracked Git blob size")
        size = int(raw_size)
        count += 1
        total += size
        if count > MAX_FILES or size > MAX_INDIVIDUAL or total > MAX_TOTAL_SOURCE:
            raise ValueError("Rollback tracked source exceeds size limits")
        data = _git(root, "cat-file", "blob", oid)
        if len(data) != size:
            raise ValueError("Git blob bytes differ from verified object size")
        destination_file = destination.joinpath(*parts)
        destination_file.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd = os.open(destination_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                     0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    if count == 0:
        raise ValueError("Empty target Git tree")
    return count


def prepare(target: str, archive: Path, destination: Path,
            *, root: Path = ROOT) -> dict:
    """Prepare data only; destination and all live installation paths stay separate."""
    rollback_preflight.preflight(target, archive, root=root)
    restore_smoke.smoke(archive)  # current readers in an isolated temp install
    if (destination.is_symlink() or destination.exists()
            or destination == root or destination == archive.parent):
        raise ValueError("Rollback candidate destination must not exist")
    _private_parent(destination)
    source_head = update.head(root, read_only=True)
    destination.mkdir(mode=0o700)
    try:
        code = destination / "source"
        code.mkdir(mode=0o700)
        files = _copy_git_blobs(root, target, code)
        backup_state.restore(archive, destination / "state")
        if (update.head(root, read_only=True) != source_head or
                update.busy(root, read_only=True)
                or not rollback_preflight._clean_checkout(root)):
            raise ValueError("Source checkout changed while preparing rollback")
        meta = {"schema": "local-search-mcp/rollback-candidate/v1",
                "source_commit": source_head, "target_commit": target,
                "source_files": files,
                "state_files": len(backup_state.verified_payload(archive)),
                "activation_performed": False,
                "ready_for_live_use": False}
        data = (json.dumps(meta, sort_keys=True, separators=(",", ":")) + "\n").encode()
        fd = os.open(destination / "candidate.json",
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        return meta
    except BaseException:
        # Do not follow symlinks, and never inspect any operator path outside
        # the one just created. This tree is not reachable by runtime services.
        import shutil
        shutil.rmtree(destination)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True,
                        help="Exact full SHA for an available local ancestor commit")
    parser.add_argument("--archive", type=Path, required=True,
                        help="Verified private state backup from current installation")
    parser.add_argument("--destination", type=Path,
                        help="New path under an existing owner-only private directory")
    parser.add_argument("--confirm", choices=("STAGE",),
                        help="Required to create a local private candidate")
    args = parser.parse_args(argv)
    try:
        if args.destination is None:
            print(rollback_preflight.preflight(args.target, args.archive))
            restore_smoke.smoke(args.archive)
            print("Read-only compatibility smoke passed under current readers. "
                  "No candidate staged and no live rollback performed.")
        else:
            if args.confirm != "STAGE":
                raise ValueError("Type --confirm STAGE to create a candidate")
            report = prepare(args.target, args.archive, args.destination)
            print(json.dumps(report, sort_keys=True))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("Rollback candidate not prepared: " + type(exc).__name__,
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
