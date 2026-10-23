#!/usr/bin/env python3
"""Conservative, ChatGPT-first interactive frontend for existing maintenance tools.

No setup or service mutations occur from import, --help, --check, --status,
--dry-run or --chatgpt. Interactive changes are individually confirmed.
"""
from __future__ import annotations

import argparse
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


def status_report() -> dict:
    return {
        "mode": "read-only",
        "local_health": health(),
        "user_systemd_units": user_unit_states(),
        "docker_containers": "not inspected; use the documented ownership-specific Docker checks",
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
    # A healthy listener is not sufficient if the project interpreter is
    # missing or its three required direct packages are unavailable.
    result["ready"] = bool(result["python_compatible"]
                           and dependencies["state"] == "available"
                           and all(installed.get(name) for name in
                                   ("fastmcp", "httpx", "idna"))
                           and result["configuration"]["available"]
                           and result["runtime"]["local_health"]["status"] == "healthy")
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
            {"menu": "6", "effect": "read-only", "action": "manual update and rollback instructions; no fetch/pull"},
            {"menu": "7", "effect": "potential service changes after explicit confirmation",
             "action": "./stop.sh (project-owned shutdown only; no shared Hermes Web stack)"},
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
        elif choice == "6":
            print("No automatic Git fetch, pull, image download, pip install or restart.")
            print("Review the worktree and deployment rollback gates in")
            print("DEPENDENCY-UPDATES.md, BACKUP.md, and SYSTEMD.md.")
            print("Use existing explicit update procedures outside this wizard.")
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

Default integration: ChatGPT only (account connection is manual).
Other clients are not configured. On-demand Docker backend sleep from #75
is not yet implemented (future opt-in idle timeout: 30 minutes).
Mutating actions require a typed confirmation in the interactive menu.
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    if args.dry_run:
        print(json.dumps(dry_run(), indent=2))
        return 0
    if args.chatgpt:
        print_chatgpt_steps()
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
