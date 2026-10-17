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
_INFLIGHT: dict[tuple, tuple[Future, int, dict]] = {}
_REFRESHING: dict[tuple, object] = {}
_POLICY_EPOCH = 0


def fetch_key(url: str, mode: str) -> tuple | None:
    """Cache only queryless public-looking URLs; authorization is separate."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.query or parsed.fragment
                or parsed.username or parsed.password
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
    global _POLICY_EPOCH
    with _LOCK:
        _CACHE.clear()
        _INFLIGHT.clear()
        _REFRESHING.clear()
        _POLICY_EPOCH = 0


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
           should_store, *, flight=None, refresh_token=None) -> bool:
    if should_store is not None and not should_store(result):
        return False
    size = _cacheable_size(result)
    if not size:
        return False
    with _LOCK:
        if kind == "fetch" and generation != _POLICY_EPOCH:
            return False
        if flight is not None and flight[2]["superseded"]:
            return False
        if refresh_token is not None and _REFRESHING.get(key) is not refresh_token:
            return False
        _purge(time.monotonic())
        _CACHE[key] = (time.monotonic() + ttl, size, deepcopy(result))
        _CACHE.move_to_end(key)
        total = sum(entry[1] for entry in _CACHE.values())
        while _CACHE and (len(_CACHE) > MAX_ITEMS or total > MAX_BYTES):
            _, removed = _CACHE.popitem(last=False)
            total -= removed[1]
        return key in _CACHE


def run(kind: str, key: tuple | None, load, *, ttl: float, fresh: bool = False,
        should_store=None):
    """Coalesce default calls; fresh starts independent work and replaces on completion.

    Existing cached content remains available during a fresh request, avoiding
    duplicate upstream loads for simultaneous default callers. A failed fresh
    request evicts the old entry. Refresh state lives only while it is active.
    """
    if key is None:
        return load()
    if fresh:
        token = object()
        with _LOCK:
            _REFRESHING[key] = token
            generation = _POLICY_EPOCH
            # If an earlier default loader is still running, it must not
            # overwrite the result of the newer forced refresh.
            prior_flight = _INFLIGHT.get(key)
            if prior_flight is not None:
                prior_flight[2]["superseded"] = True
        try:
            result = load()
            saved = _store(kind, key, result, ttl, generation, should_store,
                           refresh_token=token)
            if not saved:
                with _LOCK:
                    if _REFRESHING.get(key) is token:
                        _CACHE.pop(key, None)
            return result
        except BaseException:
            with _LOCK:
                if _REFRESHING.get(key) is token:
                    _CACHE.pop(key, None)
            raise
        finally:
            with _LOCK:
                if _REFRESHING.get(key) is token:
                    del _REFRESHING[key]
    with _LOCK:
        _purge(time.monotonic())
        value = _CACHE.get(key)
        if value is not None:
            _CACHE.move_to_end(key)
            return deepcopy(value[2])
        prior_flight = _INFLIGHT.get(key)
        if prior_flight is not None and (
            kind != "fetch" or prior_flight[1] == _POLICY_EPOCH
        ):
            flight = prior_flight
            future = flight[0]
            leader = False
        else:
            future = Future()
            flight = (future, _POLICY_EPOCH, {"superseded": False})
            leader = True
            _INFLIGHT[key] = flight
            generation = _POLICY_EPOCH
    if not leader:
        return deepcopy(future.result())
    try:
        result = load()
        _store(kind, key, result, ttl, generation, should_store, flight=flight)
        future.set_result(result)
        return result
    except BaseException as exc:
        future.set_exception(exc)
        future.exception()  # Silence unobserved-future warnings.
        raise
    finally:
        with _LOCK:
            if _INFLIGHT.get(key) is flight:
                del _INFLIGHT[key]
