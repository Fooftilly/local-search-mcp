"""Explicit, read-only PyPI release checks for the bridge's direct dependencies.

Never installs, downloads packages, modifies a repository or checks automatically.
Only the fixed allowlist from requirements.txt is queried; no URL is user-controlled.
"""
from __future__ import annotations

import argparse
from http.client import HTTPException
from importlib import metadata
import json
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, HTTPHandler, HTTPSHandler, HTTPRedirectHandler, ProxyHandler

PACKAGES = ("fastmcp", "httpx", "idna")
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
TIMEOUT_SECONDS = 5
_VERSION = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._+!\-]{0,79}\Z")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _installed_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _pypi_version(name: str) -> str:
    if name not in PACKAGES:
        raise ValueError("Unsupported dependency")
    opener = build_opener(ProxyHandler({}), HTTPSHandler(), NoRedirect())
    url = f"https://pypi.org/pypi/{name}/json"
    try:
        with opener.open(url, timeout=TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise ValueError("Unexpected HTTP status")
            if response.headers.get_content_type() != "application/json":
                raise ValueError("Unexpected content type")
            content = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, HTTPError, URLError, HTTPException) as exc:
        raise ValueError("PyPI release lookup unavailable") from exc
    if len(content) > MAX_RESPONSE_BYTES:
        raise ValueError("PyPI response too large")
    try:
        data = json.loads(content)
        version = data["info"]["version"]
    except (UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("Invalid PyPI response") from exc
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ValueError("Invalid PyPI release version")
    return version


def version_report(*, check_remote: bool = False) -> list[dict]:
    report = []
    for name in PACKAGES:
        current = _installed_version(name)
        row = {"package": name, "installed": current}
        if check_remote:
            try:
                latest = _pypi_version(name)
                row.update(pypi=latest,
                           same_version=(current == latest if current is not None else None))
            except ValueError:
                row["error"] = "PyPI check unavailable or invalid"
        report.append(row)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-remote", action="store_true",
                        help="explicitly query public PyPI for the three allowlisted packages")
    args = parser.parse_args(argv)
    results = version_report(check_remote=args.check_remote)
    print(json.dumps({"scope": "installed-direct-dependencies",
                      "remote_requested": args.check_remote,
                      "packages": results}, indent=2))
    return 1 if any("error" in row for row in results) else 0


if __name__ == "__main__":
    sys.exit(main())
