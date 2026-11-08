"""Strictly offline DOCX conversion experiment for issue #304.

No network downloader, no URL or filesystem path input, no MCP registration,
no automatic dependency installation. Only already-held, metadata-admitted
bytes enter the child. A result is UNAUTHORIZED content until an independent
source/final URL and revocation boundary exists.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

from office_docx_admission import DocxAdmissionRejected, inspect_docx_container

WALL_SECONDS = 15
MAX_WORKER_OUTPUT = 48 * 1024
_WORKER_FILE = Path(__file__).resolve().with_name("office_docx_worker.py")
_CAPACITY = threading.BoundedSemaphore(1)


class DocxPilotUnavailable(RuntimeError):
    """The optional worker could not complete under the offline limits."""


def _kill_group(child: subprocess.Popen) -> None:
    # The leader may have exited while a descendant remains alive.
    # Kill the whole private session before reaping the leader. Avoid poll()
    # here so its PID cannot be reaped/reused before killpg().
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait()


def run_offline_docx_bytes(payload: object) -> dict:
    """Do NOT call on MCP client input; no URL/permission checks happen here."""
    try:
        inspect_docx_container(payload)
    except DocxAdmissionRejected as exc:
        raise DocxPilotUnavailable("Unapproved DOCX container metadata") from exc
    if sys.platform != "linux" or not hasattr(os, "memfd_create"):
        raise DocxPilotUnavailable("Linux anonymous output storage required")
    if not _CAPACITY.acquire(blocking=False):
        raise DocxPilotUnavailable("DOCX pilot capacity exhausted")
    try:
        # Opaque kernel memory object, not a named disk/tempfile. No path or
        # document bytes appear in process args, environment, or exception text.
        try:
            fd = os.memfd_create("docx-pilot-output", os.MFD_CLOEXEC)
        except OSError as exc:
            raise DocxPilotUnavailable("Offline DOCX output storage unavailable") from exc
        try:
            try:
                child = subprocess.Popen(
                    [sys.executable, "-I", "-B", str(_WORKER_FILE)],
                    stdin=subprocess.PIPE, stdout=fd,
                    stderr=subprocess.DEVNULL, cwd="/",
                    env={"PYTHONDONTWRITEBYTECODE": "1",
                         "PYTHONNOUSERSITE": "1", "LC_ALL": "C.UTF-8"},
                    close_fds=True, start_new_session=True,
                )
            except OSError as exc:
                raise DocxPilotUnavailable("Offline DOCX worker could not start") from exc
            try:
                try:
                    child.communicate(input=payload, timeout=WALL_SECONDS)
                except subprocess.TimeoutExpired as exc:
                    raise DocxPilotUnavailable("Offline DOCX worker exceeded wall time") from exc
            finally:
                _kill_group(child)
            if child.returncode != 0:
                raise DocxPilotUnavailable("Offline DOCX worker failed closed")
            size = os.fstat(fd).st_size
            if not 1 <= size <= MAX_WORKER_OUTPUT:
                raise DocxPilotUnavailable("Offline DOCX worker output oversized")
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, MAX_WORKER_OUTPUT + 1)
            if len(raw) != size:
                raise DocxPilotUnavailable("Offline DOCX worker output incomplete")
        finally:
            os.close(fd)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise DocxPilotUnavailable("Offline DOCX worker output malformed") from exc
        if (type(parsed) is not dict or set(parsed) != {"markdown", "truncated"}
                or type(parsed["markdown"]) is not str
                or type(parsed["truncated"]) is not bool
                or not parsed["markdown"]):
            raise DocxPilotUnavailable("Offline DOCX worker output invalid")
        try:
            encoded_length = len(parsed["markdown"].encode("utf-8", "strict"))
        except UnicodeError as exc:
            raise DocxPilotUnavailable("Offline DOCX worker output invalid") from exc
        if encoded_length > 32 * 1024:
            raise DocxPilotUnavailable("Offline DOCX worker output oversized")
        # Never mark content authorized or provide a URL/document handle.
        return {"markdown": parsed["markdown"],
                "truncated": parsed["truncated"],
                "authorization_verified": False}
    finally:
        _CAPACITY.release()
