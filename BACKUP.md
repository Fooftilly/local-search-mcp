# Offline state backup, validation, and staged restore

Issue #15 maintenance slice. The backup CLI is **opt-in** and runs entirely locally. It does not contact GitHub, install packages, invoke systemctl, modify services, or overwrite active state.

The archive contains the domain-policy SQLite database (`domains.sqlite3`) and, when present, the active and three rotated JSONL history files. It **never copies** the launcher configuration, API/tunnel tokens, systemd units, `.venv`, or the derived history SQLite index. The history index can be rebuilt from JSONL after restoring. An archive may contain **sensitive search terms and domain decisions**, so treat it as private data even though no credentials are intentionally exported.

The setup wizard also offers **explicit, no-overwrite archive creation**:

```sh
./setup.sh --backup-create "$HOME/local-search-state-2026.zip" --confirm-backup BACKUP
```

Interactive menu option 16 requires typing `BACKUP` before writing.
Both call the same `backup_state.backup` implementation and return only
a generic status, not the archive's contents or pathname. Existing
archives are not replaced. The wizard does **not** stop the bridge or
guarantee a cross-file consistent snapshot while writers are active;
stop services manually before backup if consistency is required.
On failure, inspect the destination locally before retrying because
an archive may already have been published.

## Create and verify a backup

For the most consistent policy and history snapshot, first **stop the local bridge and tunnel yourself**, then run:

```sh
python3 scripts/backup_state.py backup "$HOME/local-search-state-2026.zip"
python3 scripts/backup_state.py verify "$HOME/local-search-state-2026.zip"
```

The direct `scripts/backup_state.py` CLI reports a generic error category on failure without printing source paths, history contents, domain decisions or underlying exception messages. A failure requires a local check of ownership, archive integrity and permissions; it never authorizes overwriting an existing destination.

Backup reads the live SQLite database using SQLite's online backup API (including committed WAL content), and separately copies the JSONL history files. **Cross-file atomicity is not guaranteed while services are writing**. Stop the services first if you need policy/history to represent the same moment. Source permissions must be private; the resulting archive is mode `0600`, is written atomically without replacing an existing archive, and includes a versioned SHA-256 manifest plus SQLite integrity validation. Back up only to local storage that you trust. No archive is uploaded automatically.

For a custom source state directory, pass `--state-dir /absolute/path/to/local-search-mcp` to `backup`. The default is `$XDG_STATE_HOME/local-search-mcp`, or `~/.local/state/local-search-mcp` if unset.

**Custom history locations are supported.** When `LOCAL_SEARCH_MCP_HISTORY_FILE` is set, backup reads the file and its `.1`, `.2`, and `.3` rotations from that location, even if separate from the domain-policy database. The override must be an **absolute path** in a private, user-owned directory. If the configured history file is missing or unsafe, backup **fails** rather than quietly producing an incomplete archive. The archive stores the history under canonical names (`history.jsonl`, etc.), never its source pathname. If you restore to a system using a custom history location, relocate the staged history files to that configured path manually before enabling the application.

The history writer supports up to **100,000,000 bytes per generation** (with up to three rotations). Archive validation accommodates all four full generations and bounded overrun from a final entry, plus the domain-policy SQLite snapshot; individual size caps and a total cap still guard against unexpectedly large files and decompression bombs. A backup archive can thus be several hundred megabytes, so verify sufficient local disk space.

Archives have fixed, bounded file names. Backup and verification reject unsafe symlinks, unexpectedly large contents, corrupt SQLite, archive traversal entries, duplicate names, and hash mismatches. ZIP members must use standard stored or DEFLATE compression; other compression methods are rejected before extraction. Decompression failures are normalized into ordinary validation errors for the direct CLI as well as the setup wizard, without returning private compressed-member details or beginning a staged restore. The archive stores no original absolute host paths.

The ChatGPT-first setup wizard also wraps the **same** verification and
isolated smoke routines without duplicating the archive parser:

```sh
./setup.sh --backup-verify "$HOME/local-search-state-2026.zip"
./setup.sh --restore-smoke "$HOME/local-search-state-2026.zip"
```

