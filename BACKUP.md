# Offline state backup, validation, and staged restore

Issue #15 maintenance slice. The backup CLI is **opt-in** and runs entirely locally. It does not contact GitHub, install packages, invoke systemctl, modify services, or overwrite active state.

The archive contains the domain-policy SQLite database (`domains.sqlite3`) and, when present, the active and three rotated JSONL history files. It **never copies** the launcher configuration, API/tunnel tokens, systemd units, `.venv`, or the derived history SQLite index. The history index can be rebuilt from JSONL after restoring. An archive may contain **sensitive search terms and domain decisions**, so treat it as private data even though no credentials are intentionally exported.

## Create and verify a backup

For the most consistent policy and history snapshot, first **stop the local bridge and tunnel yourself**, then run:

```sh
python3 scripts/backup_state.py backup "$HOME/local-search-state-2026.zip"
python3 scripts/backup_state.py verify "$HOME/local-search-state-2026.zip"
```

Backup reads the live SQLite database using SQLite's online backup API (including committed WAL content), and separately copies the JSONL history files. **Cross-file atomicity is not guaranteed while services are writing**. Stop the services first if you need policy/history to represent the same moment. Source permissions must be private; the resulting archive is mode `0600`, is written atomically without replacing an existing archive, and includes a versioned SHA-256 manifest plus SQLite integrity validation. Back up only to local storage that you trust. No archive is uploaded automatically.

For a custom source state directory, pass `--state-dir /absolute/path/to/local-search-mcp` to `backup`. The default is `$XDG_STATE_HOME/local-search-mcp`, or `~/.local/state/local-search-mcp` if unset.

**Custom history locations are supported.** When `LOCAL_SEARCH_MCP_HISTORY_FILE` is set, backup reads the file and its `.1`, `.2`, and `.3` rotations from that location, even if separate from the domain-policy database. The override must be an **absolute path** in a private, user-owned directory. If the configured history file is missing or unsafe, backup **fails** rather than quietly producing an incomplete archive. The archive stores the history under canonical names (`history.jsonl`, etc.), never its source pathname. If you restore to a system using a custom history location, relocate the staged history files to that configured path manually before enabling the application.

The history writer supports up to **100,000,000 bytes per generation** (with up to three rotations). Archive validation accommodates all four full generations and bounded overrun from a final entry, plus the domain-policy SQLite snapshot; individual size caps and a total cap still guard against unexpectedly large files and decompression bombs. A backup archive can thus be several hundred megabytes, so verify sufficient local disk space.

Archives have fixed, bounded file names. Backup and verification reject unsafe symlinks, unexpectedly large contents, corrupt SQLite, archive traversal entries, duplicate names, and hash mismatches. The archive stores no original absolute host paths.

## Fresh-install functional smoke test

After creating and verifying an archive, run:

```sh
python3 scripts/restore_smoke.py "$HOME/local-search-state-2026.zip"
```

The smoke command **never overwrites or modifies live state**. It verifies the
archive, copies only the allowlisted policy and history files into a new private
temporary XDG state directory, initializes the actual domain-policy reader, and
rebuilds the indexed history sidecar using the normal history indexing code.
It removes the temporary directory afterward. Live custom history paths, policy
seeding configuration, and retention overrides are isolated. The result prints
only aggregate policy and recent-event counts, not domain names, search terms,
credentials, or URLs. A missing or corrupt archive fails before initialization.
The smoke test checks application-level readability; it is **not** a live
service restart, rollback, or proof that all historical events remain after
configured retention.

## Restore safely (never overwrites live state)

```sh
python3 scripts/backup_state.py verify "$HOME/local-search-state-2026.zip"
python3 scripts/backup_state.py restore "$HOME/local-search-state-2026.zip" \
  --target-dir "$HOME/local-search-mcp-staged-restore"
```

The target directory **must not already exist**. Restore verifies the complete archive before writing, creates a private `0700` staging directory containing `0600` files, and does not touch your existing state directory, live bridge/tunnel, or credentials. Inspect the restored files and test the application separately before deciding whether to install them. **This CLI intentionally does not automate a live swap, rollback, or destructive replacement**; those steps require a future migration and recovery design. If restoring to a fresh machine, configure tunnel/Crawl4AI credentials separately using the launcher. Restarting services and verifying effective policies after a manual migration are operator tasks.

This is a first maintenance slice, **not completion of issue #15**. Automated live-state restore, rollback of updates and comprehensive fresh-install recovery still need independent testing and design.
