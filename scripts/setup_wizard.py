#!/usr/bin/env python3
"""Conservative, ChatGPT-first interactive frontend for existing maintenance tools.

No setup or service mutations occur from import, --help, --check, --status,
--dry-run or --chatgpt. Interactive changes are individually confirmed.
"""
from __future__ import annotations

import argparse
from http.client import HTTPConnection
import json
import math
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
UNIT_NAMES = ("local-search-mcp-bridge.service", "local-search-mcp-tunnel.service",
              "local-search-mcp-health.timer", "local-search-mcp-update.timer",
              "local-search-mcp-update.path")
ALLOWED = {
    "LoadState": {"loaded", "not-found", "masked", "error"},
    "ActiveState": {"active", "inactive", "failed", "activating", "deactivating",
                    "reloading"},
    "UnitFileState": {"enabled", "disabled", "masked", "static", "indirect",
                     "linked", "generated", "transient", "alias", "bad", ""},
}


def run_existing(*argv: str) -> int:
    """Invoke one fixed repository entry point without a shell."""
    result = subprocess.run([sys.executable, str(ROOT / argv[0]), *argv[1:]],
                            cwd=ROOT, check=False)
    return result.returncode


def project_python() -> Path | None:
    """Use the environment installed by bootstrap; never fall back to system Python."""
    python = ROOT / ".venv" / "bin" / "python"
    return python if python.is_file() else None


def dependency_report() -> dict:
    """Inspect project dependencies via the existing local-only version checker."""
    python = project_python()
    if python is None:
        return {"state": "missing", "note": "Project .venv Python is not installed",
                "packages": []}
    try:
        proc = subprocess.run(
            [str(python), "-I", "-B",
             str(ROOT / "scripts" / "check_dependency_versions.py")],
            cwd=ROOT, capture_output=True, text=True, timeout=15, check=False)
        if proc.returncode:
            raise ValueError("Version check failed")
        report = json.loads(proc.stdout)
        if (report.get("remote_requested") is not False
                or not isinstance(report.get("packages"), list)):
            raise ValueError("Unexpected version report")
        names = ("fastmcp", "httpx", "idna")
        packages = report["packages"]
        if (len(packages) != len(names)
                or any(not isinstance(row, dict)
                       or row.get("package") != name
                       or (row.get("installed") is not None
                           and not isinstance(row.get("installed"), str))
                       for row, name in zip(packages, names))):
            raise ValueError("Unexpected dependency list")
        # Only report installed versions; checker does not query PyPI in this mode.
        return {"state": "available", "packages": packages}
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError,
            AttributeError):
        # Never include subprocess stdout, stderr or private pathnames.
        return {"state": "unavailable",
                "note": "Project .venv dependency inspection failed",
                "packages": []}


def configured_sources() -> dict:
    """Reuse the existing redacted configuration report; never echo exceptions."""
    try:
        import bootstrap
        from config_diagnostics import collect_report
        saved = bootstrap.read_config()
        return {"available": True,
                "report": collect_report(saved, bootstrap.ROOT,
                                         bootstrap.PRIVATE_DIR,
                                         bootstrap.read_legacy_env)}
    except (OSError, ValueError, RuntimeError, Exception):
        # SetupError may mention a private pathname; keep failure text private.
        return {"available": False, "error": "Private config unreadable or unsafe; inspect permissions and symlinks manually"}


def health() -> dict:
    try:
        import health_cli
        return health_cli.snapshot()
    except (OSError, ImportError, ValueError, RuntimeError):
        return {"status": "unavailable", "checks": {},
                "note": "Local health probe unavailable; no remote registration verified"}


def user_unit_states() -> dict:
    """Read systemd state only; do not enable, start, reload or install units."""
    states = {}
    for name in UNIT_NAMES:
        try:
            proc = subprocess.run(
                ["systemctl", "--user", "show", name,
                 "--property=LoadState", "--property=ActiveState",
                 "--property=UnitFileState"],
                capture_output=True, text=True, check=False, timeout=5,
                env={**os.environ, "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0"})
            if proc.returncode:
                states[name] = {"state": "unknown"}
                continue
            parsed = dict(line.split("=", 1) for line in proc.stdout.splitlines()
                          if "=" in line)
            if not all(parsed.get(k) in v for k, v in ALLOWED.items()):
                states[name] = {"state": "unknown"}
            else:
                states[name] = {k: parsed[k] for k in ALLOWED}
        except (OSError, subprocess.TimeoutExpired, ValueError):
            states[name] = {"state": "unknown"}
    return states


def docker_status() -> dict:
    """Only query the pinned local Docker daemon; never show IDs or labels."""
    try:
        from scripts import backend_inventory
        return backend_inventory.report()
    except (OSError, ValueError, ImportError):
        return {"state": "unknown", "security_note": "Docker ownership not verified"}


