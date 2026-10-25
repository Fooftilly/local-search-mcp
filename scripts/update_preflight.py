"""Read-only local Git revision inspection for the attended setup updater.

No watcher baseline, Git network access, service action or tracked-file scan.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
_SHA = re.compile(r"[0-9a-f]{40,64}\\Z")


def local_revision(root: Path | None = None) -> str:
    root = root or ROOT
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("GIT_")}
    environment.update({"GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_CONFIG_GLOBAL": "/dev/null",
                        "GIT_TERMINAL_PROMPT": "0"})
    def run(*args: str) -> str:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
             "--no-optional-locks", "-C", str(root), *args],
            cwd=root, env=environment, capture_output=True, text=True,
            check=False, timeout=10)
        if result.returncode:
            raise ValueError("Local checkout unavailable")
        return result.stdout.strip()
    if run("rev-parse", "--show-toplevel") != str(root.resolve()):
        raise ValueError("Git repository root mismatch")
    head = run("rev-parse", "--verify", "HEAD^{commit}")
    if not _SHA.fullmatch(head):
        raise ValueError("Unexpected commit identifier")
    return head


def report() -> dict:
    result = {"mode": "read-only", "remote_checked": False,
              "updates_applied": False, "state": "unavailable",
              "local_revision": "Local Git revision unavailable",
              "next_step": "Use ./setup.sh --update for an attended reviewed update"}
    try:
        result["local_revision"] = local_revision()[:12]
        result["state"] = "inspected"
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return result