The first command verifies the existing private archive and returns a
JSON result with status and safety flags plus a `verified_files` count;
the second runs a fresh-install smoke test in disposable temporary
directories and reports the total policy count and **up to 25 recent indexed
history events** (not a total recovered-history count). Both leave the private archive and live installation untouched;
the second creates only temporary scratch state. Menu option 15 offers these
two checks interactively. All failures are redacted, with a nonzero CLI
exit status. Neither command performs a live restore, rolls back code,
starts Docker or downloads updates.

## Fresh-install functional smoke test

After creating and verifying an archive, run:

```sh
python3 scripts/restore_smoke.py "$HOME/local-search-state-2026.zip"
```

Before loading restored data through application readers, the fresh-install smoke
test now verifies each staged file has identical bytes to the validated archive
and retains a user-owned regular file with mode `0600`. This includes rotated
history generations and optional validated settings; any mismatch fails closed.
This verifies exact **staged** bytes and permissions, not historical index
retention (which may legitimately exclude expired records).

The smoke command **never overwrites or modifies live state**. It verifies the
archive, copies only the allowlisted policy and history files into a new private
temporary XDG state directory, initializes the actual domain-policy reader, and
rebuilds the indexed history sidecar using the normal history indexing code.
It removes the temporary directory afterward. Live custom history paths, policy
seeding configuration, and retention overrides are isolated. The result prints
only aggregate policy and recent-event counts, not domain names, search terms,
credentials, or URLs. A missing or corrupt archive fails before initialization.
When an archive includes validated history or upstream timeout settings, the
smoke test also restores them into a **separate temporary XDG config directory**
and checks their values through the actual runtime settings readers. It ignores
the operator's live configuration and environment overrides and never stages
launcher credentials. The smoke test checks application-level readability;
it is **not** a live service restart, rollback, or proof that all historical
events remain after configured retention.

## Restore safely (never overwrites live state)

```sh
python3 scripts/backup_state.py verify "$HOME/local-search-state-2026.zip"
python3 scripts/backup_state.py restore "$HOME/local-search-state-2026.zip" \
  --target-dir "$HOME/local-search-mcp-staged-restore"
```

The target directory **must not already exist**. Restore verifies the complete archive before writing, creates a private `0700` staging directory containing `0600` files, and does not touch your existing state directory, live bridge/tunnel, or credentials. Inspect the restored files and test the application separately before deciding whether to install them. **This CLI intentionally does not automate a live swap, rollback, or destructive replacement**; those steps require a future migration and recovery design. If restoring to a fresh machine, configure tunnel/Crawl4AI credentials separately using the launcher. Restarting services and verifying effective policies after a manual migration are operator tasks.

This is a first maintenance slice, **not completion of issue #15**. Automated live-state restore, rollback of updates and comprehensive fresh-install recovery still need independent testing and design.

## Optional validated nonsecret settings in the archive

The default `backup` command also includes existing **nonsecret** validated
`history-settings.json` and `upstream-timeouts.json`, when present, from
`${XDG_CONFIG_HOME:-$HOME/.config}/local-search-mcp/`. Each is a bounded
versioned document containing only allowlisted integers. The archive verifier
revalidates both schemas and rejects unexpected keys, unsupported versions,
invalid values, unsafe file permissions and symlinks. A malformed settings
file causes the backup to **fail**, rather than silently skip it or export
arbitrary data. The private launcher `config.json`, environment secrets,
tunnel credentials and arbitrary JSON files are never included.

For isolated test or migration backup of another state tree, `--state-dir`
**does not** read the currently logged-in user's configuration directory.
Use an explicit `--config-dir /path/to/private/config-dir` with it to
include verified nonsecret settings. An explicit but missing directory fails.

Verified staged restores place these two files alongside staged state files
rather than installing them under `$XDG_CONFIG_HOME`. After checking the
staging directory, move each file to your private configuration directory
manually only after stopping services and reviewing the running environment.
The command never overwrites live settings, the domain policy DB, or secrets.
Older archives without these optional files remain accepted.

These additions do not implement live rollback, automatically change
installed services, or download software updates; issue #15 remains open.


