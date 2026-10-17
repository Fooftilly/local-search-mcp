"""Offline public suffix validation, including private provider suffixes.

Uses libpsl's built-in Public Suffix List (PSL), which is available on Arch
Linux through pacman -S libpsl. Never fetches suffix data over the network.
"""
from __future__ import annotations

import ctypes
from ctypes.util import find_library
from functools import lru_cache


@lru_cache(maxsize=1)
def _library():
    path = find_library("psl")
    if not path:
        raise RuntimeError("libpsl is required for safe domain approvals (Arch: sudo pacman -S libpsl)")
    lib = ctypes.CDLL(path)
    lib.psl_builtin.argtypes = []
    lib.psl_builtin.restype = ctypes.c_void_p
    lib.psl_is_public_suffix2.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    lib.psl_is_public_suffix2.restype = ctypes.c_int
    lib.psl_registrable_domain.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.psl_registrable_domain.restype = ctypes.c_char_p
    context = lib.psl_builtin()
    if not context:
        raise RuntimeError("libpsl has no built-in Public Suffix List")
    return lib, context


def is_public_suffix(host: str) -> bool:
    """Return whether a DNS hostname is an ICANN or private public suffix."""
    library, context = _library()
    return bool(library.psl_is_public_suffix2(context, host.encode("ascii"), 3))

def registrable_domain(host: str) -> str:
    """Return a PSL registrable site, respecting private hosted suffixes.

    Call only with normalized ASCII DNS hostnames. The libpsl result is a
    pointer into the input buffer and ctypes converts it to Python bytes.
    """
    library, context = _library()
    result = library.psl_registrable_domain(context, host.encode("ascii"))
    return result.decode("ascii") if result else host
