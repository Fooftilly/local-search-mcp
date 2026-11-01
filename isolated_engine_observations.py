"""Private, bounded observations of isolated SearXNG /search operations.

Only calls that actually reach the local network search boundary are
recorded. This is not a claim about third-party provider uptime, elapsed
provider time, or the number of internal SearXNG/HTTP retries.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import re
from time import monotonic

_ACTIVE: ContextVar[list[dict] | None] = ContextVar("mcp_isolated_search_observations", default=None)
_ENGINE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()\-]{0,39}\Z")
_SECRET = re.compile(r"(?i)(?:token|password|secret|bearer|cookie|api.?key)")
_ALLOWED_SOURCE = frozenset(("explicit", "isolated_recovery", "same_query_recovery",
                             "paced_default", "paced_category"))
_ALLOWED_OUTCOME = frozenset(("results", "empty", "reported_unresponsive",
                              "http_error", "transport_error", "invalid_response",
                              "unverified_attribution"))


@contextmanager
def capture():
    """Isolate observations per decorated tool invocation, including threads."""
    collection: list[dict] = []
    token = _ACTIVE.set(collection)
    try:
        yield collection
    finally:
        _ACTIVE.reset(token)


def observe(params: dict, response: object, elapsed_ms: float, *, source: str) -> None:
    """Record one bounded local /search operation with one selected engine.

    No full query, URL, response body, exception, headers, or credentials are
    retained. The elapsed time measures the local search operation (and may
    include internal retries), NOT upstream engine execution time.
    """
    collection = _ACTIVE.get()
    if collection is None or len(collection) >= 3 or source not in _ALLOWED_SOURCE:
        return
    engine = params.get("engines")
    if (not isinstance(engine, str) or not _ENGINE.fullmatch(engine)
            or _SECRET.search(engine) or "," in engine):
        return
    if type(elapsed_ms) not in (int, float) or not 0 <= elapsed_ms <= 3_600_000:
        return
    status = getattr(response, "status_code", None)
    if response is None:
        outcome = "transport_error"
    elif type(status) is not int or status != 200:
        outcome = "http_error"
    else:
        try:
            payload = response.json()
        except (ValueError, UnicodeError, TypeError):
            payload = None
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            outcome = "invalid_response"
        elif payload["results"]:
            rows = payload["results"]
            outcome = "results" if all(
                isinstance(row, dict) and row.get("engine") == engine
                and isinstance(row.get("engines", []), list)
                and all(n == engine for n in row.get("engines", []))
                for row in rows) else "unverified_attribution"
        else:
            failed = payload.get("unresponsive_engines")
            reported = (isinstance(failed, list) and any(
                (item[0] if isinstance(item, (list, tuple)) and item else item) == engine
                for item in failed[:128]))
            outcome = "reported_unresponsive" if reported else "empty"
    collection.append({
        "engine": engine, "source": source, "outcome": outcome,
        "roundtrip_ms": round(elapsed_ms, 1),
    })
