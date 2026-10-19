#!/usr/bin/env python3
"""Install a private SearXNG overlay with no Docker operations or overwrites."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import stat

if __package__:
    from .crawler_compose import ComposeSetupError, _write_exclusive
else:
    from crawler_compose import ComposeSetupError, _write_exclusive

SOURCE = Path(__file__).resolve().parents[1] / "config" / "crawler"
NAMES = ("searxng.compose.yaml", "searxng-settings.yml")
PLACEHOLDER = "__GENERATE_PRIVATE_SEARXNG_KEY__"


def destination() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))).expanduser()
    return base / "local-search-mcp" / "crawler-compose"


def install(folder: Path) -> None:
    if folder.is_symlink() or not folder.is_dir():
        raise ComposeSetupError("Existing crawler Compose directory required")
    info = folder.stat()
    if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise ComposeSetupError("Crawler directory must be owner-only (0700)")
    crawler = folder / "compose.yaml"
    if crawler.is_symlink() or not crawler.is_file():
        raise ComposeSetupError("Existing crawler Compose file is missing/linked")
    for name in NAMES:
        if (folder / name).exists() or (folder / name).is_symlink():
            raise ComposeSetupError("Existing SearXNG file must not be replaced: " + name)
        if (SOURCE / name).is_symlink() or not (SOURCE / name).is_file():
            raise ComposeSetupError("Missing or linked canonical source: " + name)
    compose = (SOURCE / NAMES[0]).read_bytes()
    template = (SOURCE / NAMES[1]).read_text(encoding="utf-8")
    if template.count(PLACEHOLDER) != 1:
        raise ComposeSetupError("Unexpected settings template placeholder")
    settings = template.replace(PLACEHOLDER, secrets.token_hex(32)).encode("utf-8")
    created = []
    try:
        for name, content in zip(NAMES, (compose, settings)):
            path = folder / name
            _write_exclusive(path, content)
            created.append(path)
            if name == "searxng-settings.yml":
                # SearXNG's container UID may not match the host UID. The
                # private (0700) parent keeps this file hidden on the host;
                # Docker bind-mounts the file read-only for the container.
                path.chmod(0o644)
    except BaseException:
        for path in reversed(created):
            if path.is_file() and not path.is_symlink():
                path.unlink()
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args(argv)
    if not args.install:
        print("Preview only: add private SearXNG files; no Docker or settings changes.")
        return 0
    try:
        install(destination())
    except (ComposeSetupError, OSError) as exc:
        parser.error(str(exc))
    print("Private SearXNG Compose config created (not started).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
