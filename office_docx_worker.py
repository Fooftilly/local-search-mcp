"""EXPERIMENTAL local-only DOCX converter worker; not a remote MCP capability.

Use only through office_docx_pilot.run_offline_docx_bytes(). Input comes on
stdin, output is capped JSON on stdout. MarkItDown is an OPTIONAL dependency
in an operator-owned, separately reviewed environment. A running experiment
does not authorize fetching documents or releasing content to an MCP client.
"""
from __future__ import annotations

import ctypes
import errno
from io import BytesIO
import json
import os
from pathlib import Path
import resource
import socket
import sys

# -I deliberately omits script-directory imports. The module path here is
# fixed by this trusted program, never from a document or user-supplied path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import office_docx_admission

MAX_OUTPUT_BYTES = 48 * 1024
MAX_TEXT_BYTES = 32 * 1024
MAX_AS_BYTES = 1024 * 1024 * 1024
CPU_SECONDS = 8
_EARLY_DENIED = (
    "socket", "socketpair", "connect", "bind", "listen", "accept", "accept4",
    "sendto", "sendmsg", "sendmmsg", "recvfrom", "recvmsg", "recvmmsg",
    "fork", "vfork", "execve", "execveat", "ptrace",
)
# Some trusted native package initializers require pthread/clone to start
# CPU worker threads; they must finish before any untrusted bytes arrive.
_LATE_DENIED = ("clone", "clone3")
_FILE_MUTATION_AND_OPEN = (
    "open", "openat", "openat2", "creat", "mkdir", "mkdirat",
    "link", "linkat", "symlink", "symlinkat", "unlink", "unlinkat",
    "rename", "renameat", "renameat2", "mknod", "mknodat",
)


def _limits() -> None:
    if sys.platform != "linux":
        raise RuntimeError("Linux required")
    for kind, soft, hard in (
        (resource.RLIMIT_CORE, 0, 0),
        (resource.RLIMIT_AS, MAX_AS_BYTES, MAX_AS_BYTES),
        (resource.RLIMIT_CPU, CPU_SECONDS, CPU_SECONDS + 1),
        (resource.RLIMIT_FSIZE, MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES),
        (resource.RLIMIT_NOFILE, 64, 64),
    ):
        resource.setrlimit(kind, (soft, hard))


def _deny_io(*, block_files: bool = True) -> None:
    """Deny network and exec before trusted imports; further restrict after.

    Native library initializers may create CPU threads before untrusted input
    arrives. For that narrow startup phase, clone is permitted but socket,
    fork, exec and ptrace are forbidden. Once initialization finishes, the
    second filter also blocks clone and filesystem opening/mutations.
    """
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    # Define the C ABI declaratively rather than repeating the selector
    # worker's assignments; both workers independently fail closed.
    functions = (
        ("seccomp_init", (ctypes.c_uint32,), ctypes.c_void_p),
        ("seccomp_syscall_resolve_name", (ctypes.c_char_p,), ctypes.c_int),
        ("seccomp_rule_add", (
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
        ), ctypes.c_int),
        ("seccomp_load", (ctypes.c_void_p,), ctypes.c_int),
        ("seccomp_release", (ctypes.c_void_p,), None),
    )
    for name, arguments, result_type in functions:
        symbol = getattr(lib, name)
        symbol.argtypes = list(arguments)
        symbol.restype = result_type
    context = lib.seccomp_init(0x7FFF0000)
    if not context:
        raise RuntimeError("Seccomp unavailable")
    try:
        operations = (_EARLY_DENIED + _LATE_DENIED + _FILE_MUTATION_AND_OPEN
                      if block_files else _EARLY_DENIED)
        for name in operations:
            nr = lib.seccomp_syscall_resolve_name(name.encode("ascii"))
            if nr < 0 or lib.seccomp_rule_add(context, 0x00050001, nr, 0) != 0:
                raise RuntimeError("Seccomp rule unavailable")
        if lib.seccomp_load(context) != 0:
            raise RuntimeError("Seccomp load failed")
    finally:
        lib.seccomp_release(context)


def _isolation_check() -> dict[str, object]:
    """Never inspect page content or read a local file in this check."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        network_denied = exc.errno == errno.EPERM
    else:
        sock.close()
        network_denied = False
    try:
        fd = os.open("/dev/null", os.O_RDONLY)
    except OSError as exc:
        file_open_denied = exc.errno == errno.EPERM
    else:
        os.close(fd)
        file_open_denied = False
    return {
        "network_denied": network_denied,
        "file_open_denied": file_open_denied,
        "cpu_soft_seconds": resource.getrlimit(resource.RLIMIT_CPU)[0],
        "max_address_space": resource.getrlimit(resource.RLIMIT_AS)[0],
        "max_output_file": resource.getrlimit(resource.RLIMIT_FSIZE)[0],
    }


def _converter():
    """Register only the reviewed upstream DOCX converter, never ZIP fallback."""
    from importlib.metadata import version
    if version("markitdown") != "0.1.9":
        raise RuntimeError("Unsupported optional MarkItDown version")
    from markitdown import MarkItDown, StreamInfo
    from markitdown.converters import DocxConverter
    instance = MarkItDown(enable_builtins=False, enable_plugins=False)
    instance.register_converter(DocxConverter())
    return instance, StreamInfo


def main() -> int:
    try:
        _limits()
        if sys.argv[1:] == ["--check-isolation"]:
            _deny_io()
            evidence = _isolation_check()
            os.write(1, json.dumps(evidence, separators=(",", ":")).encode())
            return 0 if (evidence["network_denied"] and
                         evidence["file_open_denied"]) else 1
        if len(sys.argv) != 1:
            return 1
        # No network or executable loading is allowed during trusted package
        # imports. Native initialization may use threads before untrusted data
        # is read; the second filter then blocks clone and file operations.
        # No document bytes are received until both filters are installed.
        _deny_io(block_files=False)
        converter, stream_info = _converter()
        _deny_io(block_files=True)
        payload = sys.stdin.buffer.read(office_docx_admission.MAX_DOCX_BYTES + 1)
        office_docx_admission.inspect_docx_container(payload)
        result = converter.convert_stream(
            BytesIO(payload),
            stream_info=stream_info(
                mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                extension=".docx",
            ),
        )
        markdown = result.markdown
        if type(markdown) is not str or not markdown:
            return 1
        encoded = markdown.encode("utf-8", "strict")
        truncated = len(encoded) > MAX_TEXT_BYTES
        text = encoded[:MAX_TEXT_BYTES].decode("utf-8", "ignore")
        report = json.dumps({"markdown": text, "truncated": truncated},
                            ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(report) > MAX_OUTPUT_BYTES:
            return 1
        os.write(1, report)
        return 0
    except (Exception, MemoryError):
        # Upstream exceptions may include document text or source paths.
        # Never emit them via stdout, stderr or an MCP response.
        return 1


if __name__ == "__main__":
    sys.exit(main())
