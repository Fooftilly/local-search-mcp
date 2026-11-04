"""Offline, bounded negative-path probe acceptance for optional ingress."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import httpx

from scripts import remote_ingress_negative_probe as probe
from scripts import setup_wizard as wizard


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


class SetupWizardNegativeProbeTests(unittest.TestCase):
    def test_wizard_command_uses_project_python_and_sanitized_output(self):
        rows = [
            {"target": "gateway", "check": name, "status": "passed"}
            for name in ("missing_bearer", "foreign_host", "foreign_origin",
                         "unexpected_query")
        ] + [
            {"target": "origin", "check": "local_configuration",
             "status": "not_configured"},
        ]
        child = {
            "schema": probe.SCHEMA, "mode": "read-only",
            "status": "incomplete", "negative_path_only": True,
            "remote_access_verified": False, "positive_auth_verified": False,
            "origin_configured": False, "requests_sent": 4, "checks": rows,
            "extra_path": "/SECRET_PATH_MUST_NOT_ESCAPE",
        }
        completed = subprocess.CompletedProcess([], 1, stdout=json.dumps(child),
                                                 stderr="SECRET_STDERR")
        with (patch.object(wizard.os, "geteuid", return_value=1000),
              patch.object(wizard, "project_python",
                           return_value=Path("/fake/project/.venv/bin/python")),
              patch.object(wizard.subprocess, "run", return_value=completed)
              as runner):
            report = wizard.cloudflare_portal_negative_probe()
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["checks"], rows)
        self.assertNotIn("SECRET", json.dumps(report))
        args = runner.call_args.args[0]
        self.assertEqual(args[1:], ["-m", "scripts.remote_ingress_negative_probe"])
        self.assertTrue(runner.call_args.kwargs["capture_output"])
        self.assertFalse(runner.call_args.kwargs["check"])

    def test_wizard_rejects_malformed_or_excess_child_report(self):
        cases = [
            subprocess.CompletedProcess([], 0, stdout='{"status": "passed"}'),
            subprocess.CompletedProcess([], 0, stdout="SECRET_RAW_TEXT"),
            subprocess.CompletedProcess([], 0, stdout=json.dumps({
                "schema": probe.SCHEMA, "mode": "read-only",
                "negative_path_only": True, "remote_access_verified": False,
                "positive_auth_verified": False, "status": "passed",
                "origin_configured": True, "requests_sent": 8,
                "checks": [{"target": "origin", "check": "host_url",
                            "status": "passed"}],
            })),
        ]
        for completed in cases:
            with (self.subTest(returncode=completed.returncode,
                               output=completed.stdout[:10]),
                  patch.object(wizard.os, "geteuid", return_value=1000),
                  patch.object(wizard, "project_python",
                               return_value=Path("/fake/.venv/bin/python")),
                  patch.object(wizard.subprocess, "run", return_value=completed)):
                result = wizard.cloudflare_portal_negative_probe()
            self.assertEqual(result["status"], "unavailable")
            self.assertNotIn("SECRET", json.dumps(result))

    def test_wizard_rejects_partial_probe_claiming_full_pass(self):
        wrong = {
            "schema": probe.SCHEMA, "mode": "read-only",
            "negative_path_only": True, "remote_access_verified": False,
            "positive_auth_verified": False, "status": "passed",
            "origin_configured": True, "requests_sent": 8,
            "checks": [
                {"target": "gateway", "check": "missing_bearer",
                 "status": "passed"},
            ],
        }
        proc = subprocess.CompletedProcess([], 0, stdout=json.dumps(wrong))
        with (patch.object(wizard.os, "geteuid", return_value=1000),
              patch.object(wizard, "project_python",
                           return_value=Path("/fake/.venv/bin/python")),
              patch.object(wizard.subprocess, "run", return_value=proc)):
            result = wizard.cloudflare_portal_negative_probe()
        self.assertEqual(result["status"], "unavailable")

    def test_wizard_probe_does_not_fallback_to_system_python(self):
        with (patch.object(wizard.os, "geteuid", return_value=1000),
              patch.object(wizard, "project_python", return_value=None),
              patch.object(wizard.subprocess, "run",
                           side_effect=AssertionError("must not execute"))):
            report = wizard.cloudflare_portal_negative_probe()
        self.assertEqual(report["status"], "unavailable")

    def test_setup_flag_is_noninteractive_and_preserves_failure_exit(self):
        with patch.object(wizard, "cloudflare_portal_negative_probe",
                          return_value={"status": "incomplete", "checks": []}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = wizard.main(["--cloudflare-portal-probe"])
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out.getvalue())["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
