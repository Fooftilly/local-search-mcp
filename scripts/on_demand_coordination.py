"""Cross-process coordination between MCP wake/idle and operator full stop.

The lock file belongs to the same private user-owned config directory.
The file itself contains no secrets and is never deleted while the runtime
may be active; separate processes flock the same inode.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
from typing import Iterator

from scripts import on_demand_settings as settings

NAME = "on-demand.lock"


@contextmanager
def exclusive(*, folder: Path | None = None) -> Iterator[None]:
    """Serialize an on-demand Docker transition with setup and full-stop."""
    folder = settings.directory() if folder is None else folder
    settings._private_directory(folder, create=True)
    path = folder / NAME
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        link = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or (info.st_dev, info.st_ino) != (link.st_dev, link.st_ino)):
            raise ValueError("On-demand lock path is not owner-only and stable")
        fcntl.flock(fd, fcntl.LOCK_EX)
        # A lock is not authorization; callers must re-read the saved mode
        # and re-verify Docker identity *after* acquiring this lock.
        again = path.lstat()
        if (again.st_dev, again.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("On-demand lock changed while waiting")
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
