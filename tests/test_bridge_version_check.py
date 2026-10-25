"""Opt-in GitHub revision-check regression tests; never use live network."""
from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
import io
import http.client
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from scripts import check_bridge_version as versions


class FakeResponse:
    status = 200

    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, size=-1):
        return self.data[:size]


class FakeOpener:
    def __init__(self, data):
        self.response = FakeResponse(data)
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        return self.response


class BridgeVersionCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        def git(*args):
            return subprocess.run(
                ["git", *args], cwd=self.root, capture_output=True,
                check=True, text=True,
            ).stdout.strip()
        git("init", "-q")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.invalid")
        (self.root / "bridge.py").write_text("print('ok')\n")
        git("add", "bridge.py")
        git("commit", "-qm", "first")
        self.sha = git("rev-parse", "HEAD")

    def test_network_requires_explicit_flag(self):
        errors = io.StringIO()
        with patch.object(versions, "remote_head", side_effect=AssertionError("network")), \
             redirect_stderr(errors), self.assertRaises(SystemExit) as caught:
            versions.main([])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("explicit --check-remote", errors.getvalue())

    def test_equal_local_revision_without_side_effects(self):
        with patch.object(versions, "remote_head", return_value=self.sha):
            report = versions.check(root=self.root)
        self.assertIn("matches GitHub master", report)

    def test_different_revision_never_claims_newer(self):
        with patch.object(versions, "remote_head", return_value="a" * 40):
            report = versions.check(root=self.root)
        self.assertIn("differs", report)
        self.assertIn("does NOT prove a newer version", report)

    def test_remote_request_is_pinned_bounded_and_no_proxy(self):
        fake = FakeOpener(json.dumps({"sha": "a" * 40, "secret": "IGNORE"}).encode())
        with patch.object(versions.urllib.request, "build_opener",
                          return_value=fake) as builder:
            self.assertEqual(versions.remote_head(), "a" * 40)
        request, timeout = fake.calls[0]
        self.assertEqual(request.full_url, versions.REMOTE_URL)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, 7)
        self.assertTrue(any(type(x) is urllib.request.ProxyHandler
                            and x.proxies == {} for x in builder.call_args.args))
        self.assertTrue(any(isinstance(x, versions._NoRedirect)
                            for x in builder.call_args.args))
        self.assertTrue(any(isinstance(x, urllib.request.HTTPSHandler)
                            for x in builder.call_args.args))

    def test_bad_or_oversized_remote_responses_fail_closed(self):
        for raw in (b"{}", b"invalid json", b'{"sha":"short"}',
                    b"x" * (versions._MAX_JSON + 1)):
            with self.subTest(raw=raw[:30]), \
                 patch.object(versions.urllib.request, "build_opener",
                              return_value=FakeOpener(raw)):
                with self.assertRaises(ValueError):
                    versions.remote_head()

    def test_unavailable_remote_reports_no_url_or_response_body(self):
        failure = urllib.error.HTTPError(
            versions.REMOTE_URL, 302, "Redirect", {}, None,
        )
        errors = io.StringIO()
        with patch.object(versions, "ROOT", self.root), \
             patch.object(versions, "remote_head", side_effect=failure), \
             redirect_stderr(errors):
            self.assertEqual(versions.main(["--check-remote"]), 2)
        self.assertIn("unavailable", errors.getvalue())
        self.assertNotIn(versions.REMOTE_URL, errors.getvalue())

    def test_non_git_checkout_fails_without_traceback_or_network(self):
        wrong = Path(self.tmp.name) / "not-a-repo"
        wrong.mkdir()
        stderr = io.StringIO()
        with patch.object(versions, "ROOT", wrong), \
             patch.object(versions, "remote_head",
                          side_effect=AssertionError("must not contact GitHub")), \
             redirect_stderr(stderr):
            self.assertEqual(versions.main(["--check-remote"]), 2)
        message = stderr.getvalue()
        self.assertIn("Bridge revision check unavailable", message)
        self.assertIn("ValueError", message)
        self.assertNotIn("Traceback", message)
        self.assertNotIn(str(wrong), message)

    def test_interrupted_or_invalid_http_response_fails_without_traceback(self):
        errors_to_test = (
            http.client.IncompleteRead(b"some bytes", 10),
            http.client.BadStatusLine("malformed status"),
            http.client.LineTooLong("header too long"),
            subprocess.TimeoutExpired(["git", "rev-parse", "HEAD"], 1),
        )
        for failure in errors_to_test:
            with self.subTest(error=type(failure).__name__):
                stderr = io.StringIO()
                # Inject failures at the check boundary to validate CLI
                # handling without making network requests.
                with patch.object(versions, "check", side_effect=failure), \
                     redirect_stderr(stderr):
                    self.assertEqual(versions.main(["--check-remote"]), 2)
                output = stderr.getvalue()
                self.assertIn("unavailable", output)
                self.assertIn(type(failure).__name__, output)
                self.assertNotIn("Traceback", output)
                self.assertNotIn("malformed status", output)

    def test_http_read_incomplete_response_fails_without_traceback(self):
        fake = FakeOpener(json.dumps({"sha": "a" * 40}).encode())

        class InterruptedResponse(FakeResponse):
            def read(self, size=-1):
                raise http.client.IncompleteRead(b"fragment", 7)

        fake.response = InterruptedResponse(b"")
        stderr = io.StringIO()
        with patch.object(versions, "ROOT", self.root), \
             patch.object(versions.urllib.request, "build_opener",
                          return_value=fake), redirect_stderr(stderr):
            self.assertEqual(versions.main(["--check-remote"]), 2)
        self.assertIn("IncompleteRead", stderr.getvalue())
        self.assertNotIn("fragment", stderr.getvalue())

    def test_explicit_cli_matches_pinned_local_checkout(self):
        output = io.StringIO()
        with patch.object(versions, "ROOT", self.root), \
             patch.object(versions, "remote_head", return_value=self.sha), \
             redirect_stdout(output):
            self.assertEqual(versions.main(["--check-remote"]), 0)
        self.assertIn("matches GitHub master", output.getvalue())


if __name__ == "__main__":
    unittest.main()
