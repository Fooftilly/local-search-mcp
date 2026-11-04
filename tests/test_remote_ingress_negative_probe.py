"""Offline, bounded negative-path probe acceptance for optional ingress."""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest.mock import patch

import httpx

from scripts import remote_ingress_negative_probe as probe


class RemoteIngressNegativeProbeTests(unittest.TestCase):
    def setUp(self):
        self.seen = []

    def responder(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        # All probes must go to the two fixed loopback HTTP endpoints.
        self.assertIn(request.url.port, (18790, 18791))
        self.assertEqual(request.url.host, "127.0.0.1")
        self.assertEqual(request.method, "GET")
        self.assertNotIn("authorization", request.headers)
        self.assertNotIn("cookie", request.headers)
        host = request.headers.get("host")
        if request.url.path != "/mcp" or request.url.query:
            code = 404
        elif host == "forbidden.invalid" or "origin" in request.headers:
            code = 403
        else:
            code = 401
        return httpx.Response(code, text="SECRET_PAYLOAD_MUST_NOT_SURFACE")

    def test_both_listeners_deny_without_secrets_or_external_network(self):
        with patch.object(probe, "read_private_hostname",
                          return_value="private.example.org"):
            result = probe.local_negative_probe(
                transport=httpx.MockTransport(self.responder))
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["requests_sent"], 8)
        self.assertTrue(result["origin_configured"])
        self.assertTrue(result["negative_path_only"])
        self.assertFalse(result["remote_access_verified"])
        self.assertFalse(result["positive_auth_verified"])
        self.assertEqual(len(self.seen), 8)
        self.assertEqual({row["status"] for row in result["checks"]},
                         {"passed"})
        output = json.dumps(result)
        for secret in ("SECRET_PAYLOAD_MUST_NOT_SURFACE",
                       "private.example.org", "127.0.0.1", "forbidden.invalid",
                       "/mcp", "Authorization"):
            self.assertNotIn(secret, output)

    def test_missing_origin_configuration_is_not_success(self):
        with patch.object(probe, "read_private_hostname",
                          side_effect=probe.StartupRefused("SECRET_LOCAL_PATH")):
            result = probe.local_negative_probe(
                transport=httpx.MockTransport(self.responder))
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["requests_sent"], 4)
        self.assertFalse(result["origin_configured"])
        self.assertEqual(result["checks"][-1], {
            "target": "origin", "check": "local_configuration",
            "status": "not_configured"})
        self.assertNotIn("SECRET_LOCAL_PATH", json.dumps(result))

    def test_unexpected_allow_is_a_failure_not_positive_acceptance(self):
        def wrongly_open(request):
            self.seen.append(request)
            return httpx.Response(200, content=b"SECRET")

        with patch.object(probe, "read_private_hostname",
                          return_value="private.example.org"):
            result = probe.local_negative_probe(
                transport=httpx.MockTransport(wrongly_open))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["requests_sent"], 8)
        self.assertTrue(all(row["status"] == "unexpected_status"
                            for row in result["checks"]))
        self.assertNotIn("SECRET", json.dumps(result))

    def test_connection_error_does_not_count_as_rejection(self):
        def disconnected(request):
            raise httpx.ConnectError("SECRET_SOCKET_DETAILS", request=request)

        with patch.object(probe, "read_private_hostname",
                          return_value="private.example.org"):
            result = probe.local_negative_probe(
                transport=httpx.MockTransport(disconnected))
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["requests_sent"], 8)
        self.assertTrue(all(row["status"] == "unavailable"
                            for row in result["checks"]))
        self.assertNotIn("SECRET_SOCKET_DETAILS", json.dumps(result))

    def test_http_redirect_does_not_follow(self):
        def redirect(request):
            return httpx.Response(
                302, headers={"location": "https://should-not-be-visited.invalid"})

        with patch.object(probe, "read_private_hostname",
                          return_value="private.example.org"):
            result = probe.local_negative_probe(
                transport=httpx.MockTransport(redirect))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["requests_sent"], 8)

    def test_cli_emits_only_sanitized_report_and_does_not_serve(self):
        expected = {
            "schema": probe.SCHEMA, "mode": "read-only", "status": "incomplete",
            "negative_path_only": True, "remote_access_verified": False,
            "positive_auth_verified": False, "requests_sent": 0,
            "origin_configured": False, "checks": [],
        }
        output = io.StringIO()
        with (patch.object(probe, "local_negative_probe", return_value=expected),
              contextlib.redirect_stdout(output)):
            self.assertEqual(probe.main([]), 1)
        self.assertEqual(json.loads(output.getvalue()), expected)


if __name__ == "__main__":
    unittest.main()
