#!/usr/bin/env python3
"""Conservative offline audit of an old, privately staged rollback release.

Never import or execute historical source, install packages, change checkout,
run Docker, modify secrets or authorize a live service cutover.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

if __package__:
    from . import rollback_candidate
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import rollback_candidate

ROOT = Path(__file__).resolve().parents[1]
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
MAX_SOURCE = 1_000_000
MAX_REQUIREMENTS = 32_768
MAX_PACKAGES = 64
REQUIRED = {
    "bridge.py": ("web_search", "web_fetch"),
    "bootstrap.py": ("run_unattended_service",),
    "domain_policy.py": ("initialize",),
    "history_index.py": ("sync_history",),
    "history_log.py": (),
    "health_cli.py": ("snapshot",),
}
UNITS = {
    "config/systemd/local-search-mcp-bridge.service": "bridge",
    "config/systemd/local-search-mcp-tunnel.service": "tunnel",
}
_REQ_NAME = r"[a-zA-Z][a-zA-Z0-9_.-]*"
_REQ_VERSION = r"\d+(?:\.\d+){0,4}(?:a\d+|b\d+|rc\d+|post\d+)?"
_REQ_OPERATOR = r"(?:==|!=|>=|<=|~=|>|<)"
REQUIREMENT_LINE = re.compile(
    rf"^{_REQ_NAME}\s*{_REQ_OPERATOR}\s*{_REQ_VERSION}"
    rf"(?:\s*,\s*{_REQ_OPERATOR}\s*{_REQ_VERSION})*$")

# Run only with an explicitly supplied, independently prepared venv interpreter.
# This does not import any historical module or perform package installation.
ENV_CHECK = r"""
import importlib.metadata as md
import json, sys
data = json.load(sys.stdin)
try:
    from packaging.requirements import Requirement
except ImportError:
    from pip._vendor.packaging.requirements import Requirement
failed = 0
for raw in data:
    req = Requirement(raw)
    if req.url or req.extras or req.marker:
        failed += 1
        continue
    try:
        version = md.version(req.name)
    except md.PackageNotFoundError:
        failed += 1
        continue
    if version not in req.specifier:
        failed += 1
print(json.dumps({"checked": len(data), "satisfied": len(data)-failed,
                  "all_satisfied": not failed}))