def on_demand_runtime_status() -> dict:
    """Bounded local dashboard read, without starting or waking a backend."""
    conn = HTTPConnection("127.0.0.1", 8791, timeout=2)
    try:
        conn.request("GET", "/on-demand-state", headers={
            "Host": "127.0.0.1:8791", "Origin": "http://127.0.0.1:8791"})
        response = conn.getresponse()
        raw = response.read(8193)
        if response.status != 200 or len(raw) > 8192:
            raise ValueError("Unusable activity status response")
        data = json.loads(raw)
        if type(data) is not dict or set(data) != {"mode", "enabled", "idle_minutes", "backends"}:
            raise ValueError("Invalid activity response")
        # Prevent any future unexpected field from being echoed to the CLI.
        if (type(data["enabled"]) is not bool
                or type(data["idle_minutes"]) is not int
                or not 1 <= data["idle_minutes"] <= 1440
                or data["mode"] not in ("on-demand", "disabled")):
            raise ValueError("Invalid activity fields")
        backends = data["backends"]
        if backends is not None and (
                type(backends) is not dict
                or set(backends) != {"crawl4ai", "searxng"}
                or any(type(row) is not dict or set(row) != {
                    "state", "armed", "active_requests",
                    "last_activity_ago_seconds", "next_idle_stop_seconds",
                    "stop_claimed"} for row in backends.values())):
            raise ValueError("Unexpected activity fields")
        if backends is not None:
            for row in backends.values():
                if (row["state"] not in (
                        "running", "sleeping", "waking", "stopping", "failed",
                        "unverified")
                        or type(row["armed"]) is not bool
                        or type(row["active_requests"]) is not int
                        or row["active_requests"] < 0
                        or type(row["stop_claimed"]) is not bool):
                    raise ValueError("Invalid backend status fields")
                for field in ("last_activity_ago_seconds", "next_idle_stop_seconds"):
                    value = row[field]
                    if (value is not None and (
                            type(value) not in (int, float) or value < 0
                            or (type(value) is float and not math.isfinite(value)))):
                        raise ValueError("Invalid backend timer fields")
        return data
    except (OSError, ValueError, TypeError):
        return {"state": "unavailable", "note": "Running bridge activity not verified"}
    finally:
        conn.close()


def on_demand_status(*, inventory: dict | None = None) -> dict:
    """Preview eligibility only. Never enable backend sleep or wake Docker."""
    try:
        from scripts import on_demand_eligibility
        from scripts import on_demand_settings
        selected = docker_status() if inventory is None else inventory
        report = on_demand_eligibility.eligibility(inventory=selected)
        saved = on_demand_settings.read()
        report["enabled"] = saved["enabled"]
        report["idle_minutes"] = saved["idle_minutes"]
        report["can_activate"] = False  # A read-only report cannot grant activation.
        report["activation_note"] = (
            "can_activate is a read-only diagnostic and never changes the persisted "
            "enabled setting or supervisor state; inspect runtime_activity for live state")
        runtime = on_demand_runtime_status()
        report["runtime_activity"] = runtime
        if saved["enabled"]:
            # Never conceal a newly discovered shared/foreign/unverified
            # Docker deployment just because the bridge was already enabled.
            if report.get("state") != "manual_verification_required":
                # Keep the ownership blocker primary, but do not hide an
                # independent supervisor failure when it is reported as well.
                backends = runtime.get("backends")
                if (runtime.get("mode") == "on-demand"
                        and runtime.get("enabled") is True
                        and isinstance(backends, dict)
                        and any(isinstance(row, dict) and row.get("state") == "failed"
                                for row in backends.values())):
                    report["reason"] = report.get("reason", "Docker ownership unverified") + (
                        "; the running supervisor also reports a failed backend")
                return report
            report["next_gates"] = [
                "Validate cold-boot recovery and fault handling before unattended use"]
            if runtime.get("mode") != "on-demand" or runtime.get("enabled") is not True:
                report["state"] = "unverified"
                report["reason"] = (
                    "On-demand is configured but the running bridge has not confirmed "
                    "activation; restart the bridge and inspect its status")
            elif runtime.get("backends") is None:
                report["state"] = "unverified"
                report["reason"] = (
                    "Bridge reports on-demand enabled but its supervisor is not "
                    "initialized; inspect bridge startup and status")
            else:
                # Inventory and dashboard are separate snapshots: rely on the
                # live supervisor to describe transitions, not old Docker counts.
                backend_states = {row["state"] for row in runtime["backends"].values()}
                if "failed" in backend_states:
                    report["state"] = "failed"
                    report["reason"] = (
                        "The running supervisor reports a failed backend; "
                        "inspect bridge, Docker and firewall-guard logs")
                elif "unverified" in backend_states:
                    report["state"] = "unverified"
                    report["reason"] = (
                        "The supervisor is waiting for Docker inventory verification "
                        "after startup; no backend is automatically woken")
                elif not backend_states.issubset({
                        "running", "sleeping", "waking", "stopping"}):
                    report["state"] = "unverified"
                    report["reason"] = "The supervisor returned unexpected backend state"
                elif "waking" in backend_states or "stopping" in backend_states:
                    report["state"] = "transitioning"
                    report["reason"] = "A verified backend is changing state"
                elif backend_states == {"running"}:
                    report["state"] = "running"
                    report["reason"] = "On-demand runtime is active; reboot recovery is not yet established"
                elif backend_states == {"sleeping"}:
                    report["state"] = "sleeping"
                    report["reason"] = "Both managed backends are asleep; requests may wake them"
                else:
                    report["state"] = "partially_running"
                    report["reason"] = "One backend is asleep and the other is running"
        return report
    except (OSError, ValueError, ImportError, TypeError, KeyError):
        return {"mode": "read-only", "feature": "on-demand-backends",
                "enabled": False, "can_activate": False, "state": "blocked",
                "reason": "Unable to verify on-demand prerequisites"}


