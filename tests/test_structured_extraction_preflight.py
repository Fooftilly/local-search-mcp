"""No-network regression tests for fixed Crawl4AI extraction config preflight."""
from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from scripts import structured_extraction_preflight as preflight


class FakeResponse:
    def __init__(self, status=200, body=b'{}'):
        self.status = status
        self.body = body

    def read(self, amount):
        return self.body[:amount]


def _upstream_serialized_plain(value):
    """Mimic v0.9.5 to_serializable_dict's nested plain dict wrapping."""
    if type(value) is dict:
        return {"type": "dict",
                "value": {key: _upstream_serialized_plain(child)
                          for key, child in value.items()}}
    if type(value) is list:
        return [_upstream_serialized_plain(item) for item in value]
    return value


class FakeConnection:
    instances = []
    response_override = None

    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.requests = []
        self.closed = False
        self.response = FakeResponse()
        type(self).instances.append(self)

    def request(self, method, path, *, body, headers):
        self.requests.append((method, path, body, headers))
        if self.response_override is not None:
            self.response = self.response_override
        else:
            # v0.9.5 serializes every plain dict inside the schema as
            # {"type":"dict","value":...}, including field dictionaries.
            returned = json.loads(body)
            strategy = returned["params"]["extraction_strategy"]
            strategy["params"]["schema"] = _upstream_serialized_plain(
                strategy["params"]["schema"])
            self.response = FakeResponse(body=json.dumps(returned).encode())

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class StructuredPreflightTests(unittest.TestCase):
    def setUp(self):
        FakeConnection.instances.clear()
        FakeConnection.response_override = None

    def test_default_only_checks_existing_health(self):
        with (patch.object(preflight, "get_json", return_value={"status": "ok", "version": "0.9.5"}),
              patch.object(preflight, "HTTPConnection", side_effect=AssertionError("no config POST"))):
            r = preflight.report()
        self.assertEqual(r["config_status"], "not_probed")
        self.assertEqual(r["health"], "supported_and_healthy")
        self.assertEqual(r["public_crawls"], 0)
        self.assertFalse(r["approvals_modified"])

    def test_both_declarative_types_echo_with_fixed_local_payloads(self):
        with (patch.object(preflight, "get_json", return_value={"status": "ok", "version": "0.9.5"}),
              patch.dict(os.environ, {"CRAWL4AI_API_TOKEN": "PRIVATE_TOKEN"}),
              patch.object(preflight, "HTTPConnection", FakeConnection),
              contextlib.redirect_stdout(io.StringIO()) as stdout):
            rc = preflight.main(["--probe-configs"])
        self.assertEqual(rc, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["config_status"], "both_config_echoes_verified")
        self.assertEqual(len(FakeConnection.instances), 2)
        self.assertNotIn("PRIVATE_TOKEN", stdout.getvalue())
        self.assertEqual(report["public_crawls"], 0)
        self.assertEqual(report["external_requests"], 0)
        self.assertFalse(report["approvals_modified"])
        for connection, strategy in zip(FakeConnection.instances, preflight.STRATEGIES):
            self.assertEqual((connection.host, connection.port, connection.timeout),
                             ("127.0.0.1", 11235, 4))
            self.assertTrue(connection.closed)
            (method, path, body, headers), = connection.requests
            self.assertEqual((method, path), ("POST", "/config/dump"))
            self.assertEqual(headers["Authorization"], "Bearer PRIVATE_TOKEN")
            payload = json.loads(body)
            self.assertEqual(payload["type"], "CrawlerRunConfig")
            self.assertEqual(payload["params"]["extraction_strategy"]["type"], strategy)
            self.assertEqual(len(payload["params"]["extraction_strategy"]["params"]["schema"]["fields"]), 1)
            self.assertNotIn("url", payload)
            self.assertNotIn("js_code", body.decode().lower())
            self.assertNotIn("regex", body.decode().lower())

    def test_unhealthy_and_unsupported_backend_never_receive_probe(self):
        for health, expected in (({"status": "ok", "version": "0.9.4"}, "unsupported_or_unverified"),
                                 ({"status": "broken"}, "unsupported_or_unverified")):
            with (self.subTest(health=health),
                  patch.object(preflight, "get_json", return_value=health),
                  patch.dict(os.environ, {"CRAWL4AI_API_TOKEN": "SECRET"}),
                  patch.object(preflight, "HTTPConnection", side_effect=AssertionError("request"))):
                r = preflight.report(probe_configs=True)
            self.assertEqual(r["health"], expected)
            self.assertEqual(r["strategy_results"], {})

    def test_missing_token_never_requests_configs(self):
        with (patch.object(preflight, "get_json", return_value={"status": "ok", "version": "0.9.5"}),
              patch.dict(os.environ, {"CRAWL4AI_API_TOKEN": ""}),
              patch.object(preflight, "HTTPConnection", side_effect=AssertionError("request"))):
            r = preflight.report(probe_configs=True)
        self.assertEqual(r["config_status"], "operator_token_required")

    def test_non_200_redirect_does_not_follow_or_disclose_response(self):
        for status in (302, 401, 403, 500):
            FakeConnection.response_override = FakeResponse(status=status, body=b'SENSITIVE_BODY')
            with (self.subTest(status=status),
                  patch.object(preflight, "get_json", return_value={"status": "ok", "version": "0.9.5"}),
                  patch.dict(os.environ, {"CRAWL4AI_API_TOKEN": "SENSITIVE_BEARER"}),
                  patch.object(preflight, "HTTPConnection", FakeConnection),
                  contextlib.redirect_stdout(io.StringIO()) as stdout):
                rc = preflight.main(["--probe-configs"])
            self.assertEqual(rc, 1)
            self.assertEqual(json.loads(stdout.getvalue())["config_status"], "inconclusive")
            self.assertNotIn("SENSITIVE", stdout.getvalue())
            self.assertTrue(all(c.closed for c in FakeConnection.instances))

    def test_oversized_invalid_or_mismatched_echo_fails_closed(self):
        invalid = [
            FakeResponse(body=b'X' * (preflight.MAX_RESPONSE_BYTES + 1)),
            FakeResponse(body=b'{broken'),
            FakeResponse(body=b'{"type":"CrawlerRunConfig","params":{}}'),
            FakeResponse(body=b'{"type":"CrawlerRunConfig","params":{"extraction_strategy":{"type":"LLMExtractionStrategy"}}}'),
            FakeResponse(body=b'[]'),
        ]
        for response in invalid:
            FakeConnection.response_override = response
            with (self.subTest(response=response.body[:25]),
                  patch.object(preflight, "get_json", return_value={"status": "ok", "version": "0.9.5"}),
                  patch.dict(os.environ, {"CRAWL4AI_API_TOKEN": "HIDDEN"}),
                  patch.object(preflight, "HTTPConnection", FakeConnection)):
                r = preflight.report(probe_configs=True)
            self.assertEqual(r["config_status"], "inconclusive")
            self.assertEqual(len(r["strategy_results"]), 2)

    def test_invalid_credential_and_strategy_inputs_are_rejected(self):
        for token in ("", "abc\nDEF", "\u20ac", "x" * 4097):
            with self.subTest(token=token[:10]), self.assertRaises(preflight.ProbeUnavailable):
                preflight._probe(token, "JsonCssExtractionStrategy")
        with self.assertRaises(ValueError):
            preflight._fixed_payload("LLMExtractionStrategy")

    def test_upstream_wrapped_and_legacy_plain_schema_echo(self):
        for strategy in preflight.STRATEGIES:
            expected = preflight._fixed_payload(strategy)
            result = json.loads(json.dumps(expected))
            result["params"]["extraction_strategy"]["params"]["schema"] = (
                _upstream_serialized_plain(
                    expected["params"]["extraction_strategy"]["params"]["schema"]))
            with self.subTest(strategy=strategy):
                self.assertTrue(preflight._echo_supported(result, strategy, expected))
                self.assertTrue(preflight._echo_supported(expected, strategy, expected))
                wrapped = result["params"]["extraction_strategy"]["params"]["schema"]
                self.assertEqual(wrapped["type"], "dict")
                self.assertEqual(
                    wrapped["value"]["fields"][0]["type"], "dict")
                # A malicious typed object or modified nested field must never
                # be considered a successful schema echo.
                import copy
                corrupt = copy.deepcopy(result)
                corrupt["params"]["extraction_strategy"]["params"]["schema"][
                    "value"]["fields"][0]["value"]["selector"] = "script"
                self.assertFalse(preflight._echo_supported(corrupt, strategy, expected))
                injected = copy.deepcopy(result)
                injected["params"]["extraction_strategy"]["params"]["schema"][
                    "value"]["unapproved"] = "extra"
                self.assertFalse(preflight._echo_supported(injected, strategy, expected))
                fake_class = copy.deepcopy(result)
                fake_class["params"]["extraction_strategy"]["params"]["schema"][
                    "type"] = "LLMExtractionStrategy"
                self.assertFalse(preflight._echo_supported(fake_class, strategy, expected))
                fake_strategy = copy.deepcopy(result)
                fake_strategy["params"]["extraction_strategy"]["type"] = "RegexExtractionStrategy"
                self.assertFalse(preflight._echo_supported(fake_strategy, strategy, expected))

    def test_echo_requires_same_typed_schema(self):
        payload = preflight._fixed_payload("JsonCssExtractionStrategy")
        self.assertTrue(preflight._echo_supported(payload, "JsonCssExtractionStrategy", payload))
        for item in ({}, [], {"type": "CrawlerRunConfig", "params": {}},
                     {"type": "CrawlerRunConfig", "params": {"extraction_strategy":
                         {"type": "JsonCssExtractionStrategy", "params": {"schema": {}}}}}):
            with self.subTest(item=item):
                self.assertFalse(preflight._echo_supported(item, "JsonCssExtractionStrategy", payload))


if __name__ == "__main__":
    unittest.main()
