"""Read-only local Git update report for the setup wizard.

Reuses the existing service update watcher's offline inspection; does not
fetch, pull, install, restart, reset baselines or access Git remotes.
"""
from __future__ import annotations

import subprocess


def report() -> dict:
    result = {
        "mode": "read-only",
        "remote_checked": False,
        "updates_applied": False,
        "state": "unknown",
        "local_revision": "not inspected",
        "next_step": (
            "Review docs/DEPENDENCY-UPDATES.md, docs/SYSTEMD.md and BACKUP.md before "
            "any manual update; no Git fetch/pull or service action was run"
        ),
    }
    try:
        from scripts import restart_on_update
        note = restart_on_update.inspect_status()
        if not isinstance(note, str) or len(note) > 1800:
            raise ValueError("Invalid local status")
        # Only recognized messages produced by inspect_status are displayable.
        prefixes = (
            "No local update baseline;",
            "No local commit change since saved baseline;",
            "Git operation or tracked runtime edits in progress;",
            "Required Git objects are unavailable locally;",
            "Git checkout changed during inspection;",
            "Local revision differs from saved baseline ",
        )
        if not note.startswith(prefixes):
            raise ValueError("Unexpected update status")
        result["state"] = "inspected"
        result["local_revision"] = note
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError,
            ImportError):
        # Do not echo arbitrary local paths, Git stderr, credentials or args.
        result["state"] = "unavailable"
        result["local_revision"] = (
            "Cannot safely inspect local Git revision and update baseline; "
            "review local checkout/state permissions manually"
        )
    return result