def configure_on_demand(*, enabled: bool, idle_minutes: int = 30,
                        wake_before_disable: bool = False) -> dict:
    """Explicit operator action, never callable from an MCP tool request."""
    from scripts import on_demand_eligibility, on_demand_settings
    from scripts import on_demand_docker, on_demand_supervisor
    opts = on_demand_eligibility.validate_options(
        enabled=enabled, idle_minutes=idle_minutes)
    if type(wake_before_disable) is not bool or enabled and wake_before_disable:
        raise ValueError("Invalid on-demand option combination")
    if enabled:
        candidate = on_demand_eligibility.eligibility(inventory=docker_status())
        if candidate["state"] != "manual_verification_required":
            raise ValueError("Dedicated Docker ownership or restart policies are not safe")
        if (not on_demand_docker._safe_target("crawl4ai").running
                or not on_demand_docker._safe_target("searxng").running):
            raise ValueError("Both verified backends must be running before first activation")
        if (not on_demand_supervisor._healthy("crawl4ai")
                or not on_demand_supervisor._healthy("searxng")
                or not on_demand_supervisor._probe_crawler()):
            raise ValueError("Required local health or live root-owned egress probe failed")
        return on_demand_settings.save(**opts)
    # A running bridge is another process. Wait for any in-progress idle
    # stop/wake and hold the same lock through OFF persistence and optional
    # restart; otherwise an already claimed stop could run after the wake.
    from scripts import on_demand_coordination
    with on_demand_coordination.exclusive():
        on_demand_settings.save(**opts)
        if wake_before_disable:
            on_demand_docker.start("searxng", health=on_demand_supervisor._healthy)
            on_demand_docker.start(
                "crawl4ai", health=on_demand_supervisor._healthy,
                crawler_preflight=on_demand_supervisor._preflight_crawler,
                crawler_probe=on_demand_supervisor._probe_crawler)
    return opts


def local_update_status() -> dict:
    """Inspect the local Git revision only; no remote or service operations."""
    try:
        from scripts import update_preflight
        return update_preflight.report()
    except (OSError, ValueError, ImportError):
        return {"mode": "read-only", "state": "unavailable",
                "local_revision": "Local update inspection unavailable",
                "remote_checked": False, "updates_applied": False}


def status_report() -> dict:
    docker = docker_status()
    return {
        "mode": "read-only",
        "local_health": health(),
        "user_systemd_units": user_unit_states(),
        "docker_project": docker,
        "on_demand_preflight": on_demand_status(inventory=docker),
        "tunnel_note": "Listening locally does not prove ChatGPT account connection or remote tunnel registration",
    }


def check_report() -> dict:
    dependencies = dependency_report()
    installed = {row["package"]: row["installed"]
                 for row in dependencies["packages"]}
    result = {
        "mode": "read-only",
        "python_compatible": sys.version_info >= (3, 14),
        "dependency_versions_in_project_venv": dependencies,
        "project_venv_present": project_python() is not None,
        "configuration": configured_sources(),
        "runtime": status_report(),
    }
    # Report dedicated Docker ownership separately from local tool health.
    # Existing installations may intentionally use working shared backends;
    # they remain supported for ordinary MCP use, not on-demand lifecycle.
    docker = result["runtime"].get("docker_project", {})
    services = docker.get("verified_services", {})
    owned_pair = (type(services) is dict
                  and set(services) == {"crawl4ai", "searxng"}
                  and all(type(services[s]) is int and services[s] == 1
                          for s in ("crawl4ai", "searxng")))
    docker_ready = bool(
        docker.get("state") == "running"
        and type(docker.get("managed_running")) is int
        and docker["managed_running"] == 2
        and type(docker.get("managed_stopped")) is int
        and docker["managed_stopped"] == 0
        and type(docker.get("unverified_project_containers")) is int
        and docker["unverified_project_containers"] == 0
        and type(docker.get("other_published_crawlers")) is int
        and docker["other_published_crawlers"] == 0
        and owned_pair)
    result["dedicated_docker_ready"] = docker_ready
    result["ready"] = bool(result["python_compatible"]
                           and dependencies["state"] == "available"
                           and all(installed.get(name) for name in
                                   ("fastmcp", "httpx", "idna"))
                           and result["configuration"]["available"]
                           and result["runtime"]["local_health"]["status"]
                           in ("healthy", "sleeping")
                           and docker.get("state") != "unverified"
                           and type(docker.get("unverified_project_containers", 0)) is int
                           and docker.get("unverified_project_containers", 0) == 0)
    result["security_note"] = ("Local readiness does not establish remote tunnel "
                               "registration, crawler firewall isolation, or "
                               "ownership of other published Docker endpoints")
    return result


def print_chatgpt_steps() -> None:
    print("ChatGPT/OpenAI tunnel is the preferred MCP client.")
    print("1. Review the existing configuration using ./setup.sh --check.")
    print("2. Ensure your existing SearXNG and Crawl4AI services are healthy,")
    print("   and verify crawler egress isolation before enabling remote access.")
    print("3. Use ./start.sh --setup-only for explicit credential/tunnel setup.")
    print("4. Connect the resulting HTTPS MCP endpoint in ChatGPT's supported")
    print("   app/connector settings and approve it manually in your account.")
    print("   Account-side enrollment is not automated by this wizard.")
    print("No settings for Cursor, Claude, or Hermes are installed or changed.")


