#!/usr/bin/env python3
"""Smoke-test an offline backup in an isolated temporary fresh-install state.

This never changes live state, performs network operations, or restores files
to their real application location.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
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
    names = (
        "XDG_STATE_HOME",
        "LOCAL_SEARCH_MCP_HISTORY_FILE",
        "LOCAL_SEARCH_MCP_HISTORY_RETENTION_DAYS",
        "ALLOWED_CRAWL_DOMAINS",
    )
    original = {key: os.environ.get(key) for key in names}
    try:
        os.environ["XDG_STATE_HOME"] = str(home)
        os.environ.pop("LOCAL_SEARCH_MCP_HISTORY_FILE", None)
        os.environ["LOCAL_SEARCH_MCP_HISTORY_RETENTION_DAYS"] = "30"
        os.environ["ALLOWED_CRAWL_DOMAINS"] = ""
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def smoke(archive: Path) -> tuple[int, int]:
    """Load an archive through the actual policy and history modules, offline.

    Read-only to the input archive; all initialized state and indexes are
    created inside a temporary directory that is removed after the smoke test.
    """
    payload = backup_state.verified_payload(archive)
    with tempfile.TemporaryDirectory(prefix="mcp-fresh-restore-") as tmp:
        home = Path(tmp)
        state = home / "local-search-mcp"
        state.mkdir(mode=0o700)
        for name, data in payload.items():
            if name not in backup_state.ALLOWED_FILES:
                raise ValueError("Unrecognized backup file")
            file = state / name
            fd = os.open(file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            file.chmod(0o600)

        with _isolated_environment(home):
            # Runtime modules use XDG_STATE_HOME dynamically. Import only after
            # isolating the environment, so no live user state is accessed.
            import domain_policy
            import history_index

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
        f"{recent} most recent indexed events (30-day retention). "
        "No live state changed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
