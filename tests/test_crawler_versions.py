"""No live Docker or Internet requests in Crawl4AI version-check regressions."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from scripts import check_crawler_versions as versions


class _Response:
    status = 200

    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, max_bytes):
        return self.body[:max_bytes]


class _Opener:
    def __init__(self, body: bytes):
        self.calls = []
        self.response = _Response(body)

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        return self.response


class Crawl4AIVersionTests(unittest.TestCase):
    def test_default_is_offline_and_reads_only_local_health(self):
        with patch.object(versions, "get_json",
                          return_value={"status": "ok", "version": "0.9.5"}), \
             patch.object(versions, "published_release",
                          side_effect=AssertionError("remote lookup not allowed")):
            result = versions.version_report()
        self.assertEqual(result["local"]["version"], "0.9.5")
        self.assertTrue(result["local"]["supported_and_healthy"])
        self.assertFalse(result["remote_requested"])
        self.assertNotIn("published_release", result)

    def test_old_or_unhealthy_local_version_is_not_approved(self):
        for data in ({"status": "ok", "version": "0.9.4"},
                     {"status": "down", "version": "0.9.6"},
                     {"status": "ok", "version": "0.9.5-rc1"}):
            with self.subTest(data=data), patch.object(versions, "get_json", return_value=data):
                report = versions.installed_version()
            self.assertFalse(report["supported_and_healthy"])
        for data in ({"status": "ok", "version": "secret\nVALUE"}, {"token": "secret"}, []):
            with self.subTest(data=data), patch.object(versions, "get_json", return_value=data):
                self.assertIsNone(versions.installed_version()["version"])

    def test_bad_health_is_sanitized_without_leaking_response(self):
        with patch.object(versions, "get_json", side_effect=OSError("api token=secret")):
            output = versions.installed_version()
        self.assertEqual(output["error"], "Local Crawl4AI health lookup unavailable")
        self.assertNotIn("secret", json.dumps(output))

    def test_explicit_remote_is_fixed_bounded_and_proxy_free(self):
        response = _Opener(json.dumps({
            "tag_name": "v0.9.6", "draft": False, "prerelease": False
        }).encode())
        with patch.object(versions.urllib.request, "build_opener",
                          return_value=response) as builder:
            result = versions.published_release()
        self.assertEqual(result["tag"], "v0.9.6")
        self.assertTrue(result["meets_bridge_minimum"])
        request, timeout = response.calls[0]
        self.assertEqual(request.full_url, versions.LATEST_URL)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, 7)
        self.assertTrue(any(type(x) is urllib.request.ProxyHandler and x.proxies == {}
                            for x in builder.call_args.args))
        self.assertTrue(any(isinstance(x, versions._NoRedirect)
                            for x in builder.call_args.args))

    def test_published_release_below_required_minimum_is_not_accepted(self):
        response = _Opener(json.dumps({
            "tag_name": "v0.9.4", "draft": False, "prerelease": False
        }).encode())
        with patch.object(versions.urllib.request, "build_opener",
                          return_value=response):
            remote = versions.published_release()
        self.assertFalse(remote["meets_bridge_minimum"])
        self.assertEqual(remote["tag"], "v0.9.4")

    def test_invalid_remote_metadata_fails_closed(self):
        for data in (
            b"{}", b"not-json", b"x" * (versions._MAX_BYTES + 1),
            b'{"tag_name":"v0.9.5-rc.1","draft":false,"prerelease":false}',
            b'{"tag_name":"v0.9.6","draft":true,"prerelease":false}',
        ):
            with self.subTest(data=data[:40]), \
                 patch.object(versions.urllib.request, "build_opener",
                              return_value=_Opener(data)):
                with self.assertRaises(ValueError):
                    versions.published_release()

    def test_release_difference_never_automatically_upgrades(self):
        with patch.object(versions, "get_json",
                          return_value={"status": "ok", "version": "0.9.6+local"}), \
             patch.object(versions, "published_release",
                          return_value={"tag": "v0.9.6",
                                        "meets_bridge_minimum": True}):
            report = versions.version_report(check_remote=True)
        self.assertTrue(report["exact_version_match"])
        self.assertIn("no Docker, Git, package or service changes", report["note"])
        with patch.object(versions, "get_json",
                          return_value={"status": "ok", "version": "0.9.5"}), \
             patch.object(versions, "published_release",
                          return_value={"tag": "v0.9.4",
                                        "meets_bridge_minimum": False}):
            report = versions.version_report(check_remote=True)
        self.assertFalse(report["exact_version_match"])
        self.assertFalse(report["published_release"]["meets_bridge_minimum"])

    def test_cli_network_error_is_sanitized_and_nonzero(self):
        with patch.object(versions, "get_json",
                          return_value={"status": "ok", "version": "0.9.5"}), \
             patch.object(versions, "published_release",
                          side_effect=urllib.error.URLError("private-token")), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(versions.main(["--check-remote"]), 1)
        self.assertNotIn("private-token", output.getvalue())
        self.assertIn("unavailable or invalid", output.getvalue())

    def test_cli_local_unavailable_is_nonzero(self):
        with patch.object(versions, "get_json", side_effect=OSError("offline")), \
             patch.object(versions, "published_release",
                          side_effect=AssertionError("should remain offline")), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(versions.main([]), 1)
        self.assertIn("health lookup unavailable", output.getvalue())


if __name__ == "__main__":
    unittest.main()