def print_local_client_steps(client: str) -> None:
    """Print opt-in, local-only MCP configuration instructions without writes."""
    endpoint = "http://127.0.0.1:8790/mcp"
    if client == "cursor":
        print("Cursor IDE (including Grok selected as the model):")
        print("Open ~/.cursor/mcp.json; if absent, create it with")
        print('  {"mcpServers": {}}')
        print("If mcpServers is missing from an existing JSON object, add it")
        print("as an object without deleting other settings or servers.")
        print("Then add this MEMBER inside the mcpServers object:")
        print(json.dumps({"local-search": {"url": endpoint}}, indent=2))
        print("Do not nest another mcpServers wrapper or replace the whole file.")
        print("If local-search already exists, choose an unused server name")
        print("(such as local-research) instead of overwriting the entry.")
        print("Restart or refresh Cursor MCP connections and verify web_search.")
        print("This loopback URL is for Cursor running on THIS HOST only.")
        print("A remote Cursor/Grok cloud bot cannot use 127.0.0.1 on your laptop;")
        print("it needs separately authenticated HTTPS access, not the OpenAI tunnel.")
    elif client == "claude":
        print("Claude Code CLI running on THIS HOST (not Claude.ai cloud):")
        print("Inside Docker, a devcontainer or another machine, 127.0.0.1")
        print("does NOT refer to this laptop; use a separately secured endpoint.")
        print("Choose an unused MCP name; do not overwrite an existing entry.")
        print("Inspect current names before adding anything: claude mcp list")
        print("If local-search exists, replace the example name with an unused")
        print("name in the add command, then verify that name with claude mcp list.")
        print("Local-scope registration (run manually):")
        print("  claude mcp add --transport http local-search " + endpoint)
        print("Verify via claude mcp list; within Claude Code use /mcp.")
        print("The default scope is local (private to this directory).")
        print("--scope project writes shared .mcp.json; --scope user is global.")
        print("Select wider scopes only deliberately after reviewing the tools.")
        print("The wizard does not invoke claude, write .mcp.json/.claude.json,")
        print("or touch existing MCP entries or other agent configurations.")
        print("Claude.ai, Cowork and Claude Desktop REMOTE connectors are cloud-brokered")
        print("and cannot use 127.0.0.1 on your laptop. They require a separate")
        print("authenticated HTTPS MCP endpoint, not the OpenAI secure tunnel.")
        print("Local Claude Desktop config is a separate integration; this")
        print("wizard offers only Claude Code's supported HTTP CLI method.")
    elif client == "hermes":
        print("Hermes Agent running on THIS HOST:")
        print("Choose an unused MCP server name; local-search is just an example.")
        print("Do not overwrite an existing local-search entry. If it exists,")
        print("substitute an unused name in both add and test commands.")
        print("  hermes mcp add local-search --url " + endpoint)
        print("  hermes mcp test local-search")
        print("  hermes mcp list")
        print("Within a running Hermes chat: /reload-mcp")
        print("Hermes also supports url-based entries in ~/.hermes/config.yaml.")
        print("Do not change any existing mcp_servers entries or agent secrets.")
        print("If Hermes runs inside Docker or remotely, 127.0.0.1 is not this")
        print("laptop; use a separately secured and reachable endpoint.")
    else:
        raise ValueError("Unsupported optional MCP client")
    print("No client configuration, service, Docker resource or credential was changed.")


# Keep the original action implementation and all CLI flags. This is
# presentation only: top-level choices organize existing operations.
MENU_GROUPS = {
    "1": ("Setup", (
        ("3", "Guided setup / reconfigure (requires CONFIGURE)"),
        ("4", "Preview project systemd units (read-only)"),
        ("7", "Stop verified project services / backends (requires STOP)"),
        ("17", "Choose SearXNG search engines (recommended or custom)"),
    )),
    "2": ("Connections", (
        ("2", "Connect ChatGPT (manual account steps)"),
        ("12", "Connect Cursor, Claude Code or Hermes (manual steps)"),
        ("11", "Install protected ChatGPT tunnel (sudo; requires SECURE)"),
    )),
    "3": ("Status", (
        ("1", "Check existing setup / status (read-only)"),
        ("5", "Installed Python dependency versions (read-only)"),
        ("8", "Docker ownership and shared-backend audit (read-only)"),
        ("9", "On-demand configuration / eligibility (read-only)"),
        ("13", "On-demand reboot prerequisites (read-only)"),
        ("14", "Maintenance versions (offline; opt-in public lookup)"),
    )),
    "4": ("Update", (
        ("6", "Attended MCP update (FETCH then UPDATE; temporary downtime)"),
    )),
    "5": ("Advanced", (
        ("10", "Configure optional on-demand backends (requires confirmation)"),
        ("15", "Verify private backup / isolated recovery (offline)"),
        ("16", "Create private state archive (requires BACKUP)"),
    )),
}


def dry_run() -> dict:
    return {
        "mode": "preview-only",
        "default_client": "ChatGPT (manual account connection)",
        "groups": [name for name, _ in MENU_GROUPS.values()],
        "actions": [
            {"menu": "3.1", "effect": "read-only", "action": "redacted config and local status"},
            {"menu": "2.1", "effect": "read-only", "action": "ChatGPT connection instructions"},
            {"menu": "1.1", "effect": "potential changes after explicit confirmation",
             "action": "./start.sh --setup-only (may install dependencies, download tunnel client, save credentials/profile)"},
            {"menu": "1.2", "effect": "read-only",
             "action": "preview existing systemd user unit templates"},
            {"menu": "3.2", "effect": "read-only",
             "action": "installed dependencies from project .venv, without PyPI requests"},
            {"menu": "4.1", "effect": "attended change after FETCH and UPDATE confirmations",
             "action": "./setup.sh --update (owned service stop, existing backup, reviewed fast-forward and restart)"},
            {"menu": "1.3", "effect": "potential service changes after explicit confirmation",
             "action": "./stop.sh (project-owned shutdown only; no shared Hermes Web stack)"},
            {"menu": "1.4", "effect": "local engine settings and project-owned SearXNG restart after APPLY",
             "action": "choose Yahoo recommended or pick loaded general engines; no dashboard or MCP admin tool"},
            {"menu": "3.3", "effect": "read-only",
             "action": "verify ownership of private Compose project; flag foreign published crawlers"},
            {"menu": "3.4", "effect": "read-only",
             "action": "preview optional on-demand eligibility; never enable, sleep or wake"},
            {"menu": "5.1", "effect": "optional Docker sleep/wake changes only after confirmation",
             "action": "configure validated on-demand backends with operator confirmation"},
            {"menu": "2.3", "effect": "privileged system changes only after confirmation",
             "action": "validate dedicated backends; install fixed egress guard, restricted sudoers and protected ChatGPT tunnel"},
            {"menu": "2.2", "effect": "read-only",
             "action": "show optional local Cursor, Claude Code or Hermes HTTP MCP instructions; never edit third-party config"},
            {"menu": "3.5", "effect": "read-only",
             "action": "inspect on-demand reboot prerequisites; no sudo, Docker mutation or live firewall probes"},
            {"menu": "3.6", "effect": "read-only; public HTTPS requests only after explicit confirmation",
             "action": "reuse existing consolidated local maintenance versions report; optional bounded public PyPI/GitHub lookups"},
            {"menu": "5.2", "effect": "offline validation; disposable temporary files for smoke tests",
             "action": "verify a selected private state backup or smoke-test isolated fresh-install recovery; never activate live restore"},
            {"menu": "5.3", "effect": "private archive creation only after explicit BACKUP confirmation",
             "action": "create an atomic, private, no-overwrite backup using the existing archive tool; no service changes"},
        ],
        "limitations": "Optional on-demand backend sleep/wake is implemented and off by default; reboot/fault-recovery still requires host validation",
    }


