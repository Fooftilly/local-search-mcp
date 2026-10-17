"""Bounded process-only result cache and concurrent request coalescing.

No disk persistence. Policy changes invalidate all fetch entries and prevent
in-flight work initiated before the change from populating the cache.
"""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
import json
from threading import Lock
import time
from urllib.parse import urlsplit, urlunsplit

MAX_ITEMS = 64
MAX_BYTES = 4 * 1024 * 1024
MAX_ENTRY_BYTES = 128 * 1024
SEARCH_TTL = 45
FETCH_TTL = 120

_LOCK = Lock()
_CACHE: OrderedDict[tuple, tuple[float, int, object]] = OrderedDict()
_INFLIGHT: dict[tuple, Future] = {}
_POLICY_EPOCH = 0
_FRESH_REVISION = 0


def fetch_key(url: str, mode: str) -> tuple | None:
    """Cache only queryless public-looking URLs; authorization is separate."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.query or parsed.username or parsed.password
                or parsed.port not in (None, 80, 443)):
            return None
        netloc = parsed.hostname.lower()
        # Do not conflate non-default explicit ports or distinct paths.
        if parsed.port is not None:
            netloc += f":{parsed.port}"
        path = parsed.path or "/"
        return ("fetch", mode, urlunsplit((parsed.scheme.lower(), netloc, path, "", "")))
    except (ValueError, TypeError):
        return None


def policy_epoch() -> int:
    with _LOCK:
        return _POLICY_EPOCH


def invalidate_fetch() -> None:
    """Called by dashboard policy writes after SQLite commit."""
    global _POLICY_EPOCH
    with _LOCK:
        _POLICY_EPOCH += 1
        for key in list(_CACHE):
            if key[0] == "fetch":
                del _CACHE[key]


def reset_for_test() -> None:
    """Tests only: isolate process-local cache between independently mocked APIs."""
    global _POLICY_EPOCH, _FRESH_REVISION
    with _LOCK:
        _CACHE.clear()
        _INFLIGHT.clear()
        _POLICY_EPOCH = 0
        _FRESH_REVISION = 0


def _purge(now: float) -> None:
    for key, (expiry, _size, _value) in list(_CACHE.items()):
        if expiry <= now:
            del _CACHE[key]


def _cacheable_size(result: object) -> int:
    try:
        if not isinstance(result, (list, dict)):
            return 0
        size = len(json.dumps(result, ensure_ascii=True).encode("utf-8"))
        return size if size <= MAX_ENTRY_BYTES else 0
    except (TypeError, ValueError, UnicodeError):
        return 0


def _store(kind: str, key: tuple, result, ttl: float, generation: int,
           revision: int, should_store) -> None:
    if should_store is not None and not should_store(result):
        return
    size = _cacheable_size(result)
    if not size:
        return
    with _LOCK:
        # A concurrent forced refresh or domain-rule edit supersedes this
        # request; late results must not overwrite newer cache content.
        if revision != _FRESH_REVISION or (
            kind == "fetch" and generation != _POLICY_EPOCH
        ):
            return
        _purge(time.monotonic())
        _CACHE[key] = (time.monotonic() + ttl, size, deepcopy(result))
        _CACHE.move_to_end(key)
        total = sum(entry[1] for entry in _CACHE.values())
        while _CACHE and (len(_CACHE) > MAX_ITEMS or total > MAX_BYTES):
            _, removed = _CACHE.popitem(last=False)
            total -= removed[1]


def run(kind: str, key: tuple | None, load, *, ttl: float, fresh: bool = False,
        should_store=None):
    """Cache bounded valid results; coalesce concurrent identical requests.

    An explicit fresh call skips cache lookup and single-flight but refreshes
    the cached value if successful. A failure evicts the old cached value.
    """
    global _FRESH_REVISION
    if key is None:
        return load()
    if fresh:
        with _LOCK:
            _CACHE.pop(key, None)
            _FRESH_REVISION += 1
            revision = _FRESH_REVISION
            generation = _POLICY_EPOCH
        result = load()
        _store(kind, key, result, ttl, generation, revision, should_store)
        return result
    with _LOCK:
        _purge(time.monotonic())
        value = _CACHE.get(key)
        if value is not None:
            _CACHE.move_to_end(key)
            return deepcopy(value[2])
        future = _INFLIGHT.get(key)
        leader = future is None
        if leader:
            future = Future()
            _INFLIGHT[key] = future
            generation = _POLICY_EPOCH
            revision = _FRESH_REVISION
    if not leader:
        return deepcopy(future.result())
    try:
        result = load()
        _store(kind, key, result, ttl, generation, revision, should_store)
        future.set_result(result)
        return result
    except BaseException as exc:
        future.set_exception(exc)
        # Prevent unobserved-future warnings when no follower consumes it.
        future.exception()
        raise
    finally:
        with _LOCK:
            if _INFLIGHT.get(key) is future:
                del _INFLIGHT[key]
