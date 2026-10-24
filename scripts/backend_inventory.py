"""Read-only, privacy-preserving inventory of the local-search-mcp Docker project.

No start/stop/update, no Compose mutations and no Docker context inheritance.
The published crawler check reports only counts, never external project names,
container IDs, paths, image names, tokens or raw Docker error output.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess

PROJECT = "local-search-crawler"
DOCKER = ("docker", "--host", "unix:///var/run/docker.sock")
MAX_CONTAINERS = 256
MAX_OUTPUT = 65536


def _run(*args: str) -> str | None:
    environment = os.environ.copy()
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                 "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
                 "COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES"):
        environment.pop(name, None)
    try:
        result = subprocess.run(
            [*DOCKER, *args], capture_output=True, text=True, check=False,
            timeout=8, env=environment)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode or len(result.stdout) > MAX_OUTPUT:
        return None
    return result.stdout


def _ids(*filters: str) -> list[str] | None:
    raw = _run("ps", "--all", "--no-trunc", *filters, "--format", "{{.ID}}")
    if raw is None:
        return None
    ids = raw.splitlines()
    # IDs are never printed or passed to a shell. Reject malformed/partial lists.
    if (len(ids) > MAX_CONTAINERS or len(ids) != len(set(ids))
            or any(len(ident) != 64 or
                   any(c not in "0123456789abcdef" for c in ident) for ident in ids)):
        return None
    return ids


def _private_files(base: Path) -> tuple[set[str], bool] | None:
    """Require local private Compose file ownership, not just Docker labels."""
    folder = base / "local-search-mcp" / "crawler-compose"
    try:
        mode = folder.lstat()
        if (not stat.S_ISDIR(mode.st_mode) or mode.st_uid != os.getuid()
                or mode.st_mode & 0o077):
            return None
        crawler = folder / "compose.yaml"
        st = crawler.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return None
        files = {str(crawler.resolve(strict=True))}
        overlay = folder / "searxng.compose.yaml"
        try:
            st = overlay.lstat()
        except FileNotFoundError:
            return files, False
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return None
        files.add(str(overlay.resolve(strict=True)))
        return files, True
    except OSError:
        return None


def _inspect(ident: str) -> tuple[dict, str, str | None] | None:
    raw_labels = _run("inspect", "--type=container", "--format",
                      "{{json .Config.Labels}}", ident)
    running = _run("inspect", "--type=container", "--format",
                   "{{.State.Running}}", ident)
    raw_policy = _run("inspect", "--type=container", "--format",
                      "{{json .HostConfig.RestartPolicy}}", ident)
    if raw_labels is None or running is None or running.strip() not in ("true", "false"):
        return None
    try:
        labels = json.loads(raw_labels)
    except (ValueError, TypeError):
        return None
    if not isinstance(labels, dict):
        return None
    # Restart-policy lookup is a separate eligibility gate: missing/malformed
    # policy must not erase otherwise verified container ownership or state.
    policy_name = None
    if raw_policy is not None:
        try:
            policy = json.loads(raw_policy)
        except (ValueError, TypeError):
            policy = None
        if isinstance(policy, dict) and type(policy.get("Name")) is str:
            # Docker may encode "no restart" as either an empty string or "no".
            mode = policy["Name"]
            if mode in ("", "no", "always", "unless-stopped", "on-failure"):
                policy_name = "no" if mode == "" else mode
    return labels, running.strip(), policy_name


def report(*, config_base: Path | None = None) -> dict:
    """Summarize verified project resources without exposing private details."""
    if config_base is None:
        config_base = Path(os.environ.get("XDG_CONFIG_HOME")
                           or (Path.home() / ".config")).expanduser()
    inventory = _ids("--filter", f"label=com.docker.compose.project={PROJECT}")
    published = _ids("--filter", "publish=11235")
    output = {
        "state": "unknown",
        "managed_running": 0,
        "managed_stopped": 0,
        "verified_services": {"crawl4ai": 0, "searxng": 0},
        "verified_restart_policies": {"crawl4ai": None, "searxng": None},
        "unverified_project_containers": 0,
        "other_published_crawlers": None,
        "security_note": ("Docker ownership is not firewall/egress proof; "
                          "a live guard probe is required before remote crawling"),
    }
    if inventory is None or published is None:
        return output
    # Report unmanaged published endpoints, but never assert that they are
    # safe or name their Compose projects/containers.
    output["other_published_crawlers"] = len(set(published) - set(inventory))
    private = _private_files(config_base)
    if not inventory:
        output["state"] = "not_configured" if private is None else "not_started"
        return output
    if private is None:
        output["state"] = "unverified"
        output["unverified_project_containers"] = len(inventory)
        return output
    allowed_files, overlay_exists = private
    for ident in inventory:
        data = _inspect(ident)
        if data is None:
            output["unverified_project_containers"] += 1
            continue
        labels, running, restart_policy = data
        raw_files = labels.get("com.docker.compose.project.config_files")
        service = labels.get("com.docker.compose.service")
        if not isinstance(raw_files, str):
            output["unverified_project_containers"] += 1
            continue
        names = raw_files.split(",")
        files = set(names)
        crawler_file = next(name for name in allowed_files if
                            name.endswith("/compose.yaml"))
        overlay_file = next((name for name in allowed_files if
                             name.endswith("/searxng.compose.yaml")), None)
        valid = (labels.get("com.docker.compose.project") == PROJECT
                 and service in ("crawl4ai", "searxng")
                 and len(names) == len(files)
                 and crawler_file in files
                 and files.issubset(allowed_files)
                 and (service != "searxng" or
                      (overlay_exists and overlay_file in files)))
        if not valid:
            output["unverified_project_containers"] += 1
            continue
        # A matching Compose project label alone is insufficient for a
        # lifecycle action: two crawlers cannot substitute for one searcher.
        output["verified_services"][service] += 1
        output["verified_restart_policies"][service] = restart_policy
        if running == "true":
            output["managed_running"] += 1
        else:
            output["managed_stopped"] += 1
    if output["unverified_project_containers"]:
        output["state"] = "unverified"
    elif output["managed_running"] and output["managed_stopped"]:
        output["state"] = "partially_running"
    elif output["managed_running"]:
        output["state"] = "running"
    else:
        output["state"] = "stopped"
    return output
