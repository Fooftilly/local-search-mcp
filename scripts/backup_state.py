#!/usr/bin/env python3
"""Private offline backup, verification, and staged restoration of local MCP state.

Never copies launcher credentials, updates software, or overwrites live state.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
from urllib.parse import quote
import zipfile
import zlib

VERSION = 1
DB_NAME = "domains.sqlite3"
HISTORY_NAMES = ("history.jsonl", "history.jsonl.1", "history.jsonl.2", "history.jsonl.3")
# Nonsecret files are explicit schemas with bounded integers only. Never
# archive config.json or an arbitrary JSON file from the private config dir.
SETTINGS_SCHEMAS = {
    "history-settings.json": {
        "local-search-mcp/nonsecret-history/v1": {
            "retention_days": (1, 365), "max_bytes": (64_000, 100_000_000),
        },
    },
    "upstream-timeouts.json": {
        "local-search-mcp/upstream-timeouts/v1": {
            "searxng_seconds": (5, 120), "crawl_seconds": (15, 300),
        },
    },
    # Optional cache lifetime preferences, introduced separately by issue #13.
    # The backup CLI validates this schema without importing the UI module.
    "cache-ttl.json": {
        "local-search-mcp/cache-ttl/v1": {
            "search_seconds": (5, 900), "fetch_seconds": (5, 1800),
        },
    },
    # A staged backup may preserve the opted-in idle preference, but restore
    # never activates the supervisor or copies it into a live installation.
    # No credentials, Docker identities, or compose paths are in this schema.
    "on-demand.json": {
        "local-search-mcp/on-demand/v1": {
            "enabled": None, "idle_minutes": (1, 1440),
        },
    },
}
SETTINGS_NAMES = tuple(SETTINGS_SCHEMAS)
MAX_SETTINGS_FILE = 1024
ALLOWED_FILES = (DB_NAME, *HISTORY_NAMES, *SETTINGS_NAMES)
# history_log.append_history allows 100,000,000 bytes per generation,
# plus the last bounded record that may cross the rotation threshold.
MAX_DB_FILE = 64 * 1024 * 1024
MAX_HISTORY_FILE = 110_000_000
MAX_TOTAL = (MAX_DB_FILE + len(HISTORY_NAMES) * MAX_HISTORY_FILE
             + len(SETTINGS_NAMES) * MAX_SETTINGS_FILE)
MAX_ARCHIVE = MAX_TOTAL + 8 * 1024 * 1024
MANIFEST = "manifest.json"


def state_directory() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local/state"
    return base / "local-search-mcp"


def settings_directory() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "local-search-mcp"


def _reject_duplicate_json_members(pairs: list[tuple[str, object]]) -> dict:
    """Reject ambiguous JSON keys before preserving any original raw bytes.

    Python's default last-wins parser would otherwise allow a duplicate
    'settings' object to hide credential-bearing content during validation
    while archiving the entire unredacted document.
    """
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate nonsecret settings JSON member")
        result[key] = value
    return result


def _verify_nonsecret_setting(name: str, raw: bytes) -> None:
    """Allow only bounded nonsecret fields, including one exact boolean."""
    if not raw or len(raw) > MAX_SETTINGS_FILE:
        raise ValueError(f"Invalid nonsecret settings size: {name}")
    try:
        doc = json.loads(raw.decode("utf-8"),
                         object_pairs_hook=_reject_duplicate_json_members)
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"Malformed nonsecret settings: {name}") from exc
    allowed = SETTINGS_SCHEMAS[name]
    if not isinstance(doc, dict) or set(doc) != {"schema", "settings"}:
        raise ValueError(f"Unexpected nonsecret settings structure: {name}")
    spec = allowed.get(doc["schema"]) if isinstance(doc["schema"], str) else None
    fields = doc["settings"]
    if spec is None or not isinstance(fields, dict) or set(fields) != set(spec):
        raise ValueError(f"Unsupported or unsafe nonsecret settings: {name}")
    for key, bounds in spec.items():
        value = fields[key]
        if bounds is None:
            # Only the on-demand enabled field can be a boolean. Reject
            # strings and integers, including 0/1, rather than coercing.
            if name != "on-demand.json" or key != "enabled" or type(value) is not bool:
                raise ValueError(f"Invalid nonsecret setting: {key}")
        else:
            low, high = bounds
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"Invalid nonsecret setting: {key}")


def _private_file(path: Path, *, max_size: int = MAX_DB_FILE) -> None:
    if path.is_symlink():
        raise ValueError(f"Refusing symbolic link: {path.name}")
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise ValueError(f"Source must be private owned regular file: {path.name}")
    if info.st_size > max_size:
        raise ValueError(f"Source exceeds size limit: {path.name}")


def _verify_sqlite(contents: bytes) -> None:
    if not contents.startswith(b"SQLite format 3\x00"):
        raise ValueError("Invalid SQLite backup header")
    with tempfile.TemporaryDirectory(prefix="mcp-db-check-") as folder:
        filename = Path(folder) / DB_NAME
        filename.write_bytes(contents)
        filename.chmod(0o600)
        uri = "file:" + quote(str(filename)) + "?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("SQLite integrity check failed")
                if not conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='domains'"
                ).fetchone():
                    raise ValueError("Backup does not contain the domain policy table")
        except sqlite3.DatabaseError as exc:
            raise ValueError("Corrupt SQLite snapshot") from exc


def _sqlite_snapshot(source: Path) -> bytes:
    # SQLite backup API incorporates committed WAL pages without raw WAL copies.
    uri = "file:" + quote(str(source.resolve())) + "?mode=ro"
    with tempfile.TemporaryDirectory(prefix="mcp-db-snapshot-") as folder:
        target = Path(folder) / DB_NAME
        try:
            with closing(sqlite3.connect(uri, uri=True, timeout=10)) as conn:
                with closing(sqlite3.connect(target)) as output:
                    conn.backup(output, pages=256, sleep=0.05)
        except sqlite3.DatabaseError as exc:
            raise ValueError("Unable to snapshot domain policy SQLite") from exc
        if target.stat().st_size > MAX_DB_FILE:
            raise ValueError("Domain policy database exceeds backup limit")
        snapshot = target.read_bytes()
        _verify_sqlite(snapshot)
        return snapshot


def _source_files(directory: Path, *, config_dir: Path | None = None,
                  explicit_config_dir: bool = False) -> dict[str, bytes]:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("State directory is missing or symlinked")
    st = directory.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise ValueError("State directory must be privately owned (0700)")
    source = directory / DB_NAME
    if not source.is_file() or source.is_symlink():
        raise ValueError("A regular domain policy database is required")
    _private_file(source)
    payload: dict[str, bytes] = {DB_NAME: _sqlite_snapshot(source)}
    total = len(payload[DB_NAME])
    # Match history_log.history_path()'s override precedence. Store canonical
    # member names, never the configured absolute path or machine-specific name.
    override = os.environ.get("LOCAL_SEARCH_MCP_HISTORY_FILE")
    history = Path(override).expanduser() if override else directory / "history.jsonl"
    if override and not history.is_absolute():
        raise ValueError("LOCAL_SEARCH_MCP_HISTORY_FILE must be absolute for backups")
    if override:
        parent = history.parent
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError("Configured history directory is missing or symlinked")
        info = parent.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Configured history directory must be privately owned")
        if not history.exists() and not history.is_symlink():
            raise ValueError("Configured history file is missing; refusing partial backup")
    for name in HISTORY_NAMES:
        suffix = name[len("history.jsonl"):]
        path = history.with_name(history.name + suffix)
        if not path.exists() and not path.is_symlink():
            continue
        _private_file(path, max_size=MAX_HISTORY_FILE)
        with path.open("rb") as stream:
            content = stream.read(MAX_HISTORY_FILE + 1)
        if len(content) > MAX_HISTORY_FILE:
            raise ValueError(f"History exceeds file limit: {name}")
        payload[name] = content
        total += len(content)
        if total > MAX_TOTAL:
            raise ValueError("State archive exceeds total limit")
    if config_dir is not None:
        if config_dir.is_symlink():
            raise ValueError("Refusing symlinked settings directory")
        if not config_dir.exists():
            if explicit_config_dir:
                raise ValueError("Explicit settings directory is missing")
        else:
            if not config_dir.is_dir():
                raise ValueError("Settings directory must be a real directory")
            info = config_dir.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("Settings directory must be privately owned (0700)")
            for name in SETTINGS_NAMES:
                path = config_dir / name
                if not path.exists() and not path.is_symlink():
                    continue
                _private_file(path, max_size=MAX_SETTINGS_FILE)
                raw = path.read_bytes()
                _verify_nonsecret_setting(name, raw)
                payload[name] = raw
                total += len(raw)
                if total > MAX_TOTAL:
                    raise ValueError("State archive exceeds total limit")
    return payload


def _manifest(payload: dict[str, bytes]) -> bytes:
    data = {
        "format": "local-search-mcp-state-v1",
        "version": VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": {
            name: {"size": len(contents),
                   "sha256": hashlib.sha256(contents).hexdigest()}
            for name, contents in sorted(payload.items())
        },
    }
    return (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()


def backup(destination: Path, *, state_dir: Path | None = None,
           config_dir: Path | None = None) -> None:
    """Atomically publish a private archive, never replacing existing files.

    Explicit alternate state-dir intentionally does not read the operator's
    default config directory. Pass config-dir to include matching settings.
    """
    if destination.is_symlink() or destination.exists():
        raise ValueError("Backup destination already exists")
    if not destination.parent.is_dir() or destination.parent.is_symlink():
        raise ValueError("Backup parent must be a real existing directory")
    selected_config = (config_dir if config_dir is not None else
                       settings_directory() if state_dir is None else None)
    contents = _source_files(state_dir or state_directory(),
                             config_dir=selected_config,
                             explicit_config_dir=config_dir is not None)
    fd, tmp = tempfile.mkstemp(
        prefix=".mcp-backup-", suffix=".zip", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            with zipfile.ZipFile(
                stream, "w", compression=zipfile.ZIP_DEFLATED,
                compresslevel=6, allowZip64=False,
            ) as archive:
                archive.writestr(MANIFEST, _manifest(contents))
                for name, data in sorted(contents.items()):
                    archive.writestr(name, data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o600)
        # os.link refuses a concurrently created destination; os.replace
        # could silently overwrite it.
        os.link(tmp, destination)
    finally:
        os.unlink(tmp)


def verified_payload(archive_path: Path) -> dict[str, bytes]:
    if archive_path.is_symlink():
        raise ValueError("Refusing symlinked archive")
    _private_file(archive_path, max_size=MAX_ARCHIVE)
    if archive_path.stat().st_size > MAX_ARCHIVE:
        raise ValueError("Archive is too large")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if (len(names) != len(set(names)) or MANIFEST not in names or
                    any(name not in (*ALLOWED_FILES, MANIFEST) for name in names)):
                raise ValueError("Unexpected, unsafe or duplicate archive entries")
            for entry in entries:
                mode = entry.external_attr >> 16
                limit = (1024 * 1024 if entry.filename == MANIFEST else
                         MAX_DB_FILE if entry.filename == DB_NAME else
                         MAX_SETTINGS_FILE if entry.filename in SETTINGS_NAMES else
                         MAX_HISTORY_FILE)
                # Current writer uses DEFLATED; older/test archives may
                # be STORED. Reject any other method before reading members,
                # with the same generic error used for corrupt ZIP streams.
                if entry.compress_type not in (
                        zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise ValueError("Corrupt backup archive")
                if (entry.is_dir() or stat.S_IFMT(mode) == stat.S_IFLNK or
                        entry.file_size > limit or entry.flag_bits & 1):
                    raise ValueError("Unsafe backup entry")
            if sum(e.file_size for e in entries) > MAX_TOTAL + 1024 * 1024:
                raise ValueError("Excessive uncompressed archive size")
            if archive.getinfo(MANIFEST).file_size > 1024 * 1024:
                raise ValueError("Oversize backup manifest")
            metadata = json.loads(archive.read(MANIFEST))
            if (not isinstance(metadata, dict) or
                    set(metadata) != {"format", "version", "created_utc", "files"} or
                    metadata["format"] != "local-search-mcp-state-v1" or
                    metadata["version"] != VERSION or
                    not isinstance(metadata["files"], dict)):
                raise ValueError("Unsupported or malformed backup manifest")
            declared = metadata["files"]
            if set(declared) != set(names) - {MANIFEST} or DB_NAME not in declared:
                raise ValueError("Backup contents do not match manifest")
            payload = {}
            for name, record in declared.items():
                if (name not in ALLOWED_FILES or not isinstance(record, dict) or
                        set(record) != {"size", "sha256"} or
                        type(record["size"]) is not int or
                        not 0 <= record["size"] <= (
                            MAX_DB_FILE if name == DB_NAME else
                            MAX_SETTINGS_FILE if name in SETTINGS_NAMES else
                            MAX_HISTORY_FILE
                        ) or
                        not isinstance(record["sha256"], str)):
                    raise ValueError("Malformed backup file metadata")
                content = archive.read(name)
                if (len(content) != record["size"] or
                        hashlib.sha256(content).hexdigest() != record["sha256"]):
                    raise ValueError(f"Integrity mismatch: {name}")
                if name in SETTINGS_NAMES:
                    _verify_nonsecret_setting(name, content)
                payload[name] = content
            _verify_sqlite(payload[DB_NAME])
            return payload
    except (zipfile.BadZipFile, json.JSONDecodeError, KeyError,
            OverflowError, NotImplementedError, RuntimeError, zlib.error) as exc:
        # Normalize ZIP read/decompress errors for every consumer (wizard,
        # direct CLI, restore smoke, rollback preflight). Never echo data
        # from an attacker-controlled compressed member.
        raise ValueError("Corrupt backup archive") from exc


def restore(archive_path: Path, target_dir: Path) -> None:
    """Write verified data into a new directory only; no live state changes."""
    payload = verified_payload(archive_path)
    if target_dir.is_symlink() or target_dir.exists():
        raise ValueError("Restore target must be a new, nonexistent directory")
    if not target_dir.parent.is_dir() or target_dir.parent.is_symlink():
        raise ValueError("Restore parent must be a real existing directory")
    target_dir.mkdir(mode=0o700)
    try:
        for name, contents in payload.items():
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(target_dir / name, flags, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(contents)
                output.flush()
                os.fsync(output.fileno())
    except BaseException:
        for child in target_dir.iterdir():
            child.unlink()
        target_dir.rmdir()
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="action", required=True)
    create = modes.add_parser("backup", help="Create a private, no-clobber archive")
    create.add_argument("archive", type=Path)
    create.add_argument("--state-dir", type=Path, help="Override source state directory")
    create.add_argument("--config-dir", type=Path,
                        help="Explicit private nonsecret settings directory (for --state-dir)")
    check = modes.add_parser("verify", help="Verify hashes and SQLite integrity")
    check.add_argument("archive", type=Path)
    stage = modes.add_parser("restore", help="Restore to a new staging directory")
    stage.add_argument("archive", type=Path)
    stage.add_argument("--target-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "backup":
            backup(args.archive, state_dir=args.state_dir, config_dir=args.config_dir)
            print("Private state archive created (contains sensitive approvals/history)")
        elif args.action == "verify":
            data = verified_payload(args.archive)
            print(f"Verified {len(data)} backup files with SQLite integrity and SHA-256")
        else:
            restore(args.archive, args.target_dir)
            print("Restored to new directory only; live state unchanged")
        return 0
    except (OSError, ValueError, sqlite3.DatabaseError) as exc:
        # Archive validation errors may contain user-chosen source paths,
        # history filenames, or private SQLite details. Never echo their
        # message from the CLI. The exception category is safe and useful.
        print(f"State {args.action} failed ({type(exc).__name__}); "
              "inspect the relevant private input and destination permissions locally",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
