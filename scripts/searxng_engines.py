"""Operator-only SearXNG engine chooser; no agent or dashboard mutation surface.

Only a verified dedicated project's bind-mounted settings.yml is touched,
after an explicit APPLY. Use the SearXNG /config API for live engine names.
"""
from __future__ import annotations

from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

# The setup wizard launches this helper as a path, not as a -m module.
# Make imports work without PYTHONPATH while retaining normal module imports.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import backend_inventory, on_demand_coordination, on_demand_docker

START = "# BEGIN local-search-mcp managed engine selection"
END = "# END local-search-mcp managed engine selection"
RECOMMENDED = ("yahoo", "mojeek", "startpage", "qwant")
KNOWN_UNRELIABLE = ("brave", "duckduckgo", "google cse", "wikipedia")
PRESET = ("yahoo",)
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9 ._-]{0,79}\Z")
DEFAULT = re.compile(r"(?m)^use_default_settings:[ \t]*true[ \t]*(?:#.*)?$")
ANY_ENGINES = re.compile(r"(?m)^engines[ \t]*:")
MAX_BYTES = 128 * 1024


class EngineSetupError(ValueError):
    """Controlled operator diagnostic, never include private file contents."""


def settings_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "local-search-mcp" / "crawler-compose" / "searxng-settings.yml"


def _read_settings(path: Path) -> str:
    try:
        parent = path.parent.lstat()
        item = path.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or parent.st_mode & 0o077 or not stat.S_ISREG(item.st_mode)
                or item.st_uid != os.getuid() or item.st_size > MAX_BYTES
                or item.st_nlink != 1):
            raise EngineSetupError("SearXNG settings ownership or permissions are unsafe")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise EngineSetupError("Unable to read verified SearXNG settings") from exc


def _check_bind_mount(container_id: str, path: Path) -> None:
    raw = backend_inventory._run(
        "inspect", "--type=container", "--format", "{{json .Mounts}}", container_id)
    try:
        mounts = json.loads(raw) if raw is not None else None
        matched = [m for m in mounts if isinstance(m, dict)
                   and m.get("Type") == "bind"
                   and m.get("Destination") == "/etc/searxng/settings.yml"
                   and m.get("Source") == str(path.resolve(strict=True))]
        if len(matched) != 1:
            raise EngineSetupError("Verified SearXNG container does not bind this settings file")
    except (TypeError, ValueError, OSError) as exc:
        raise EngineSetupError("Cannot verify SearXNG settings bind mount") from exc


def live_engines() -> list[dict]:
    """Read-only localhost-only engine inventory; do not start Docker."""
    try:
        conn = HTTPConnection("127.0.0.1", 8888, timeout=4)
        try:
            conn.request("GET", "/config")
            response = conn.getresponse()
            if response.status != 200:
                raise EngineSetupError("SearXNG /config is unavailable")
            raw = response.read(128 * 1024 + 1)
        finally:
            conn.close()
        if len(raw) > 128 * 1024:
            raise EngineSetupError("SearXNG engine list is too large")
        data = json.loads(raw)
        engines = data.get("engines") if isinstance(data, dict) else None
        if not isinstance(engines, list):
            raise EngineSetupError("SearXNG did not return an engine list")
        found = {}
        for row in engines:
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            categories = row.get("categories", [])
            if (not isinstance(name, str) or not NAME.fullmatch(name)
                    or not isinstance(categories, list)
                    or "general" not in categories):
                continue
            if name in found or len(found) >= 120:
                raise EngineSetupError("SearXNG returned duplicate or excessive engines")
            found[name] = {"name": name, "enabled": row.get("enabled") is True}
        if not found:
            raise EngineSetupError("No general-search engines found in /config")
        return sorted(found.values(), key=lambda row: (
            0 if row["name"] == "yahoo" else
            1 if row["name"] in RECOMMENDED else 2, row["name"]))
    except (OSError, TimeoutError, ValueError) as exc:
        if isinstance(exc, EngineSetupError):
            raise
        raise EngineSetupError("Cannot read SearXNG /config (backend may be sleeping)") from exc


