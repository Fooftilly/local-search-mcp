# Manual PyPI dependency release checks (issue #15)

The bridge uses three direct Python dependencies (`fastmcp`, `httpx` and `idna`).
To view the versions installed in **the same Python environment used by the bridge**, run:

```sh
.venv/bin/python scripts/check_dependency_versions.py
```

This is offline; it does not contact PyPI or modify the installation.
To **explicitly opt in** to comparing installed versions with PyPI's
currently advertised releases:

```sh
.venv/bin/python scripts/check_dependency_versions.py --check-remote
```

Only three fixed public `https://pypi.org/pypi/{name}/json` endpoints are
requested, with five-second timeouts, bounded bodies, redirects disabled and
ambient HTTP proxies bypassed. No credentials, local settings, queries or
history are sent. Failures produce an `error` field and nonzero exit status.

A difference means **only that the version strings differ**. It is not proof
that PyPI's release is newer than the installed version, appropriate for
your Python interpreter, compatible with the bridge or secure. Package
compatibility, transitive dependencies, hashes and release provenance are
**not validated** by this command. No scheduled checks, downloads, installs,
restarts or edits are performed. Keep updates and rollback manual, with
a verified state backup and a test run before updating active services.
This check is separate from the Git bridge revision checker in PR #58.

For an independent local Crawl4AI version report and opt-in public GitHub release
check, see [CRAWLER-VERSIONS.md](CRAWLER-VERSIONS.md). This does not install images.

## Consolidated version inventory (read-only)

To gather installed bridge dependency versions from the **project virtual
environment**, local Crawl4AI version/health, and offline Git baseline
diagnostics in one JSON report, run:

```sh
python3 -m scripts.maintenance_versions
```

This default operation does not query public hosts, install packages, fetch
Git objects, pull Docker images, restart services, or change settings.
If a maintainer explicitly wants current public version metadata as well:

```sh
python3 -m scripts.maintenance_versions --check-remote
```

The optional switch runs the existing bounded HTTPS checks against PyPI and
GitHub for direct Python dependencies, the published Crawl4AI release, and
the bridge repository's public master revision. It uses the project `.venv`
Python for installed dependencies. Failures are reported with redacted,
non-actionable status instead of exposing private paths or credentials.

A version difference is **not** evidence that upgrading is safe. The public
Crawl4AI release tag may lag available Docker builds; a remote bridge revision
may diverge rather than be newer. Review compatibility, image digests, security,
backup integrity and controlled restart requirements manually. This command
never performs updates, rollback, or client configuration. The tunnel client's
version remains outside this inventory because its executable identity is not
yet safely established by a dedicated version checker.
