#!/usr/bin/env python3
"""Bounded, read-only tunnel-client fingerprint and opt-in public release audit.

Never executes the local tunnel binary, downloads release assets, installs
software, reads tunnel configuration, or prints profile paths/credentials.
A public release tag cannot identify which local binary is installed.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import stat
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LATEST = "https://api.github.com/repos/openai/tunnel-client/releases/latest"
MAX_BINARY = 60_000_000
MAX_RESPONSE = 128_000
_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_fingerprint(*, root: Path = ROOT) -> dict:
    """Hash a pinned, owned regular local binary, without executing it."""
    path = root / "tunnel-client"
    try:
        status = path.lstat()
    except FileNotFoundError:
        return {"state": "not_installed"}
    if (not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.getuid()
            or status.st_mode & 0o022
            or not 0 < status.st_size <= MAX_BINARY):
        raise ValueError("Tunnel client file is untrusted or oversized")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or not 0 < info.st_size <= MAX_BINARY
                or (info.st_dev, info.st_ino) != (status.st_dev, status.st_ino)):
            raise ValueError("Tunnel binary identity changed during inspection")
        digest = hashlib.sha256()
        count = 0
        with os.fdopen(fd, "rb", closefd=False) as reader:
            while chunk := reader.read(65536):
                count += len(chunk)
                if count > MAX_BINARY:
                    raise ValueError("Tunnel binary exceeds size limit")
                digest.update(chunk)
        if count != info.st_size:
            raise ValueError("Tunnel binary changed during inspection")
        return {"state": "installed", "sha256": digest.hexdigest(),
                "size_bytes": count, "version": "unverified"}
    finally:
        os.close(fd)


def public_latest() -> dict:
    """Use only the official fixed GitHub endpoint, no proxy or redirects."""
    req = urllib.request.Request(
        LATEST, method="GET",
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "local-search-mcp-tunnel-audit/1.0",
                 "X-GitHub-Api-Version": "2022-11-28"})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect(),
        urllib.request.HTTPSHandler())
    with opener.open(req, timeout=7) as response:
        if response.status != 200:
            raise ValueError("Unexpected public release response")
        data = response.read(MAX_RESPONSE + 1)
    if len(data) > MAX_RESPONSE:
        raise ValueError("Public release metadata too large")
    document = json.loads(data)
    if type(document) is not dict or not isinstance(document.get("tag_name"), str):
        raise ValueError("Invalid public release metadata")
    tag = document["tag_name"]
    if not _TAG.fullmatch(tag):
        raise ValueError("Invalid public release tag")
    assets = document.get("assets")
    if type(assets) is not list or len(assets) > 100:
        raise ValueError("Invalid public release assets")
    expected = {f"tunnel-client-{tag}-linux-amd64.zip", "SHA256SUMS.txt"}
    present = set()
    for asset in assets:
        if type(asset) is not dict:
            raise ValueError("Invalid release asset")
        name = asset.get("name")
        if name in expected:
            url = asset.get("browser_download_url")
            if (type(url) is not str or url !=
                    f"https://github.com/openai/tunnel-client/releases/download/{tag}/{name}"):
                raise ValueError("Unexpected official release asset origin")
            present.add(name)
    return {"state": "available", "tag": tag,
            "verified_asset_names": sorted(present),
            "complete_linux_amd64_release": present == expected}


def version_report(*, check_remote: bool = False, root: Path = ROOT) -> dict:
    report = {
        "schema": "local-search-mcp/tunnel-version-audit/v1",
        "mode": "read-only",
        "remote_requested": check_remote,
        "updates_applied": False,
        "downloads_performed": False,
    }
    try:
        report["local_binary"] = local_fingerprint(root=root)
    except (OSError, ValueError):
        report["local_binary"] = {"state": "unavailable"}
    if check_remote:
        try:
            report["public_release"] = public_latest()
        except (OSError, ValueError, TypeError, urllib.error.URLError,
                http.client.HTTPException, json.JSONDecodeError):
            report["public_release"] = {"state": "unavailable"}
    report["note"] = (
        "Local SHA-256 is a fingerprint, not a verified version. A public "
        "release tag does not prove an update is needed or compatible. "
        "No local binary was executed and no releases were downloaded."
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-remote", action="store_true",
                        help="explicit official GitHub release metadata lookup")
    args = parser.parse_args(argv)
    print(json.dumps(version_report(check_remote=args.check_remote),
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