"""


def _source_file(candidate: Path, relative: str, *, limit: int = MAX_SOURCE) -> bytes:
    """Fixed source paths only, verified through no-follow file descriptors."""
    if relative not in (*REQUIRED, *UNITS, "requirements.txt"):
        raise ValueError("Unsupported candidate contract path")
    path = candidate / "source" / relative
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077
                or info.st_size > limit):
            raise ValueError("Historical source entry is unsafe")
        return stream.read(limit + 1)


def _registered_tool(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in func.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if (not isinstance(target, ast.Attribute) or target.attr != "tool"
                or not isinstance(target.value, ast.Name)
                or target.value.id != "mcp"):
            continue
        if not isinstance(decorator, ast.Call):
            return True
        # FastMCP can rename a public MCP tool; dynamic aliases cannot
        # establish web_search / web_fetch API compatibility.
        if decorator.args or any(item.arg is None for item in decorator.keywords):
            return False
        aliases = [item.value for item in decorator.keywords if item.arg == "name"]
        if not aliases:
            return True
        return (len(aliases) == 1 and isinstance(aliases[0], ast.Constant)
                and type(aliases[0].value) is str
                and aliases[0].value == func.name)
    return False

def _python_contract(candidate: Path, name: str, names: tuple[str, ...]) -> bool:
    raw = _source_file(candidate, name)
    try:
        tree = ast.parse(raw, filename=name)
    except (SyntaxError, UnicodeError, ValueError):
        return False
    funcs = {node.name: node for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if not all(n in funcs for n in names):
        return False
    return (name != "bridge.py"
            or all(_registered_tool(funcs[n]) for n in names))


def _unit_contract(candidate: Path, name: str, component: str) -> bool:
    try:
        text = _source_file(candidate, name, limit=16_384).decode("utf-8")
    except UnicodeError:
        return False
    # Exact checked-in template match rejects overrides of privilege,
    # Environment/File, ExecStart/Pre/Post, and service/user identity.
    # Historical template changes must be independently reviewed.
    canonical = ROOT / name
    if canonical.is_symlink():
        return False
    try:
        expected = canonical.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return (text == expected
            and "ExecStart=$EXE $LAUNCH --service " + component in text)

def _requirements(candidate: Path) -> list[str]:
    text = _source_file(candidate, "requirements.txt",
                        limit=MAX_REQUIREMENTS).decode("utf-8")
    names = []
    for original in text.splitlines():
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        # Reject includes, constraints, URLs, environment markers, options,
        # shell continuations, substitution and path/direct references.
        if (len(line) > 160 or not REQUIREMENT_LINE.fullmatch(line)
                or not any(char in line for char in "<>=!~")):
            raise ValueError("Historical requirement needs manual review")
        names.append(line)
    if not names or len(names) > MAX_PACKAGES:
        raise ValueError("Historical requirement set is missing or too large")
    return names


def _environment(python: Path, requirements: list[str], candidate: Path) -> dict:
    """Check metadata in a separate operator-selected venv, fully offline."""
    if (not python.is_absolute() or python.parent.name != "bin"
            or python.name not in ("python", "python3")
            or python.parent.parent == candidate
            or candidate.resolve() in python.parent.parent.resolve().parents
            or python.parent.parent.resolve() == candidate.resolve()
            or not (python.parent.parent / "pyvenv.cfg").is_file()):
        raise ValueError("Select an independent prepared virtualenv interpreter")
    root = python.parent.parent
    info = root.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022 or not python.is_file()):
        raise ValueError("Selected virtualenv is unsafe")
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
           "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        result = subprocess.run([str(python), "-I", "-c", ENV_CHECK],
                                input=json.dumps(requirements).encode(),
                                capture_output=True, check=False,
                                timeout=20, env=env)
        if result.returncode or len(result.stdout) > 4096:
            raise ValueError("Isolated virtualenv metadata unavailable")
        data = json.loads(result.stdout)
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
        raise ValueError("Isolated virtualenv check failed") from exc
    if (not isinstance(data, dict)
            or set(data) != {"checked", "satisfied", "all_satisfied"}
            or type(data["checked"]) is not int
            or type(data["satisfied"]) is not int
            or type(data["all_satisfied"]) is not bool
            or data["checked"] != len(requirements)
            or not 0 <= data["satisfied"] <= data["checked"]
            or data["all_satisfied"] != (data["checked"] == data["satisfied"])):
        raise ValueError("Unexpected virtualenv metadata response")
    return data


def audit(candidate: Path, *, python: Path | None = None) -> dict:
    # Verify full private hash/mode inventory before inspecting static contracts.
    rollback_candidate.inspect(candidate)
    blockers = []
    for module, names in REQUIRED.items():
        try:
            if not _python_contract(candidate, module, names):
                blockers.append("historical Python contract missing or invalid")
        except (OSError, ValueError):
            blockers.append("historical Python contract unavailable")
    for unit, component in UNITS.items():
        try:
            if not _unit_contract(candidate, unit, component):
                blockers.append("historical systemd template contract differs")
        except (OSError, ValueError):
            blockers.append("historical systemd template unavailable")
    try:
        requirements = _requirements(candidate)
    except (OSError, ValueError, UnicodeError):
        blockers.append("historical dependency input needs manual review")
        requirements = []
    verified_environment = False
    if python is None:
        blockers.append("independent historical Python environment not provided")
    elif requirements:
        try:
            env = _environment(python, requirements, candidate)
            verified_environment = env["all_satisfied"]
            if not verified_environment:
                blockers.append("installed historical dependencies are incompatible")
        except (OSError, ValueError):
            blockers.append("independent historical virtualenv could not be verified")
    # Crucially: no old reader/imports are run and no host deployment is tested.
    return {
        "static_contracts_passed": not any(
            x.startswith(("historical Python", "historical systemd",
                          "historical dependency input")) for x in blockers),
        "dependency_environment_verified": verified_environment,
        "offline_checks_passed": not blockers,
        "blockers": sorted(set(blockers)),
        "old_state_reader_tested": False,
        "host_services_tested": False,
        "ready_for_live_use": False,
        "activation_performed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--python", type=Path,
                        help="Independent historical virtualenv's bin/python; no installs")
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        parser.error("Run as an unprivileged operator, not root")
    try:
        report = audit(args.candidate, python=args.python)
    except (OSError, ValueError, RecursionError, subprocess.SubprocessError):
        print("Offline historical compatibility audit failed; inspect the private "
              "candidate and virtualenv locally", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0 if report["offline_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
