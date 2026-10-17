"""Read-only redacted diagnostics for the launcher's configuration sources.

Never prints tokens, profile contents, URL credentials, private file paths,
or the persisted domain approval list. Inspecting does not call Docker or
write to the SQLite database.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit


def _source(key: str, saved: dict[str, str], legacy: dict[str, str]) -> tuple[str, bool]:
    """Report the interactive setup source, excluding Docker's unqueried token."""
    if os.environ.get(key):
        return "environment", True
    if saved.get(key):
        return "private JSON", True
    if legacy.get(key):
        return "legacy env", True
    return "missing", False


def _endpoint(key: str, default: str) -> dict[str, object]:
    """Parse an endpoint without ever displaying credentials or remote hosts."""
    raw = os.environ.get(key, default)
    source = "environment" if key in os.environ else "default"
    try:
        parsed = urlsplit(raw)
        valid = (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
                 and parsed.username is None and parsed.password is None
                 and not parsed.path.strip("/") and not parsed.query and not parsed.fragment
                 and (parsed.port is None or 1 <= parsed.port <= 65535))
        if valid:
            return {"source": source, "valid_loopback": True,
                    "value": f"http://{parsed.hostname}:{parsed.port or (8888 if key == 'SEARXNG_URL' else 11235)}"}
    except (ValueError, TypeError):
        pass
    return {"source": source, "valid_loopback": False, "value": "<redacted>",
            "warning": "Endpoint must be checked manually; remote/malformed endpoints are not displayed"}


def _dashboard_port() -> dict[str, object]:
    raw = os.environ.get("LOCAL_SEARCH_MCP_DASHBOARD_PORT", "8791")
    try:
        port = int(raw)
        if 1 <= port <= 65535:
            return {"source": "environment" if "LOCAL_SEARCH_MCP_DASHBOARD_PORT" in os.environ else "default",
                    "value": port, "warning": "Health monitor currently probes port 8791" if port != 8791 else None}
    except (TypeError, ValueError):
        pass
    return {"source": "environment", "value": "<invalid>", "warning": "Expected integer port 1–65535"}


def collect_report(saved: dict[str, str], root: Path, private_dir: Path, legacy_reader) -> dict:
    """Return a JSON-safe diagnostic without modifying files or evaluating secrets."""
    legacy = {**legacy_reader(root / ".env"), **legacy_reader(root / ".tunnel-env")}
    secrets = {}
    for key in ("CONTROL_PLANE_API_KEY", "CRAWL4AI_API_TOKEN"):
        source, configured = _source(key, saved, legacy)
        secrets[key] = {"source": source, "configured": configured,
                        "value": "<redacted>" if configured else "<missing>"}
    source, configured = _source("ALLOWED_CRAWL_DOMAINS", saved, legacy)
    state_home = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")).expanduser()
    database = state_home / "local-search-mcp" / "domains.sqlite3"
    if database.is_symlink():
        authority = "unsafe SQLite symlink (inspect manually)"
    elif database.is_file():
        authority = "persisted SQLite rules (not modified or listed)"
    else:
        authority = "initial seed on next launch"
    return {
        "secrets": secrets,
        "domain_policy": {"authority": authority, "seed_source": source if configured else "launcher default",
                          "seed_applies_only_before_first_initialization": True},
        "endpoints": {"searxng": _endpoint("SEARXNG_URL", "http://127.0.0.1:8888"),
                      "crawl4ai": _endpoint("CRAWL4AI_URL", "http://127.0.0.1:11235"),
                      "dashboard_port": _dashboard_port()},
        "service_mode": "systemd --service uses saved private JSON credentials, not env overrides",
        "crawl_token_note": "interactive setup may prefer a Docker-discovered token; Docker was not queried",
        "profile": "tunnel-client profile is separately managed and never inspected here",
        "private_json_exists": (private_dir / "config.json").is_file(),
        "read_only": True,
    }
