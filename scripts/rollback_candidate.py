#!/usr/bin/env python3
"""Prepare an offline, isolated rollback candidate; NEVER activate live rollback.

After the existing strict read-only rollback preflight, validate state through
the current application's fresh-install smoke test. Copy the exact local
ancestor's tracked source as data from 'git archive' into a NEW private
directory and stage a verified state restore. No old code runs, no services
restart, and the current Git HEAD/index, secrets and live files stay intact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tarfile
import tempfile

if __package__:
    from . import backup_state, rollback_preflight, restore_smoke
    from . import restart_on_update as update
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import backup_state, rollback_preflight, restore_smoke
    from scripts import restart_on_update as update

ROOT = Path(__file__).resolve().parents[1]
MAX_TAR = 128 * 1024 * 1024
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


def _extract_verified_archive(tar_path: Path, target: Path) -> int:
    count = 0
    total = 0
    seen = set()
    with tarfile.open(tar_path, mode="r:") as archive:
        for item in archive:
            # Git tracked source is data, not something to execute while
            # preparing. Refuse symbolic/hard links, devices and all specials.
            if not (item.isfile() or item.isdir()):
                raise ValueError("Rollback candidate has unsafe tracked file type")
            parts = _valid_member(item.name.rstrip("/") if item.isdir() else item.name)
            key = "/".join(parts)
            if key in seen:
                raise ValueError("Duplicate rollback source path")
            seen.add(key)
            destination = target.joinpath(*parts)
            if item.isdir():
                destination.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            count += 1
            total += item.size
            if count > MAX_FILES or item.size > MAX_INDIVIDUAL or total > MAX_TAR:
                raise ValueError("Rollback source exceeds fixed bounds")
            destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            stream = archive.extractfile(item)
            if stream is None:
                raise ValueError("Missing regular rollback source")
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                         0o600)
            with os.fdopen(fd, "wb") as out:
                remaining = item.size
                while remaining:
                    block = stream.read(min(65536, remaining))
                    if not block:
                        raise ValueError("Truncated archived source")
                    out.write(block)
                    remaining -= len(block)
                out.flush()
                os.fsync(out.fileno())
    if count == 0:
        raise ValueError("Empty rollback candidate code archive")
    return count


def _git_archive(root: Path, target: str, destination: Path) -> None:
    """Offline, literal SHA, no checkout, smudge driver or old-code execution."""
    env = update._inspection_env()
    with destination.open("xb") as output:
        os.chmod(destination, 0o600)
        try:
            process = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "archive",
                 "--format=tar", target],
                cwd=root, stdout=output, stderr=subprocess.DEVNULL,
                env=env, timeout=45, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Local Git archive creation failed") from exc
    if process.returncode or destination.stat().st_size > MAX_TAR:
        raise ValueError("Local Git archive unavailable or oversized")


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
    tar_path = destination / ".candidate.tar"
    try:
        _git_archive(root, target, tar_path)
        code = destination / "source"
        code.mkdir(mode=0o700)
        files = _extract_verified_archive(tar_path, code)
        tar_path.unlink()
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
    except (OSError, ValueError, subprocess.SubprocessError,
            tarfile.TarError) as exc:
        print("Rollback candidate not prepared: " + type(exc).__name__,
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
