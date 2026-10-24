#!/usr/bin/env python3
"""Consolidated maintenance version report; never downloads or applies updates."""
from __future__ import annotations

import argparse
import http.client
import json
from pathlib import Path
import re
import subprocess
import sys

from scripts import (
    check_bridge_version, check_crawler_versions, check_tunnel_version,
    setup_wizard, update_preflight,
)

ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAMES = ("fastmcp", "httpx", "idna")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,79}\Z")
_MAX_OUTPUT = 8192


def _unavailable(reason: str) -> dict:
    return {"state": "unavailable", "note": reason}


def _dependency_versions(check_remote: bool) -> dict:
    """Reuse the project interpreter and fixed allowlist, never system packages."""
    if not check_remote:
        return setup_wizard.dependency_report()
    python = setup_wizard.project_python()
    if python is None:
        return _unavailable("Project .venv Python is not installed")
    try:
        completed = subprocess.run(
            [str(python), "-I", "-B",
             str(ROOT / "scripts" / "check_dependency_versions.py"),
             "--check-remote"],
            cwd=ROOT, capture_output=True, text=True, timeout=35, check=False,
        )
        if len(completed.stdout) > _MAX_OUTPUT:
            raise ValueError("Oversized dependency report")
        data = json.loads(completed.stdout)
        if type(data) is not dict:
            raise ValueError("Unexpected dependency report structure")
        rows = data.get("packages")
        if (data.get("remote_requested") is not True or type(rows) is not list
                or len(rows) != len(_PACKAGE_NAMES)):
            raise ValueError("Unexpected dependency report")
        sanitized = []
        for name, row in zip(_PACKAGE_NAMES, rows):
            if (type(row) is not dict or row.get("package") != name
                    or set(row) - {"package", "installed", "pypi", "same_version", "error"}):
                raise ValueError("Unexpected dependency row")
            cleaned = {"package": name}
            for key in ("installed", "pypi"):
                value = row.get(key)
                if value is not None:
                    if type(value) is not str or not _VERSION.fullmatch(value):
                        raise ValueError("Invalid package version")
                cleaned[key] = value
            for key in ("same_version",):
                value = row.get(key)
                if value is not None and type(value) is not bool:
                    raise ValueError("Invalid comparison")
                if key in row:
                    cleaned[key] = value
            if "error" in row:
                # Replace any future source-error text with a stable message.
                cleaned["error"] = "PyPI lookup unavailable or invalid"
            sanitized.append(cleaned)
        return {
            "state": "available" if completed.returncode == 0 else "partial",
            "packages": sanitized,
            "remote_requested": True,
        }
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return _unavailable("Dependency version inspection unavailable")


def report(*, check_remote: bool = False) -> dict:
    """Use existing read-only checkers, returning sanitized partial failures."""
    result = {
        "schema": "local-search-mcp/maintenance-versions/v1",
        "mode": "read-only", "remote_requested": check_remote,
        "updates_applied": False, "downloads_performed": False,
        "dependencies": _dependency_versions(check_remote),
        "local_git": update_preflight.report(),
    }
    try:
        result["crawler"] = check_crawler_versions.version_report(
            check_remote=check_remote)
    except (OSError, ValueError, TypeError):
        result["crawler"] = _unavailable("Crawler version inspection unavailable")
    try:
        result["tunnel_client"] = check_tunnel_version.version_report(
            check_remote=check_remote)
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        result["tunnel_client"] = _unavailable("Tunnel client inspection unavailable")
    if check_remote:
        try:
            result["public_bridge_revision"] = {
                "state": "checked", "summary": check_bridge_version.check()}
        except (OSError, ValueError, subprocess.SubprocessError,
                http.client.HTTPException):
            result["public_bridge_revision"] = _unavailable(
                "Public bridge revision lookup unavailable")
    result["note"] = (
        "Version differences do not establish safe upgrades. Review release notes, "
        "Git ancestry, dependency compatibility, security and backups manually. "
        "No packages, Git objects, images, or services were changed."
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-remote", action="store_true",
                        help="explicitly query public PyPI and GitHub; no installs")
    args = parser.parse_args(argv)
    print(json.dumps(report(check_remote=args.check_remote), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
