#!/usr/bin/env python3
"""Preview, install and remove opt-in systemd user units without enabling them."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MARKER = "# Managed by local-search-mcp; contains no credentials.\n"
NAMES = (
    "local-search-mcp-bridge.service",
    "local-search-mcp-tunnel.service",
    "local-search-mcp-health.service",
    "local-search-mcp-health.timer",
    "local-search-mcp-update.service",
    "local-search-mcp-update.path",
    "local-search-mcp-update.timer",
)


def quote(value: str) -> str:
    """Escape paths with spaces, quotes or systemd percent specifiers."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def unit_files(python: str, root: Path, *, auto_restart: bool = False, reflog_path: Path | None = None) -> dict[str, str]:
    """Generate secrets-free unit content with absolute paths."""
    exe = quote(str(Path(python).resolve()))
    launch = quote(str(root / "bootstrap.py"))
    health = quote(str(root / "health_cli.py"))
    common = (
        "\n[Service]\nType=simple\n"
        "Restart=on-failure\nRestartSec=30s\n"
        "UMask=0077\nNoNewPrivileges=true\n"
    )
    units = {
        NAMES[0]: MARKER + "[Unit]\nDescription=Local SearXNG/Crawl4AI MCP bridge\nStartLimitIntervalSec=300\nStartLimitBurst=5\n" +
                  common + f"ExecStart={exe} {launch} --service bridge\n" +
                  "\n[Install]\nWantedBy=default.target\n",
        NAMES[1]: MARKER + "[Unit]\nDescription=Local research MCP OpenAI tunnel\nStartLimitIntervalSec=300\nStartLimitBurst=5\n"
                  "Wants=local-search-mcp-bridge.service\nAfter=local-search-mcp-bridge.service\n" +
                  common + f"ExecStart={exe} {launch} --service tunnel\n" +
                  "\n[Install]\nWantedBy=default.target\n",
        NAMES[2]: MARKER + "[Unit]\nDescription=Local research MCP health snapshot\n"
                  "\n[Service]\nType=oneshot\nUMask=0077\nNoNewPrivileges=true\n" +
                  f"ExecStart={exe} {health} --json\n",
        NAMES[3]: MARKER + "[Unit]\nDescription=Periodic local research MCP health check\n"
                  "\n[Timer]\nOnBootSec=2min\nOnUnitActiveSec=2min\n"
                  "Unit=local-search-mcp-health.service\n"
                  "\n[Install]\nWantedBy=timers.target\n",
    }
    if auto_restart:
        if reflog_path is None:
            from scripts.restart_on_update import reflog
            reflog_path = reflog(root)
        checker = quote(str(root / "scripts" / "restart_on_update.py"))
        units.update({
            NAMES[4]: MARKER + "[Unit]\\nDescription=Restart local MCP services after local Git updates\\n" +
                      "\\n[Service]\\nType=oneshot\\nUMask=0077\\nNoNewPrivileges=true\\nTimeoutStartSec=90s\\n" +
                      f"ExecStart={exe} {checker}\\n",
            NAMES[5]: MARKER + "[Unit]\\nDescription=Watch local MCP Git HEAD reflog\\n" +
                      "\\n[Path]\\n" + f"PathChanged={quote(str(reflog_path))}\\n" +
                      "Unit=local-search-mcp-update.service\\n" +
                      "\\n[Install]\\nWantedBy=default.target\\n",
            NAMES[6]: MARKER + "[Unit]\\nDescription=Recheck local MCP Git revision without network access\\n" +
                      "\\n[Timer]\\nOnStartupSec=1min\\nOnUnitActiveSec=1min\\nAccuracySec=10s\\n" +
                      "Unit=local-search-mcp-update.service\\n" +
                      "\\n[Install]\\nWantedBy=timers.target\\n",
        })
    return units


def destination() -> Path:
    """Stay within the current account's XDG systemd user-unit directory."""
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))).expanduser() / "systemd" / "user"


def write_owned(path: Path, content: str) -> None:
    """Atomically replace only marker-bearing units, not unrelated files."""
    if path.is_symlink():
        raise ValueError(f"Refusing symlink: {path}")
    if path.exists() and not path.read_text().startswith(MARKER):
        raise ValueError(f"Refusing unmanaged unit: {path}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        os.replace(temporary, path)
        path.chmod(0o644)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    """Preview by default; explicit flags are necessary to install/remove."""
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--install", action="store_true")
    modes.add_argument("--remove", action="store_true")
    modes.add_argument("--remove-auto-restart", action="store_true")
    parser.add_argument("--auto-restart", action="store_true", help="Include opt-in local Git change watcher and timer")
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        parser.error("Run as your normal user, not root")
    if args.auto_restart and (args.remove or args.remove_auto_restart):
        parser.error("--auto-restart is only valid for preview or --install")
    folder = destination()
    files = unit_files(sys.executable, ROOT, auto_restart=args.auto_restart)
    if args.install:
        if args.auto_restart:
            from scripts.restart_on_update import initialize
            initialize(ROOT)
        folder.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            write_owned(folder / name, content)
        print("Installed user units. Run systemctl --user daemon-reload")
    elif args.remove or args.remove_auto_restart:
        for name in (NAMES[4:] if args.remove_auto_restart else NAMES):
            path = folder / name
            if path.is_symlink():
                raise ValueError(f"Refusing symlink: {path}")
            if path.exists():
                if not path.read_text().startswith(MARKER):
                    raise ValueError(f"Refusing unmanaged unit: {path}")
                path.unlink()
        print("Removed requested user units. Run systemctl --user daemon-reload")
    else:
        for name, content in files.items():
            print(f"===== {name} =====\n{content}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
