"""Bounded process-only result cache and concurrent request coalescing.

No disk persistence. Policy changes invalidate all fetch entries and prevent
in-flight work initiated before the change from populating the cache.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future
from copy import deepcopy
from datetime import datetime, timezone
import json
from threading import Lock
import time
from urllib.parse import urlsplit, urlunsplit

MAX_ITEMS = 64
MAX_BYTES = 4 * 1024 * 1024
MAX_ENTRY_BYTES = 128 * 1024
MAX_KEY_BYTES = 2048
SEARCH_TTL = 45
FETCH_TTL = 120

_LOCK = Lock()
_CACHE: OrderedDict[tuple, tuple[float, int, object, tuple[float, str]]] = OrderedDict()
_INFLIGHT: dict[tuple, tuple[Future, int, dict]] = {}
_REFRESHING: dict[tuple, object] = {}
_POLICY_EPOCH = 0
_METRIC_NAMES = ("hits", "misses", "coalesced", "refreshes", "uncacheable",
                 "stores", "evictions", "policy_invalidations")
_METRICS = {kind: {metric: 0 for metric in _METRIC_NAMES}
            for kind in ("search", "fetch")}


def metrics_snapshot() -> dict:
    """Process-lifetime counters only: never reveal cache keys or URL/query text."""
    with _LOCK:
        kinds = {}
        for kind in ("search", "fetch"):
            counts = dict(_METRICS[kind])
            entries = [entry[1] for key, entry in _CACHE.items() if key[0] == kind]
            counts["entries"] = len(entries)
            counts["bytes"] = sum(entries)
            kinds[kind] = counts
        return {"scope": "process_lifetime", "kinds": kinds}


def fetch_key(url: str, mode: str) -> tuple | None:
    """Cache only queryless public-looking URLs; authorization is separate."""
    try:
        # urlsplit discards the distinction between /page and /page? or
        # /page#. A browser may render different content for these URLs.
        # Skip all literal query/fragment delimiters, even empty ones.
        if "?" in url or "#" in url:
            return None
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
        canonical = urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))
        if len(canonical.encode("utf-8")) > MAX_KEY_BYTES:
            return None
        return ("fetch", mode, canonical)
    except (ValueError, TypeError, UnicodeError):
        return None


def policy_epoch() -> int:
    with _LOCK:
        return _POLICY_EPOCH


def invalidate_search() -> None:
    """Evict old engine-selection results after an operator config change.

    Detach pending search loaders and fresh refresh tokens, so a request
    started before the config update cannot reinsert obsolete search results.
    Caller owns the operator-only engine-change permission/verification.
    """
    with _LOCK:
        for key in list(_CACHE):
            if key[0] == "search":
                del _CACHE[key]
                _METRICS["search"]["evictions"] += 1
        for key in list(_INFLIGHT):
            if key[0] == "search":
                _INFLIGHT[key][2]["superseded"] = True
                del _INFLIGHT[key]
        for key in list(_REFRESHING):
            if key[0] == "search":
                del _REFRESHING[key]


def invalidate_fetch() -> None:
    """Invalidate all fetch caches and detach old in-flight fetch operations.

    Dashboard calls this BEFORE committing an explicit allow/deny/delete/rename
    while it holds the SQLite write lock, so post-commit reads never see the
    old generation. Old callers still wake and fail the bridge epoch check.
    """
    global _POLICY_EPOCH
    with _LOCK:
        _POLICY_EPOCH += 1
        _METRICS["fetch"]["policy_invalidations"] += 1
        for key in list(_CACHE):
            if key[0] == "fetch":
                del _CACHE[key]
                _METRICS["fetch"]["evictions"] += 1
        for key in list(_INFLIGHT):
            if key[0] == "fetch":
                _INFLIGHT[key][2]["superseded"] = True
                del _INFLIGHT[key]
        # A pre-change fresh request still owns its local token. Detach the
        # token now so that when its loader finishes it cannot evict a valid
        # entry populated by a later, post-change request for the same key.
        for key in list(_REFRESHING):
            if key[0] == "fetch":
                del _REFRESHING[key]


def reset_for_test() -> None:
    """Tests only: isolate process-local cache between independently mocked APIs."""
    global _POLICY_EPOCH
    with _LOCK:
        _CACHE.clear()
        _INFLIGHT.clear()
        _REFRESHING.clear()
        _POLICY_EPOCH = 0
        for kind in _METRICS:
            for name in _METRIC_NAMES:
                _METRICS[kind][name] = 0


def _purge(now: float) -> None:
    for key, (expiry, _size, _value, _origin) in list(_CACHE.items()):
        if expiry <= now:
            del _CACHE[key]
            _METRICS[key[0]]["evictions"] += 1


def _cacheable_size(result: object, key: tuple) -> int:
    try:
        if not isinstance(result, (list, dict)):
            return 0
        size = (len(json.dumps(result, ensure_ascii=True).encode("utf-8"))
                + len(json.dumps(key, ensure_ascii=True).encode("utf-8")))
        return size if size <= MAX_ENTRY_BYTES else 0
    except (TypeError, ValueError, UnicodeError):
        return 0


def _retrieval_stamp() -> tuple[float, str]:
    """Monotonic age plus fixed UTC retrieval time, no user URL or text."""
    return (time.monotonic(), datetime.fromtimestamp(
        time.time(), tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def _provenance(stamp: tuple[float, str], source: str) -> dict:
    return {
        "retrieved_at": stamp[1],
        "served_from_cache": source == "cache_hit",
        "cache_age_seconds": round(max(0.0, time.monotonic() - stamp[0]), 3)
        if source == "cache_hit" else 0.0,
        "source": source,
    }


def _returned(result, stamp: tuple[float, str], source: str, include: bool):
    return (result, _provenance(stamp, source)) if include else result


def _store(kind: str, key: tuple, result, ttl: float | Callable[[], float], generation: int,
           should_store, *, flight=None, refresh_token=None,
           stamp: tuple[float, str]) -> tuple[bool, bool]:
    """Return (stored, cache_eligible); rejected content is never 'fresh'."""
    if should_store is not None and not should_store(result):
        return False, False
    size = _cacheable_size(result, key)
    if not size:
        return False, False
    # Read a live setting only after the upstream loader finishes. Never
    # hold _LOCK during filesystem I/O or retain the pre-request TTL for a
    # result inserted after an operator changed the saved settings.
    effective_ttl = ttl() if callable(ttl) else ttl
    with _LOCK:
        if kind == "fetch" and generation != _POLICY_EPOCH:
            return False, True
        if flight is not None and flight[2]["superseded"]:
            return False, True
        if refresh_token is not None and _REFRESHING.get(key) is not refresh_token:
            return False, True
        _purge(time.monotonic())
        _CACHE[key] = (time.monotonic() + effective_ttl, size, deepcopy(result), stamp)
        _METRICS[kind]["stores"] += 1
        _CACHE.move_to_end(key)
        total = sum(entry[1] for entry in _CACHE.values())
        while _CACHE and (len(_CACHE) > MAX_ITEMS or total > MAX_BYTES):
            old_key, removed = _CACHE.popitem(last=False)
            _METRICS[old_key[0]]["evictions"] += 1
            total -= removed[1]
        return key in _CACHE, True


def run(kind: str, key: tuple | None, load, *, ttl: float | Callable[[], float], fresh: bool = False,
        should_store=None, with_provenance: bool = False):
    """Coalesce default calls; fresh starts independent work and replaces on completion.

    Existing cached content remains available during a fresh request, avoiding
    duplicate upstream loads for simultaneous default callers. A failed fresh
    request evicts the old entry. Refresh state lives only while it is active.
    A callable TTL is evaluated at successful cache insertion, after loading.
    """
    if kind not in _METRICS:
        raise ValueError("Unknown cache operation kind")
    if key is None:
        with _LOCK:
            _METRICS[kind]["uncacheable"] += 1
        result = load()
        return _returned(result, _retrieval_stamp(), "uncached", with_provenance)
    if fresh:
        with _LOCK:
            _METRICS[kind]["refreshes"] += 1
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
            stamp = _retrieval_stamp()
            saved, cache_eligible = _store(
                kind, key, result, ttl, generation, should_store,
                refresh_token=token, stamp=stamp)
            if not saved:
                with _LOCK:
                    if _REFRESHING.get(key) is token:
                        if _CACHE.pop(key, None) is not None:
                            _METRICS[kind]["evictions"] += 1
            return _returned(result, stamp,
                             "fresh" if cache_eligible else "uncached",
                             with_provenance)
        except BaseException:
            with _LOCK:
                if _REFRESHING.get(key) is token:
                    if _CACHE.pop(key, None) is not None:
                        _METRICS[kind]["evictions"] += 1
            raise
        finally:
            with _LOCK:
                if _REFRESHING.get(key) is token:
                    del _REFRESHING[key]
    with _LOCK:
        _purge(time.monotonic())
        value = _CACHE.get(key)
        if value is not None:
            _METRICS[kind]["hits"] += 1
            _CACHE.move_to_end(key)
            return _returned(deepcopy(value[2]), value[3], "cache_hit",
                             with_provenance)
        prior_flight = _INFLIGHT.get(key)
        if prior_flight is not None and (
            kind != "fetch" or prior_flight[1] == _POLICY_EPOCH
        ):
            flight = prior_flight
            future = flight[0]
            leader = False
            _METRICS[kind]["coalesced"] += 1
        else:
            future = Future()
            # An uncached default call may begin while a forced refresh is
            # already running. Its result must not overwrite that newer
            # refresh, even if it completes last.
            flight = (future, _POLICY_EPOCH,
                      {"superseded": key in _REFRESHING})
            leader = True
            _INFLIGHT[key] = flight
            generation = _POLICY_EPOCH
            _METRICS[kind]["misses"] += 1
    if not leader:
        result = deepcopy(future.result())
        # The leader records its origin before making the result visible.
        stamp = flight[2]["stamp"]
        return _returned(result, stamp,
                         "coalesced" if flight[2]["cache_eligible"] else "uncached",
                         with_provenance)
    try:
        result = load()
        stamp = _retrieval_stamp()
        _, cache_eligible = _store(kind, key, result, ttl, generation,
                                    should_store, flight=flight, stamp=stamp)
        flight[2]["stamp"] = stamp
        flight[2]["cache_eligible"] = cache_eligible
        future.set_result(result)
        return _returned(result, stamp,
                         "fresh" if cache_eligible else "uncached",
                         with_provenance)
    except BaseException as exc:
        future.set_exception(exc)
        future.exception()  # Silence unobserved-future warnings.
        raise
    finally:
        with _LOCK:
            if _INFLIGHT.get(key) is flight:
                del _INFLIGHT[key]
