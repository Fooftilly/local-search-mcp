"""Bounded synchronous batch orchestration for the existing guarded fetch tool.

Every unique URL goes through web_fetch's original approval, network and
Crawl4AI security checks. Duplicates reuse its result, not its authorization.
A private audit event is recorded for each input (including duplicates).
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from copy import deepcopy
from threading import Condition, Event
from typing import Callable

from history_log import logged_tool
import document_pages
import request_cache

MAX_URLS = 8
MAX_WORKERS = 3
MAX_URL_CHARS = 2048
MAX_TOTAL_URL_CHARS = 8192

class _FairBatchLimiter:
    """FIFO active-fetch permits shared by all local batch calls.

    Waiting tasks are served in arrival order. Cancelled workers can abandon
    their ticket without waiting for another crawler to finish.
    """

    def __init__(self, limit: int):
        self._limit = limit
        self._condition = Condition()
        self._running = 0
        self._waiting: deque[object] = deque()

    def acquire(self, cancelled: Event | None) -> bool:
        ticket = object()
        with self._condition:
            self._waiting.append(ticket)
            while True:
                if cancelled is not None and cancelled.is_set():
                    self._waiting.remove(ticket)
                    self._condition.notify_all()
                    return False
                if self._waiting[0] is ticket and self._running < self._limit:
                    self._waiting.popleft()
                    self._running += 1
                    self._condition.notify_all()
                    return True
                # A cancelled MCP call may not signal this condition directly.
                self._condition.wait(timeout=0.1 if cancelled is not None else None)

    def release(self) -> None:
        with self._condition:
            self._running -= 1
            self._condition.notify_all()


# All batches use this single FIFO limiter; per-call thread pools remain
# bounded, and no caller can repeatedly jump ahead of earlier waiters.
_ACTIVE_BATCH_FETCHES = _FairBatchLimiter(MAX_WORKERS)


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


def _finalize_result(url: str, answer: dict, *, policy_changed: bool) -> dict:
    """Never return content revoked mid-batch or already-evicted page handles."""
    if policy_changed:
        # Failed Crawl4AI pages may also carry markdown, metadata or handles.
        # Redact the *entire* earlier result, not just successful responses.
        return {"url": url, "success": False, "status": "blocked",
                "reason": "Domain permissions changed during the batch. Retry the URL."}
    if answer.get("success") is True and "document_id" in answer:
        try:
            continuation = document_pages.read(answer["document_id"], 2)
            valid = continuation.get("success") is True
        except (ValueError, TypeError):
            valid = False
        if not valid:
            # Discard any unusable token, page text or other cached details
            # rather than presenting a fake usable long-document result.
            return {"url": url, "success": False, "status": "continuation_evicted",
                    "reason": "The temporary continuation expired or was evicted while fetching other batch URLs. Fetch this URL individually."}
    return answer


def fetch_many(urls: list[str], mode: str, fresh: bool,
               fetch: Callable[[str, str, bool], dict],
               *, cancelled: Event | None = None,
               authorize_result: Callable[[str, dict], bool] | None = None) -> dict:
    """Map approved single-page fetches to independent ordered results.

    At most 3 distinct URL jobs execute at a time. Their existing HTTP timeouts
    and retry limits still apply. No new URLs are approved by this function.
    """
    validate_batch(urls, mode, fresh)
    starting_policy_epoch = request_cache.policy_epoch()
    unique = list(dict.fromkeys(urls))
    results: dict[str, dict] = {}
    def scheduled_fetch(url: str) -> dict:
        # This permit is process-wide. Multiple simultaneous client batches
        # may queue workers but cannot exceed 3 active underlying fetches.
        if not _ACTIVE_BATCH_FETCHES.acquire(cancelled):
            result = {"url": url, "success": False, "status": "cancelled",
                      "reason": "Batch cancelled before this URL began fetching."}
            return _audit_duplicate(url, result)
        try:
            return fetch(url, mode, fresh)
        finally:
            _ACTIVE_BATCH_FETCHES.release()

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(unique)),
                            thread_name_prefix="local-search-batch") as pool:
        tasks = {pool.submit(scheduled_fetch, url): url for url in unique}
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
    # Invalidation is intentionally conservative: permission changes to any
    # domain block earlier successes, even if another domain was edited. A
    # single-fetch result may have completed minutes before the last worker.
    changed = starting_policy_epoch != request_cache.policy_epoch()
    finalized = {
        url: _finalize_result(url, result, policy_changed=changed)
        for url, result in results.items()
    }
    for index, url in enumerate(urls):
        result = deepcopy(finalized[url])
        if url in seen:
            _audit_duplicate(url, result)
        else:
            seen.add(url)
        output.append({"index": index, "requested_url": url, "result": result})
    # A second check also covers an edit while the final rows are assembled.
    if starting_policy_epoch != request_cache.policy_epoch():
        for row in output:
            row["result"] = _finalize_result(
                row["requested_url"], row["result"], policy_changed=True)
    # A timed rule can expire after one worker returns but before the slowest
    # worker finishes. The operator may also deny a redirected final host.
    # Recheck finished content just before assembling the MCP response, and
    # never expose a previously accepted Markdown/page handle on failure.
    if authorize_result is not None:
        valid_by_url: dict[str, bool] = {}
        for row in output:
            answer = row["result"]
            if answer.get("success") is not True:
                continue
            url = row["requested_url"]
            if url not in valid_by_url:
                try:
                    valid_by_url[url] = bool(authorize_result(url, answer))
                except Exception:
                    valid_by_url[url] = False
            if not valid_by_url[url]:
                row["result"] = {"url": url, "success": False, "status": "blocked",
                                 "reason": "Domain permission is no longer valid. Retry after local approval."}
    # Revocation during reauthorization must also redact all prior successes.
    if starting_policy_epoch != request_cache.policy_epoch():
        for row in output:
            row["result"] = _finalize_result(
                row["requested_url"], row["result"], policy_changed=True)
    return {"results": output, "count": len(output)}
