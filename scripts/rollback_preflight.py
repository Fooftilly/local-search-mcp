#!/usr/bin/env python3
"""Offline, read-only preflight for an operator-planned local Git rollback.

No checkout, service operations, Git remote access, or live state changes.
Validates an existing private state archive, not the runtime compatibility
of an older application version. Does not perform or approve a rollback.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path
import subprocess
import sys

if __package__:
    from . import backup_state, restart_on_update as update
else:
    from pathlib import Path as _Path
    sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
    from scripts import backup_state, restart_on_update as update


ROOT = Path(__file__).resolve().parents[1]
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Use no optional Git locks and never lazily fetch promisor objects."""
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, check=False, timeout=15,
        env=update._inspection_env(),
    )


def _clean_checkout(root: Path) -> bool:
    """Conservatively reject staged, unstaged and untracked content.

    Includes untracked files: switching commits could clobber their paths.
    Git's ignored state (e.g. .venv) is not validated by this report.
    """
    result = _git(root, "status", "--porcelain=v1", "--no-renames", "-z",
                  "--untracked-files=normal", "--")
    if result.returncode:
        raise ValueError("Unable to inspect local Git checkout")
    return not result.stdout


def preflight(target: str, archive: Path, *, root: Path = ROOT) -> str:
    """Fail closed unless the target, checkout and existing backup pass checks."""
    if not _SHA.fullmatch(target):
        raise ValueError("Target must be an exact lowercase full Git commit SHA")
    update.verify_checkout(root, read_only=True)
    current = update.head(root, read_only=True)
    if target == current:
        raise ValueError("Target is already HEAD; no rollback revision to inspect")
    if update.busy(root, read_only=True) or not _clean_checkout(root):
        raise ValueError("Git operation in progress or checkout is not clean")
    # All objects must be available locally. In partial clones, lazy fetching
    # is disabled; no remote operation can be triggered by this preflight.
    probe = _git(root, "cat-file", "-e", target + "^{commit}")
    if probe.returncode:
        raise ValueError("Target commit is unavailable locally")
    ancestor = _git(root, "merge-base", "--is-ancestor", target, current)
    if ancestor.returncode == 1:
        raise ValueError("Target is not an ancestor of current HEAD")
    if ancestor.returncode:
        raise ValueError("Cannot verify ancestry with local Git objects")
    try:
        changed = update.changed_files(root, target, current, read_only=True)
        included = update.referenced_requirement_files(root, read_only=True)
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise ValueError("Cannot inspect local revision differences") from exc
    dependency_changes = [name for name in changed
                          if update.dependency_manifest(name) or name in included]
    runtime_changes = [name for name in changed if update.affects_runtime(name)]
    # Even the latest manifest graph cannot establish that dependencies of
    # older commits are installed. Manual environment review is ALWAYS needed.
    # Surface missing/symlinked input as a validation failure, not an
    # unhandled FileNotFoundError. Verification still checks owner/perms and
    # fully validates the manifest and contents independently.
    if archive.is_symlink() or not archive.is_file():
        raise ValueError("Private backup archive is missing or symlinked")
    payload = backup_state.verified_payload(archive)
    count = len(payload)
    # Recheck after potentially slow verification. A dirty or changed checkout
    # cannot produce a positive preflight result.
    if (update.head(root, read_only=True) != current
            or update.busy(root, read_only=True)
            or not _clean_checkout(root)):
        raise ValueError("Checkout changed during preflight; inspect again")
    return (
        f"Offline rollback preflight passed for local ancestor {target[:12]} "
        f"from HEAD {current[:12]}. Private archive verified ({count} files); "
        f"{len(runtime_changes)} runtime file(s) and "
        f"{len(dependency_changes)} dependency input(s) differ. "
        "This does NOT prove target dependencies, data formats, credentials, "
        "service configuration or rollback safety. Manually review changes, "
        "stop the update watcher and affected services, prepare a compatible "
        "environment, and follow a separately reviewed recovery plan. "
        "No checkout, baseline, service or live state was modified."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="Exact full local ancestor commit SHA")
    parser.add_argument("--archive", type=Path, required=True,
                        help="Existing owner-only validated state archive")
    args = parser.parse_args(argv)
    try:
        print(preflight(args.target, args.archive))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"Offline rollback preflight failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
