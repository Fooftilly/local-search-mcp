#!/usr/bin/env python3
"""Interactive, restart-safe launcher for the existing local SearXNG/Crawl4AI bridge.

Run with: python3 bootstrap.py
No Python packages are required before startup. Designed for Linux.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile

from scripts.crawler_compose import ComposeSetupError, generate as generate_crawler_compose, inspect_existing_image

ROOT = Path(__file__).resolve().parent
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))).expanduser()
PRIVATE_DIR = CONFIG_HOME / "local-search-mcp"
PRIVATE_CONFIG = PRIVATE_DIR / "config.json"
PROFILE_NAME = "local-research"
BRIDGE_PORT = 8790
TUNNEL_HEALTH_PORT = 8080
GITHUB_LATEST = "https://api.github.com/repos/openai/tunnel-client/releases/latest"
ENV_KEYS = ("CONTROL_PLANE_API_KEY", "CRAWL4AI_API_TOKEN", "ALLOWED_CRAWL_DOMAINS")
DEFAULT_DOMAINS = "example.com,kernel.me,tehnomax.me"


class SetupError(Exception):
    pass


def info(message: str) -> None:
    print(message, flush=True)


def run_command(args: list[str], *, env: dict[str, str] | None = None) -> None:
    try:
        subprocess.run(args, check=True, cwd=ROOT, env=env)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SetupError(f"Command failed: {args[0]} {args[1] if len(args) > 1 else ''} ({exc})") from exc


def _verify_private_location() -> None:
    """Reject private-directory and config symlinks, including dangling links."""
    if PRIVATE_DIR.is_symlink() or PRIVATE_CONFIG.is_symlink():
        raise SetupError("Refusing symlinked private configuration directory or file")
    if PRIVATE_DIR.exists():
        directory = PRIVATE_DIR.stat()
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid():
            raise SetupError("Private configuration directory is not an owned directory")
        if directory.st_mode & 0o077:
            raise SetupError("Private configuration directory permissions are too broad; require mode 700")
    if PRIVATE_CONFIG.exists():
        info = PRIVATE_CONFIG.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise SetupError("Private configuration is not an owned regular file")


def read_config() -> dict[str, str]:
    _verify_private_location()
    if not PRIVATE_CONFIG.exists():
        return {}
    if PRIVATE_CONFIG.stat().st_mode & 0o077:
        raise SetupError(f"Private config permissions are too broad: chmod 600 '{PRIVATE_CONFIG}'")
    try:
        data = json.loads(PRIVATE_CONFIG.read_text())
    except (ValueError, OSError) as exc:
        raise SetupError(f"Cannot read {PRIVATE_CONFIG}: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise SetupError("Private configuration must be a JSON object")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise SetupError("Private configuration values must be strings")
    return data


def _ensure_private_dir_durable() -> None:
    """Create missing configuration directories and persist each new entry."""
    missing = []
    directory = PRIVATE_DIR
    while not directory.exists():
        if directory.is_symlink():
            raise SetupError("Refusing symlinked private configuration parent")
        missing.append(directory)
        if directory.parent == directory:
            raise SetupError("Cannot create private configuration directory")
        directory = directory.parent
    for new_dir in reversed(missing):
        try:
            new_dir.mkdir(mode=0o700)
        except FileExistsError:
            # A concurrent first-run launcher may already have created it.
            # Never treat a symlink or non-directory as a successful mkdir.
            if new_dir.is_symlink() or not new_dir.is_dir():
                raise SetupError("Refusing symlinked or non-directory private configuration path") from None
        if new_dir.is_symlink() or not new_dir.is_dir():
            raise SetupError("Refusing symlinked or non-directory private configuration path")
        # Persist each new directory entry in its actual parent. Existing XDG
        # config roots may themselves be symlinks (common in dotfiles setups).
        # Resolve those ancestors, but do not allow the private dir as a link.
        parent_fd = os.open(new_dir.parent.resolve(strict=True),
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)


def save_config(config: dict[str, str]) -> None:
    """Atomically store owner-only credentials without following symlinks."""
    _verify_private_location()
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in config.items()):
        raise SetupError("Private configuration values must be strings")
    _ensure_private_dir_durable()
    _verify_private_location()
    os.chmod(PRIVATE_DIR, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".config-", dir=PRIVATE_DIR)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(config, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        _verify_private_location()
        os.replace(tmp, PRIVATE_CONFIG)
        # The file fsync protects contents; syncing its directory persists the
        # renamed directory entry after a crash on supported Linux filesystems.
        dirfd = os.open(PRIVATE_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_legacy_env(path: Path) -> dict[str, str]:
    """Import simple KEY=value shell literals without executing shell code."""
    if not path.is_file():
        return {}
    out = {}
    try:
        for line in path.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            try:
                parts = shlex.split(line, comments=True)
            except ValueError:
                continue
            if len(parts) != 1 or "=" not in parts[0]:
                continue
            key, value = parts[0].split("=", 1)
            if key in ENV_KEYS and value:
                out[key] = value
    except OSError:
        pass
    return out


def docker_token() -> str:
    """Best-effort extraction of the user-owned Crawl4AI Docker env token."""
    if not shutil.which("docker"):
        return ""
    try:
        ids = subprocess.run(
            ["docker", "ps", "--filter", "publish=11235", "--format", "{{.ID}}"],
            capture_output=True, text=True, timeout=8, check=True,
        ).stdout.splitlines()
        if not ids:
            return ""
        env_lines = subprocess.run(
            ["docker", "inspect", ids[0], "--format", "{{range .Config.Env}}{{println .}}{{end}}"],
            capture_output=True, text=True, timeout=8, check=True,
        ).stdout.splitlines()
        return next((s.split("=", 1)[1] for s in env_lines if s.startswith("CRAWL4AI_API_TOKEN=") and s.split("=", 1)[1]), "")
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


def prompt_secret(label: str) -> str:
    if not sys.stdin.isatty():
        raise SetupError(f"Missing {label}; run interactively to enter it securely")
    return getpass.getpass(label + ": ").strip()


def validate_domains(value: str) -> str:
    """Use the same PSL-aware validation as the dashboard and MCP policy."""
    from domain_policy import normalize_domain

    raw = [item.strip() for item in value.split(",") if item.strip()]
    if not raw or len(raw) > 100:
        raise SetupError("Provide 1 to 100 allowed public domains")
    try:
        return ",".join(dict.fromkeys(normalize_domain(item) for item in raw))
    except (ValueError, RuntimeError) as exc:
        raise SetupError(f"Invalid domain policy: {exc}") from exc


def collect_settings(old: dict[str, str], *, refresh_runtime_key: bool = False, refresh_crawl_token: bool = False, domains_override: str | None = None) -> dict[str, str]:
    legacy = {**read_legacy_env(ROOT / ".env"), **read_legacy_env(ROOT / ".tunnel-env")}
    updated = dict(old)
    token = "" if refresh_crawl_token else docker_token()
    if token:
        info("Crawl4AI token: discovered from local Docker container (not printed).")
    else:
        token = "" if refresh_crawl_token else (os.environ.get("CRAWL4AI_API_TOKEN") or old.get("CRAWL4AI_API_TOKEN") or legacy.get("CRAWL4AI_API_TOKEN", ""))
        if token:
            info("Crawl4AI token: loaded from saved configuration/environment.")
        else:
            token = prompt_secret("Crawl4AI API token (not found in Docker)")
    if not token:
        raise SetupError("Crawl4AI API token cannot be empty")
    updated["CRAWL4AI_API_TOKEN"] = token

    runtime = "" if refresh_runtime_key else (os.environ.get("CONTROL_PLANE_API_KEY") or old.get("CONTROL_PLANE_API_KEY") or
               legacy.get("CONTROL_PLANE_API_KEY", ""))
    if runtime:
        info("OpenAI runtime key: loaded from saved configuration/environment.")
    else:
        info("Create a runtime key at https://platform.openai.com/settings/organization/api-keys")
        runtime = prompt_secret("OpenAI CONTROL_PLANE_API_KEY")
    if not runtime:
        raise SetupError("OpenAI runtime key cannot be empty")
    updated["CONTROL_PLANE_API_KEY"] = runtime

    domains = domains_override or os.environ.get("ALLOWED_CRAWL_DOMAINS") or old.get("ALLOWED_CRAWL_DOMAINS") or legacy.get("ALLOWED_CRAWL_DOMAINS")
    if not domains:
        if sys.stdin.isatty():
            info("Allowed Crawl4AI domains limit which public pages can be fetched.")
            domains = input(f"Allowed domains (comma-separated) [{DEFAULT_DOMAINS}]: ").strip() or DEFAULT_DOMAINS
        else:
            domains = DEFAULT_DOMAINS
    updated["ALLOWED_CRAWL_DOMAINS"] = validate_domains(domains)
    return updated


def make_venv() -> Path:
    venvpy = ROOT / ".venv" / "bin" / "python"
    if not venvpy.exists():
        info("Creating Python virtual environment ...")
        run_command([sys.executable, "-m", "venv", str(ROOT / ".venv")])
    verify = (
        "import fastmcp, httpx, importlib.metadata as m; "
        "assert 4 <= int(m.version('fastmcp').split('.')[0]) < 5; "
        "assert int(m.version('httpx').split('.')[0]) == 0"
    )
    probe = subprocess.run([str(venvpy), "-c", verify], capture_output=True, timeout=15)
    if probe.returncode != 0:
        info("Installing missing/incompatible Python packages from requirements.txt ...")
        run_command([str(venvpy), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])
        run_command([str(venvpy), "-c", verify])
    return venvpy


def http_ok(url: str, *, expect_json: bool = False) -> bool:
    try:
        with urlopen(Request(url, headers={"User-Agent": "local-search-mcp-bootstrap/1.0"}), timeout=8) as r:
            raw = r.read(500_000)
            if expect_json:
                json.loads(raw)
            return r.status == 200
    except (OSError, HTTPError, URLError, ValueError):
        return False


def check_services() -> None:
    if not http_ok("http://127.0.0.1:8888/search?q=healthcheck&format=json", expect_json=True):
        raise SetupError("SearXNG unavailable on 127.0.0.1:8888 (or JSON format disabled). Start your existing service.")
    info("SearXNG: OK")
    from crawler_security import local_container_warning, verify_health
    try:
        with urlopen("http://127.0.0.1:11235/health", timeout=8) as response:
            reason = verify_health(json.load(response))
    except (OSError, HTTPError, URLError, ValueError) as exc:
        raise SetupError(f"Crawl4AI health unavailable: {type(exc).__name__}") from exc
    if reason:
        raise SetupError(reason)
    warning = local_container_warning()
    if warning:
        raise SetupError(warning)
    info("Crawl4AI hardened API: OK (>= 0.9.5 stable); verify container egress policy separately")


def _download(url: str, dest: Path, size_limit: int = 90_000_000) -> None:
    req = Request(url, headers={"User-Agent": "local-search-mcp-bootstrap/1.0", "Accept": "application/octet-stream"})
    with urlopen(req, timeout=45) as response, dest.open("wb") as output:
        copied = 0
        while True:
            chunk = response.read(128 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            if copied > size_limit:
                raise SetupError("Download exceeds expected size")
            output.write(chunk)


def download_tunnel_client() -> Path:
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
        raise SetupError("Automatic tunnel download currently supports Linux x86_64 only")
    info("Tunnel client not found; downloading official Linux AMD64 release ...")
    try:
        request = Request(GITHUB_LATEST, headers={"User-Agent": "local-search-mcp-bootstrap/1.0", "Accept": "application/vnd.github+json"})
        with urlopen(request, timeout=30) as r:
            release = json.load(r)
        tag = release["tag_name"]
        filename = f"tunnel-client-{tag}-linux-amd64.zip"
        assets = {item["name"]: item["browser_download_url"] for item in release["assets"]}
        if filename not in assets or "SHA256SUMS.txt" not in assets:
            raise SetupError(f"No verified Linux AMD64 asset in latest release ({tag})")
        for name in (filename, "SHA256SUMS.txt"):
            if not assets[name].startswith(f"https://github.com/openai/tunnel-client/releases/download/{tag}/"):
                raise SetupError("Unexpected download host/path")
        with tempfile.TemporaryDirectory(prefix="local-search-tunnel-") as tmp:
            archive = Path(tmp) / filename
            sums = Path(tmp) / "SHA256SUMS.txt"
            _download(assets[filename], archive)
            _download(assets["SHA256SUMS.txt"], sums, size_limit=3_000_000)
            expected = ""
            for line in sums.read_text().splitlines():
                parts = line.strip().split()
                if len(parts) == 2 and parts[1].lstrip("*") == filename:
                    expected = parts[0].lower()
                    break
            actual = hashlib.sha256(archive.read_bytes()).hexdigest()
            if not expected or not re.fullmatch(r"[0-9a-f]{64}", expected) or actual != expected:
                raise SetupError("Tunnel client SHA256 mismatch; nothing was installed")
            with zipfile.ZipFile(archive) as zf:
                names = zf.namelist()
                if "tunnel-client" not in names:
                    raise SetupError("Release archive lacks tunnel-client")
                for filename_in_zip in ("tunnel-client", "cloudflared", "cloudflared-manifest.json"):
                    if filename_in_zip not in names:
                        continue
                    info_member = zf.getinfo(filename_in_zip)
                    if info_member.is_dir() or (info_member.file_size > 55_000_000):
                        raise SetupError("Invalid release archive member")
                    if (info_member.external_attr >> 16) & 0o170000 not in (0, stat.S_IFREG):
                        raise SetupError("Refusing non-file release member")
                    target = ROOT / filename_in_zip
                    with zf.open(info_member) as src:
                        fd, temporary_path = tempfile.mkstemp(prefix=".tunnel-", dir=ROOT)
                        try:
                            with os.fdopen(fd, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                            os.chmod(temporary_path, 0o700 if filename_in_zip != "cloudflared-manifest.json" else 0o600)
                            os.replace(temporary_path, target)
                        finally:
                            if os.path.exists(temporary_path):
                                os.unlink(temporary_path)
    except (HTTPError, URLError, OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        raise SetupError(f"Could not download tunnel client: {exc}. Install it manually from openai/tunnel-client releases.") from exc
    info("Official tunnel-client downloaded; SHA256 verified.")
    return ROOT / "tunnel-client"


def tunnel_client() -> Path:
    binary = ROOT / "tunnel-client"
    if not binary.is_file():
        binary = download_tunnel_client()
    if not os.access(binary, os.X_OK):
        os.chmod(binary, binary.stat().st_mode | stat.S_IXUSR)
    return binary


def profile_path() -> Path:
    custom = os.environ.get("TUNNEL_CLIENT_PROFILE_DIR")
    root = Path(custom).expanduser() if custom else CONFIG_HOME / "tunnel-client"
    return root / f"{PROFILE_NAME}.yaml"


def ensure_profile(binary: Path, old: dict[str, str], env: dict[str, str]) -> None:
    path = profile_path()
    if path.is_file():
        info(f"Reusing existing tunnel profile: {path}")
        return
    tunnel_id = os.environ.get("CONTROL_PLANE_TUNNEL_ID") or old.get("CONTROL_PLANE_TUNNEL_ID", "")
    if not tunnel_id:
        info("Create a tunnel at https://platform.openai.com/settings/organization/tunnels")
        if not sys.stdin.isatty():
            raise SetupError("Missing tunnel ID; run interactively")
        tunnel_id = input("Tunnel ID (tunnel_...): ").strip()
    if not re.fullmatch(r"tunnel_[A-Za-z0-9_-]+", tunnel_id):
        raise SetupError("Tunnel ID must start with tunnel_")
    info("Creating local-research tunnel profile ...")
    run_command([
        str(binary), "init", "--sample", "sample_mcp_remote_no_auth",
        "--profile", PROFILE_NAME, "--tunnel-id", tunnel_id,
        "--mcp-server-url", f"http://127.0.0.1:{BRIDGE_PORT}/mcp",
    ], env=env)


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def stop_process(proc: subprocess.Popen[bytes] | None) -> None:
    if not proc or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=8)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=4)


def launch(venv_python: Path, binary: Path, env: dict[str, str]) -> None:
    if port_open(BRIDGE_PORT):
        raise SetupError(f"Port {BRIDGE_PORT} is in use. Stop the old 'python bridge.py' first (Ctrl+C).")
    if port_open(TUNNEL_HEALTH_PORT):
        raise SetupError(f"Port {TUNNEL_HEALTH_PORT} is in use. Stop the old tunnel-client process first.")
    bridge = None
    tunnel = None
    try:
        info("Starting MCP bridge ...")
        bridge = subprocess.Popen([str(venv_python), str(ROOT / "bridge.py")], cwd=ROOT, env=env, start_new_session=True)
        for _ in range(40):
            if bridge.poll() is not None:
                raise SetupError(f"bridge.py exited with status {bridge.returncode}")
            if port_open(BRIDGE_PORT):
                break
            time.sleep(0.25)
        else:
            raise SetupError("MCP bridge did not start within 10 seconds")
        info("Bridge ready: http://127.0.0.1:8790/mcp")
        info("Checking tunnel profile with tunnel-client doctor ...")
        run_command([str(binary), "doctor", "--profile", PROFILE_NAME, "--explain"], env=env)
        info("Starting OpenAI tunnel. Press Ctrl+C to stop both processes.")
        tunnel = subprocess.Popen([str(binary), "run", "--profile", PROFILE_NAME], cwd=ROOT, env=env, start_new_session=True)
        while True:
            if tunnel.poll() is not None:
                raise SetupError(f"Tunnel client exited with status {tunnel.returncode}")
            if bridge.poll() is not None:
                raise SetupError(f"Bridge exited with status {bridge.returncode}")
            time.sleep(0.4)
    except KeyboardInterrupt:
        info("\nStopping tunnel and bridge ...")
    finally:
        stop_process(tunnel)
        stop_process(bridge)


def run_unattended_service(component: str) -> None:
    """Exec a prepared service without prompts, downloads or secret arguments."""
    config = read_config()
    env = dict(os.environ)
    if component == "bridge":
        for key in ("CRAWL4AI_API_TOKEN", "ALLOWED_CRAWL_DOMAINS"):
            if not config.get(key):
                raise SetupError(f"Missing {key}; run ./start.sh --setup-only first")
            env[key] = config[key]
        binary = ROOT / ".venv" / "bin" / "python"
        argv = [str(binary), str(ROOT / "bridge.py")]
    elif component == "tunnel":
        if not config.get("CONTROL_PLANE_API_KEY"):
            raise SetupError("Missing CONTROL_PLANE_API_KEY; run ./start.sh --setup-only first")
        if not profile_path().is_file():
            raise SetupError("Missing private tunnel profile; run ./start.sh --setup-only first")
        env["CONTROL_PLANE_API_KEY"] = config["CONTROL_PLANE_API_KEY"]
        binary = ROOT / "tunnel-client"
        if binary.is_symlink():
            raise SetupError("Refusing symlinked tunnel executable")
        argv = [str(binary), "run", "--profile", PROFILE_NAME]
    else:
        raise SetupError("Unknown service component")
    if not binary.is_file():
        raise SetupError(f"Missing service executable: {binary}; run ./start.sh --setup-only first")
    env["PYTHONUNBUFFERED"] = "1"
    os.execve(str(binary), argv, env)

def main() -> int:
    parser = argparse.ArgumentParser(description="Configure and run local SearXNG/Crawl4AI MCP and OpenAI tunnel")
    parser.add_argument("--setup-only", action="store_true", help="Configure dependencies/credentials but do not start processes")
    parser.add_argument("--generate-crawler-compose", action="store_true",
                        help="With --setup-only, create a private Crawl4AI Compose scaffold; never start Docker")
    parser.add_argument("--crawler-image",
                        help="Existing verified Crawl4AI image for --generate-crawler-compose (default: inspect crawl4ai)")
    parser.add_argument("--show-config", action="store_true", help="Show redacted configuration sources without starting services")
    parser.add_argument("--service", choices=("bridge", "tunnel"), help=argparse.SUPPRESS)
    parser.add_argument("--refresh-runtime-key", action="store_true", help="Prompt for a new OpenAI runtime key and replace the saved one")
    parser.add_argument("--refresh-crawl-token", action="store_true", help="Prompt for a replacement Crawl4AI token instead of detecting Docker token")
    parser.add_argument("--domains", help="Replace the allowed public crawl domains, comma-separated")
    args = parser.parse_args()
    try:
        if args.show_config:
            if (args.service or args.setup_only or args.refresh_runtime_key or args.refresh_crawl_token
                    or args.domains or args.generate_crawler_compose or args.crawler_image):
                raise SetupError("--show-config cannot be combined with setup/service options")
            from config_diagnostics import collect_report
            print(json.dumps(collect_report(read_config(), ROOT, PRIVATE_DIR, read_legacy_env), indent=2))
            return 0
        if args.service:
            if (args.setup_only or args.refresh_runtime_key or args.refresh_crawl_token or args.domains
                    or args.generate_crawler_compose or args.crawler_image):
                raise SetupError("--service cannot be combined with interactive setup options")
            run_unattended_service(args.service)
            return 0
        if args.generate_crawler_compose:
            if (not args.setup_only or args.refresh_runtime_key or args.refresh_crawl_token
                    or args.domains):
                raise SetupError("Compose generation requires --setup-only without other setup flags")
            # Scaffold generation is usable before either backend is running.
            # Never reuse the tunnel runtime key for Docker and never print the token.
            existing = read_config()
            # Avoid inspecting an unrelated Docker container's environment.
            token = (os.environ.get("CRAWL4AI_API_TOKEN")
                     or existing.get("CRAWL4AI_API_TOKEN"))
            if not token:
                token = prompt_secret("Crawl4AI API token for private Compose scaffold")
            image = args.crawler_image or inspect_existing_image()
            _ensure_private_dir_durable()
            _verify_private_location()
            folder = generate_crawler_compose(PRIVATE_DIR, image=image, token=token)
            info(f"Private Compose scaffold created at {folder} (not started).")
            info("Review the old container's mounts and settings before migration; "
                 "the existing Compose project was not modified.")
            return 0
        if args.crawler_image:
            raise SetupError("--crawler-image requires --generate-crawler-compose")
        # Do not ask for credentials if basic prerequisites are unavailable.
        check_services()
        venv_python = make_venv()
        binary = tunnel_client()
        old = read_config()
        updated = collect_settings(old, refresh_runtime_key=args.refresh_runtime_key, refresh_crawl_token=args.refresh_crawl_token, domains_override=args.domains)
        env = dict(os.environ)
        env.update({key: updated[key] for key in ENV_KEYS})
        save_config(updated)
        info(f"Private configuration saved: {PRIVATE_CONFIG} (mode 600)")
        # Reuse existing profile or create one without overwriting it.
        ensure_profile(binary, updated, env)
        if args.setup_only:
            info("Setup complete. Run: ./start.sh")
            return 0
        launch(venv_python, binary, env)
        return 0
    except (SetupError, ComposeSetupError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
