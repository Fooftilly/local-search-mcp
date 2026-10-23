#!/usr/bin/env python3
"""Read-only check whether private local Docker Compose image refs are digest-pinned."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

_PIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-fA-F0-9]{64}$")
_MAX_OUTPUT = 8192


def default_directory() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))).expanduser()
    return base / "local-search-mcp" / "crawler-compose"


def report(folder: Path) -> dict:
    """Use Compose's image-only output; never read/print interpolated secrets."""
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError("Private Compose directory missing or linked")
    config = folder / "compose.yaml"
    if config.is_symlink() or not config.is_file():
        raise ValueError("Crawler Compose file missing or linked")
    envfile = folder / ".env"
    if envfile.is_symlink() or not envfile.is_file():
        raise ValueError("Private Compose .env file missing or linked")
    # Ignore unrelated working-directory .env files; inspect the private
    # project with the exact env file that its installer generated.
    argv = ["docker", "compose", "--project-directory", str(folder),
            "--env-file", str(envfile), "-f", str(config)]
    searxng = folder / "searxng.compose.yaml"
    if searxng.is_symlink():
        raise ValueError("Refusing linked SearXNG Compose overlay")
    if searxng.is_file():
        argv += ["-f", str(searxng)]
    argv += ["config", "--images"]
    try:
        result = subprocess.run(argv, capture_output=True, text=True,
                                check=False, timeout=15, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Docker Compose image inspection unavailable") from exc
    # Never return stderr: it may include operator-specific variables.
    if result.returncode or len(result.stdout) > _MAX_OUTPUT:
        raise ValueError("Docker Compose image inspection failed")
    refs = result.stdout.splitlines()
    # This project defines one crawler, plus optionally one SearXNG service.
    # A truncated image listing must not be reported as fully pinned.
    expected = 2 if searxng.is_file() else 1
    if len(refs) != expected or any(not value or len(value) > 300 for value in refs):
        raise ValueError("Incomplete or invalid Docker Compose image listing")
    rows = [{"index": i + 1, "digest_pinned": bool(_PIN.fullmatch(ref))}
            for i, ref in enumerate(refs)]
    return {
        "scope": "local-compose-images",
        "image_count": len(rows),
        "images": rows,
        "all_digest_pinned": all(item["digest_pinned"] for item in rows),
        "changed_anything": False,
        "note": ("Digest notation proves a fixed registry reference, not image trust, "
                 "signature validity, vulnerability status or a safe migration."),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        status = report(default_directory())
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps(status, indent=2))
    return 0 if status["all_digest_pinned"] else 1


if __name__ == "__main__":
    sys.exit(main())
