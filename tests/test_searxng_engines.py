"""Safety and usability tests for operator-only SearXNG engine selection."""
import contextlib
from http.client import HTTPConnection
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from scripts import searxng_engines as e
from scripts.on_demand_docker import Target


class EngineChooserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "local-search-mcp" / "crawler-compose"
        self.folder.mkdir(parents=True, mode=0o700)
        self.folder.chmod(0o700)
        self.file = self.folder / "searxng-settings.yml"
        self.original = "use_default_settings: true\nserver:\n  secret_key: REDACTED\n"
        self.file.write_text(self.original)
        self.file.chmod(0o600)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": self.temp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_render_default_preset_only_overrides_named_engines(self):
        choice = e.render(self.original, ("yahoo",),
                          ("yahoo", "brave", "duckduckgo", "google cse", "wikipedia"))
        self.assertIn("secret_key: REDACTED", choice)
        self.assertIn('  - name: "yahoo"\n    disabled: false', choice)
        self.assertIn('  - name: "brave"\n    disabled: true', choice)
        self.assertEqual(choice.count("engines:"), 1)
        # Repeated configuration replaces only our block.
        second = e.render(choice, ("yahoo", "duckduckgo"),
                          ("yahoo", "duckduckgo", "brave"))
        self.assertIn('  - name: "duckduckgo"\n    disabled: false', second)
        self.assertEqual(second.count("engines:"), 1)
        self.assertEqual(second.count(e.START), 1)
        self.assertIn("secret_key: REDACTED", second)

    def test_refuse_unmanaged_or_edited_engine_yaml(self):
        for original in (
            "use_default_settings: false\n",
            self.original + "engines:\n  - name: custom\n    disabled: false\n",
            self.original + "---\n",
        ):
            with self.subTest(original=original), self.assertRaises(e.EngineSetupError):
                e.render(original, ("yahoo",), ("yahoo",))
        managed = e.render(self.original, ("yahoo",), ("yahoo",))
        with self.assertRaises(e.EngineSetupError):
            e.render(managed.replace("disabled: false", "disabled: false # changed"),
                     ("yahoo",), ("yahoo",))
        with self.assertRaises(e.EngineSetupError):
            e.render(managed + "engines:\n  - name: changed\n",
                     ("yahoo",), ("yahoo",))

    def test_reject_invalid_selected_engine_and_injection(self):
        for selected, controlled in (
            (("yahoo",), ("brave",)),
            (("evil:\n  secret: foo",), ("evil:\n  secret: foo",)),
            (("yahoo",), ("yahoo", "yahoo")),
            ((), ("yahoo",)),
        ):
            with self.subTest(selected=selected), self.assertRaises(e.EngineSetupError):
                e.render(self.original, selected, controlled)

    def test_live_inventory_filters_non_general_and_sorts_recommendations(self):
        response = {"engines": [
            {"name": "brave", "categories": ["general"], "enabled": True},
            {"name": "wikipedia", "categories": ["general"], "enabled": True},
            {"name": "yahoo", "categories": ["general"], "enabled": False},
            {"name": "mojeek", "categories": ["general"], "enabled": False},
            {"name": "google images", "categories": ["images"], "enabled": True},
        ]}
        conn = Mock()
        reply = Mock()
        reply.status = 200
        reply.read.return_value = json.dumps(response).encode()
        conn.getresponse.return_value = reply
        with patch.object(e, "HTTPConnection", return_value=conn):
            data = e.live_engines()
        self.assertEqual([x["name"] for x in data],
                         ["yahoo", "mojeek", "brave", "wikipedia"])
        conn.request.assert_called_once_with("GET", "/config")
        conn.close.assert_called_once()

    def test_invalid_inventory_cannot_become_yaml(self):
        data = {"engines": [
            {"name": "yahoo\nmalicious: true", "categories": ["general"], "enabled": True}
        ]}
        conn = Mock()
        conn.getresponse.return_value = Mock(status=200)
        conn.getresponse.return_value.read.return_value = json.dumps(data).encode()
        with patch.object(e, "HTTPConnection", return_value=conn):
            with self.assertRaises(e.EngineSetupError):
                e.live_engines()

    def test_unconfirmed_menu_has_no_writes_restart_or_docker_wake(self):
        fake = Target("searxng", "a"*64, True, "no")
        for inputs in (["0"], ["1", "CANCEL"], ["2", "1,2", "CANCEL"]):
            with self.subTest(inputs=inputs), (
                patch.object(e.on_demand_docker, "_safe_target", return_value=fake),
                patch.object(e, "_check_bind_mount"),
                patch.object(e, "live_engines", return_value=[
                    {"name": "yahoo", "enabled": False},
                    {"name": "brave", "enabled": True}]),
                patch("builtins.input", side_effect=inputs),
                patch.object(e, "apply", side_effect=AssertionError("cannot mutate")),
                patch.object(e.os, "geteuid", return_value=1000),
                patch.object(e.sys, "stdin", Mock(isatty=lambda: True)),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(e.main(), 0)
            self.assertEqual(self.file.read_text(), self.original)

    def test_sleeping_backend_saves_without_starting_or_restarting(self):
        fake = Target("searxng", "a"*64, False, "no")
        with (patch.object(e.on_demand_docker, "_safe_target", return_value=fake),
              patch.object(e, "_check_bind_mount"),
              patch.object(e, "_restart_verified",
                           side_effect=AssertionError("sleeping backend must not restart"))):
            answer = e.apply(("yahoo",), ("yahoo", "brave"))
        self.assertIn("sleeping", answer)
        saved = self.file.read_text()
        self.assertIn('  - name: "yahoo"', saved)
        self.assertIn("secret_key: REDACTED", saved)

    def test_running_backend_only_restarts_verified_target_and_checks_config(self):
        fake = Target("searxng", "a"*64, True, "no")
        calls = []
        with (patch.object(e.on_demand_docker, "_safe_target", return_value=fake),
              patch.object(e, "_check_bind_mount"),
              patch.object(e, "_restart_verified", side_effect=lambda target: calls.append(target)),
              patch.object(e, "live_engines", return_value=[
                  {"name": "yahoo", "enabled": True},
                  {"name": "brave", "enabled": False}])):
            out = e.apply(("yahoo",), ("yahoo", "brave"))
        self.assertIn("verified", out)
        self.assertEqual(calls, [fake])

    def test_failed_restarted_health_restores_original_settings(self):
        fake = Target("searxng", "a"*64, True, "no")
        calls = []
        with (patch.object(e.on_demand_docker, "_safe_target", return_value=fake),
              patch.object(e, "_check_bind_mount"),
              patch.object(e, "_restart_verified", side_effect=lambda target: calls.append(target)),
              patch.object(e, "live_engines", return_value=[
                  {"name": "yahoo", "enabled": False},
                  {"name": "brave", "enabled": True}]),
              patch.object(e.time, "sleep")):
            with self.assertRaisesRegex(e.EngineSetupError, "previous settings restored"):
                e.apply(("yahoo",), ("yahoo", "brave"))
        self.assertEqual(self.file.read_text(), self.original)
        self.assertEqual(calls, [fake, fake])


if __name__ == "__main__":
    unittest.main()
