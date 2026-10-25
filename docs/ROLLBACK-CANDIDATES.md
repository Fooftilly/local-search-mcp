# Offline rollback candidate integrity

The rollback candidate is a **staged rehearsal**, not an installable release.
It never switches Git HEAD, replaces state in the current installation,
downloads dependencies, modifies Docker, restarts systemd or changes tunnel
credentials. A validated archive may contain private domain approvals and
search/browsing history: keep both the archive and the staged candidate
under owner-only directories (0700) and never upload them to issue comments.

## Stage and verify (operator initiated, offline)

Create an owner-only staging parent and an existing owner-only private backup.
Select an **exact full ancestor commit SHA available locally**; the source
checkout must be clean. For example:

\`\`\`sh
mkdir -p -m 700 "$HOME/.local/state/local-search-mcp-rollback"
python3 scripts/rollback_preflight.py --target "$OLD_COMMIT" --archive "$BACKUP"
python3 scripts/rollback_candidate.py --target "$OLD_COMMIT" --archive "$BACKUP" \
    --destination "$HOME/.local/state/local-search-mcp-rollback/candidate-1" --confirm STAGE
python3 scripts/verify_rollback_candidate.py \
    "$HOME/.local/state/local-search-mcp-rollback/candidate-1"
\`\`\`

The staging command builds a private copy of **raw locally available Git
objects** and verified state, never runs older code, and records each file's
SHA-256 digest in the versioned, private \`candidate.json\` manifest.
The verification command checks private ownership and modes, symlink and
hardlink rejection, allowed staged state names, per-file digests, SQLite
integrity, nonsecret settings schemas and unexpected files. It is read-only.
The manifest is not a digital signature: a party with access to the owner
account and the manifest could alter both files and checksums.

Old v1 candidates have no digest inventory and must be **restaged** using
the v2 format. Do not relabel or edit the metadata to bypass validation.
An integrity success does **not** establish that historical application
code, Python package versions, network/egress guard, Docker image,
systemd unit configuration or credentials are compatible. In particular,
\`ready_for_live_use\` remains \`false\`; no command in this workflow
performs live rollback.

## Rollback/recovery conditions

Before attempting any live change, review target-source dependency manifests,
the active virtualenv, any schema changes, installed systemd unit references,
tunnel profile and crawler egress protection. Freeze new MCP operations.
Stop/disable the update watcher and the project's bridge/tunnel only after
verifying unit identity and obtaining an independent, intact recovery backup.
Never stop shared Docker projects, modify a root-owned firewall rule, or
overwrite live state from a staged candidate without a separately reviewed
and tested installation-specific cutover plan.

For recovery from a failed upgrade, the safe supported baseline is to retain
the previous code revision and its private state archive, validate both
offline, and require operator-controlled service and state recovery.
No unattended update, automatic reset, checkout, migration or restart
is supplied here. A functioning live rollback and host-level recovery drill
are still necessary before issue #15 can be considered complete.
