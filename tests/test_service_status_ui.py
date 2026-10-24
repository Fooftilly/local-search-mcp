"""Read-only service status, localhost-only probes and origin protections."""
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from unittest.mock import patch

import admin_ui
import service_status


class FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class StatusTests(unittest.TestCase):
    def test_default_services_probe_only_local_ports_and_hide_secrets(self):
        calls = []
        def connect(target, timeout):
            calls.append((target, timeout))
            return FakeConnection()
        with patch.dict(os.environ, {"SEARXNG_URL": "http://127.0.0.1:8888",
                "CRAWL4AI_URL": "http://127.0.0.1:11235"}, clear=True):
            report = service_status.probe_services(connector=connect)
        self.assertEqual(len(report), 2)
        self.assertEqual([x["status"] for x in report], ["TCP reachable"] * 2)
        self.assertEqual(calls, [(("127.0.0.1", 8888), 0.3),
                                 (("127.0.0.1", 11235), 0.3)])
        self.assertNotIn("8888", repr(report))
        self.assertNotIn("11235", repr(report))

    def test_localhost_is_numeric_loopback_and_explicit_zero_port_is_invalid(self):
        calls = []
        def connect(target, timeout):
            calls.append((target, timeout))
            return FakeConnection()
        with patch.dict(os.environ, {
            "SEARXNG_URL": "http://localhost:8888",
            "CRAWL4AI_URL": "http://[::1]:11235",
        }, clear=True):
            report = service_status.probe_services(connector=connect)
        self.assertTrue(all(x["status"] == "TCP reachable" for x in report))
        self.assertEqual(calls, [(("127.0.0.1", 8888), 0.3),
                                 (("::1", 11235), 0.3)])
        calls.clear()
        with patch.dict(os.environ, {
            "SEARXNG_URL": "http://localhost:0",
            "CRAWL4AI_URL": "https://127.0.0.1:0",
        }, clear=True):
            report = service_status.probe_services(connector=connect)
        self.assertFalse(calls)
        self.assertTrue(all(x["status"] == "configuration invalid"
                            for x in report))

    def test_remote_and_credentials_never_get_probed_or_disclosed(self):
        calls = []
        with patch.dict(os.environ, {
            "SEARXNG_URL": "https://user:SECRET@evil.example:443",
            "CRAWL4AI_URL": "http://10.0.0.1:11235"}, clear=True):
            report = service_status.probe_services(
                connector=lambda *a, **k: calls.append(a))
        self.assertFalse(calls)
        self.assertTrue(all(x["status"] == "configuration invalid" for x in report))
        self.assertNotIn("SECRET", repr(report))
        self.assertNotIn("evil.example", repr(report))
        self.assertNotIn("10.0.0.1", repr(report))

    def test_explicit_zero_port_is_invalid_without_any_tcp_probe(self):
        calls = []
        for url in ("http://127.0.0.1:0", "https://localhost:0",
                    "http://[::1]:0"):
            with self.subTest(url=url):
                self.assertIsNone(service_status._endpoint(url))
                with patch.dict(os.environ, {
                    "SEARXNG_URL": url, "CRAWL4AI_URL": url}, clear=True):
                    report = service_status.probe_services(
                        connector=lambda *args, **kwargs: calls.append(args))
                self.assertEqual([r["status"] for r in report],
                                 ["configuration invalid"] * 2)
        self.assertEqual(calls, [])

    def test_missing_port_uses_scheme_default_without_hiding_zero(self):
        self.assertEqual(service_status._endpoint("http://127.0.0.1"),
                         ("127.0.0.1", 80))
        self.assertEqual(service_status._endpoint("https://localhost"),
                         ("127.0.0.1", 443))

    def test_down_service_is_not_reported_healthy(self):
        with patch.dict(os.environ, clear=True):
            report = service_status.probe_services(
                connector=lambda *a, **k: (_ for _ in ()).throw(
                    ConnectionRefusedError("refused")))
        self.assertTrue(all(x["status"] == "TCP unavailable" for x in report))

    def test_unavailable_tcp_can_be_reported_as_expected_sleeping_without_wake(self):
        sleeping = {"state": "sleeping", "armed": False,
                    "active_requests": 0, "stop_claimed": False}
        active = {"state": "running", "armed": True,
                  "active_requests": 0, "stop_claimed": False}
        scenarios = (
            ({"mode": "on-demand", "enabled": True, "backends": {
                "searxng": sleeping, "crawl4ai": active}},
             ["TCP unavailable (supervisor reports sleeping)", "TCP unavailable"]),
            ({"mode": "on-demand", "enabled": True, "backends": {
                "searxng": sleeping, "crawl4ai": sleeping}},
             ["TCP unavailable (supervisor reports sleeping)"] * 2),
            ({"mode": "on-demand", "enabled": False, "backends": {
                "searxng": sleeping, "crawl4ai": sleeping}},
             ["TCP unavailable"] * 2),
            ({"mode": "on-demand", "enabled": True, "backends": None},
             ["TCP unavailable"] * 2),
            ({"mode": "on-demand", "enabled": True, "backends": {
                "searxng": {**sleeping, "active_requests": 1},
                "crawl4ai": {**sleeping, "stop_claimed": True}}},
             ["TCP unavailable"] * 2),
        )
        for activity, expected in scenarios:
            with (self.subTest(expected=expected),
                  patch.dict(os.environ, {}, clear=True),
                  patch.object(service_status, "_supervisor_activity",
                               return_value=activity) as snapshot):
                result = service_status.probe_services(
                    connector=lambda *a, **k: (_ for _ in ()).throw(
                        ConnectionRefusedError("private error")))
                self.assertEqual([row["status"] for row in result], expected)
                snapshot.assert_called_once_with()
                self.assertNotIn("private error", repr(result))

    def test_custom_loopback_ports_are_not_mislabelled_as_supervisor_sleep(self):
        asleep = {"state": "sleeping", "armed": False,
                  "active_requests": 0, "stop_claimed": False}
        active_mode = {"mode": "on-demand", "enabled": True, "backends": {
            "searxng": asleep, "crawl4ai": asleep}}
        with (patch.dict(os.environ, {
                "SEARXNG_URL": "http://127.0.0.1:9000",
                "CRAWL4AI_URL": "http://localhost:9111"}, clear=True),
              patch.object(service_status, "_supervisor_activity",
                           return_value=active_mode)):
            result = service_status.probe_services(
                connector=lambda *a, **k: (_ for _ in ()).throw(ConnectionRefusedError()))
        self.assertEqual([item["status"] for item in result], ["TCP unavailable"] * 2)

        # An explicit environment URL for the same managed port is still
        # the actual supervised TCP listener and may report sleeping.
        with (patch.dict(os.environ, {
                "SEARXNG_URL": "http://localhost:8888",
                "CRAWL4AI_URL": "http://127.0.0.1:11235"}, clear=True),
              patch.object(service_status, "_supervisor_activity",
                           return_value=active_mode)):
            result = service_status.probe_services(
                connector=lambda *a, **k: (_ for _ in ()).throw(ConnectionRefusedError()))
        self.assertEqual([item["status"] for item in result],
                         ["TCP unavailable (supervisor reports sleeping)"] * 2)

    def test_reachable_or_invalid_backend_never_needs_supervisor_state(self):
        with (patch.dict(os.environ, {}, clear=True),
              patch.object(service_status, "_supervisor_activity",
                           side_effect=AssertionError("No optional status access"))):
            result = service_status.probe_services(connector=lambda *a, **k: FakeConnection())
        self.assertEqual([row["status"] for row in result], ["TCP reachable"] * 2)
        with (patch.dict(os.environ, {
                "SEARXNG_URL": "https://outside.invalid/SECRET",
                "CRAWL4AI_URL": "http://127.0.0.1:0"}, clear=True),
              patch.object(service_status, "_supervisor_activity",
                           side_effect=AssertionError("Unsafe config must not query state"))):
            result = service_status.probe_services(
                connector=lambda *a, **k: self.fail("Invalid endpoint probed"))
        self.assertEqual([row["status"] for row in result], ["configuration invalid"] * 2)

    def test_reported_sleep_state_is_not_trusted_without_verified_snapshot(self):
        sleeping = {"state": "sleeping", "armed": False,
                    "active_requests": 0, "stop_claimed": False}
        for candidate in (
            {"mode": "on-demand", "enabled": True, "backends": {"searxng": sleeping}},
            {"mode": "on-demand", "enabled": True, "backends": {
                "searxng": {**sleeping, "armed": True}, "crawl4ai": sleeping}},
            {"mode": "on-demand", "enabled": True, "backends": {
                "searxng": {**sleeping, "active_requests": True}, "crawl4ai": sleeping}},
            {"mode": "on-demand", "enabled": True, "backends": {
                "searxng": {**sleeping, "state": "failed"}, "crawl4ai": sleeping}},
            {"mode": "unavailable", "enabled": True, "backends": {
                "searxng": sleeping, "crawl4ai": sleeping}},
            None,
        ):
            with self.subTest(candidate=candidate):
                self.assertFalse(service_status._expected_sleeping(candidate, "searxng"))

    def test_port_report_only_numeric_local_ports(self):
        with patch.dict(os.environ, {
            "SEARXNG_URL": "https://localhost:17777/private?token=SECRET",
            "CRAWL4AI_URL": "http://[::1]:11235/",
        }, clear=True):
            result = service_status.configured_ports()
        self.assertEqual([item["port"] for item in result], [17777, 11235])
        self.assertTrue(all(item["valid"] for item in result))
        self.assertTrue(all(item["source"] == "environment" for item in result))
        self.assertNotIn("SECRET", repr(result))
        self.assertNotIn("/private", repr(result))
        self.assertTrue(all("restart" in item["apply"] for item in result))

    def test_invalid_remote_or_secret_endpoint_is_not_shown(self):
        with patch.dict(os.environ, {
            "SEARXNG_URL": "https://user:SECRET@outside.example:9999",
            "CRAWL4AI_URL": "http://10.0.0.8:11235",
        }, clear=True):
            report = service_status.configured_ports()
            page = admin_ui.status_page()
        self.assertEqual([x["port"] for x in report], [None, None])
        self.assertFalse(any(x["valid"] for x in report))
        self.assertNotIn("SECRET", repr(report) + page)
        self.assertNotIn("outside.example", repr(report) + page)
        self.assertNotIn("10.0.0.8", repr(report) + page)
        self.assertIn("Invalid (check local configuration)", page)

    def test_default_backend_ports_require_no_dashboard_write(self):
        with patch.dict(os.environ, {}, clear=True):
            report = service_status.configured_ports()
        self.assertEqual([x["port"] for x in report], [8888, 11235])
        self.assertEqual([x["source"] for x in report], ["default", "default"])

    def test_status_page_warns_about_restart_and_monitor_port(self):
        with (patch.dict(os.environ, {}, clear=True),
              patch.object(service_status, "probe_services", return_value=[])):
            page = admin_ui.status_page()
        self.assertIn("Backend port configuration (read-only)", page)
        self.assertIn("8790", page)
        self.assertIn("8791", page)
        self.assertIn("8888 (SearXNG)", page)
        self.assertIn("11235 (Crawl4AI)", page)
        self.assertIn("backend or dashboard ports", page)
        self.assertIn("degraded", page)
        self.assertIn("restart", page.lower())
        self.assertIn("<td>8888</td>", page)
        self.assertIn("<td>11235</td>", page)

    def test_page_escapes_even_mocked_untrusted_status(self):
        with patch.object(service_status, "probe_services",
                          return_value=[{"service": "<script>", "status": "down",
                                         "source": "test"}]):
            page = admin_ui.status_page()
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>", page)

    def test_status_endpoint_stays_read_only_and_same_origin(self):
        server = admin_ui.ThreadingHTTPServer(("127.0.0.1", 0), admin_ui.Handler)
        server.daemon_threads = True
        self.addCleanup(server.server_close)
        port = server.server_address[1]
        patch_port = patch.object(admin_ui, "PORT", port)
        patch_port.start()
        self.addCleanup(patch_port.stop)
        runner = threading.Thread(target=server.serve_forever, daemon=True)
        runner.start()
        self.addCleanup(server.shutdown)
        def request(path, headers=None):
            conn = HTTPConnection("127.0.0.1", port, timeout=4)
            try:
                conn.request("GET", path, headers=headers or {})
                resp = conn.getresponse()
                return resp.status, resp.read().decode("utf-8")
            finally:
                conn.close()
        with patch.object(service_status, "probe_services",
                          return_value=[{"service": "SearXNG",
                                         "status": "TCP reachable",
                                         "source": "default"}]):
            status, body = request("/status")
            self.assertEqual(status, 200)
            self.assertIn("Read-only TCP availability", body)
            self.assertIn("SearXNG", body)
            self.assertEqual(request("/status", {
                "Sec-Fetch-Site": "cross-site"})[0], 403)
            self.assertEqual(request("/status", {"Host": "evil.invalid"})[0], 403)
            self.assertEqual(request("/status", {"Origin": "http://evil.invalid"})[0], 403)
        self.assertEqual(request("/status/actions/restart")[0], 404)


if __name__ == "__main__":
    unittest.main()
