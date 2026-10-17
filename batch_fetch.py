"""Bounded synchronous batch orchestration for the existing guarded fetch tool.

Every unique URL goes through web_fetch's original approval, network and
Crawl4AI security checks. Duplicates reuse its result, not its authorization.
A private audit event is recorded for each input (including duplicates).
"""
from __future__ import annotations

from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from copy import deepcopy
from typing import Callable

from history_log import logged_tool

MAX_URLS = 8
MAX_WORKERS = 3
MAX_URL_CHARS = 2048
MAX_TOTAL_URL_CHARS = 8192


@logged_tool("fetch")
def _audit_duplicate(url: str, response: dict) -> dict:
    """Record the same sanitized fetch event without requesting the URL again."""
    return response


def validate_batch(urls: list[str], mode: str, fresh: bool) -> None:
    """Reject an oversized request before submitting any worker."""
    if not isinstance(urls, list) or not 1 <= len(urls) <= MAX_URLS:
        raise ValueError("urls must be a list of 1 to 8 URLs")
    if (any(not isinstance(url, str) or not 1 <= len(url) <= MAX_URL_CHARS
            for url in urls)
            or sum(len(url) for url in urls) > MAX_TOTAL_URL_CHARS):
        raise ValueError("each URL must be 1–2048 characters; at most 8192 total")
    if mode not in ("raw", "fit"):
        raise ValueError("mode must be raw or fit")
    if not isinstance(fresh, bool):
        raise ValueError("fresh must be a boolean")


def _safe_failure(url: str, exc: Exception) -> dict:
    """Do not reflect exception strings (which can include secrets or tokens)."""
    return {"url": url, "success": False, "status": "cancelled"
            if isinstance(exc, CancelledError) else "fetch_error",
            "reason": "The individual fetch was cancelled or failed; retry this URL.",
            "error_type": type(exc).__name__}


def fetch_many(urls: list[str], mode: str, fresh: bool,
               fetch: Callable[[str, str, bool], dict]) -> dict:
    """Map approved single-page fetches to independent ordered results.

    At most 3 distinct URL jobs execute at a time. Their existing HTTP timeouts
    and retry limits still apply. No new URLs are approved by this function.
    """
    validate_batch(urls, mode, fresh)
    unique = list(dict.fromkeys(urls))
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(unique)),
                            thread_name_prefix="local-search-batch") as pool:
        tasks = {pool.submit(fetch, url, mode, fresh): url for url in unique}
        for future in as_completed(tasks):
            url = tasks[future]
            try:
                answer = future.result()
                if not isinstance(answer, dict):
                    answer = {"url": url, "success": False, "status": "invalid_response",
                              "reason": "The fetch tool returned an invalid result."}
            except Exception as exc:
                answer = _safe_failure(url, exc)
            results[url] = answer

    # Input order is retained even if individual workers complete out of
    # order. Each duplicate receives its own response object and audit event.
    seen: set[str] = set()
    output = []
    for index, url in enumerate(urls):
        result = deepcopy(results[url])
        if url in seen:
            _audit_duplicate(url, result)
        else:
            seen.add(url)
        output.append({"index": index, "requested_url": url, "result": result})
    return {"results": output, "count": len(output)}