def render(original: str, selected: tuple[str, ...], controlled: tuple[str, ...]) -> str:
    """Replace only our marked block; reject user-owned YAML engine entries."""
    if not DEFAULT.search(original):
        raise EngineSetupError("Expected use_default_settings: true; settings left unchanged")
    if not selected or any(not NAME.fullmatch(x) for x in controlled):
        raise EngineSetupError("Invalid engine selection")
    if len(set(controlled)) != len(controlled) or not set(selected).issubset(controlled):
        raise EngineSetupError("Selected engines must appear in the verified list")
    if original.count(START) != original.count(END) or original.count(START) > 1:
        raise EngineSetupError("Existing managed engine block is malformed")
    if START in original:
        begin = original.index(START)
        end = original.index(END) + len(END)
        # Never replace unrelated YAML or user changes inside the managed block.
        existing = original[begin:end]
        if not re.fullmatch(
            re.escape(START) + r"\n(?:  - name: .+\n    disabled: (?:true|false)\n)*"
            + re.escape(END), existing):
            raise EngineSetupError("Managed engine block was edited; refusing overwrite")
        prefix = original[:begin]
        # Only remove our own immediately preceding top-level engines key.
        # Never erase another owner's engines block from the rest of the file.
        match = re.search(r"engines:\n\Z", prefix)
        if match is None:
            raise EngineSetupError("Managed engines section header is missing")
        rest = prefix[:match.start()] + original[end:]
    else:
        rest = original
    if ANY_ENGINES.search(rest):
        raise EngineSetupError("Existing custom engines configuration; refusing to overwrite")
    # Refuse surprising YAML document boundaries in the restricted editor.
    if re.search(r"(?m)^(?:---|\.\.\.)[ \t]*$", rest):
        raise EngineSetupError("Multi-document settings are not supported by the guided editor")
    block = ["engines:", START]
    for name in controlled:
        block.extend(("  - name: " + json.dumps(name, ensure_ascii=True),
                      "    disabled: " + ("false" if name in selected else "true")))
    block.append(END)
    return rest.rstrip("\n") + "\n\n" + "\n".join(block) + "\n"


def _save(path: Path, body: str, *, expected: str) -> None:
    """Preserve the inode: Docker bind mounts retain it across container restart.

    An atomic os.replace() here would leave /etc/searxng/settings.yml mounted
    to the *old* inode. Always edit the existing owner-verified regular file.
    """
    payload = body.encode("utf-8")
    if len(payload) > MAX_BYTES:
        raise EngineSetupError("Engine configuration would be too large")
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        named = path.lstat()
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or st.st_nlink != 1 or st.st_dev != named.st_dev
                or st.st_ino != named.st_ino):
            raise EngineSetupError("SearXNG settings changed during edit")
        with os.fdopen(fd, "r+b", closefd=False) as f:
            if f.read(MAX_BYTES + 1) != expected.encode("utf-8"):
                raise EngineSetupError("SearXNG settings changed since preview")
            f.seek(0)
            f.write(payload)
            f.truncate()
            f.flush()
            os.fsync(f.fileno())
    finally:
        os.close(fd)


