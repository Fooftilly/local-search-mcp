#!/usr/bin/env python3
"""Conservative, ChatGPT-first interactive frontend for existing maintenance tools.

No setup or service mutations occur from import, --help, --check, --status,
--dry-run or --chatgpt. Interactive changes are individually confirmed.
"""
from __future__ import annotations

import argparse
from http.client import HTTPConnection
import json
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
        if type(data["enabled"]) is not bool or type(data["idle_minutes"]) is not int:
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
        report["can_activate"] = False  # Diagnostic is never an authorization.
        report["runtime_activity"] = on_demand_runtime_status()
        if saved["enabled"]:
            report["state"] = ("sleeping" if selected.get("state") == "stopped"
                               else "running" if selected.get("state") == "running"
                               else "partially_running" if selected.get("state") == "partially_running"
                               else "failed")
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
    # Persist OFF first, preventing further supervisor wake attempts. An
    # optional deliberate wake may still need the fixed egress probe.
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
        "python_compatible": sys.version_info >= (3, 11),
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


def dry_run() -> dict:
    return {
        "mode": "preview-only",
        "default_client": "ChatGPT (manual account connection)",
        "actions": [
            {"menu": "1", "effect": "read-only", "action": "redacted config and local status"},
            {"menu": "2", "effect": "read-only", "action": "ChatGPT connection instructions"},
            {"menu": "3", "effect": "potential changes after explicit confirmation",
             "action": "./start.sh --setup-only (may install dependencies, download tunnel client, save credentials/profile)"},
            {"menu": "4", "effect": "read-only",
             "action": "preview existing systemd user unit templates"},
            {"menu": "5", "effect": "read-only",
             "action": "installed dependencies from project .venv, without PyPI requests"},
            {"menu": "6", "effect": "read-only",
             "action": "inspect local Git update baseline and show manual rollback guidance; no fetch/pull"},
            {"menu": "7", "effect": "potential service changes after explicit confirmation",
             "action": "./stop.sh (project-owned shutdown only; no shared Hermes Web stack)"},
            {"menu": "8", "effect": "read-only",
             "action": "verify ownership of private Compose project; flag foreign published crawlers"},
            {"menu": "9", "effect": "read-only",
             "action": "preview optional on-demand eligibility; never enable, sleep or wake"},
        ],
        "future": "Issue #75 optional 30-minute backend sleep is not implemented; off by default",
    }


def confirm(word: str) -> bool:
    try:
        return input("Type " + word + " to confirm, anything else cancels: ").strip() == word
    except (EOFError, KeyboardInterrupt):
        return False


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
        print("1  Check existing setup / status (read-only)")
        print("2  Connect ChatGPT (manual account steps)")
        print("3  Run existing guided setup (may change configuration)")
        print("4  Preview systemd user units (read-only)")
        print("5  Check installed dependency versions (local-only)")
        print("6  Review manual update and rollback steps")
        print("7  Stop project-owned services and containers (changes state)")
        print("8  Inspect Docker ownership (read-only; no stop/restart)")
        print("9  Inspect on-demand backend configuration and eligibility")
        print("10 Configure optional on-demand backends (privileged checks)")
        print("0  Exit")
        try:
            choice = input("Choose: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExited without further changes")
            return 0
        if choice == "0":
            return 0
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
        elif choice == "6":
            print(json.dumps(local_update_status(), indent=2, ensure_ascii=True))
            print("No automatic Git fetch, pull, image download, pip install or restart.")
            print("For a reviewed backup/rollback, see docs/DEPENDENCY-UPDATES.md,")
            print("BACKUP.md, and docs/SYSTEMD.md. Apply updates explicitly outside")
            print("this wizard only after examining an operator-approved plan.")
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
  ./setup.sh --docker-status       Read-only verified Compose/Docker ownership inventory
  ./setup.sh --update-status       Read-only LOCAL revision/baseline check; no network fetch
  ./setup.sh --on-demand-status    Read-only Docker ownership/eligibility and configured mode
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
    choices.add_argument("--docker-status", action="store_true",
                         help="read-only Docker ownership audit (pinned local daemon, no changes)")
    choices.add_argument("--update-status", action="store_true",
                         help="inspect local Git update baseline; never fetch/pull/restart")
    choices.add_argument("--on-demand-status", action="store_true",
                         help="preview disabled on-demand mode; never sleep/wake or reconfigure")
    choices.add_argument("--on-demand-enable", action="store_true",
                         help="explicitly enable verified managed backends only")
    choices.add_argument("--on-demand-disable", action="store_true",
                         help="disable future background wake (optionally restore sleeping backends)")
    parser.add_argument("--idle-minutes", type=int, default=30,
                        help="validated idle time in minutes (default 30 minutes)")
    parser.add_argument("--wake-before-disable", action="store_true")
    parser.add_argument("--confirm-on-demand", choices=("ENABLE", "DISABLE"),
                        help="explicit confirmation required for noninteractive changes")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
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
    if args.dry_run:
        print(json.dumps(dry_run(), indent=2))
        return 0
    if args.chatgpt:
        print_chatgpt_steps()
        return 0
    if args.docker_status:
        print(json.dumps(docker_status(), indent=2, ensure_ascii=True))
        return 0
    if args.update_status:
        print(json.dumps(local_update_status(), indent=2, ensure_ascii=True))
        return 0
    if args.on_demand_status:
        print(json.dumps(on_demand_status(), indent=2, ensure_ascii=True))
        return 0
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