def confirm(word: str) -> bool:
    try:
        return input("Type " + word + " to confirm, anything else cancels: ").strip() == word
    except (EOFError, KeyboardInterrupt):
        return False


def maintenance_versions_report(*, check_remote: bool = False) -> dict:
    """Reuse one existing checker; importing it does not request public URLs."""
    from scripts import maintenance_versions
    return maintenance_versions.report(check_remote=check_remote)


def _operator_archive_path(filename: Path) -> Path:
    """Resolve relative archive names against the setup.sh caller's directory.

    setup.sh changes to the repository directory before launching Python.
    Direct module callers instead naturally use their own working directory.
    The caller directory is provided by the fixed shell wrapper only.
    """
    filename = filename.expanduser()
    if filename.is_absolute():
        return filename
    base = Path(os.environ.get("LOCAL_SEARCH_MCP_SETUP_CALLER_CWD") or os.getcwd())
    if not base.is_absolute() or not base.is_dir() or base.is_symlink():
        raise ValueError("Invalid invoking directory")
    return base / filename


def backup_check(archive: Path, *, smoke: bool = False) -> dict:
    """Reuse existing offline archive checks; never restore to live paths.

    Do not expose archive filenames, domain approvals, search history, or
    underlying exception text in a user-facing response.
    """
    result = {
        "mode": "isolated-fresh-install-smoke" if smoke else "archive-integrity",
        "status": "failed",
        "live_restore_performed": False,
        "remote_checked": False,
    }
    try:
        # Expand the operator's home only inside the redaction boundary:
        # a missing home account may raise RuntimeError containing local data.
        archive = _operator_archive_path(archive)
        if smoke:
            from scripts import restore_smoke
            policies, events = restore_smoke.smoke(archive)
            result["policy_entries"] = policies
            result["recent_indexed_events"] = events
        else:
            from scripts import backup_state
            result["verified_files"] = len(backup_state.verified_payload(archive))
        result["status"] = "passed"
    except Exception:
        # Privacy boundary: ZIP decompression, compression-method, SQLite and
        # filesystem failures can raise different exception classes. Never
        # expose exception text or traceback from a selected private archive.
        result["note"] = (
            "Archive validation failed; inspect permissions, integrity and "
            "format locally without exposing private archive contents"
        )
    return result


def create_private_backup(destination: Path) -> dict:
    """Reuse the private no-overwrite archiver after explicit operator consent.

    Never display the destination name or untrusted exception details. A
    failure can happen after the atomic link: do not claim the archive
    definitely does not exist, and never retry by overwriting it.
    """
    report = {"mode": "private-backup-create", "status": "failed",
              "live_restore_performed": False, "remote_checked": False}
    try:
        from scripts import backup_state
        backup_state.backup(_operator_archive_path(destination))
        report["status"] = "created"
    except (Exception, KeyboardInterrupt):
        report["note"] = ("Private backup creation failed; verify the destination "
                          "and source permissions locally before retrying. "
                          "An existing archive is never overwritten.")
    return report