def _restart_verified(target: on_demand_docker.Target) -> None:
    """Restart *only* an already-running, verified SearXNG container."""
    current = on_demand_docker._safe_target("searxng")
    if current.ident != target.ident or not current.running:
        raise EngineSetupError("SearXNG container identity/state changed")
    env = os.environ.copy()
    for k in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY",
              "DOCKER_CERT_PATH", "COMPOSE_FILE", "COMPOSE_PROJECT_NAME"):
        env.pop(k, None)
    try:
        result = subprocess.run(
            [*backend_inventory.DOCKER, "restart", "--time", "10", target.ident],
            timeout=45, capture_output=True, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EngineSetupError("Verified SearXNG restart unavailable") from exc
    if result.returncode:
        raise EngineSetupError("Verified SearXNG restart failed")


def apply(selected: tuple[str, ...], controlled: tuple[str, ...]) -> str:
    """One confirmed config change; never wake a sleeping backend."""
    if os.geteuid() == 0:
        raise EngineSetupError("Run setup as the normal user")
    with on_demand_coordination.exclusive():
        target = on_demand_docker._safe_target("searxng")
        path = settings_path()
        _check_bind_mount(target.ident, path)
        original = _read_settings(path)
        updated = render(original, selected, controlled)
        if updated == original:
            return "Engine selection already configured; no change"
        _save(path, updated, expected=original)
        if not target.running:
            return "Settings saved; sleeping SearXNG will use them on its next managed wake"
        try:
            _restart_verified(target)
            # SearXNG may need seconds to recover after its container restarts.
            for _ in range(30):
                try:
                    check = live_engines()
                    enabled = {row["name"] for row in check if row["enabled"]}
                    if all((name in enabled) == (name in selected) for name in controlled):
                        return "Engine selection applied; verified SearXNG restarted successfully"
                except EngineSetupError:
                    pass
                time.sleep(0.5)
            raise EngineSetupError("Engine selection could not be verified after restart")
        except EngineSetupError as exc:
            _save(path, original, expected=updated)
            # Recovery is scoped to the same previously verified container.
            try:
                _restart_verified(target)
            except EngineSetupError:
                raise EngineSetupError(
                    "Verification failed; settings restored but SearXNG restart also failed") from exc
            raise EngineSetupError("Verification failed; previous settings restored") from exc


def main() -> int:
    if not sys.stdin.isatty() or os.geteuid() == 0:
        print("Interactive normal-user terminal required", file=sys.stderr)
        return 2
    try:
        target = on_demand_docker._safe_target("searxng")
        _check_bind_mount(target.ident, settings_path())
        _read_settings(settings_path())
        try:
            engines = live_engines() if target.running else []
        except EngineSetupError:
            engines = []
        names = tuple(row["name"] for row in engines)
        print("\nSearXNG search engines (operator-only)")
        print("1  Recommended: Yahoo only (verified working on this installation)")
        print("2  Choose from installed general-search engines" +
              ("" if names else " (requires running SearXNG)"))
        print("0  Cancel")
        choice = input("Choose: ").strip()
        if choice == "0":
            return 0
        if choice == "1":
            # Overrides the four observed default failures; does not alter
            # unrelated search categories, private credentials or tokens.
            controlled = tuple(dict.fromkeys((*PRESET, *KNOWN_UNRELIABLE)))
            selected = PRESET
        elif choice == "2" and names:
            for index, row in enumerate(engines, 1):
                tag = "recommended" if row["name"] in RECOMMENDED else ""
                active = "currently default" if row["enabled"] else "not default"
                print(f"{index:2}  {row['name']}  ({active}) {tag}")
            raw = input("Comma-separated engine numbers (blank cancels): ").strip()
            if not raw:
                return 0
            pieces = [x.strip() for x in raw.split(",")]
            if (len(pieces) > len(names) or not all(x.isascii() and x.isdigit()
                    and 1 <= int(x) <= len(names) for x in pieces)):
                raise EngineSetupError("Invalid engine numbers")
            selected = tuple(dict.fromkeys(names[int(x)-1] for x in pieces))
            controlled = names
            print("Custom selection changes defaults for listed general engines.")
        else:
            print("Cancelled; installed engine list unavailable or invalid choice")
            return 0
        print("Enable by default: " + ", ".join(selected))
        print("Other affected engines will be excluded from default searches.")
        print("A running verified SearXNG container will be restarted; it may interrupt searches.")
        if input("Type APPLY to change settings, anything else cancels: ").strip() != "APPLY":
            print("Cancelled; no changes made")
            return 0
        print(apply(selected, controlled))
        return 0
    except (EngineSetupError, on_demand_docker.LifecycleError, OSError, EOFError,
            KeyboardInterrupt, ValueError) as exc:
        print("SearXNG engine setup did not complete: " + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