### Optional cache lifetime preferences (issue #13 integration)

The archive verifier also understands the strictly allowlisted
`cache-ttl.json` schema (two bounded integers). If present in the private
settings directory, backup includes it without exporting cache **contents**
or any URL/query/key. Staged restore preserves the same private-file modes.
Invalid, unsupported or duplicate-key cache settings abort backup and archive verification.
The absence of this optional file remains compatible with existing archives.

This is independent of the settings UI under PR #60: on installations that
have not yet incorporated that feature, a staged cache settings file remains
inert. Neither backup nor smoke installs the file into a live configuration.
The fresh-install smoke check now also loads an archived `cache-ttl.json`
through the real runtime settings reader in its temporary owner-only config
directory, verifies the effective values, and ignores inherited live cache
environment overrides. Missing optional cache settings remain supported.
The test does not copy cache contents, tokens, or launcher credentials.

After adopting the UI, confirm the staged settings with its real reader
before manually enabling services. No automatic upgrade or rollback occurs.

## On-demand idle preferences in offline backups

When the default private config directory contains `on-demand.json`,
backups now include that **nonsecret, versioned** document alongside history,
upstream timeout and cache TTL preferences. For an explicit `--state-dir`,
the caller must also provide `--config-dir` to include any settings;
`--state-dir` alone never reads the operator's default configuration. Only `enabled` (a strict boolean) and
`idle_minutes` (a strict integer from 1 through 1440) are permitted. Unknown
keys, duplicate JSON members, symlinks, unsafe permissions and malformed
fields abort the backup rather than copying possible credentials or an
unsupported configuration.

An archived `enabled: true` is preserved **only as staged data**.
`backup_state.py restore` never installs it into the running config,
and `restore_smoke.py` reads it only inside a disposable isolated
`XDG_CONFIG_HOME`. Neither routine initializes the supervisor or starts
Docker. On a different machine, never activate the restored preference until
its dedicated Docker identity, firewall startup guard, effective protected
tunnel, and post-wake live security probes have been separately validated
with the normal setup and `--boot-audit` workflows. Historical archives
without this optional file remain supported and default to on-demand OFF
on a clean installation.

## Offline rollback preflight (does not perform a rollback)

Before attempting to return to an older *local ancestor commit*, take and
verify an owner-only backup of current state using the instructions above.
The following command confirms that a specified **full Git commit SHA** is
available locally, is an ancestor of HEAD, that the Git checkout is clean,
and that the provided private backup passes integrity/schema checks:

```sh
python3 scripts/rollback_preflight.py --target YOUR_FULL_LOCAL_COMMIT_SHA \
  --archive "$HOME/local-search-state-2026.zip"
```

Preflight is opt-in, **offline**, and **non-mutating to live state**:
Git commands disable optional index writes and partial-clone lazy fetching.
It refuses staged, unstaged or untracked files, a missing/nonancestor target,
an invalid or insecure archive, or a checkout that changes while validating.
The archive verifier temporarily copies SQLite contents for integrity checks;
no restored files are installed. Never paste a shortened commit ID.

Passing preflight **does not mean rollback is safe**. A previous release may
not understand current state/database schemas, dependencies, credentials,
systemd units or tunnel configuration. The tool cannot validate compatibility
with the selected old commit and never stops services, changes the checkout,
resets the restart-watcher baseline, or invokes a rollback. Manually stop the
optional update watcher and affected services; review the Git diff and
dependencies, check version-specific migration/restore requirements, and use
a separately reviewed operator-controlled recovery procedure. Do not
automatically swap a live database based only on this result. Full rollback
implementation remains tracked under issue #15.

## Prepare a reversible rollback candidate without touching live services

The optional `rollback_candidate.py` command builds on the existing offline
ancestor/backup preflight. It verifies that the requested full commit is a
*local ancestor* of a clean current checkout, validates your **private**
backup, and runs the current application's isolated fresh-install smoke test.

First run its **read-only** checks:

```sh
python3 scripts/rollback_candidate.py --target YOUR_FULL_LOCAL_COMMIT_SHA --archive "$HOME/local-search-state-2026.zip"
```

