#!/usr/bin/env python3
"""Opt-in read-only Crawl4AI version report (no image pull or service change)."""
from __future__ import annotations

import argparse
from http.client import HTTPException
import json
import re
import sys
import urllib.error
import urllib.request

from crawler_security import MINIMUM_VERSION, verify_health
from health_cli import get_json

LATEST_URL = "https://api.github.com/repos/unclecode/crawl4ai/releases/latest"
_MAX_BYTES = 2 * 1024 * 1024
_LOCAL_VERSION = re.compile(r"\d+\.\d+\.\d+(?:\+[A-Za-z0-9._-]+)?\Z")
_RELEASE_TAG = re.compile(r"v?(\d+)\.(\d+)\.(\d+)\Z")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def installed_version() -> dict[str, object]:
    """Read only the fixed loopback health endpoint; never inspect Docker env."""
    try:
        data = get_json(11235, "/health")
    except (OSError, ValueError, TypeError, HTTPException):
        return {"version": None, "supported_and_healthy": False,
                "error": "Local Crawl4AI health lookup unavailable"}
    if not isinstance(data, dict):
        return {"version": None, "supported_and_healthy": False,
                "error": "Invalid local Crawl4AI health response"}
    version = data.get("version")
    if not isinstance(version, str) or len(version) > 80 or not _LOCAL_VERSION.fullmatch(version):
        return {"version": None, "supported_and_healthy": False,
                "error": "Invalid local Crawl4AI version"}
    return {"version": version, "supported_and_healthy": verify_health(data) is None}


def published_release() -> dict[str, object]:
    """Read bounded public GitHub release metadata; never fetch source/images."""
    request = urllib.request.Request(
        LATEST_URL,
        headers={"Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "local-search-mcp-explicit-crawler-version-check"},
        method="GET",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
        urllib.request.HTTPSHandler(),
    )
    with opener.open(request, timeout=7) as response:
        if response.status != 200:
            raise ValueError("Unexpected release metadata status")
        content = response.read(_MAX_BYTES + 1)
    if len(content) > _MAX_BYTES:
        raise ValueError("GitHub release metadata exceeds size limit")
    try:
        data = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid GitHub release metadata") from exc
    if not isinstance(data, dict) or data.get("draft") is not False or data.get("prerelease") is not False:
        raise ValueError("GitHub release is not a stable published release")
    tag = data.get("tag_name")
    match = _RELEASE_TAG.fullmatch(tag) if isinstance(tag, str) else None
    if not match:
        raise ValueError("GitHub release tag is not a stable numeric version")
    numeric = tuple(map(int, match.groups()))
    return {"tag": tag, "meets_bridge_minimum": numeric >= MINIMUM_VERSION}


def version_report(*, check_remote: bool = False) -> dict[str, object]:
    """Report facts, not upgrade recommendations or semver ordering guesses."""
    local = installed_version()
    report: dict[str, object] = {
        "scope": "crawl4ai-loopback-and-optional-public-release",
        "minimum_supported_by_bridge": ".".join(map(str, MINIMUM_VERSION)),
        "local": local,
        "remote_requested": check_remote,
    }
    if check_remote:
        try:
            remote = published_release()
            report["published_release"] = remote
            local_number = local.get("version")
            report["exact_version_match"] = (
                local_number.removeprefix("v").split("+", 1)[0] == remote["tag"].lstrip("v")
                if isinstance(local_number, str) else None
            )
        except (OSError, ValueError, HTTPException, urllib.error.URLError):
            report["published_release"] = {"error": "GitHub release lookup unavailable or invalid"}
    report["note"] = (
        "A different version is not proof of a safe or newer image. "
        "A published release below the bridge minimum must not be installed. "
        "Verify release notes, image digest and security before any manual migration; "
        "no Docker, Git, package or service changes were made."
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-remote", action="store_true",
                        help="explicitly request public GitHub stable release metadata")
    args = parser.parse_args(argv)
    report = version_report(check_remote=args.check_remote)
    print(json.dumps(report, sort_keys=True, indent=2))
    local = report["local"]
    remote = report.get("published_release", {})
    return 1 if ("error" in local or (args.check_remote and "error" in remote)) else 0


if __name__ == "__main__":
    sys.exit(main())
