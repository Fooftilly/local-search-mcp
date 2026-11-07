"""Short-lived no-network HTML selector worker for an already-authorized crawl.

Run only as: python -I structured_selector_worker.py
Input is ONE JSON document on stdin (not an URL). Output is ONE bounded JSON
document on stdout. All selector execution happens after Linux rlimits and a
seccomp filter denying networking and spawning more processes are installed.
Failure to install either boundary fails closed. This code NEVER fetches URLs.
"""
from __future__ import annotations

import ctypes
import json
import os
import resource
import sys

from lxml import etree
from lxml import html as lxml_html
from lxml.cssselect import CSSSelector

MAX_HTML_BYTES = 256 * 1024
MAX_WORKER_ROWS = 24
MAX_OUTPUT_BYTES = 64 * 1024
MAX_FIELDS = 8
MAX_CELL_CHARS = 240
MAX_AS_BYTES = 384 * 1024 * 1024
CPU_SECONDS = 2

# Linux seccomp syscall names (libseccomp resolves architecture numbers).
# Network creation/communication and new child processes must fail with EPERM.
_FORBIDDEN = (
    "socket", "socketpair", "connect", "bind", "listen", "accept", "accept4",
    "sendto", "sendmsg", "sendmmsg", "recvfrom", "recvmsg", "recvmmsg",
    "fork", "vfork", "clone", "clone3", "execve", "execveat", "ptrace",
)


def _lock_down() -> None:
    """Install non-optional Linux kernel limits before touching page data."""
    if sys.platform != "linux":
        raise RuntimeError("Linux resource isolation required")
    for limit, soft, hard in (
        (resource.RLIMIT_CORE, 0, 0),
        (resource.RLIMIT_AS, MAX_AS_BYTES, MAX_AS_BYTES),
        (resource.RLIMIT_CPU, CPU_SECONDS, CPU_SECONDS + 1),
        (resource.RLIMIT_FSIZE, MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES),
    ):
        resource.setrlimit(limit, (soft, hard))

    # No Python socket monkeypatches: the kernel must reject networking even
    # if a native parser/library attempts a socket operation.
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
    ]
    lib.seccomp_rule_add.restype = ctypes.c_int
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_load.restype = ctypes.c_int
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.restype = None

    ctx = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not ctx:
        raise RuntimeError("seccomp context unavailable")
    try:
        for name in _FORBIDDEN:
            number = lib.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number < 0:
                raise RuntimeError("Cannot resolve seccomp syscall")
            # SCMP_ACT_ERRNO(EPERM); deny syscall in the worker, not the host.
            if lib.seccomp_rule_add(ctx, 0x00050001, number, 0) != 0:
                raise RuntimeError("Cannot install seccomp rule")
        if lib.seccomp_load(ctx) != 0:
            raise RuntimeError("Cannot activate seccomp")
    finally:
        lib.seccomp_release(ctx)


def _extract(payload: object) -> dict:
    if type(payload) is not dict or set(payload) != {"html", "spec"}:
        raise ValueError("Invalid worker input")
    text = payload["html"]
    spec = payload["spec"]
    if type(text) is not str or len(text) > MAX_HTML_BYTES:
        raise ValueError("HTML too large")
    encoded = text.encode("utf-8", "strict")
    if not encoded or len(encoded) > MAX_HTML_BYTES:
        raise ValueError("Invalid HTML")
    if type(spec) is not dict or set(spec) != {"strategy", "base_selector", "fields"}:
        raise ValueError("Invalid worker specification")
    strategy, fields = spec["strategy"], spec["fields"]
    if strategy not in ("css", "xpath") or type(fields) is not list or not 1 <= len(fields) <= MAX_FIELDS:
        raise ValueError("Invalid worker strategy/fields")

    # No external entities, no external resources, no giant-tree override.
    parser = etree.HTMLParser(no_network=True, recover=True, huge_tree=False)
    document = lxml_html.document_fromstring(encoded, parser=parser)
    if strategy == "css":
        base_nodes = CSSSelector(spec["base_selector"])(document)
        selectors = [CSSSelector(field["selector"]) for field in fields]
    else:
        base_nodes = document.xpath(spec["base_selector"])
        selectors = [field["selector"] for field in fields]
    if type(base_nodes) is not list:
        raise ValueError("Invalid base selection")

    rows: list[dict[str, str | None]] = []
    source_capped = len(base_nodes) > MAX_WORKER_ROWS
    for node in base_nodes[:MAX_WORKER_ROWS]:
        if not isinstance(node, etree._Element):
            raise ValueError("Selector returned an invalid node")
        row = {}
        for field, selector in zip(fields, selectors):
            matches = (selector(node) if strategy == "css"
                       else node.xpath(selector))
            if type(matches) is not list or any(
                not isinstance(match, etree._Element) for match in matches[:1]
            ):
                raise ValueError("Selector returned an invalid field")
            if not matches:
                value = None
            elif field["type"] == "attribute":
                value = matches[0].get(field["attribute"])
            else:
                value = "".join(matches[0].itertext()).strip()
            if value is not None and type(value) is not str:
                raise ValueError("Invalid field value")
            row[field["name"]] = value[:MAX_CELL_CHARS] if value is not None else None
        # Keep the entire worker JSON far below its hard file-size limit.
        candidate = json.dumps({"rows": rows + [row], "source_capped": True},
                               ensure_ascii=True).encode("utf-8")
        if len(candidate) > MAX_OUTPUT_BYTES - 4096:
            source_capped = True
            break
        rows.append(row)
    return {"rows": rows, "source_capped": source_capped}


def main() -> int:
    try:
        _lock_down()
        # The supervisor bounds this pipe; worker applies a separate cap.
        request = sys.stdin.buffer.read(MAX_HTML_BYTES * 2 + 1)
        if len(request) > MAX_HTML_BYTES * 2:
            return 1
        result = _extract(json.loads(request))
        output = json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode()
        if len(output) > MAX_OUTPUT_BYTES:
            return 1
        os.write(1, output)
        return 0
    except (Exception, MemoryError):
        # Never print HTML, selectors or exception details into host logs.
        return 1


if __name__ == "__main__":
    sys.exit(main())