If that succeeds, make a **new** private destination under an existing
owner-only `0700` parent (the destination itself must not exist).
Create a private parent **only if it does not exist**; otherwise check its
ownership and mode and refuse symlinks before proceeding:

```sh
private_parent="$HOME/.local/state/local-search-mcp-rollback"
if [ ! -e "$private_parent" ]; then mkdir -m 700 "$private_parent"; fi
# If the parent exists, confirm it is a real directory owned by you (0700).
# Always choose a NEW candidate destination for subsequent staging runs.
python3 scripts/rollback_candidate.py --target YOUR_FULL_LOCAL_COMMIT_SHA --archive "$HOME/local-search-state-2026.zip" --destination "$HOME/.local/state/local-search-mcp-rollback/candidate-1" --confirm STAGE
```

The command copies only the target's locally available *tracked source files*
into `source/` using bounded **raw Git tree/blob plumbing**, rejecting
symlinks, gitlinks, path traversal, duplicates and non-regular entries. It
does **not** use `git archive` or a working-tree checkout because those may
invoke configured smudge filters or other operator hooks. It never executes
content filters, runs older code, or installs dependencies. The independently verified recovery data goes
to `state/` through the same no-clobber owner-only restore mechanism used
elsewhere. An owner-only `candidate.json` records exact source/target
commits, file hashes and modes, directories, file counts and the explicit
`ready_for_live_use: false` flag.
No launcher credential, tunnel profile, `.venv`, live checkout file, Git HEAD,
service, daemon, Docker container or auto-restart baseline is modified.
Incomplete staging is removed on errors; an existing candidate is never
overwritten.

**Staging is NOT activating a working rollback.** An older revision can need
different dependency versions, state schemas, tunnel binaries, configuration
and service files; current-reader smoke tests cannot certify old-reader
compatibility. Review the staged source and release differences, prepare a
*separate* virtual environment, and test its operation and restored policy
privately. Disable the Git restart watcher and stop the affected services
*before* any future reviewed activation or live-state swap. Preserve the
original checkout, archive and rollback candidate so manual recovery can
return to the previous revision if validation fails. Do not perform a blind
`git reset --hard`, replace current state in place, or assume a clean
preflight authorizes a live deployment.

Issue #15 remains open until a controlled and operator-tested activation,
failure rollback and restart procedure has been validated on a real host.


## Verifying a private rollback candidate (v2)

After a successful offline `scripts/rollback_candidate.py --confirm STAGE` run,
validate its exact staged source and recovered data *without running the older code*:

```sh
python3 scripts/verify_rollback_candidate.py \
  "$HOME/.local/state/local-search-mcp-rollback/candidate-1"
```

New candidates record a versioned SHA-256 inventory binding the exact private
source file modes (including executable bits), expected directory entries and
state files. Read-only verification refuses changed bytes/modes, added paths,
symlinks/hardlinks, invalid metadata, invalid database or nonsecret settings,
and stale legacy v1 candidates. Archives and candidates must remain under
private, owner-only directories; never upload their history or approvals.

The manifest is an integrity record, **not a signature** and **not proof of
runtime compatibility** with historical dependencies, the Python venv, tunnel,
Docker firewall rules or systemd configuration. The verifier does not switch
code revisions, install/reinstall dependencies, alter live state, stop/start
services, or claim a deployable rollback. Passing verification leaves
`ready_for_live_use: false`; test and review the complete live cutover
separately before restoring any running installation.


## Read-only stopped-state gate before host maintenance

After stopping and disabling project-owned services by your existing reviewed
procedure, check their *effective* systemd user unit identity and status:

```sh
python3 scripts/maintenance_readiness.py --candidate \
  "$HOME/.local/state/local-search-mcp-rollback/candidate-1"
```

