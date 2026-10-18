#!/usr/bin/env python3
"""Explicit GitHub bridge revision check; never fetch or install updates."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
import sys
import urllib.error
import urllib.request

if __package__:
    from . import restart_on_update as update
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import restart_on_update as update

ROOT = Path(__file__).resolve().parents[1]
REMOTE_URL = "https://api.github.com/repos/Fooftilly/local-search-mcp/commits/master"
_MAX_JSON = 256 * 1024
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def remote_head() -> str:
    """One bounded, TLS-protected public metadata request without proxies."""
    request = urllib.request.Request(
        REMOTE_URL,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "local-search-mcp-explicit-revision-check",
                 "X-GitHub-Api-Version": "2022-11-28"},
        method="GET",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
        urllib.request.HTTPSHandler(),
    )
    with opener.open(request, timeout=7) as response:
        if response.status != 200:
            raise ValueError("Unexpected GitHub response status")
        content = response.read(_MAX_JSON + 1)
    if len(content) > _MAX_JSON:
        raise ValueError("GitHub revision metadata exceeds size limit")
    try:
        data = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid GitHub revision response") from exc
    value = data.get("sha") if isinstance(data, dict) else None
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError("Response lacks a valid Git commit SHA")
    return value


def check(*, root: Path | None = None) -> str:
    """Report a revision difference, never asserting that remote is newer."""
    if root is None:
        root = ROOT
    update.verify_checkout(root, read_only=True)
    local = update.head(root, read_only=True)
    if not _SHA.fullmatch(local):
        raise ValueError("Local checkout lacks a valid Git commit SHA")
    remote = remote_head()
    if local == remote:
        return f"Local bridge HEAD {local[:12]} matches GitHub master."
    return (
        f"Local bridge HEAD {local[:12]} differs from GitHub master "
        f"{remote[:12]}. This does NOT prove a newer version is available: "
        "local commits, divergent history and force-pushes are possible. "
        "Review ancestry, release notes, dependencies and backups manually. "
        "No updates downloaded, checkout modified, or services restarted."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-remote", action="store_true",
                        help="Explicitly request public GitHub metadata over HTTPS")
    args = parser.parse_args(argv)
    if not args.check_remote:
        parser.error("Remote lookup requires explicit --check-remote")
    try:
        print(check())
        return 0
    except (OSError, ValueError, urllib.error.URLError) as exc:
        # Never echo remote response bodies, credentials or arbitrary URLs.
        print(f"Bridge revision check unavailable: {type(exc).__name__}",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
