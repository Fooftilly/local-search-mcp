#!/usr/bin/env python3
"""Prepare an offline, isolated rollback candidate; NEVER activate live rollback.

After the existing strict read-only rollback preflight, validate state through
the current application's fresh-install smoke test. Copy the exact local
ancestor's tracked raw Git blobs into a NEW private directory and stage a verified state restore. No old code runs, no services
restart, and the current Git HEAD/index, secrets and live files stay intact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tempfile

if __package__:
    from . import backup_state, rollback_preflight, restore_smoke
    from . import restart_on_update as update
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import backup_state, rollback_preflight, restore_smoke
    from scripts import restart_on_update as update

ROOT = Path(__file__).resolve().parents[1]
MAX_TOTAL_SOURCE = 128 * 1024 * 1024
MAX_FILES = 4000
# Bound even pathological Git trees reusing empty subtrees (no blobs).
MAX_TREE_VISITS = 512
MAX_TREE_ENTRIES = MAX_FILES + MAX_TREE_VISITS
MAX_INDIVIDUAL = 32 * 1024 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _private_parent(path: Path) -> None:
    parent = path.parent
    info = parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError("Rollback candidate parent must be owned and private (0700)")


def _valid_member(name: str) -> tuple[str, ...]:
    if type(name) is not str or len(name) > 1024:
        raise ValueError("Invalid archived source name")
    rel = PurePosixPath(name)
    parts = rel.parts
    if (not parts or rel.is_absolute() or any(
            p in ("", ".", "..") or "\\" in p or "\x00" in p for p in parts)
            or name != "/".join(parts)):
        raise ValueError("Unsafe archive member path")
    return parts


_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_HEADER = re.compile(rb"([0-9a-f]{40}|[0-9a-f]{64}) (commit|tree|blob) ([0-9]{1,10})")


def _object(root: Path, oid: str, expected_type: str, limit: int) -> bytes:
    """Read exactly one raw Git object via fixed argv, without filters or hooks.

    Object identifiers travel over stdin, never in a command-line argument.
    The bounded batch-check runs BEFORE requesting any object's bytes.
    """
    if not isinstance(oid, str) or not _OID.fullmatch(oid):
        raise ValueError("Invalid local Git object identifier")
    if expected_type not in ("commit", "tree", "blob"):
        raise ValueError("Unsupported local Git object type")
    env = update._inspection_env()
    env["GIT_NO_REPLACE_OBJECTS"] = "1"

    def run(flag: str) -> bytes:
        try:
            result = subprocess.run(
                ["git", "cat-file", flag], cwd=root, capture_output=True,
                input=(oid + "\n").encode("ascii"), check=False,
                env=env, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Local Git object lookup failed") from exc
        if result.returncode:
            raise ValueError("Required local Git object unavailable")
        return result.stdout

    header = run("--batch-check").rstrip(b"\n")
    match = _HEADER.fullmatch(header)
    if (match is None or match.group(1).decode("ascii") != oid
            or match.group(2).decode("ascii") != expected_type):
        raise ValueError("Unexpected local Git object metadata")
    size = int(match.group(3))
    if size > limit:
        raise ValueError("Tracked Git object exceeds size limit")
    output = run("--batch")
    actual_header, sep, remaining = output.partition(b"\n")
    if sep != b"\n" or actual_header != header:
        raise ValueError("Git object changed between metadata and read")
    if len(remaining) != size + 1 or not remaining.endswith(b"\n"):
        raise ValueError("Incomplete local Git object")
    return remaining[:-1]


def _parse_tree(contents: bytes, hash_bytes: int) -> list[tuple[str, str, str, bytes]]:
    """Decode Git tree objects, refusing links and unsafe path components."""
    items = []
    position = 0
    while position < len(contents):
        end = contents.find(b"\0", position)
        if end < 0:
            raise ValueError("Malformed local Git tree")
        prefix = contents[position:end]
        mode, separator, name_bytes = prefix.partition(b" ")
        if separator != b" " or not name_bytes:
            raise ValueError("Invalid Git tree entry")
        try:
            name = name_bytes.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ValueError("Invalid tracked source filename") from exc
        _valid_member(name)
        if "/" in name or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise ValueError("Invalid tracked source path component")
        kind = "tree" if mode in (b"40000", b"040000") else (
            "blob" if mode in (b"100644", b"100755") else None)
        if kind is None:
            # Symlinks, gitlinks and unknown executable types are forbidden.
            raise ValueError("Unsupported tracked Git entry type")
        first = end + 1
        last = first + hash_bytes
        if last > len(contents):
            raise ValueError("Incomplete Git tree digest")
        items.append((name, kind, contents[first:last].hex(), mode))
        position = last
    return items


def _copy_git_blobs(root: Path, target: str, destination: Path) -> int:
    """Copy literal tracked blobs without Git archive, filters or worktree edits."""
    commit = _object(root, target, "commit", 256_000)
    match = re.search(rb"^tree ([0-9a-f]{40}|[0-9a-f]{64})\n", commit, re.M)
    if match is None:
        raise ValueError("Commit lacks valid tree identifier")
    tree_oid = match.group(1).decode("ascii")
    hash_bytes = len(tree_oid) // 2
    count = 0
    total = 0
    tree_visits = 0
    examined_entries = 0

    def walk(tree: str, path: tuple[str, ...], depth: int) -> None:
        nonlocal count, total, tree_visits, examined_entries
        tree_visits += 1
        if tree_visits > MAX_TREE_VISITS:
            raise ValueError("Too many Git tree visits in rollback source")
        if depth > 24:
            raise ValueError("Tracked source tree is too deep")
        tree_bytes = _object(root, tree, "tree", 512_000)
        entries = _parse_tree(tree_bytes, hash_bytes)
        seen = set()
        for name, kind, oid, mode in entries:
            if name in seen:
                raise ValueError("Duplicate tracked source entry")
            seen.add(name)
            examined_entries += 1
            if examined_entries > MAX_TREE_ENTRIES:
                raise ValueError("Too many tracked Git tree entries")
            parts = (*path, name)
            if kind == "tree":
                walk(oid, parts, depth + 1)
                continue
            count += 1
            if count > MAX_FILES:
                raise ValueError("Too many tracked source files")
            data = _object(root, oid, "blob", MAX_INDIVIDUAL)
            total += len(data)
            if total > MAX_TOTAL_SOURCE:
                raise ValueError("Tracked source exceeds total size limit")
            if len("source/" + "/".join(parts)) > 1024:
                raise ValueError("Tracked source path is too long")
            output_path = destination.joinpath(*parts)
            parent = destination
            for component in parts[:-1]:
                parent = parent / component
                if not parent.exists() and not parent.is_symlink():
                    parent.mkdir(mode=0o700)
                info = parent.lstat()
                if (not stat.S_ISDIR(info.st_mode)
                        or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o700):
                    raise ValueError("Unsafe rollback source parent directory")
            fd = os.open(output_path,
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                         0o700 if mode == b"100755" else 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())

    walk(tree_oid, (), 0)
    return count



def _inventory(destination: Path) -> dict[str, str]:
    """Hash private staged files without following any source or state links.

    This is an integrity check of our staging result, NOT a proof that old
    dependencies, service units or state formats are safe to activate.
    """
    result: dict[str, str] = {}
    visits = 0
    for section in ("source", "state"):
        root = destination / section
        pending = [root]
        count = 0
        total = 0
        while pending:
            folder = pending.pop()
            info = folder.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077):
                raise ValueError("Rollback candidate has an unsafe directory")
            visits += 1
            if visits > MAX_TREE_ENTRIES:
                raise ValueError("Rollback candidate has too many entries")
            for path in folder.iterdir():
                parts = path.relative_to(destination).parts
                _valid_member("/".join(parts))
                if len(parts) > 26:
                    raise ValueError("Rollback candidate path is too deep")
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode) and section == "source":
                    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                        raise ValueError("Rollback candidate has unsafe directory permissions")
                    result["/".join(parts) + "/"] = "D:0700"
                    pending.append(path)
                    continue
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077 or info.st_nlink != 1):
                    raise ValueError("Rollback candidate contains an unsafe file")
                if section == "state" and (len(parts) != 2
                                           or parts[1] not in backup_state.ALLOWED_FILES):
                    raise ValueError("Rollback candidate has an unexpected state file")
                limit = (MAX_INDIVIDUAL if section == "source" else
                         backup_state.MAX_DB_FILE if parts[1] == backup_state.DB_NAME else
                         backup_state.MAX_SETTINGS_FILE
                         if parts[1] in backup_state.SETTINGS_NAMES else
                         backup_state.MAX_HISTORY_FILE)
                if info.st_size > limit:
                    raise ValueError("Rollback candidate file exceeds size limit")
                count += 1
                total += info.st_size
                if (count > (MAX_FILES if section == "source" else
                             len(backup_state.ALLOWED_FILES))
                        or total > (MAX_TOTAL_SOURCE if section == "source"
                                   else backup_state.MAX_TOTAL)):
                    raise ValueError("Rollback candidate exceeds size limits")
                fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
                with os.fdopen(fd, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(opened.st_mode)
                            or opened.st_uid != os.getuid()
                            or opened.st_mode & 0o077 or opened.st_nlink != 1
                            or (opened.st_dev, opened.st_ino, opened.st_size)
                            != (info.st_dev, info.st_ino, info.st_size)):
                        raise ValueError("Rollback candidate changed during inspection")
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    # Validate sensitive state via this exact verified file
                    # descriptor; never reopen a pathname after hashing.
                    if section == "state" and (
                            parts[1] == backup_state.DB_NAME
                            or parts[1] in backup_state.SETTINGS_NAMES):
                        stream.seek(0)
                        content = stream.read(limit + 1)
                        if len(content) > limit:
                            raise ValueError("Rollback state exceeds size limit")
                        if parts[1] == backup_state.DB_NAME:
                            backup_state._verify_sqlite(content)
                        else:
                            backup_state._verify_nonsecret_setting(parts[1], content)
                    after = os.fstat(stream.fileno())
                    if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                            opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns):
                        raise ValueError("Rollback candidate changed during hashing")
                result["/".join(parts)] = (
                    "F:" + format(stat.S_IMODE(opened.st_mode), "04o") + ":" + digest)
        if section == "source" and count < 1:
            raise ValueError("Rollback candidate contains no source")
        if section == "state" and "state/domains.sqlite3" not in result:
            raise ValueError("Rollback candidate is missing policy state")
    return dict(sorted(result.items()))


def inspect(destination: Path) -> dict[str, object]:
    """Read-only candidate integrity audit, never executing staged code."""
    info = destination.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise ValueError("Rollback candidate must be an owned private directory")
    if {p.name for p in destination.iterdir()} != {
            "source", "state", "candidate.json"}:
        raise ValueError("Rollback candidate has unexpected entries")
    manifest = destination / "candidate.json"
    candidate_info = manifest.lstat()
    if (not stat.S_ISREG(candidate_info.st_mode)
            or candidate_info.st_uid != os.getuid()
            or candidate_info.st_mode & 0o077 or candidate_info.st_nlink != 1
            or candidate_info.st_size > 1_000_000):
        raise ValueError("Rollback candidate manifest must be a private regular file")
    fd = os.open(manifest, os.O_RDONLY | _NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if os.fstat(stream.fileno()).st_ino != candidate_info.st_ino:
            raise ValueError("Rollback candidate manifest changed")
        raw = stream.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError("Rollback candidate manifest is too large")
    try:
        meta = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Rollback candidate manifest is malformed") from exc
    expected = {"schema", "source_commit", "target_commit", "source_files",
                "state_files", "activation_performed", "ready_for_live_use",
                "inventory"}
    if (type(meta) is not dict or set(meta) != expected
            or meta["schema"] != "local-search-mcp/rollback-candidate/v2"
            or type(meta["source_files"]) is not int
            or type(meta["state_files"]) is not int
            or meta["activation_performed"] is not False
            or meta["ready_for_live_use"] is not False
            or any(not isinstance(meta[k], str) or not _OID.fullmatch(meta[k])
                   for k in ("source_commit", "target_commit"))
            or type(meta["inventory"]) is not dict):
        raise ValueError("Rollback candidate manifest has unsupported fields")
    inventory = _inventory(destination)
    if meta["inventory"] != inventory:
        raise ValueError("Rollback candidate files changed since staging")
    if (sum(k.startswith("source/") and value.startswith("F:")
            for k, value in inventory.items()) != meta["source_files"]
            or sum(k.startswith("state/") and value.startswith("F:")
                   for k, value in inventory.items()) != meta["state_files"]):
        raise ValueError("Rollback candidate file counts differ")
    # The exact validated descriptors are checked inside _inventory.
    # Never reopen a staged pathname after the symlink/owner checks.
    return {
        "source_files": meta["source_files"],
        "state_files": meta["state_files"],
        "integrity_verified": True,
        "activation_performed": False,
        "ready_for_live_use": False,
        "reason": "Source dependencies, historical settings and service cutover "
                  "require separate offline and host validation",
    }


def prepare(target: str, archive: Path, destination: Path,
            *, root: Path = ROOT) -> dict:
    """Prepare data only; destination and all live installation paths stay separate."""
    rollback_preflight.preflight(target, archive, root=root)
    restore_smoke.smoke(archive)  # current readers in an isolated temp install
    if (destination.is_symlink() or destination.exists()
            or destination == root or destination == archive.parent):
        raise ValueError("Rollback candidate destination must not exist")
    _private_parent(destination)
    source_head = update.head(root, read_only=True)
    destination.mkdir(mode=0o700)
    try:
        code = destination / "source"
        code.mkdir(mode=0o700)
        files = _copy_git_blobs(root, target, code)
        if files < 1:
            raise ValueError("Target Git tree contains no regular source files")
        backup_state.restore(archive, destination / "state")
        if (update.head(root, read_only=True) != source_head or
                update.busy(root, read_only=True)
                or not rollback_preflight._clean_checkout(root)):
            raise ValueError("Source checkout changed while preparing rollback")
        meta = {"schema": "local-search-mcp/rollback-candidate/v2",
                "source_commit": source_head, "target_commit": target,
                "source_files": files,
                "state_files": len(backup_state.verified_payload(archive)),
                "activation_performed": False,
                "ready_for_live_use": False,
                "inventory": _inventory(destination)}
        data = (json.dumps(meta, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if len(data) > 1_000_000:
            raise ValueError("Rollback candidate inventory exceeds manifest limit")
        fd = os.open(destination / "candidate.json",
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        return meta
    except BaseException:
        # Do not follow symlinks, and never inspect any operator path outside
        # the one just created. This tree is not reachable by runtime services.
        import shutil
        shutil.rmtree(destination)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True,
                        help="Exact full SHA for an available local ancestor commit")
    parser.add_argument("--archive", type=Path, required=True,
                        help="Verified private state backup from current installation")
    parser.add_argument("--destination", type=Path,
                        help="New path under an existing owner-only private directory")
    parser.add_argument("--confirm", choices=("STAGE",),
                        help="Required to create a local private candidate")
    args = parser.parse_args(argv)
    try:
        if args.destination is None:
            print(rollback_preflight.preflight(args.target, args.archive))
            restore_smoke.smoke(args.archive)
            print("Read-only compatibility smoke passed under current readers. "
                  "No candidate staged and no live rollback performed.")
        else:
            if args.confirm != "STAGE":
                raise ValueError("Type --confirm STAGE to create a candidate")
            report = prepare(args.target, args.archive, args.destination)
            # The full private inventory belongs only in candidate.json.
            # Do not expose tracked source filenames, file hashes or paths
            # through terminal logs / automated agent transcripts.
            summary = {name: report[name] for name in (
                "schema", "source_files", "state_files",
                "activation_performed", "ready_for_live_use")}
            print(json.dumps(summary, sort_keys=True))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("Rollback candidate not prepared: " + type(exc).__name__,
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
