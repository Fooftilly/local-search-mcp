"""Private one-use proof propagation and fetch-result release regressions."""
import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import urlsplit
from unittest.mock import patch
import os
import socket
import tempfile
import unittest

import httpx
import bridge
import document_pages
import domain_policy as policy
import request_cache


PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
               ("93.184.216.34", 443))]


class GuardedOnceFetchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": tmp.name,
            "ALLOWED_CRAWL_DOMAINS": "example.com",
        })
        env.start()
        self.addCleanup(env.stop)
        request_cache.reset_for_test()
        self.addCleanup(request_cache.reset_for_test)
        ctx = patch.object(bridge, "CRAWLER_SECURITY_WARNING", None)
        ctx.start()
        self.addCleanup(ctx.stop)
        dns = patch.object(policy.socket, "getaddrinfo", return_value=PUBLIC_DNS)
        dns.start()
        self.addCleanup(dns.stop)
        pages = patch.object(document_pages, "_CACHE", OrderedDict())
        pages.start()
        self.addCleanup(pages.stop)

    def test_single_use_fetch_releases_once_and_never_caches_or_reuses(self):
        grant = policy._stage_once_grant("docs.example.org", expires_minutes=60)
        url = "https://docs.example.org/private?token=SECRET"
        body = {"url": url, "success": True, "markdown": "PRIVATE_CONTENT",
                "truncated": False}
        with patch.object(bridge, "_run_fetch", return_value=body) as crawler:
            first = bridge.web_fetch(url)
            second = bridge.web_fetch(url)
        self.assertTrue(first["success"])
        self.assertEqual(first["markdown"], "PRIVATE_CONTENT")
        self.assertEqual(second["status"], "approval_required")
        self.assertNotIn("PRIVATE_CONTENT", repr(second))
        self.assertNotIn(grant, repr(first))
        self.assertNotIn(grant, repr(second))
        crawler.assert_called_once_with(url, "raw", once_proof=("docs.example.org", grant))
        self.assertEqual(request_cache.metrics_snapshot()["kinds"]["fetch"]["entries"], 0)
        self.assertFalse(policy.is_domain_currently_allowed("docs.example.org"))

    def test_restage_while_claiming_rebases_epoch_without_losing_once(self):
        url = "https://docs.example.org/just-approved"
        policy._stage_once_grant("docs.example.org")
        initial_epoch = request_cache.policy_epoch()
        actual_claim = policy._claim_once_for_public_url
        updated_grant = []

        def claim_after_local_reapproval(address):
            # Deterministic race: web_fetch captured its epoch and observed
            # the earlier grant; the operator replaces that grant immediately
            # before this request atomically claims the new one.
            updated_grant.append(policy._stage_once_grant("docs.example.org"))
            return actual_claim(address)

        payload = {"url": url, "success": True, "markdown": "AUTHORIZED",
                   "truncated": False}
        with patch.object(policy, "_claim_once_for_public_url",
                          side_effect=claim_after_local_reapproval), \\
             patch.object(bridge, "_run_fetch", return_value=payload) as crawler:
            result = bridge.web_fetch(url)

        self.assertGreater(request_cache.policy_epoch(), initial_epoch)
        self.assertEqual(result["markdown"], "AUTHORIZED")
        crawler.assert_called_once_with(
            url, "raw", once_proof=("docs.example.org", updated_grant[0]))
        self.assertIsNone(policy._consume_once_grant("docs.example.org"))
        self.assertNotIn(updated_grant[0], repr(result))

    def test_epoch_mutation_after_one_use_claim_still_blocks_release(self):
        url = "https://docs.example.org/operation"
        policy._stage_once_grant("docs.example.org")

        def change_unrelated_policy(raw, mode, *, once_proof=None):
            policy.decide("other.example.net", "deny")
            return {"url": raw, "success": True, "markdown": "REVOKED_CONTENT",
                    "truncated": False}

        with patch.object(bridge, "_run_fetch", side_effect=change_unrelated_policy):
            result = bridge.web_fetch(url)
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("REVOKED_CONTENT", repr(result))

    def test_permanent_approval_works_normally_and_does_not_burn_once(self):
        grant = policy._stage_once_grant("docs.example.org")
        policy.decide("docs.example.org", "allow", scope="exact")
        url = "https://docs.example.org/a"
        result = {"url": url, "success": True, "markdown": "safe",
                  "truncated": False}
        with patch.object(bridge, "_run_fetch", return_value=result) as crawler:
            self.assertTrue(bridge.web_fetch(url)["success"])
            self.assertTrue(bridge.web_fetch(url)["success"])
        crawler.assert_called_once_with(url, "raw")
        self.assertEqual(policy._consume_once_grant("docs.example.org"), grant)

    def test_revoke_or_deny_mid_fetch_redacts_every_content_field(self):
        for change in ("revoke", "deny"):
            with self.subTest(change=change):
                policy._revoke_once_grant("docs.example.org")
                policy.decide("docs.example.org", "delete")
                policy._stage_once_grant("docs.example.org")
                def crawl(url, mode, *, once_proof=None):
                    if change == "revoke":
                        policy._revoke_once_grant("docs.example.org")
                    else:
                        policy.decide("docs.example.org", "deny")
                    return {"url": url, "success": True, "markdown": "SECRET",
                            "metadata": {"title": "PRIVATE"}, "truncated": False}
                with patch.object(bridge, "_run_fetch", side_effect=crawl):
                    output = bridge.web_fetch("https://docs.example.org/data")
                self.assertEqual(output["status"], "blocked")
                self.assertNotIn("SECRET", repr(output))
                self.assertNotIn("PRIVATE", repr(output))

    def test_unsafe_redirect_requires_independent_final_host_permission(self):
        source = "https://docs.example.org/start"
        target = "https://other.example.net/end"
        policy._stage_once_grant("docs.example.org")
        def redirected(url, mode, *, once_proof=None):
            return {"url": target, "success": True, "markdown": "SECRET",
                    "truncated": False}
        with patch.object(bridge, "_run_fetch", side_effect=redirected):
            rejected = bridge.web_fetch(source)
        self.assertEqual(rejected["status"], "blocked")
        self.assertNotIn("SECRET", repr(rejected))
        policy._stage_once_grant("docs.example.org")
        policy.decide("other.example.net", "allow", scope="exact")
        with patch.object(bridge, "_run_fetch", side_effect=redirected):
            allowed = bridge.web_fetch(source)
        self.assertTrue(allowed["success"])
        self.assertEqual(allowed["markdown"], "SECRET")

    def test_real_crawl_same_host_redirect_uses_original_once_proof(self):
        url = "https://docs.example.org/start"
        target = "https://docs.example.org./final"
        grant = policy._stage_once_grant("docs.example.org")
        def handler(request):
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            return httpx.Response(200, json={"results": [{
                "url": url, "redirected_url": target,
                "success": True, "markdown": {"raw_markdown": "SAFE_MARKDOWN"}
            }]})
        original = httpx.Client
        def factory(**kwargs):
            return original(transport=httpx.MockTransport(handler),
                            trust_env=False, timeout=kwargs.get("timeout", 75))
        with patch.object(bridge.httpx, "Client", side_effect=factory), \
             patch.object(bridge, "_check_public_target",
                          side_effect=AssertionError("same host must not be reapproved")):
            response = bridge.web_fetch(url)
        self.assertTrue(response["success"])
        self.assertEqual(response["url"], target)
        self.assertEqual(response["markdown"], "SAFE_MARKDOWN")
        self.assertNotIn(grant, repr(response))
        self.assertIsNone(policy._consume_once_grant("docs.example.org"))

    def test_real_crawl_redirect_with_unsafe_port_redacts_page(self):
        url = "https://docs.example.org/start"
        policy._stage_once_grant("docs.example.org")
        def handler(request):
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            return httpx.Response(200, json={"results": [{
                "url": url, "redirected_url": "https://docs.example.org:8080/end",
                "success": True, "markdown": {"raw_markdown": "PRIVATE_TEXT"}
            }]})
        original = httpx.Client
        def factory(**kwargs):
            return original(transport=httpx.MockTransport(handler),
                            trust_env=False, timeout=kwargs.get("timeout", 75))
        with patch.object(bridge.httpx, "Client", side_effect=factory):
            response = bridge.web_fetch(url)
        self.assertEqual(response["status"], "blocked")
        self.assertNotIn("PRIVATE_TEXT", repr(response))
        self.assertIsNone(policy._consume_once_grant("docs.example.org"))

    def test_normalized_international_and_trailing_dot_hostnames_keep_proof(self):
        for original in ("https://docs.example.org./article",
                         "https://bücher.example.org/article"):
            with self.subTest(original=original):
                name = policy.normalize_domain(urlsplit(original).hostname)
                policy._stage_once_grant(name)
                body = {"url": original, "success": True,
                        "markdown": "VISIBLE", "truncated": False}
                with patch.object(bridge, "_run_fetch", return_value=body):
                    output = bridge.web_fetch(original)
                self.assertTrue(output["success"])
                self.assertEqual(output["markdown"], "VISIBLE")
                self.assertIsNone(policy._consume_once_grant(name))
                policy._revoke_once_grant(name)

    def test_trailing_dot_document_continuation_checks_normalized_proof(self):
        url = "https://docs.example.org./long"
        normalized = "docs.example.org"
        claim = policy._stage_once_grant(normalized)
        text = "x" * document_pages.PAGE_CHARS + "FOLLOWUP"
        def crawler(raw, mode, *, once_proof=None):
            self.assertEqual(once_proof, (normalized, claim))
            token, count = document_pages.save(
                raw, text, {}, source_url=raw, once_proof=once_proof)
            return {"url": raw, "success": True,
                    "markdown": text[:document_pages.PAGE_CHARS],
                    "document_id": token, "truncated": True, "page": 1,
                    "page_count": count, "next_page": 2}
        with patch.object(bridge, "_run_fetch", side_effect=crawler):
            first = bridge.web_fetch(url)
        self.assertTrue(first["success"])
        second = bridge.web_fetch_page(first["document_id"], 2)
        self.assertEqual(second["markdown"], "FOLLOWUP")

    def test_continuation_proof_bound_to_original_fetch_and_revocation(self):
        grant = policy._stage_once_grant("docs.example.org", expires_minutes=60)
        url = "https://docs.example.org/long"
        markdown = "A" * document_pages.PAGE_CHARS + "PRIVATE_TAIL"
        def crawl(source, mode, *, once_proof=None):
            self.assertEqual(once_proof, ("docs.example.org", grant))
            handle, count = document_pages.save(
                source, markdown, {"title": "safe"},
                source_url=source, once_proof=once_proof)
            return {"url": source, "success": True,
                    "markdown": markdown[:document_pages.PAGE_CHARS],
                    "document_id": handle, "truncated": True,
                    "page": 1, "page_count": count, "next_page": 2}
        with patch.object(bridge, "_run_fetch", side_effect=crawl):
            first = bridge.web_fetch(url)
        token = first["document_id"]
        self.assertNotIn(grant, repr(first))
        old_api, source_url = document_pages.read_with_context(token, 2)
        self.assertEqual(source_url, url)
        self.assertNotIn(grant, repr(old_api))
        continuation = bridge.web_fetch_page(token, 2)
        self.assertEqual(continuation["markdown"], "PRIVATE_TAIL")
        self.assertNotIn(grant, repr(continuation))
        # No second fetch authorization: the consumed once grant is sufficient
        # only for this *same* temporary document handle.
        self.assertIsNone(policy._consume_once_grant("docs.example.org"))
        policy._revoke_once_grant("docs.example.org")
        blocked = bridge.web_fetch_page(token, 2)
        self.assertEqual(blocked["status"], "blocked")
        self.assertNotIn("PRIVATE_TAIL", repr(blocked))

    def test_expired_proof_blocks_continuation_without_wiping_other_documents(self):
        with patch.object(policy, "utcnow", return_value="2026-10-18T12:00:00Z"):
            grant = policy._stage_once_grant("docs.example.org", expires_minutes=60)
            # Internal claim here isolates read checks from crawler timing.
            self.assertEqual(policy._consume_once_grant("docs.example.org"), grant)
        handle, _ = document_pages.save(
            "https://docs.example.org/a", "x" * document_pages.PAGE_CHARS + "SECRET",
            {}, once_proof=("docs.example.org", grant))
        with patch.object(policy, "utcnow", return_value="2026-10-18T12:30:00Z"):
            self.assertEqual(bridge.web_fetch_page(handle, 2)["markdown"], "SECRET")
        with patch.object(policy, "utcnow", return_value="2026-10-18T13:00:00Z"):
            denied = bridge.web_fetch_page(handle, 2)
        self.assertEqual(denied["status"], "blocked")
        self.assertNotIn("SECRET", repr(denied))

    def test_batch_never_consumes_one_use_grants(self):
        grant = policy._stage_once_grant("docs.example.org")
        url = "https://docs.example.org/a"
        with patch.object(bridge, "_run_fetch") as crawler:
            output = asyncio.run(bridge.web_fetch_batch([url]))
        self.assertEqual(output["results"][0]["result"]["status"], "approval_required")
        crawler.assert_not_called()
        self.assertEqual(policy._consume_once_grant("docs.example.org"), grant)

    def test_concurrent_one_use_fetches_have_exactly_one_content_release(self):
        grant = policy._stage_once_grant("docs.example.org")
        url = "https://docs.example.org/page"
        gate = Barrier(8)
        def invoke(_):
            gate.wait(timeout=15)
            return bridge.web_fetch(url)
        with patch.object(bridge, "_run_fetch",
                          return_value={"url": url, "success": True,
                                        "markdown": "ONCE", "truncated": False}
                          ) as crawler, ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(invoke, range(8)))
        self.assertEqual([r.get("markdown") for r in results if r.get("success")],
                         ["ONCE"])
        self.assertEqual(crawler.call_count, 1)
        self.assertNotIn(grant, repr(results))

    def test_invalid_private_continuation_proof_rejected(self):
        markdown = "x" * (document_pages.PAGE_CHARS + 1)
        for proof in (("", ""), ("example.org", "BAD"), (1, 2), "invalid"):
            with self.subTest(proof=proof), self.assertRaises(ValueError):
                document_pages.save(
                    "https://example.org/a", markdown, {}, once_proof=proof)


if __name__ == "__main__":
    unittest.main()
