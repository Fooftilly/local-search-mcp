#!/usr/bin/env python3
"""Smoke-test an offline backup in an isolated temporary fresh-install state.

This never changes live state, performs network operations, or restores files
to their real application location.
"""
from __future__ import annotations

import argparse
import json
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile

# Support running as a script or importing for tests.
if __package__:
    from . import backup_state
else:
    # Direct script invocation otherwise puts only scripts/ on sys.path,
    # hiding the application's policy and history modules.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import backup_state


@contextmanager
def _isolated_environment(home: Path):
    """Use a new XDG state root; never inherit a live custom history path."""
    # Never consult the operator's real configuration, including timeout
    # overrides, when validating files restored from an archive.
    names = (
        "XDG_STATE_HOME", "XDG_CONFIG_HOME", "ALLOWED_CRAWL_DOMAINS",
        "LOCAL_SEARCH_MCP_HISTORY_FILE",
        "LOCAL_SEARCH_MCP_HISTORY_RETENTION_DAYS",
        "LOCAL_SEARCH_MCP_HISTORY_MAX_BYTES",
        "LOCAL_SEARCH_MCP_SEARXNG_TIMEOUT",
        "LOCAL_SEARCH_MCP_CRAWL_TIMEOUT",
        "LOCAL_SEARCH_MCP_CACHE_SEARCH_TTL",
        "LOCAL_SEARCH_MCP_CACHE_FETCH_TTL",
    )
    original = {key: os.environ.get(key) for key in names}
    try:
        os.environ["XDG_STATE_HOME"] = str(home / "state")
        os.environ["XDG_CONFIG_HOME"] = str(home / "config")
        for key in names[3:]:
            os.environ.pop(key, None)
        os.environ["ALLOWED_CRAWL_DOMAINS"] = ""
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value



def _verify_staged_copy(path: Path, original: bytes) -> None:
    """Verify exact bytes and owner-only permissions before app readers run."""
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size != len(original)):
        raise ValueError("Staged recovery file has incorrect type, owner, permissions or size")
    with path.open("rb") as restored:
        actual_hash = hashlib.file_digest(restored, "sha256").digest()
    if actual_hash != hashlib.sha256(original).digest():
        raise ValueError("Staged recovery bytes differ from validated archive")


def smoke(archive: Path) -> tuple[int, int]:
    """Load an archive through the actual policy and history modules, offline.

    Read-only to the input archive; all initialized state and indexes are
    created inside a temporary directory that is removed after the smoke test.
    """
    payload = backup_state.verified_payload(archive)
    with tempfile.TemporaryDirectory(prefix="mcp-fresh-restore-") as tmp:
        home = Path(tmp)
        state = home / "state" / "local-search-mcp"
        config = home / "config" / "local-search-mcp"
        state.mkdir(parents=True, mode=0o700)
        for name, data in payload.items():
            if name not in backup_state.ALLOWED_FILES:
                raise ValueError("Unrecognized backup file")
            # Backup archive members are flattened; settings belong to
            # XDG_CONFIG_HOME, not the XDG_STATE_HOME directory.
            destination = config if name in backup_state.SETTINGS_NAMES else state
            destination.mkdir(parents=True, mode=0o700, exist_ok=True)
            file = destination / name
            fd = os.open(file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            _verify_staged_copy(file, data)

        with _isolated_environment(home):
            # Runtime modules use XDG_STATE_HOME dynamically. Import only after
            # isolating the environment, so no live user state is accessed.
            import domain_policy
            import history_index
            import safe_settings
            import upstream_settings
            import cache_settings
            from scripts import on_demand_settings

            # Verify the files using real application readers, not just the
            # backup schema verifier. Missing optional files are valid.
            for name, reader in (
                ("history-settings.json", safe_settings.dashboard_settings),
                ("upstream-timeouts.json", upstream_settings.dashboard_settings),
                ("cache-ttl.json", cache_settings.dashboard_settings),
            ):
                if name in payload:
                    expected = json.loads(payload[name].decode("utf-8"))["settings"]
                    if reader()["values"] != expected:
                        raise ValueError("Recovered settings differ from archive")

            if "on-demand.json" in payload:
                expected = json.loads(payload["on-demand.json"].decode("utf-8"))["settings"]
                # Call only the inert settings reader. This smoke must not
                # instantiate Supervisor or initiate Docker / guard activity.
                actual = on_demand_settings.read()
                if actual != expected:
                    raise ValueError("Recovered on-demand preferences differ from archive")

            domain_policy.initialize()
            with sqlite3.connect(domain_policy.database_path()) as conn:
                policy_count = conn.execute(
                    "SELECT count(*) FROM domains"
                ).fetchone()[0]
            # The sidecar was intentionally not backed up. Rebuild it from
            # JSONL in a fresh install to confirm the real indexer works.
            history_index.sync_history()
            records = history_index.search_history(limit=25)
            index = history_index.index_path()
            if not index.is_file() or index.is_symlink():
                raise ValueError("Fresh history index was not generated")
            if stat.S_IMODE(index.stat().st_mode) != 0o600:
                raise ValueError("Fresh history index must be private")
            return policy_count, len(records)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="Private archive to smoke-test")
    args = parser.parse_args(argv)
    try:
        policies, recent = smoke(args.archive)
    except (OSError, ValueError, sqlite3.DatabaseError) as exc:
        print(f"Fresh-install smoke check failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(
        f"Fresh-install smoke check passed: {policies} policy entries; "
        f"{recent} most recent indexed events (restored or default retention). "
        "No live state changed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
