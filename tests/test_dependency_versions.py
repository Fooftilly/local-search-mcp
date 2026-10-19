"""No-network regressions for manual dependency release checks."""
import json
from email.message import Message
from http.client import BadStatusLine, IncompleteRead, LineTooLong
import unittest
from unittest.mock import patch

from scripts import check_dependency_versions as check


class Response:
    status = 200

    def __init__(self, payload, content_type="application/json"):
        self.payload = payload
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, size):
        return self.payload[:size]


class Opener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, url, *, timeout):
        self.requests.append((url, timeout))
        return self.response


class VersionCheckTests(unittest.TestCase):
    def test_local_only_does_not_build_network_opener(self):
        with patch.object(check, "build_opener", side_effect=AssertionError("network")):
            with patch.object(check, "_installed_version", return_value="1.2"):
                report = check.version_report()
        self.assertEqual([x["installed"] for x in report], ["1.2", "1.2"])
        self.assertTrue(all("pypi" not in x for x in report))

    def test_explicit_remote_fixes_host_and_disables_proxy_redirect(self):
        opener = Opener(Response(b'{"info":{"version":"4.2.1"}}'))
        with patch.object(check, "build_opener", return_value=opener) as factory:
            result = check._pypi_version("fastmcp")
        self.assertEqual(result, "4.2.1")
        self.assertEqual(opener.requests, [("https://pypi.org/pypi/fastmcp/json", 5)])
        self.assertTrue(any(isinstance(x, check.ProxyHandler) and x.proxies == {}
                            for x in factory.call_args.args))
        self.assertTrue(any(isinstance(x, check.NoRedirect) for x in factory.call_args.args))

    def test_invalid_response_and_error_do_not_infer_upgrades(self):
        for payload in (b"not-json", b"{}", b'{"info":{"version":123}}',
                        b'{"info":{"version":"bad\\nversion"}}',
                        b"x" * (check.MAX_RESPONSE_BYTES + 1)):
            with self.subTest(payload=payload[:30]):
                with patch.object(check, "build_opener",
                                  return_value=Opener(Response(payload))):
                    with self.assertRaises(ValueError):
                        check._pypi_version("httpx")
        with patch.object(check, "build_opener", return_value=Opener(
                Response(b'{"info":{"version":"1.1"}}', "text/html"))):
            with self.assertRaises(ValueError):
                check._pypi_version("httpx")
        with self.assertRaises(ValueError):
            check._pypi_version("some-other-package")

    def test_http_protocol_errors_are_reported_not_tracebacks(self):
        """Malformed headers, status lines and truncated bodies fail closed."""
        for failure in (BadStatusLine("broken response"),
                        LineTooLong("header"),
                        IncompleteRead(b"part", 7)):
            with self.subTest(error=type(failure).__name__):
                with patch.object(check, "_installed_version", return_value="2.0"):
                    with patch.object(check, "build_opener") as opener:
                        opener.return_value.open.side_effect = failure
                        report = check.version_report(check_remote=True)
                        self.assertEqual(len(report), len(check.PACKAGES))
                        self.assertTrue(all("error" in row for row in report))
                        self.assertTrue(all("pypi" not in row for row in report))
                        self.assertEqual(check.main(["--check-remote"]), 1)

    def test_truncated_body_exception_also_becomes_error_field(self):
        class IncompleteResponse(Response):
            def read(self, size):
                raise IncompleteRead(b"partial", 128)

        with patch.object(check, "_installed_version", return_value="2.0"):
            with patch.object(check, "build_opener",
                              return_value=Opener(IncompleteResponse(b""))):
                self.assertEqual(check.main(["--check-remote"]), 1)
                self.assertTrue(all("error" in result for result in
                                    check.version_report(check_remote=True)))

    def test_fail_closed_and_differences_have_no_upgrade_verdict(self):
        with patch.object(check, "_installed_version", return_value="2.0"):
            with patch.object(check, "_pypi_version", side_effect=["1.0", ValueError("bad")]):
                report = check.version_report(check_remote=True)
        self.assertEqual(report[0]["same_version"], False)
        self.assertNotIn("upgrade_available", report[0])
        self.assertNotIn("pypi", report[1])
        self.assertIn("error", report[1])
        with patch.object(check, "version_report", return_value=report):
            self.assertEqual(check.main(["--check-remote"]), 1)


if __name__ == "__main__":
    unittest.main()
