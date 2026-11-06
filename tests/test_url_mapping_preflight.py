"""Offline no-network, no-secret contract checks for URL mapping preflight."""
from __future__ import annotations

import contextlib
import io
import json
import os
from unittest import TestCase
from unittest.mock import patch

from scripts import url_mapping_preflight as preflight


class FakeHTTPResponse:
    def __init__(self, status=200, body=b'{}'):
        self.status = status
        self.body = body

    def read(self, amount):
        return self.body[:amount]


class FakeHTTPConnection:
    instances = []

    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.requests = []
        self.closed = False
        self.response = FakeHTTPResponse(body=json.dumps({"paths": {"/crawl": {"post": {}},
                                                                   "/map": {"post": {}}}}).encode())
        self.instances.append(self)

    def request(self, method, path, headers):
        self.requests.append((method, path, headers))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class URLMappingPreflightTests(TestCase):
    def setUp(self):
        FakeHTTPConnection.instances.clear()

    def test_default_uses_only_existing_health_check(self):
        with (patch.object(preflight, "get_json", return_value={"status": "ok", "version": "0.9.5"}) as health,
              patch.object(preflight, "HTTPConnection", side_effect=AssertionError("no route request"))):
            output = preflight.report()
        health.assert_called_once_with(11235, "/health")
        self.assertEqual(output["health"], "supported_and_healthy")
        self.assertEqual(output["route_status"], "not_inspected")
        self.assertEqual(output["external_requests"], 0)
        self.assertEqual(output["approvals_modified"], False)

    def test_unsupported_version_stops_before_schema(self):
        with (patch.object(preflight, "get_json", return_value={"status": "ok", "version": "0.9.4"}),
              patch.dict(os.environ, {"CRAWL4AI_API_TOKEN": "SECRET"}),
              patch.object(preflight, "HTTPConnection", side_effect=AssertionError("no schema request"))):
            output = preflight.report(inspect_routes=True)
        self.assertEqual(output["health"], "unverified_or_unsupported")
        self.assertIsNone(output["installed_version"])

    def test_missing_operator_token_fails_closed(self):
        with (patch.object(preflight, "get_json", return_value={"status": "ok", "version": "0.9.5"}),
              patch.dict(os.environ, {"CRAWL4AI_API_TOKEN": ""}),
              patch.object(preflight, "HTTPConnection", side_effect=AssertionError("no request"))):
            output = preflight.report(inspect_routes=True)
        self.assertEqual(output["route_status"], "operator_token_required")

    def test_auth_only_to_fixed_loopback_schema_and_not_reported(self):
        with (patch.object(preflight, "get_json", return_value={"status": "ok", "version": "0.9.5"}),
              patch.dict(os.environ, {"CRAWL4AI_API_TOKEN": "SENSITIVE-BEARER"}),
              patch.object(preflight, "HTTPConnection", FakeHTTPConnection)):
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                code = preflight.main(["--inspect-routes"])
        self.assertEqual(code, 0)
        conn = FakeHTTPConnection.instances[0]
        self.assertEqual((conn.host, conn.port, conn.timeout), ("127.0.0.1", 11235, 3))
        self.assertEqual(conn.requests[0][0:2], ("GET", "/openapi.json"))
        self.assertEqual(conn.requests[0][2]["Authorization"], "Bearer SENSITIVE-BEARER")
        self.assertTrue(conn.closed)
        result = json.loads(buf.getvalue())
        self.assertEqual(result["route_status"], "candidates_require_review")
        self.assertEqual(result["mapping_route_candidates"], [{"path": "/map", "methods": ["POST"]}])
        self.assertNotIn("SENSITIVE", buf.getvalue())

    def test_no_route_is_not_claimed_as_proven_absence(self):
        info = preflight._route_candidates({"paths": {"/crawl": {"post": {}}, "/schema": {"get": {}}}})
        self.assertEqual(info["route_status"], "no_advertised_mapping_route")
        self.assertEqual(info["advertised_path_count"], 2)

    def test_unknown_malformed_and_large_inventory_fail_closed(self):
        for value in (None, {}, {"paths": []}, {"paths": {"//bad": {"get": {}}}},
                      {"paths": {"/a\nb": {"post": {}}}},
                      {"paths": {f"/x/{i}": {} for i in range(257)}}):
            with self.subTest(data=str(value)[:80]), self.assertRaises(preflight.SchemaUnavailable):
                preflight._route_candidates(value)
        self.assertEqual(preflight._route_candidates({"paths": {"/crawl/{url}": {"post": {}}}})["mapping_route_candidates"], [])

    def test_response_errors_never_leak_body_or_token(self):
        for response in (FakeHTTPResponse(status=302, body=b'SENSITIVE_LOCATION'),
                         FakeHTTPResponse(status=401, body=b'SENSITIVE_TOKEN'),
                         FakeHTTPResponse(status=200, body=b'X' * (preflight.MAX_SCHEMA_BYTES + 1)),
                         FakeHTTPResponse(status=200, body=b'{broken')):
            class ErrorConnection(FakeHTTPConnection):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    self.response = response
            with (self.subTest(code=response.status),
                  patch.object(preflight, "get_json", return_value={"status": "ok", "version": "0.9.5"}),
                  patch.dict(os.environ, {"CRAWL4AI_API_TOKEN": "SENSITIVE_TOKEN"}),
                  patch.object(preflight, "HTTPConnection", ErrorConnection)):
                with contextlib.redirect_stdout(io.StringIO()) as buf:
                    exit_code = preflight.main(["--inspect-routes"])
                self.assertEqual(exit_code, 1)
                self.assertIn("schema_unavailable_or_invalid", buf.getvalue())
                self.assertNotIn("SENSITIVE", buf.getvalue())

    def test_schema_query_does_not_run_on_failed_health(self):
        with (patch.object(preflight, "get_json", side_effect=OSError("SENSITIVE_ERROR")),
              patch.object(preflight, "HTTPConnection", side_effect=AssertionError("unexpected")),
              contextlib.redirect_stdout(io.StringIO()) as buf):
            code = preflight.main(["--inspect-routes"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(buf.getvalue())["health"], "unavailable")
        self.assertNotIn("SENSITIVE_ERROR", buf.getvalue())
