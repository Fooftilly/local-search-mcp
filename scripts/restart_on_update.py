#!/usr/bin/env python3
"""Restart active local-search-mcp user services after local Git code updates.

Never fetch, pull, install packages, or modify the Git worktree.
"""
from __future__ import annotations

import argparse
import ast
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import tokenize

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("local-search-mcp-bridge.service", "local-search-mcp-tunnel.service")
IN_PROGRESS = (
    "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD",
    "rebase-apply", "rebase-merge", "sequencer", "index.lock",
    "HEAD.lock", "packed-refs.lock",
)


def run(args: list[str], *, root: Path = ROOT, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=root, text=True, capture_output=True,
                          check=True, timeout=timeout)


def git(root: Path, *args: str) -> str:
    return run(["git", *args], root=root).stdout.strip()


def verify_checkout(root: Path) -> None:
    """Never run a service-update action outside this exact Git worktree."""
    top = Path(git(root, "rev-parse", "--show-toplevel")).resolve()
    if top != root.resolve():
        raise ValueError("Script root does not match the Git worktree")
    if git(root, "rev-parse", "--is-inside-work-tree") != "true":
        raise ValueError("Not a Git worktree")


def git_path(root: Path, name: str) -> Path:
    value = Path(git(root, "rev-parse", "--git-path", name))
    return (root / value).resolve() if not value.is_absolute() else value.resolve()


def reflog(root: Path = ROOT) -> Path:
    verify_checkout(root)
    path = git_path(root, "logs/HEAD")
    if not path.is_file():
        raise ValueError("Git HEAD reflog is missing; enable core.logAllRefUpdates")
    return path


def head(root: Path) -> str:
    return git(root, "rev-parse", "--verify", "HEAD^{commit}")


def state_file() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))).expanduser()
    return base / "local-search-mcp" / "last-service-commit"


def read_state(path: Path) -> str | None:
    if path.is_symlink():
        raise ValueError("Refusing symlinked update state")
    if not path.exists():
        return None
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Update state must be a private, owned regular file")
    value = path.read_text().strip()
    if len(value) not in (40, 64) or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("Invalid update-state commit")
    return value


def write_state(path: Path, value: str) -> None:
    folder = path.parent
    if folder.is_symlink():
        raise ValueError("Refusing symlinked update-state directory")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if folder.is_symlink() or folder.stat().st_uid != os.getuid():
        raise ValueError("Update-state directory is not owned by this user")
    # Re-check before atomic replacement to avoid following a link or overwriting
    # a different user's file.
    read_state(path)
    fd, temp = tempfile.mkstemp(prefix=".last-service-commit-", dir=folder)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        read_state(path)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def initialize(root: Path = ROOT, *, path: Path | None = None) -> None:
    """Seed the pre-installation commit, never resetting an existing baseline."""
    verify_checkout(root)
    location = path or state_file()
    if read_state(location) is None:
        write_state(location, head(root))


def busy(root: Path) -> bool:
    return any(git_path(root, name).exists() for name in IN_PROGRESS)


def changed_files(root: Path, old: str, new: str) -> list[str]:
    # Disabling rename detection surfaces both a removed runtime source and
    # its new documentation destination, so neither is silently ignored.
    output = subprocess.run(
        ["git", "diff", "--no-renames", "--name-only", "--no-ext-diff", "-z", old, new, "--"],
        cwd=root, capture_output=True, check=True, timeout=15,
    ).stdout
    return [os.fsdecode(name) for name in output.split(b"\0") if name]


def affects_runtime(name: str) -> bool:
    if name.startswith((".github/", "tests/", "docs/")):
        return False
    if name.endswith((".md", ".rst")) or name in ("LICENSE", ".gitignore"):
        return False
    # A code, asset, manifest or runtime settings change may affect services.
    return True


def tracked_source_dirty(root: Path) -> bool:
    """Do not restart with partially checked-out or locally edited tracked code."""
    # --no-renames emits individual add/delete paths rather than an R/C record
    # with an extra NUL-delimited source name (which cannot be sliced as XY path).
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--no-renames", "-z", "--untracked-files=no", "--"],
        cwd=root, capture_output=True, check=True, timeout=15,
    )
    names = [os.fsdecode(item[3:]) for item in result.stdout.split(b"\0") if item]
    return any(affects_runtime(name) for name in names)


def validate_python(root: Path, files: list[str]) -> None:
    for name in files:
        path = root / name
        if name.endswith(".py") and path.is_file():
            with tokenize.open(path) as f:
                ast.parse(f.read(), filename=name)



