"""Fail-closed compatibility gate for Crawl4AI Docker's hardened HTTP API.

The bridge cannot replace the crawler's own network egress checks. Crawl4AI
0.9.5 closes additional SSRF bypasses (including CacheValidator), and older or unverified versions are
refused for remote MCP fetches.
"""
from __future__ import annotations

import re

MINIMUM_VERSION = (0, 9, 5)


def verify_health(data: object) -> str | None:
    """Return None if safe to proceed, otherwise a user-visible failure reason."""
    if not isinstance(data, dict) or data.get("status") != "ok":
        return "Crawl4AI health endpoint did not report a healthy server"
    version = data.get("version")
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:\+[A-Za-z0-9._-]+)?", version) if isinstance(version, str) else None
    if not match or tuple(map(int, match.groups())) < MINIMUM_VERSION:
        return "Crawl4AI 0.9.5 or newer (stable) is required; older versions have known SSRF bypasses. Upgrade the Crawl4AI Docker server"
    return None


def local_container_warning() -> str | None:
    """Detect explicitly unsafe Crawl4AI flags when local Docker is available.

    Docker may not be available to the bridge (or the upstream server may be
    remote), so None does NOT prove that firewall isolation is configured.
    """
    import shutil
    import subprocess

    if not shutil.which("docker"):
        return None
    try:
        ids = subprocess.run(
            ["docker", "ps", "--filter", "publish=11235", "--format", "{{.ID}}"],
            check=True, capture_output=True, text=True, timeout=4,
        ).stdout.splitlines()
        if not ids:
            return None
        lines = subprocess.run(
            ["docker", "inspect", ids[0], "--format", "{{range .Config.Env}}{{println .}}{{end}}"],
            check=True, capture_output=True, text=True, timeout=4,
        ).stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return None
    unsafe = {"CRAWL4AI_ALLOW_INTERNAL_URLS", "CRAWL4AI_ALLOW_INSECURE_TLS"}
    for line in lines:
        key, _, value = line.partition("=")
        if key in unsafe and value.lower().strip() in ("true", "1", "yes", "on"):
            return f"Unsafe Crawl4AI container setting {key}=true; disable it before enabling remote MCP fetches"
    return None