This gate checks only the known project-owned bridge/tunnel, update watcher,
health timer and guard-probe user units. Every installed file must carry the
managed marker, be user-owned and not symlinked or group/world-writable;
the systemd manager must point to the same effective file with
`NeedDaemonReload=no`. Only the exact reviewed tunnel crawler-egress startup
guard drop-in is accepted; other overrides are blocked. Every loaded unit
must report strictly inactive and have no enabled trigger. A failed
oneshot is conservatively blocked even when its process has stopped; inspect
the failure and run `systemctl --user reset-failed UNIT` if appropriate,
then recheck the inactive state. Never reset without inspecting the cause. The on-demand wake preference must be disabled. Missing units are accepted only
when systemd also reports them not found. The candidate is re-verified
read-only when specified. Errors are redacted; an unknown systemd bus,
unrecognized override, active tunnel, unverified unit or invalid candidate
returns nonzero.

**This result is a point-in-time observation, not a service lock or rollback
authorization.** External agents, timers, and logins can change status
immediately afterward. Do not treat it as permission to replace a state DB
or to run historical application code. The root-owned egress firewall,
Docker backends, tunnel credentials, custom history locations and service
restart sequencing are outside this command's scope. No **mutating** systemctl action,
Docker command, file modification or automatic update is performed; only
read-only `systemctl --user show` queries are issued. A
reviewed live cutover and failure-recovery drill remain outstanding for #15.

## Static historical release and independent dependency audit

Use the operator-staged, v2-inventory-verified candidate. Prepare a **separate**
private Python virtualenv for its historical `requirements.txt` yourself;
never install packages into the current running checkout. Once the environment
is prepared without unattended downloads or updates, run:

```sh
python3 scripts/rollback_release_audit.py \
  "$HOME/.local/state/local-search-mcp-rollback/candidate-1" \
  --python "$HOME/.local/state/rollback-env/bin/python"
```

This offline audit rechecks the candidate inventory, parses historical runtime
Python entrypoints without **importing or running** them, and confirms that
`web_search` and `web_fetch` remain registered MCP tools. It checks the
expected private-systemd template safety contract and parses a restricted
requirements format; pip options, remote URLs and dynamic references fail
closed for manual review. The separate operator-supplied virtualenv is queried
using its isolated interpreter for package metadata only. Nothing is installed,
downloaded, restarted, switched or granted access. A missing, incompatible or
unproven environment prevents the offline checks from passing.

Even a fully successful **static** report sets `ready_for_live_use: false`.
It cannot establish whether historical SQLite readers support current data,
whether credentials/tunnel profiles are compatible, whether egress guards will
work or whether services will start. Those require a separate isolated
historical runtime rehearsal and the actual host recovery acceptance drill.


## Opt-in historical state-reader sandbox rehearsal

After creating the private v2 candidate and independently preparing its
historical Python virtualenv, the operator may additionally test the **old**
policy and history readers against disposable copies of the restored files:

```sh
python3 scripts/rollback_runtime_smoke.py \
  "$HOME/.local/state/local-search-mcp-rollback/candidate-1" \
  --python "$HOME/.local/state/rollback-env/bin/python"
```

Run this Linux-only test **as an unprivileged operator** (never root).
It requires a trusted root-owned `/usr/bin/bwrap` and
unprivileged user/mount/PID/network namespaces enabled by the host. It
fails closed where the sandbox cannot be started; it does not fall back to
executing old source in the host namespace. The candidate inventory and
independent Python dependency compatibility audit must pass first.

The command copies only allowlisted owner-only recovery files into a
disposable XDG state/config input directory. That input is mounted **read-only**
at `/input` inside a newly isolated bubblewrap namespace, where the worker
copies the files again to a **512 MiB size-limited writable tmpfs** at `/work`
for SQLite initialization and history-index generation. A separate 16 MiB
tmpfs contains `/tmp`. No host directory is mounted writable. Recovery
archives near these limits may be rejected instead of risking uncontrolled
disk or memory usage. The historical policy/history readers run with
**no network** and
no inherited access to the rest of the operator's home directory, live
checkout, launcher config, tunnel tokens, Docker socket, or systemd bus.
The explicitly mounted historical source and prepared venv may themselves
reside under the operator's home; keep credentials and unrelated private
files **out of those mounted subtrees**. The only other mounted read-only
locations are OS executables and runtime libraries under `/usr/bin`,
`/usr/lib`, and (where present) `/usr/lib64`; when the host uses a real
root-owned `/lib64` directory instead of a symlink, that loader directory
is also mounted read-only. Dependency metadata is checked before historical
`/app` is added to Python's import path, preventing staged files from
spoofing installed distribution versions. The command reports aggregate
reader success and a capped recent history count; neither approvals nor
search terms are printed.

