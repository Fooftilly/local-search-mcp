#!/usr/bin/env python3
"""Preview, install and remove opt-in systemd user units without enabling them."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile
from string import Template

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "config" / "systemd"
MARKER = "# Managed by local-search-mcp; contains no credentials.\n"
NAMES = (
    "local-search-mcp-bridge.service",
    "local-search-mcp-tunnel.service",
    "local-search-mcp-health.service",
    "local-search-mcp-health.timer",
)
# Already-installed watcher units may still exist on user systems.
LEGACY_UPDATE_NAMES = (
    "local-search-mcp-update.service",
    "local-search-mcp-update.path",
    "local-search-mcp-update.timer",
)
GUARD_PROBE = "local-search-mcp-guard-probe.service"
GUARD_PREFLIGHT = "local-search-mcp-guard-preflight.service"


def quote(value: str) -> str:
    """Escape paths with spaces, quotes or systemd percent specifiers."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _render_unit(name: str, substitutions: dict[str, str]) -> str:
    """Install a reviewed repository template, filling only local paths."""
    path = TEMPLATE_DIR / name
    if path.is_symlink():
        raise ValueError("Refusing linked systemd unit template: " + name)
    template = path.read_text(encoding="utf-8")
    if not template.startswith(MARKER):
        raise ValueError("Missing managed-unit marker in source: " + name)
    return Template(template).substitute(substitutions)


def unit_files(python: str, root: Path, *,
               guard_probe: bool = False) -> dict[str, str]:
    """Render only the supported project-owned units (no Git watcher)."""
    fields = {
        "EXE": quote(str(Path(python).resolve())),
        "LAUNCH": quote(str(root / "bootstrap.py")),
        "HEALTH": quote(str(root / "health_cli.py")),
    }
    names = (*NAMES, GUARD_PROBE, GUARD_PREFLIGHT) if guard_probe else NAMES
    return {name: _render_unit(name, fields) for name in names}


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
    """Preview by default; install core units or retire owned legacy files."""
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--install", action="store_true")
    modes.add_argument("--remove", action="store_true")
    modes.add_argument("--remove-auto-restart", action="store_true",
                       help="remove already-disabled legacy Git watcher unit files")
    parser.add_argument("--guard-probe", action="store_true",
                        help="include the optional fixed on-demand egress probe unit")
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        parser.error("Run as your normal user, not root")
    if args.guard_probe and (args.remove or args.remove_auto_restart):
        parser.error("--guard-probe is only valid for preview or --install")
    folder = destination()
    files = unit_files(sys.executable, ROOT, guard_probe=args.guard_probe)
    if args.install:
        folder.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            write_owned(folder / name, content)
        print("Installed user units. Run systemctl --user daemon-reload")
    elif args.remove or args.remove_auto_restart:
        selected = (LEGACY_UPDATE_NAMES if args.remove_auto_restart
                    else (*NAMES, *LEGACY_UPDATE_NAMES, GUARD_PROBE, GUARD_PREFLIGHT))
        for name in selected:
            path = folder / name
            if path.is_symlink():
                raise ValueError(f"Refusing symlink: {path}")
            if path.exists():
                if not path.read_text().startswith(MARKER):
                    raise ValueError(f"Refusing unmanaged unit: {path}")
                path.unlink()
        print("Removed requested owned user unit files. Run systemctl --user daemon-reload")
    else:
        for name, content in files.items():
            print(f"===== {name} =====\n{content}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