def menu() -> int:
    if not sys.stdin.isatty():
        print("ERROR: interactive setup requires a TTY; see ./setup.sh --help",
              file=sys.stderr)
        return 2
    if os.geteuid() == 0:
        print("ERROR: run interactive setup as your normal Linux user",
              file=sys.stderr)
        return 2
    actions = {
        "3": ("CONFIGURE", ("bootstrap.py", "--setup-only")),
        "7": ("STOP", ("scripts/stop_stack.py",)),
    }
    while True:
        print("\nlocal-search-mcp setup (ChatGPT first)")
        for number, (name, _) in MENU_GROUPS.items():
            print(number + "  " + name)
        print("0  Exit")
        try:
            group = input("Choose group: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExited without further changes")
            return 0
        if group == "0":
            return 0
        if group not in MENU_GROUPS:
            print("Unknown group; no changes made")
            continue
        title, entries = MENU_GROUPS[group]
        print("\n" + title + " actions")
        for number, (_, description) in enumerate(entries, start=1):
            print(str(number) + "  " + description)
        print("0  Back")
        try:
            selection = input("Choose action: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExited without further changes")
            return 0
        if selection == "0":
            continue
        if not selection.isascii() or not selection.isdecimal():
            print("Unknown action; no changes made")
            continue
        position = int(selection)
        if not 1 <= position <= len(entries):
            print("Unknown action; no changes made")
            continue
        choice = entries[position - 1][0]
        if choice == "1":
            print(json.dumps(check_report(), indent=2, ensure_ascii=True))
        elif choice == "2":
            print_chatgpt_steps()
        elif choice == "4":
            run_existing("scripts/install_user_services.py")
        elif choice == "5":
            print(json.dumps(dependency_report(), indent=2, ensure_ascii=True))
        elif choice == "8":
            print(json.dumps(docker_status(), indent=2, ensure_ascii=True))
        elif choice == "9":
            print(json.dumps(on_demand_status(), indent=2, ensure_ascii=True))
        elif choice == "10":
            print(json.dumps(on_demand_status(), indent=2, ensure_ascii=True))
            try:
                action = input("Enable (ON), disable (OFF), or cancel: ").strip()
                if action == "ON":
                    idle_text = input("Idle timeout, minutes (1-1440; default 30): ").strip()
                    minutes = 30 if not idle_text else int(idle_text)
                    print("Requires dedicated backends, installed root-owned egress")
                    print("probe and Docker permissions. Cold starts add latency.")
                    if confirm("ENABLE"):
                        print(json.dumps(configure_on_demand(
                            enabled=True, idle_minutes=minutes), indent=2))
                        print("Restart the bridge to apply the new mode.")
                    else:
                        print("Cancelled")
                elif action == "OFF":
                    wake = input("Start any sleeping managed backends first? (YES/no): ").strip() == "YES"
                    if confirm("DISABLE"):
                        print(json.dumps(configure_on_demand(
                            enabled=False, wake_before_disable=wake), indent=2))
                        print("Restart the bridge to finish disabling the supervisor.")
                    else:
                        print("Cancelled")
                else:
                    print("Cancelled")
            except (ValueError, OSError, EOFError, KeyboardInterrupt) as exc:
                print("On-demand change was not fully applied: " +
                      type(exc).__name__ + "; inspect status before retrying")
        elif choice == "13":
            from scripts import on_demand_boot_audit
            print(json.dumps(on_demand_boot_audit.audit(), indent=2, ensure_ascii=True))
        elif choice == "14":
            public_started = False
            try:
                mode = input("Versions: LOCAL (default), REMOTE, or cancel: ").strip().upper()
                if mode in ("", "LOCAL"):
                    print(json.dumps(maintenance_versions_report(check_remote=False),
                                     indent=2, ensure_ascii=True))
                elif mode == "REMOTE":
                    print("REMOTE checks request public PyPI/GitHub release and commit metadata over HTTPS.")
                    print("No downloads, installs, Git fetch, Docker changes or restarts.")
                    if confirm("PUBLIC"):
                        public_started = True
                        print(json.dumps(maintenance_versions_report(check_remote=True),
                                         indent=2, ensure_ascii=True))
                    else:
                        print("Cancelled; no public version checks run")
                else:
                    print("Cancelled; no public version checks run")
            except (EOFError, KeyboardInterrupt):
                if public_started:
                    print("Interrupted; public version lookups may have started; "
                          "no updates or service changes were made")
                else:
                    print("Cancelled; no public version checks run")
        elif choice == "15":
            try:
                name = input("Private backup archive path (blank cancels): ").strip()
                if not name:
                    print("Cancelled; no archive read")
                else:
                    variant = input("Check: VERIFY (default) or SMOKE (isolated): ").strip().upper()
                    if variant in ("", "VERIFY", "SMOKE"):
                        check = backup_check(Path(name), smoke=variant == "SMOKE")
                        print(json.dumps(check, indent=2, ensure_ascii=True))
                    else:
                        print("Cancelled; no archive read")
            except (EOFError, KeyboardInterrupt):
                print("Archive validation interrupted; any temporary scratch "
                      "files are managed by the existing validators; no live restore requested")
        elif choice == "16":
            try:
                name = input("New private backup filename (blank cancels): ").strip()
                if not name:
                    print("Cancelled; no backup requested")
                else:
                    print("This creates a private local archive of policy and history")
                    print("and validated nonsecret settings. It contains sensitive data.")
                    print("It never overwrites an existing archive or stops services.")
                    print("Stop writing services first for a consistent multi-file snapshot.")
                    if confirm("BACKUP"):
                        print(json.dumps(create_private_backup(Path(name)),
                                         indent=2, ensure_ascii=True))
                    else:
                        print("Cancelled; no backup requested")
            except (EOFError, KeyboardInterrupt):
                print("Backup operation interrupted; check the destination locally "
                      "before retrying. No existing archive is overwritten.")
        elif choice == "12":
            try:
                client = input("Client (cursor/claude/hermes), or enter to cancel: ").strip().lower()
                if client in ("cursor", "claude", "hermes"):
                    print_local_client_steps(client)
                else:
                    print("Cancelled; no client settings changed")
            except (EOFError, KeyboardInterrupt):
                print("Cancelled; no client settings changed")
        elif choice == "11":
            if run_existing("scripts/install_secure_tunnel.py") != 0:
                print("Read-only prerequisites were not met; no privileged installation attempted.")
            elif confirm("SECURE"):
                code = run_existing("scripts/install_secure_tunnel.py",
                                    "--install", "--confirm", "SECURE")
                print("Secure tunnel setup exited with status " + str(code))
            else:
                print("Cancelled; no privileged installation attempted")
        elif choice == "17":
            code = run_existing("scripts/searxng_engines.py")
            print("Search-engine setup exited with status " + str(code))
        elif choice == "6":
            print("After FETCH and UPDATE confirmations, this stops owned services,")
            print("backs up policy/history, fast-forwards master and updates dependencies.")
            print("Partial failures require operator-controlled recovery.")
            run_existing("scripts/attended_update.py")
        elif choice in actions:
            word, command = actions[choice]
            if choice == "3":
                print("This may install Python packages, download the verified")
                print("tunnel client, and save private credentials or a profile.")
                print("Existing backends must be healthy; no other clients are touched.")
            else:
                print("Stops only verified project-owned resources; does NOT")
                print("stop legacy/shared Hermes Web or change root firewall rules.")
            if confirm(word):
                try:
                    code = run_existing(*command)
                    print("Command exited with status " + str(code))
                    if choice == "3" and code == 0:
                        print("Next: Setup > Choose SearXNG search engines for reliable defaults.")
                except OSError:
                    print("Command unavailable; no success claimed")
            else:
                print("Cancelled")
        else:
            print("Unknown selection; no changes made")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ChatGPT-first setup and maintenance (conservative first slice of issue #74)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  ./setup.sh                       Interactive menu (requires a TTY)
  ./setup.sh --help                Complete help; no dependencies or side effects
  ./setup.sh --check               Read-only redacted config, local health, service status
  ./setup.sh --status              Read-only health and systemd status
  ./setup.sh --dry-run             Read-only menu action preview
  ./setup.sh --chatgpt             Read-only manual ChatGPT connector instructions
  ./setup.sh --client cursor       Read-only local Cursor / Grok-in-Cursor MCP instructions
  ./setup.sh --client hermes       Read-only local Hermes Agent MCP instructions
  ./setup.sh --client claude       Read-only local Claude Code HTTP MCP instructions
  ./setup.sh --docker-status       Read-only verified Compose/Docker ownership inventory
  ./setup.sh --search-engines      Choose installed SearXNG engines; APPLY to save/restart
  ./setup.sh --update              Attended update, backup and controlled restart (requires TTY)
  ./setup.sh --update-status       Read-only LOCAL Git revision check; no network fetch
  ./setup.sh --maintenance-versions  Read-only local dependency, Git, crawler, tunnel inventory
  ./setup.sh --maintenance-versions-remote
                                  Explicit public PyPI/GitHub metadata lookups; no updates
  ./setup.sh --backup-create NEW.zip --confirm-backup BACKUP
                                  Create private no-overwrite archive after explicit consent
  ./setup.sh --backup-verify PRIVATE.zip
                                  Offline integrity and SQLite check; archive is not modified
  ./setup.sh --restore-smoke PRIVATE.zip
                                  Offline isolated fresh-install smoke; disposable temp files only
  ./setup.sh --on-demand-status    Read-only Docker ownership/eligibility and configured mode
  ./setup.sh --boot-audit          Read-only systemd, firewall-install and Docker restart-policy prerequisites
  ./setup.sh --secure-tunnel-preview
                                  Read-only preview of guarded production tunnel setup
  ./setup.sh --secure-tunnel --confirm-secure-tunnel SECURE
                                  Install root-owned firewall, restricted sudoers, protected tunnel
  ./setup.sh --on-demand-enable --idle-minutes 30 --confirm-on-demand ENABLE
                                  Explicit validated opt-in with root-owned egress probe
  ./setup.sh --on-demand-disable --confirm-on-demand DISABLE
                                  Stop future on-demand wake attempts
  ./setup.sh --on-demand-disable --wake-before-disable --confirm-on-demand DISABLE
                                  Start previously sleeping verified backends first

Default integration: ChatGPT only (account connection is manual).
Other clients are not configured. On-demand backend sleep is OFF by default.
Activation requires an operator-installed fixed probe service and validated
dedicated Docker deployment. Mutations require an explicit confirmation.
The existing ./start.sh and ./stop.sh remain supported independently.""")
    choices = parser.add_mutually_exclusive_group()
    choices.add_argument("--check", action="store_true",
                         help="read-only redacted config, prerequisites, local health and unit checks")
    choices.add_argument("--status", action="store_true",
                         help="read-only service and local health status; no Docker mutation")
    choices.add_argument("--dry-run", action="store_true",
                         help="preview wizard commands and effects without prompts or writes")
    choices.add_argument("--chatgpt", action="store_true",
                         help="show manual ChatGPT/OpenAI tunnel connection steps")
    choices.add_argument("--client", choices=("cursor", "claude", "hermes"),
                         help="read-only manual instructions for Cursor, Claude Code or Hermes")
    choices.add_argument("--docker-status", action="store_true",
                         help="read-only Docker ownership audit (pinned local daemon, no changes)")
    choices.add_argument("--update", action="store_true",
                         help="attended Git fast-forward, state backup and controlled restart")
    choices.add_argument("--search-engines", action="store_true",
                         help="interactive operator-only SearXNG engine selection")
    choices.add_argument("--update-status", action="store_true",
                         help="inspect local Git revision; never fetch/pull/restart")
    choices.add_argument("--maintenance-versions", action="store_true",
                         help="read-only local maintenance versions; no public requests or changes")
    choices.add_argument("--maintenance-versions-remote", action="store_true",
                         help="explicit public PyPI/GitHub release checks; never download or apply updates")
    choices.add_argument("--backup-create", type=Path, metavar="NEW.zip",
                         help="create a private no-overwrite state archive; requires --confirm-backup BACKUP")
    choices.add_argument("--backup-verify", type=Path, metavar="PRIVATE.zip",
                         help="verify private backup archive and SQLite integrity; no live restore")
    choices.add_argument("--restore-smoke", type=Path, metavar="PRIVATE.zip",
                         help="offline recovery test in temporary isolated state; no live restore")
    choices.add_argument("--on-demand-status", action="store_true",
                         help="read-only configured on-demand and runtime state; no Docker changes")
    choices.add_argument("--boot-audit", action="store_true",
                         help="read-only reboot prerequisites; no sudo, Docker mutations or firewall probes")
    choices.add_argument("--secure-tunnel-preview", action="store_true",
                         help="read-only preview of production firewall/tunnel setup")
    choices.add_argument("--secure-tunnel", action="store_true",
                         help="install reviewed root guard, restricted probe and protected ChatGPT tunnel")
    choices.add_argument("--on-demand-enable", action="store_true",
                         help="explicitly enable verified managed backends only")
    choices.add_argument("--on-demand-disable", action="store_true",
                         help="disable future background wake (optionally restore sleeping backends)")
    parser.add_argument("--idle-minutes", type=int, default=30,
                        help="validated idle time in minutes (default 30 minutes)")
    parser.add_argument("--wake-before-disable", action="store_true")
    parser.add_argument("--confirm-on-demand", choices=("ENABLE", "DISABLE"),
                        help="explicit confirmation required for noninteractive changes")
    parser.add_argument("--confirm-secure-tunnel", choices=("SECURE",),
                        help="explicit acknowledgement for privileged tunnel installation")
    parser.add_argument("--confirm-backup", choices=("BACKUP",),
                        help="explicit acknowledgement to write a new private archive")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    # Validate confirmation flags before any early-returning command dispatch.
    if args.confirm_backup is not None and args.backup_create is None:
        raise SystemExit("--confirm-backup requires --backup-create")
    if args.secure_tunnel or args.secure_tunnel_preview:
        if args.confirm_on_demand is not None or args.wake_before_disable:
            raise SystemExit("Unrelated on-demand options cannot be combined with tunnel setup")
        if args.secure_tunnel and args.confirm_secure_tunnel != "SECURE":
            raise SystemExit("Explicit --confirm-secure-tunnel SECURE required")
        if args.secure_tunnel_preview and args.confirm_secure_tunnel is not None:
            raise SystemExit("Preview does not accept confirmation flags")
        if os.geteuid() == 0:
            raise SystemExit("Run setup as the normal user")
        return run_existing("scripts/install_secure_tunnel.py", *(
            ("--install", "--confirm", "SECURE") if args.secure_tunnel else ()))
    if args.confirm_secure_tunnel is not None:
        raise SystemExit("--confirm-secure-tunnel requires --secure-tunnel")
    if args.on_demand_enable or args.on_demand_disable:
        expected = "ENABLE" if args.on_demand_enable else "DISABLE"
        if (args.confirm_on_demand != expected
                or args.wake_before_disable and args.on_demand_enable):
            raise SystemExit("Explicit --confirm-on-demand " + expected + " required")
        if os.geteuid() == 0:
            raise SystemExit("Configure on-demand mode as the normal user")
        try:
            value = configure_on_demand(
                enabled=args.on_demand_enable, idle_minutes=args.idle_minutes,
                wake_before_disable=args.wake_before_disable)
        except (ValueError, OSError) as exc:
            print("On-demand configuration failed safely (" +
                  type(exc).__name__ + ")", file=sys.stderr)
            return 1
        print(json.dumps(value, sort_keys=True))
        print("Restart the bridge after changing on-demand mode.")
        return 0
    if args.wake_before_disable or args.confirm_on_demand is not None:
        raise SystemExit("On-demand options require explicit enable/disable mode")
    if args.search_engines:
        if os.geteuid() == 0:
            raise SystemExit("Run search-engine setup as the normal user")
        return run_existing("scripts/searxng_engines.py")
    if args.update:
        if os.geteuid() == 0:
            raise SystemExit("Run update as the normal user")
        return run_existing("scripts/attended_update.py")
    if args.backup_create is not None:
        if args.confirm_backup != "BACKUP":
            raise SystemExit("Creating a private archive requires --confirm-backup BACKUP")
        report = create_private_backup(args.backup_create)
        print(json.dumps(report, indent=2, ensure_ascii=True))
        return 0 if report["status"] == "created" else 1
    if args.dry_run:
        print(json.dumps(dry_run(), indent=2))
        return 0
    if args.chatgpt:
        print_chatgpt_steps()
        return 0
    if args.client:
        print_local_client_steps(args.client)
        return 0
    if args.docker_status:
        print(json.dumps(docker_status(), indent=2, ensure_ascii=True))
        return 0
    if args.update_status:
        print(json.dumps(local_update_status(), indent=2, ensure_ascii=True))
        return 0
    if args.maintenance_versions or args.maintenance_versions_remote:
        report = maintenance_versions_report(check_remote=args.maintenance_versions_remote)
        print(json.dumps(report, indent=2, ensure_ascii=True))
        return 0
    if args.backup_verify is not None or args.restore_smoke is not None:
        report = backup_check(
            args.backup_verify if args.backup_verify is not None else args.restore_smoke,
            smoke=args.restore_smoke is not None)
        print(json.dumps(report, indent=2, ensure_ascii=True))
        return 0 if report["status"] == "passed" else 1
    if args.on_demand_status:
        print(json.dumps(on_demand_status(), indent=2, ensure_ascii=True))
        return 0
    if args.boot_audit:
        from scripts import on_demand_boot_audit
        report = on_demand_boot_audit.audit()
        print(json.dumps(report, indent=2, ensure_ascii=True))
        return 0 if report["state"] == "ready_for_manual_reboot_test" else 1
    if args.status:
        print(json.dumps(status_report(), indent=2, ensure_ascii=True))
        return 0
    if args.check:
        report = check_report()
        print(json.dumps(report, indent=2, ensure_ascii=True))
        return 0 if report["ready"] else 1
    return menu()


if __name__ == "__main__":
    raise SystemExit(main())
