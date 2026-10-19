# Manual PyPI dependency release checks (issue #15)

The bridge uses two direct Python dependencies (`fastmcp` and `httpx`).
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

Only two fixed public `https://pypi.org/pypi/{name}/json` endpoints are
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
