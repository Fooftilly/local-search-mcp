"""Safety and usability tests for operator-only SearXNG engine selection."""
import contextlib
import io
import json
import os
import subprocess
import sys
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
        # mkdir(parents=True) creates intermediate parents under the umask,
        # often 0755. The real on-demand cross-process lock correctly rejects
        # that; the isolated test must model the actual private 0700 directory.
        self.folder.parent.chmod(0o700)
        self.folder.chmod(0o700)
        self.file = self.folder / "searxng-settings.yml"
        self.original = "use_default_settings: true\nserver:\n  secret_key: REDACTED\n"
        self.file.write_text(self.original)
        self.file.chmod(0o600)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": self.temp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_direct_script_invocation_loads_modules_without_pythonpath(self):
        source = Path(__file__).resolve().parents[1] / "scripts" / "searxng_engines.py"
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        result = subprocess.run([sys.executable, str(source)],
                                env=env, capture_output=True, text=True,
                                timeout=10)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("Interactive normal-user terminal required", result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)

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
            self.original + '"engines":\n  - name: custom\n',
            self.original + "'engines':\n  - name: custom\n",
            self.original + "engines :\n  - name: custom\n",
            self.original + '"engines" :\n  - name: custom\n',
            self.original + "---\n",
        ):
            with self.subTest(original=original), self.assertRaises(e.EngineSetupError):
                e.render(original, ("yahoo",), ("yahoo",))
        managed = e.render(self.original, ("yahoo",), ("yahoo",))
        with self.assertRaises(e.EngineSetupError):
            e.render(managed.replace("disabled: false", "disabled: false # changed"),
                     ("yahoo",), ("yahoo",))
        with self.assertRaises(e.EngineSetupError):
            e.render(managed.replace("disabled: false", "disabled: true"),
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

    def test_stale_preview_does_not_rollback_someone_elses_edit(self):
        fake = Target("searxng", "a"*64, False, "no")
        orig_save = e._save
        def concurrent_change(path, body, *, expected):
            if expected is not None:
                path.write_text(self.original + "# another editor added this\n")
            return orig_save(path, body, expected=expected)
        with (patch.object(e.on_demand_docker, "_safe_target", return_value=fake),
              patch.object(e, "_check_bind_mount"),
              patch.object(e, "_save", side_effect=concurrent_change)):
            with self.assertRaisesRegex(e.EngineSetupError, "no changes made"):
                e.apply(("yahoo",), ("yahoo", "brave"))
        self.assertEqual(self.file.read_text(),
                         self.original + "# another editor added this\n")
        self.assertFalse(e._recovery_path(self.file).exists())

    def test_recommended_preset_controls_all_live_general_engines(self):
        fake = Target("searxng", "a"*64, True, "no")
        captured = []
        with (patch.object(e.on_demand_docker, "_safe_target", return_value=fake),
              patch.object(e, "_check_bind_mount"),
              patch.object(e, "live_engines", return_value=[
                  {"name": "yahoo", "enabled": False},
                  {"name": "bing", "enabled": True},
                  {"name": "brave", "enabled": True}]),
              patch.object(e, "apply", side_effect=lambda selected, controlled:
                           captured.append((selected, controlled)) or "applied"),
              patch.object(e.os, "geteuid", return_value=1000),
              patch.object(e.sys, "stdin", Mock(isatty=lambda: True)),
              patch("builtins.input", side_effect=["1", "APPLY"]),
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(e.main(), 0)
        self.assertEqual(captured, [(("yahoo",), ("yahoo", "bing", "brave"))])

    def test_operator_sees_safe_recovery_diagnostic_not_secret_exceptions(self):
        with (patch.object(e.on_demand_docker, "_safe_target",
                           side_effect=e.EngineSetupError(
                               "Original settings restored; SearXNG restart requires operator attention")),
              patch.object(e.os, "geteuid", return_value=1000),
              patch.object(e.sys, "stdin", Mock(isatty=lambda: True)),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(e.main(), 1)
        self.assertIn("SearXNG restart requires operator attention", errors.getvalue())

        with (patch.object(e.on_demand_docker, "_safe_target",
                           side_effect=OSError("SECRET_PRIVATE_CONFIG")),
              patch.object(e.os, "geteuid", return_value=1000),
              patch.object(e.sys, "stdin", Mock(isatty=lambda: True)),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(e.main(), 1)
        self.assertNotIn("SECRET_PRIVATE_CONFIG", errors.getvalue())
        self.assertIn("OSError", errors.getvalue())

    def test_unconfirmed_menu_has_no_writes_restart_or_docker_wake(self):
        fake = Target("searxng", "a"*64, True, "no")
        for inputs in (["0"], ["1", "CANCEL"], ["2", "1,2", "CANCEL"]):
            with (self.subTest(inputs=inputs),
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

    def test_bind_mount_in_place_update_keeps_inode(self):
        inode = self.file.stat().st_ino
        e._save(self.file, "use_default_settings: true\nengines: []\n",
                expected=self.original)
        self.assertEqual(self.file.stat().st_ino, inode)
        self.assertIn("engines: []", self.file.read_text())
        with self.assertRaises(e.EngineSetupError):
            e._save(self.file, "no", expected=self.original)

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

    def test_partial_write_failure_restores_in_place_from_checkpoint(self):
        fake = Target("searxng", "a"*64, False, "no")
        original_writer = e._save
        calls = []
        def broken_writer(path, body, *, expected):
            calls.append(expected)
            if len(calls) == 1:
                # Simulate a short/incomplete write to the *mounted* inode.
                with path.open("r+") as f:
                    f.write("use_default_settings: tr")
                    f.truncate()
                raise OSError("simulated write failure")
            return original_writer(path, body, expected=expected)
        with (patch.object(e.on_demand_docker, "_safe_target", return_value=fake),
              patch.object(e, "_check_bind_mount"),
              patch.object(e, "_save", side_effect=broken_writer)):
            with self.assertRaisesRegex(e.EngineSetupError, "previous settings restored"):
                e.apply(("yahoo",), ("yahoo", "brave"))
        self.assertEqual(self.file.read_text(), self.original)
        self.assertFalse(e._recovery_path(self.file).exists())
        self.assertEqual(calls, [self.original, None])

    def test_new_manual_edit_after_write_is_not_overwritten_during_rollback(self):
        fake = Target("searxng", "a"*64, True, "no")
        def changed_restart(target):
            self.file.write_text(self.file.read_text() + "# newer operator change\n")
            raise e.on_demand_docker.LifecycleError("Docker became unavailable")
        with (patch.object(e.on_demand_docker, "_safe_target", return_value=fake),
              patch.object(e, "_check_bind_mount"),
              patch.object(e, "_restart_verified", side_effect=changed_restart)):
            with self.assertRaisesRegex(e.EngineSetupError, "edit kept"):
                e.apply(("yahoo",), ("yahoo", "brave"))
        self.assertIn("# newer operator change", self.file.read_text())
        self.assertTrue(e._recovery_path(self.file).is_file())
        self.assertEqual(e._recovery_path(self.file).read_text(), self.original)

    def test_malformed_http_reply_from_engine_inventory_is_controlled(self):
        conn = Mock()
        conn.getresponse.side_effect = e.HTTPException("untrusted SECRET token")
        with patch.object(e, "HTTPConnection", return_value=conn):
            with self.assertRaisesRegex(e.EngineSetupError, "Cannot read SearXNG"):
                e.live_engines()
        conn.close.assert_called_once()

    def test_docker_inspection_failure_restores_settings(self):
        fake = Target("searxng", "a"*64, True, "no")
        seen = []
        def bad_restart(target):
            seen.append(target)
            if len(seen) == 1:
                raise e.on_demand_docker.LifecycleError("Docker unavailable")
        with (patch.object(e.on_demand_docker, "_safe_target", return_value=fake),
              patch.object(e, "_check_bind_mount"),
              patch.object(e, "_restart_verified", side_effect=bad_restart)):
            with self.assertRaisesRegex(e.EngineSetupError, "previous settings restored"):
                e.apply(("yahoo",), ("yahoo", "brave"))
        self.assertEqual(self.file.read_text(), self.original)
        self.assertFalse(e._recovery_path(self.file).exists())
        self.assertEqual(seen, [fake, fake])

    def test_keep_current_settings_clears_only_explicit_recovery_copy(self):
        fake = Target("searxng", "a"*64, False, "no")
        e._checkpoint(self.file, self.original)
        self.file.write_text(self.original + "# manually repaired\n")
        with (patch.object(e.on_demand_docker, "_safe_target", return_value=fake),
              patch.object(e, "_check_bind_mount"),
              patch.object(e.os, "geteuid", return_value=1000),
              patch.object(e.sys, "stdin", Mock(isatty=lambda: True)),
              patch("builtins.input", side_effect=["KEEP"]),
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(e.main(), 0)
        self.assertIn("# manually repaired", self.file.read_text())
        self.assertFalse(e._recovery_path(self.file).exists())

    def test_delayed_keep_cannot_delete_replaced_checkpoint(self):
        fake = Target("searxng", "a"*64, False, "no")
        checkpoint = e._checkpoint(self.file, self.original)
        def swapped_checkpoint(_prompt):
            # Simulate another session clearing the first recovery copy and
            # starting a new edit under the same checkpoint path.
            checkpoint.unlink()
            e._checkpoint(self.file, "NEWER SETTINGS")
            return "KEEP"
        with (patch.object(e.on_demand_docker, "_safe_target", return_value=fake),
              patch.object(e, "_check_bind_mount"),
              patch.object(e.os, "geteuid", return_value=1000),
              patch.object(e.sys, "stdin", Mock(isatty=lambda: True)),
              patch("builtins.input", side_effect=swapped_checkpoint),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(e.main(), 1)
        self.assertIn("changed since confirmation", errors.getvalue())
        self.assertTrue(checkpoint.is_file())
        self.assertEqual(checkpoint.read_text(), "NEWER SETTINGS")
        self.assertEqual(self.file.read_text(), self.original)

    def test_delayed_restore_cannot_use_newer_checkpoint(self):
        fake = Target("searxng", "a"*64, False, "no")
        checkpoint = e._checkpoint(self.file, self.original)
        def swapped_checkpoint(_prompt):
            checkpoint.unlink()
            e._checkpoint(self.file, "NEWER SETTINGS")
            return "RESTORE"
        with (patch.object(e.on_demand_docker, "_safe_target", return_value=fake),
              patch.object(e, "_check_bind_mount"),
              patch.object(e.os, "geteuid", return_value=1000),
              patch.object(e.sys, "stdin", Mock(isatty=lambda: True)),
              patch("builtins.input", side_effect=swapped_checkpoint),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(e.main(), 1)
        self.assertIn("changed since confirmation", errors.getvalue())
        self.assertEqual(checkpoint.read_text(), "NEWER SETTINGS")
        self.assertEqual(self.file.read_text(), self.original)

    def test_interrupted_recovery_copy_can_be_restored_via_setup(self):
        fake = Target("searxng", "a"*64, False, "no")
        e._checkpoint(self.file, self.original)
        self.file.write_text("broken: [")
        with (patch.object(e.on_demand_docker, "_safe_target", return_value=fake),
              patch.object(e, "_check_bind_mount")):
            result = e.recover_interrupted()
        self.assertIn("Previous settings restored", result)
        self.assertEqual(self.file.read_text(), self.original)
        self.assertFalse(e._recovery_path(self.file).exists())

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