The namespace assumes a Linux system with a merged `/usr` filesystem.
Missing `bwrap`, disabled unprivileged namespaces, incompatible historical
Python imports or schema migrations cause a nonzero failure. The test does
**not** prove service startup, firewall protections, credential migration,
reboot recovery, or a successful live rollback. The report explicitly
retains `ready_for_live_use: false`; keep issue #15 open until a
reviewed reversible cutover and a host-level failure drill pass.

## Seal a verified pre-cutover recovery checkpoint

**Before any live release cutover**, manually bring only the known project
bridge/tunnel and update triggers into a verified quiescent state; keep Docker,
the crawler egress firewall, credentials and unrelated units unchanged.
Run the existing read-only maintenance readiness check first:

```sh
python3 scripts/maintenance_readiness.py --candidate \
  "$HOME/.local/state/local-search-mcp-rollback/candidate-1"
```

From the currently installed, clean checkout (while all known project-owned
units remain safely inactive/disabled and optional on-demand wake is disabled),
choose a **new** checkpoint directory under an owner-only 0700 parent.
The `mkdir` example intentionally refuses an existing parent; if it already
exists, independently verify its owner and exact 0700 mode before use:

```sh
mkdir -m 700 "$HOME/.local/state/local-search-mcp-recovery"
python3 scripts/rollback_recovery_checkpoint.py \
  --candidate "$HOME/.local/state/local-search-mcp-rollback/candidate-1" \
  --destination "$HOME/.local/state/local-search-mcp-recovery/current-1"

python3 scripts/rollback_recovery_checkpoint.py \
  --inspect "$HOME/.local/state/local-search-mcp-recovery/current-1" \
  --candidate "$HOME/.local/state/local-search-mcp-rollback/candidate-1"
```

The tool **never stops/restarts any service** and does not alter the Git
checkout, on-disk live state, or a prior checkpoint. It requires the candidate
inventory to match the exact installed Git revision and strict
inactive/project-owned systemd gate to pass **both before and after** the
snapshot. The private backup also passes the hardened local Git rollback
preflight, proving its target is a distinct, locally available ancestor of
the unchanged current checkout (not that its dependencies or services work).
The tool also verifies that the interactive shell and both managed
systemd services use the **same** effective HOME, XDG state/config and optional
history-log locations. It rejects per-service Environment/EnvironmentFile
overrides and any mismatched manager environment, rather than silently
archiving a valid database that belongs to a different session. If this check
fails, inspect `systemctl --user show-environment` locally and adjust the
invocation environment to match the reviewed service (do not copy secret
values into messages or scripts). It creates a private ZIP using the existing SQLite-consistent
snapshot API and allowlisted nonsecret settings, verifies every archive member
and runs an isolated fresh-install reader smoke test. It then rechecks the
candidate and checkout, and writes `checkpoint.json` last with a SHA-256
commitment to the backup, current Git SHA and chosen historical candidate.
Successful inspection returns `checkpoint_verified: true` but always
`ready_for_live_use: false`.

A crash or failure **after the destination was created** but before
the final manifest leaves an intentionally unsealed directory. Earlier gate
failures do not create any destination. Inspection rejects an incomplete
directory even if its archive seems valid;
do not reuse or overwrite it. Investigate and choose a new destination.
Archives can contain sensitive history/approvals and must remain owner-only.
Secrets (`config.json`, tunnel credentials and launcher profiles) are **not**
exported. The checkpoint does not capture service enablement, dependency
venvs, Docker state, the root-owned egress guard or systemd units.

This is a prerequisite for a future reviewed operator-initiated rollback
transaction, **not an activation command**. Returning to a working release,
failed startup, and host crash/reboot recovery must be implemented and
exercised separately before issue #15 can be closed.
