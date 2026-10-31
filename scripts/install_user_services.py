#!/usr/bin/env python3
"""Preview, install and remove opt-in systemd user units without enabling them."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import subprocess
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
CLOUDFLARE_NAMES = (
    "local-search-mcp-remote-gateway.service",
    "local-search-mcp-cloudflare-origin.service",
)


def quote(value: str) -> str:
    """Escape paths with spaces, quotes or systemd percent specifiers."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def working_directory(value: Path) -> str:
    """Systemd WorkingDirectory is a path, not an ExecStart command word.

    Quoted paths are interpreted with literal quote characters by systemd.
    Escape path whitespace and specifiers, without adding outer quotes.
    """
    if not value.is_absolute():
        raise ValueError("Systemd working directory must be absolute")
    return (str(value).replace("\\", r"\x5c")
            .replace(" ", r"\x20").replace("\t", r"\x09")
            .replace('"', r"\x22").replace("%", "%%"))


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
               guard_probe: bool = False,
               cloudflare_portal: bool = False) -> dict[str, str]:
    """Render only the supported project-owned units (no Git watcher)."""
    fields = {
        # Virtual environments commonly symlink python to the base binary.
        # Resolving that symlink bypasses the venv's site-packages at runtime.
        "EXE": quote(str(Path(python) if cloudflare_portal
                         else Path(python).resolve())),
        "LAUNCH": quote(str(root / "bootstrap.py")),
        "HEALTH": quote(str(root / "health_cli.py")),
    }
    if cloudflare_portal and guard_probe:
        raise ValueError("Choose one optional service family")
    if cloudflare_portal:
        fields["ROOT"] = working_directory(root)
        names = CLOUDFLARE_NAMES
    else:
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


def verify_cloudflare_removal_safe(folder: Path) -> None:
    """Refuse to remove optional unit files while any listener could be active.

    A direct installer --remove must not unlink a running systemd unit, leaving
    the process alive yet unmanaged. Only fixed unit names and fixed properties
    are inspected; systemd output and private paths are never printed.
    """
    for name in CLOUDFLARE_NAMES:
        path = folder / name
        try:
            result = subprocess.run(
                ["systemctl", "--user", "show", name,
                 "--property=LoadState", "--property=ActiveState",
                 "--property=UnitFileState", "--property=FragmentPath"],
                capture_output=True, text=True, check=False, timeout=5)
            if result.returncode or len(result.stdout) > 2048:
                raise ValueError("Unavailable systemd state")
            properties = {}
            for line in result.stdout.splitlines():
                key, separator, value = line.partition("=")
                if not separator or key in properties:
                    raise ValueError("Malformed systemd state")
                properties[key] = value
            if set(properties) != {
                    "LoadState", "ActiveState", "UnitFileState", "FragmentPath"}:
                raise ValueError("Incomplete systemd state")
            load = properties["LoadState"]
            active = properties["ActiveState"]
            enabled = properties["UnitFileState"]
            fragment = properties["FragmentPath"]
            if (load not in ("loaded", "not-found")
                    or active not in ("inactive", "failed")
                    or enabled not in ("disabled", "masked", "not-found", "")
                    or (load == "loaded" and fragment != str(path))
                    or (load == "not-found" and fragment)):
                raise ValueError("Optional systemd unit not proven stopped")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise ValueError(
                "Optional Cloudflare units must be verified stopped and disabled "
                "before removing their files; use the attended rollback workflow"
            ) from None


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
    parser.add_argument("--cloudflare-portal", action="store_true",
                        help="preview/install/remove only the two optional loopback gateway/adapter units")
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        parser.error("Run as your normal user, not root")
    if args.cloudflare_portal and (args.guard_probe or args.remove_auto_restart):
        parser.error("Cloudflare option cannot change guard or legacy units")
    if args.guard_probe and (args.remove or args.remove_auto_restart):
        parser.error("--guard-probe is only valid for preview or --install")
    folder = destination()
    if args.cloudflare_portal and args.install and not (
            ROOT / ".venv" / "bin" / "python").is_file():
        parser.error("Project .venv must be installed first")
    interpreter = (str(ROOT / ".venv" / "bin" / "python")
                   if args.cloudflare_portal else sys.executable)
    files = unit_files(interpreter, ROOT, guard_probe=args.guard_probe,
                       cloudflare_portal=args.cloudflare_portal)
    if args.install:
        folder.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            write_owned(folder / name, content)
        print("Installed user units. Run systemctl --user daemon-reload")
    elif args.remove or args.remove_auto_restart:
        selected = (CLOUDFLARE_NAMES if args.cloudflare_portal
                    else LEGACY_UPDATE_NAMES if args.remove_auto_restart
                    else (*NAMES, *LEGACY_UPDATE_NAMES, GUARD_PROBE, GUARD_PREFLIGHT))
        if args.cloudflare_portal:
            verify_cloudflare_removal_safe(folder)
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
