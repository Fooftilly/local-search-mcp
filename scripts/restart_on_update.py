#!/usr/bin/env python3
"""Restart active local-search-mcp user services after local Git code updates.

Never fetch, pull, install packages, or modify the Git worktree.
"""
from __future__ import annotations

import argparse
import ast
import os
import shlex
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
    info = folder.stat()
    if (folder.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError("Update-state directory must be private and owned by this user")
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


def reset_baseline(expected_commit: str, root: Path = ROOT, *,
                   path: Path | None = None) -> str:
    """Manually accept an inspected checkout; never restart services.

    Requiring the exact local HEAD makes this an explicit operator action
    rather than a silent repair when history has been pruned or replaced.
    """
    verify_checkout(root)
    location = path or state_file()
    if read_state(location) is None:
        raise ValueError(
            "No update baseline at " + str(location) +
            "; check --state-file against the installed unit with "
            "systemctl --user cat local-search-mcp-update.service"
        )
    if not expected_commit or expected_commit != head(root):
        raise ValueError("Provide the exact current Git HEAD commit to reset the baseline")
    if busy(root) or tracked_source_dirty(root):
        raise ValueError("Cannot reset baseline during Git operations or tracked runtime edits")
    # Do not advance the baseline across a concurrent checkout.
    if head(root) != expected_commit or busy(root) or tracked_source_dirty(root):
        raise ValueError("Git checkout changed while resetting the baseline")
    write_state(location, expected_commit)
    return ("Reset local update baseline to reviewed commit " + expected_commit +
            "; no services were restarted")


def busy(root: Path) -> bool:
    return any(git_path(root, name).exists() for name in IN_PROGRESS)


def checkout_stable(root: Path, commit: str) -> bool:
    """Recheck both revision and worktree at the point of applying an update."""
    return head(root) == commit and not busy(root) and not tracked_source_dirty(root)


def state_argument(path: Path) -> str:
    """Provide a shell-safe argument for manual actions on this exact state."""
    return "--state-file " + shlex.quote(str(path))


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



def dependency_manifest(name: str) -> bool:
    """Recognize conventional Python dependency manifests, including moves."""
    if name.startswith((".github/", "docs/", "tests/")):
        return False
    path = Path(name)
    leaf = path.name.lower()
    if leaf in {
        "pyproject.toml", "poetry.lock", "uv.lock", "pdm.lock",
        "pipfile", "pipfile.lock", "setup.cfg", "setup.py",
        "environment.yml", "environment.yaml",
    }:
        return True
    if leaf.startswith(("requirements", "constraints", "dependencies", "deps")):
        return leaf.endswith((".txt", ".in", ".pip", ".lock", ".toml", ".yml", ".yaml"))
    return (any(part.lower() in {"requirements", "constraints", "deps", "reqs"}
                for part in path.parts[:-1]) and leaf.endswith((".txt", ".in", ".pip")))


def pip_include_path(raw: str) -> str:
    """Decode a local pip -r/-c path without executing it or accessing network."""
    import re

    values = shlex.split(raw)
    if len(values) != 1:
        raise ValueError("Expected a single quoted/unquoted requirement include")
    result = os.path.expandvars(values[0])
    if re.search(r'\$\{[^}]+\}', result):
        raise ValueError(
            "Unresolved environment variable in pip include; configure it "
            "for the systemd user manager (systemctl --user set-environment "
            "NAME=value or ~/.config/environment.d/) before running the watcher"
        )
    return result


def referenced_requirement_files(
    root: Path, *, origins: dict[str, set[str]] | None = None,
    roles: dict[str, set[tuple[str, bool]]] | None = None,
) -> set[str]:
    """Find pip -r/-c closure and, optionally, each file's source manifest.

    Retain source-manifest context so a changed arbitrary include is validated
    through its original -r/-c edge, preserving constraint semantics.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--"], cwd=root,
        capture_output=True, check=True, timeout=15,
    ).stdout
    tracked_names = [os.fsdecode(item) for item in tracked.split(b"\0") if item]
    found: set[str] = set()
    visited: set[tuple[str, str, bool]] = set()
    base = root.resolve()

    def visit(name: str, source: str, constrained: bool = False) -> None:
        if origins is not None:
            origins.setdefault(name, set()).add(source)
        if roles is not None:
            roles.setdefault(name, set()).add((source, constrained))
        if (name, source, constrained) in visited:
            return
        if len(visited) >= 512:
            raise ValueError("Too many included requirement files")
        visited.add((name, source, constrained))
        found.add(name)
        original = root / name
        if original.is_symlink():
            raise ValueError("Refusing symlinked requirements include")
        resolved = original.resolve()
        if not resolved.is_relative_to(base):
            raise ValueError("Requirements include escapes Git checkout")
        if not resolved.exists():
            return  # Deletions must still be dependency-gated.
        if not resolved.is_file():
            raise ValueError("Requirements include is not a file")
        pending = ""
        for raw in resolved.read_text().splitlines():
            raw = raw.strip()
            if raw.endswith("\\") and not raw.lstrip().startswith("#"):
                pending += raw[:-1] + " "
                continue
            line = (pending + raw).split(" #", 1)[0].strip()
            pending = ""
            if not line or line.startswith("#"):
                continue
            for prefix in ("--requirement=", "--requirement ",
                           "--constraint=", "--constraint ", "-r ", "-c ",
                           "-r", "-c"):
                if line.startswith(prefix):
                    target = pip_include_path(line[len(prefix):].strip())
                    included = (resolved.parent / target).resolve()
                    if not included.is_relative_to(base):
                        raise ValueError("Requirements include escapes Git checkout")
                    visit(included.relative_to(base).as_posix(), source,
                          prefix.startswith(("--constraint", "-c")))
                    break

    for name in tracked_names:
        if dependency_manifest(name) and name.endswith((".txt", ".in", ".pip")):
            visit(name, name)
    return found


def constraint_manifest(name: str) -> bool:
    """Standalone constraints restrict versions; they don't require installs."""
    parts = [part.lower() for part in Path(name).parts]
    return any(p.startswith("constraint") or p == "constraints" for p in parts)


def requirements_ready(
    root: Path, changed_manifests: list[str] | None = None,
    origins: dict[str, set[str]] | None = None,
    roles: dict[str, set[tuple[str, bool]]] | None = None,
) -> bool:
    """Validate manually acknowledged pip requirements entirely offline.

    Use source manifests for changed -r/-c inputs to preserve constraint
    roles, even when requirements.txt was removed. Other formats rely on
    manual operator verification plus pip check.
    """
    manifest = root / "requirements.txt"
    interpreter = root / ".venv" / "bin" / "python"
    if not interpreter.is_file() or manifest.is_symlink():
        return False
    if not manifest.is_file() and manifest.exists():
        return False

    # False (regular requirement) takes precedence over True (constraint).
    candidates: dict[str, bool] = {}

    def add(name: str, constrained: bool) -> None:
        path = root / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError("Unsafe dependency manifest")
        if path.is_file():
            candidates[name] = candidates.get(name, True) and constrained

    try:
        if manifest.is_file():
            add("requirements.txt", False)
        for name in changed_manifests or []:
            sources = (origins or {}).get(name, set())
            if "requirements.txt" in sources and (root / "requirements.txt").is_file():
                # Validate the primary runtime manifest and its include graph,
                # not optional dev roots that share an included file.
                add("requirements.txt", False)
            elif name in sources:
                runtime_sources = {
                    source for source in sources
                    if source != name and not any(
                        part.startswith(("dev", "test")) or
                        part.endswith(("-dev.txt", "-test.txt"))
                        for part in Path(source).parts
                    )
                }
                included_sources = {
                    source for source, _constrained in (roles or {}).get(name, set())
                    if source in runtime_sources
                }
                if included_sources:
                    # Preserve every runtime include edge: -c only constrains
                    # optional distributions, whereas -r requires them.
                    # A shared input can have BOTH roles in different roots.
                    for source in included_sources:
                        add(source, constraint_manifest(source))
                else:
                    # Without a recorded include edge, validate the shared
                    # base directly rather than unrelated dev-only parents.
                    add(name, constraint_manifest(name))
            elif sources:
                runtime_sources = {
                    source for source in sources
                    if not any(part.startswith(("dev", "test")) or
                               part.endswith(("-dev.txt", "-test.txt"))
                               for part in Path(source).parts)
                }
                for source in (runtime_sources or sources):
                    add(source, constraint_manifest(source))
            elif name.endswith((".txt", ".in", ".pip")):
                # For direct API calls without a graph, validate included files
                # by their own contents instead of silently running pip check.
                add(name, constraint_manifest(name))
            if (dependency_manifest(name) and name.endswith((".txt", ".in", ".pip"))
                    and (not sources or sources == {name})):
                # A file included by another manifest must keep the -r/-c
                # role assigned by its parent. Do not re-walk it as a
                # standalone requirement merely because its name matches.
                add(name, constraint_manifest(name))
    except (OSError, ValueError):
        return False

    probe = r"""
import os
import re
import shlex
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from pip._vendor.packaging.requirements import Requirement

root = Path.cwd().resolve()
visiting = set()

def walk(filename, constrained=False):
    original = Path(filename)
    path = original.resolve()
    if (not path.is_relative_to(root) or original.is_symlink()
            or not path.is_file() or path in visiting):
        raise ValueError('Unsafe, absent or cyclic requirement file')
    visiting.add(path)
    try:
        pending = ''
        for raw in path.read_text().splitlines():
            raw = raw.strip()
            if raw.endswith('\\') and not raw.lstrip().startswith('#'):
                pending += raw[:-1] + ' '
                continue
            line = (pending + raw).split(' #', 1)[0].strip()
            pending = ''
            if not line or line.startswith('#'):
                continue
            for option, is_constraint in (
                ('--requirement=', False), ('--requirement ', False),
                ('--constraint=', True), ('--constraint ', True),
                ('-r ', False), ('-c ', True), ('-r', False), ('-c', True),
            ):
                if line.startswith(option):
                    tokens = shlex.split(line[len(option):].strip())
                    if len(tokens) != 1:
                        raise ValueError('Expected one pip include path')
                    included = os.path.expandvars(tokens[0])
                    if re.search(r'\$\{[^}]+\}', included):
                        raise ValueError('Unresolved include environment variable')
                    # Pip -r switches back to requirement semantics even if
                    # encountered within a constraint file (-c).
                    walk(path.parent / included, is_constraint)
                    break
            else:
                if line.startswith(('-e ', '-e=', '--editable ', '--editable=')):
                    # Editable paths may include extras, e.g. -e .[dev].
                    editable = line.split(' ', 1)[-1] if ' ' in line else line.split('=', 1)[1]
                    if not editable.startswith(('git+', 'https:', 'http:')):
                        local_path = re.sub(r'\[[^\[\]]+\]$', '', editable)
                        target = (path.parent / local_path).resolve()
                        if not target.is_relative_to(root) or not target.exists():
                            raise ValueError('Unsafe or missing editable path')
                        pyproject = target / 'pyproject.toml'
                        if pyproject.is_file():
                            import tomllib
                            metadata = tomllib.loads(pyproject.read_text())
                            name = metadata.get('project', {}).get('name')
                            if name:
                                version(name)
                    continue
                line = re.sub(r'\s+--hash(?:=|\s+)\S+', '', line).strip()
                if not line:
                    continue
                # These pip configuration directives cannot be verified from
                # installed distributions. Explicit operator review is required.
                if re.match(
                    r'^(?:--index-url|--extra-index-url|--find-links|'
                    r'--trusted-host|-i|-f)(?:=|\s)|^--no-index(?:\s|$)',
                    line,
                ):
                    continue
                requirement = Requirement(line)
                if requirement.marker is not None and not requirement.marker.evaluate():
                    continue
                try:
                    installed = version(requirement.name)
                except PackageNotFoundError:
                    if constrained:
                        continue
                    raise
                if not requirement.url and not requirement.specifier.contains(installed):
                    raise ValueError('Unsatisfied local package requirement')
        if pending:
            raise ValueError('Unterminated requirement continuation')
    finally:
        visiting.remove(path)

for entry in sys.argv[1:]:
    walk(root / entry[2:], constrained=entry.startswith('C:'))
"""
    try:
        if candidates:
            entries = [("C:" if constrained else "R:") + name
                       for name, constrained in candidates.items()]
            check = subprocess.run(
                [str(interpreter), "-I", "-c", probe, *entries],
                cwd=root, capture_output=True, timeout=15,
            )
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
        if dependencies_ready:
            raise ValueError(
                "No update baseline at " + str(location) +
                "; check --state-file against the installed systemd unit before "
                "acknowledging dependencies"
            )
        write_state(location, current)
        return "Initialized local update baseline; no restart"
    if original == current:
        return "No local commit change"
    try:
        git(root, "cat-file", "-e", original + "^{commit}")
    except subprocess.CalledProcessError:
        # A removed or rewritten branch cannot be diffed reliably.
        return ("Previous commit unavailable; manual service review required "
                "(baseline unchanged). After inspection and manual service "
                "preparation, run python3 scripts/restart_on_update.py " +
                state_argument(location) +
                ' --reset-baseline "$(git rev-parse HEAD)"')
    if busy(root) or tracked_source_dirty(root):
        return "Git operation or tracked runtime edits in progress; will retry"
    time.sleep(settle_seconds)
    if not checkout_stable(root, current):
        return "Git checkout changed while checking; will retry"
    changed = changed_files(root, original, current)
    # Follow -r/-c includes even when their filenames are otherwise ordinary
    # or their parent directory would normally be ignored as documentation.
    manifest_origins: dict[str, set[str]] = {}
    manifest_roles: dict[str, set[tuple[str, bool]]] = {}
    dependency_paths = referenced_requirement_files(
        root, origins=manifest_origins, roles=manifest_roles,
    )
    changed_manifests = [
        name for name in changed
        if dependency_manifest(name) or name in dependency_paths
    ]
    relevant = [name for name in changed
                if affects_runtime(name) or name in changed_manifests]
    if not relevant:
        if not checkout_stable(root, current):
            return "Git checkout changed before recording update; will retry"
        write_state(location, current)
        return "Documentation/test-only commit change; restart not required"
    if changed_manifests and not dependencies_ready:
        return ("Python requirements changed, moved or removed; no automatic "
                "package downloads. Review and prepare dependencies manually, "
                "then run python3 scripts/restart_on_update.py " +
                state_argument(location) + " --dependencies-ready")
    if changed_manifests and not requirements_ready(
        root, changed_manifests, manifest_origins, manifest_roles,
    ):
        return ("Installed Python requirements failed offline validation; "
                "fix the environment manually; running services left untouched")
    try:
        validate_python(root, relevant)
    except (OSError, SyntaxError, UnicodeError) as exc:
        return f"Changed Python source failed preflight ({type(exc).__name__}); will retry"
    bridge = active(SERVICES[0])
    tunnel = active(SERVICES[1])
    # Dependencies and Python preflight can take time, as can checking units.
    # Do not record the commit or restart processes if Git moved during them.
    if not checkout_stable(root, current):
        return "Git checkout changed after preflight; will retry"
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


def inspect_status(root: Path = ROOT, *, path: Path | None = None) -> str:
    """Inspect local revision changes without writing state or touching services.

    This is a comparison, not a package audit, update fetch, restart/rollback,
    or approval to change the running configuration.
    """
    verify_checkout(root)
    location = path or state_file()
    previous = read_state(location)
    current = head(root)
    if previous is None:
        return ("No local update baseline; check the pinned --state-file path "
                "or install the opt-in watcher. Nothing changed.")
    if busy(root) or tracked_source_dirty(root):
        return ("Git operation or tracked runtime edits in progress; cannot "
                "classify local update. Nothing changed.")
    if previous == current:
        return "No local commit change since saved baseline; nothing changed."
    try:
        git(root, "cat-file", "-e", previous + "^{commit}")
    except subprocess.CalledProcessError:
        return ("Saved baseline commit is unavailable locally; manual revision "
                "and service review required. Nothing changed.")
    changed = changed_files(root, previous, current)
    dependency_paths = referenced_requirement_files(root)
    manifests = [name for name in changed
                 if dependency_manifest(name) or name in dependency_paths]
    runtime = [name for name in changed if affects_runtime(name)]
    if not checkout_stable(root, current):
        return "Git checkout changed during inspection; retry. Nothing changed."
    prefix = ("Local revision differs from saved baseline "
              f"({previous[:12]} -> {current[:12]}). ")
    if manifests:
        return (prefix + f"{len(manifests)} dependency input(s) changed, "
                f"{len(runtime)} runtime file(s) changed. Manual dependency "
                "review/preparation and explicit --dependencies-ready are "
                "required before the watcher can restart services. "
                "No service or state change performed.")
    if runtime:
        return (prefix + f"{len(runtime)} runtime file(s) changed. "
                "The watcher may restart active services only after its own "
                "preflight checks; this inspection does not validate readiness. "
                "No service or state change performed.")
    return (prefix + "Documentation/test-only change; the watcher does not "
            "require a service restart. No service or state change performed.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--status", action="store_true",
                         help="Inspect local revisions only; never restart or download")
    actions.add_argument("--dependencies-ready", action="store_true",
                         help="Acknowledge that changed Python requirements were installed manually")
    actions.add_argument("--reset-baseline", metavar="COMMIT",
                         help="After reviewing and preparing the checkout, acknowledge exact Git HEAD without restarting")
    parser.add_argument("--state-file", type=Path,
                        help="Absolute state-file path from the installed systemd unit")
    args = parser.parse_args(argv)
    if args.state_file is not None and not args.state_file.is_absolute():
        parser.error("--state-file must be an absolute path")
    try:
        if args.status:
            message = inspect_status(path=args.state_file)
        elif args.reset_baseline is not None:
            message = reset_baseline(args.reset_baseline, path=args.state_file)
        else:
            message = apply(path=args.state_file, dependencies_ready=args.dependencies_ready)
        print(message, flush=True)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Local auto-restart check failed: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