def requirements_ready(root: Path) -> bool:
    """Check installed versions against requirements without downloading anything.

    If the manifest was removed/renamed, manual --dependencies-ready confirmation
    is the only available dependency check; do not require a deleted file.
    """
    manifest = root / "requirements.txt"
    if not manifest.is_file():
        return not manifest.exists()
    interpreter = root / ".venv" / "bin" / "python"
    if not interpreter.is_file():
        return False
    probe = """
from importlib.metadata import version
from pathlib import Path
from pip._vendor.packaging.requirements import Requirement
for raw in Path('requirements.txt').read_text().splitlines():
    line = raw.split(' #', 1)[0].strip()
    if not line or line.startswith('#'):
        continue
    requirement = Requirement(line)  # Rejects unsupported pip directives.
    if requirement.url:
        raise ValueError('Direct URL requirements need manual review')
    if requirement.marker is not None and not requirement.marker.evaluate():
        continue
    if not requirement.specifier.contains(version(requirement.name)):
        raise ValueError('Unsatisfied local package requirement')
"""
    try:
        check = subprocess.run([str(interpreter), "-I", "-c", probe], cwd=root,
                               capture_output=True, timeout=15)
        if check.returncode != 0:
            return False
        dependencies = subprocess.run(
            [str(interpreter), "-I", "-m", "pip", "check"], cwd=root,
            capture_output=True, timeout=15,
        )
        return dependencies.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False

def active(service: str) -> bool:
    return subprocess.run(["systemctl", "--user", "is-active", "--quiet", service],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          timeout=15).returncode == 0


def restart(service: str) -> None:
    subprocess.run(["systemctl", "--user", "try-restart", service],
                   check=True, timeout=40)


def apply(root: Path = ROOT, *, path: Path | None = None,
          dependencies_ready: bool = False, settle_seconds: float = 2.0) -> str:
    verify_checkout(root)
    location = path or state_file()
    original = read_state(location)
    current = head(root)
    if original is None:
        write_state(location, current)
        return "Initialized local update baseline; no restart"
    if original == current:
        return "No local commit change"
    try:
        git(root, "cat-file", "-e", original + "^{commit}")
    except subprocess.CalledProcessError:
        # A removed or rewritten branch cannot be diffed reliably.
        return "Previous commit unavailable; manual service review required (baseline unchanged)"
    if busy(root) or tracked_source_dirty(root):
        return "Git operation or tracked runtime edits in progress; will retry"
    time.sleep(settle_seconds)
    if head(root) != current or busy(root) or tracked_source_dirty(root):
        return "Git checkout changed while checking; will retry"
    changed = changed_files(root, original, current)
    relevant = [name for name in changed if affects_runtime(name)]
    if not relevant:
        write_state(location, current)
        return "Documentation/test-only commit change; restart not required"
    if "requirements.txt" in changed and not dependencies_ready:
        return ("Python requirements changed, moved or removed; no automatic package downloads. "
                "Review and prepare dependencies manually, then run "
                "python3 scripts/restart_on_update.py --dependencies-ready")
    if "requirements.txt" in changed and not requirements_ready(root):
        return ("Installed Python requirements failed offline validation; "
                "fix the environment manually; running services left untouched")
    try:
        validate_python(root, relevant)
    except (OSError, SyntaxError, UnicodeError) as exc:
        return f"Changed Python source failed preflight ({type(exc).__name__}); will retry"
    bridge = active(SERVICES[0])
    tunnel = active(SERVICES[1])
    if not (bridge or tunnel):
        write_state(location, current)
        return "Updated checkout; both systemd services inactive (not started)"
    if tunnel and not bridge:
        # The tunnel unit Wants=bridge; restarting the tunnel would start an
        # intentionally inactive/failed bridge as an implicit dependency.
        # Keep the old baseline: once the bridge is brought up manually, a
        # subsequent tick can safely restart both services on the new code.
        return ("Bridge inactive while tunnel runs; restart deferred until "
                "bridge is active (baseline unchanged)")
    # Record exactly once *before* affecting live processes: a failed restart
    # must not cause an endless restart loop on each timer tick.
    write_state(location, current)
    if bridge:
        restart(SERVICES[0])
    if tunnel:
        restart(SERVICES[1])
    return "Restart requested for previously active services after local commit update"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dependencies-ready", action="store_true",
                        help="Acknowledge that changed Python requirements were installed manually")
    args = parser.parse_args(argv)
    try:
        print(apply(dependencies_ready=args.dependencies_ready), flush=True)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Local auto-restart check failed: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
