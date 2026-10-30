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
            (("yahoo", "yahoo"), ("yahoo",)),
            ((), ()),
        ):
            with self.subTest(selected=selected), self.assertRaises(e.EngineSetupError):
                e.render(self.original, selected, controlled)

    def test_live_inventory_accepts_current_all_category_engine_count(self):
        rows = [
            {"name": f"sample-{i}", "categories": ["general"], "enabled": False}
            for i in range(280)
        ] + [{"name": "yahoo", "categories": ["general"], "enabled": True},
             {"name": "google cse images", "categories": ["images"],
              "enabled": True}]
        conn = Mock()
        reply = Mock(status=200)
        reply.read.return_value = json.dumps({"engines": rows}).encode()
        conn.getresponse.return_value = reply
        with patch.object(e, "HTTPConnection", return_value=conn):
            result = e.live_engines()
        self.assertEqual(len(result), 282)
        self.assertEqual(result[0]["name"], "yahoo")
        self.assertIn("google cse images", {item["name"] for item in result})

    def test_live_inventory_includes_news_images_and_sorts_recommendations(self):
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
                         ["yahoo", "mojeek", "brave", "google images", "wikipedia"])
        self.assertEqual(data[3]["categories"], ("images",))
        conn.request.assert_called_once_with("GET", "/config")
        conn.close.assert_called_once()

    def test_parenthesized_engine_names_remain_visible_and_managed(self):
        # Parenthesized labels are valid SearXNG configuration names but were
        # previously discarded before the wizard's Google-exclusion choice.
        rows = [
            {"name": "yahoo", "categories": ["general"], "enabled": True},
            {"name": "naver (KO)", "categories": ["general"], "enabled": False},
            {"name": "google (images)", "categories": ["images"], "enabled": True},
        ]
        connection = Mock()
        connection.getresponse.return_value = Mock(
            status=200, read=Mock(return_value=json.dumps({"engines": rows}).encode()))
        with patch.object(e, "HTTPConnection", return_value=connection):
            inventory = e.live_engines()
        self.assertEqual({x["name"] for x in inventory},
                         {"yahoo", "naver (KO)", "google (images)"})
        self.assertEqual(inventory[0]["name"], "yahoo")

        # Even when choice 3 targets a different category from the selected
        # default search, Google is included in the exact managed set.
        target = Target("searxng", "a" * 64, True, "no")
        captured = []
        with (patch.object(e.on_demand_docker, "_safe_target", return_value=target),
              patch.object(e, "_check_bind_mount"),
              patch.object(e, "live_engines", return_value=inventory),
              patch.object(e, "apply", side_effect=lambda selected, controlled, **kwargs:
                           captured.append((selected, controlled)) or "applied"),
              patch.object(e.os, "geteuid", return_value=1000),
              patch.object(e.sys, "stdin", Mock(isatty=lambda: True)),
              patch("builtins.input", side_effect=["3", "APPLY"]),
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(e.main(), 0)
        self.assertEqual(captured, [((), ("google (images)",))])
        managed = e.render(self.original, *captured[0])
        self.assertIn('  - name: "google (images)"\n    disabled: true', managed)
        updated = e.render(managed, ("yahoo",), ("yahoo", "naver (KO)"))
        self.assertIn('  - name: "naver (KO)"\n    disabled: true', updated)
        self.assertIn('  - name: "google (images)"\n    disabled: true', updated)
        self.assertEqual(e.render(updated, ("yahoo",), ("yahoo",)), updated)

    def test_unsafe_google_named_engine_fails_closed_not_silently_skipped(self):
        # Skipping an unrepresentable Google-named row would make option 3
        # falsely report an exhaustive exclusion across all categories.
        for bad in (
            {"name": "google [images]", "enabled": True, "categories": ["images"]},
            {"name": "google\\images", "enabled": True, "categories": ["images"]},
            {"name": "google images", "enabled": True, "categories": ["images:unsafe"]},
            {"name": "google images", "enabled": True, "categories": []},
            {"name": "google images", "enabled": "true", "categories": ["images"]},
            {"name": "google images", "categories": ["images"]},
        ):
            with self.subTest(bad=bad):
                reply = Mock(status=200)
                reply.read.return_value = json.dumps({"engines": [
                    {"name": "yahoo", "categories": ["general"], "enabled": True},
                    bad,
                ]}).encode()
                connection = Mock()
                connection.getresponse.return_value = reply
                with patch.object(e, "HTTPConnection", return_value=connection):
                    with self.assertRaisesRegex(
                            e.EngineSetupError, "Cannot safely identify every Google engine"):
                        e.live_engines()
                self.assertEqual(self.file.read_text(), self.original)

    def test_unidentified_engine_records_reject_partial_inventory(self):
        # A missing or non-string engine name cannot safely be classified
        # as a non-Google engine for the global exclusion option.
        for bad in ({}, {"name": None}, {"name": 123}, "bad-row", []):
            with self.subTest(bad=bad):
                conn = Mock()
                conn.getresponse.return_value = Mock(
                    status=200,
                    read=Mock(return_value=json.dumps({"engines": [
                        {"name": "yahoo", "categories": ["general"], "enabled": True},
                        bad,
                    ]}).encode()))
                with patch.object(e, "HTTPConnection", return_value=conn):
                    with self.assertRaisesRegex(
                            e.EngineSetupError, "inventory incomplete"):
                        e.live_engines()
                self.assertEqual(self.file.read_text(), self.original)

    def test_global_google_inventory_stale_at_apply_does_not_write(self):
        target = Target("searxng", "a" * 64, True, "no")
        initial = [
            {"name": "yahoo", "enabled": True, "categories": ("general",)},
            {"name": "google images", "enabled": True, "categories": ("images",)},
        ]
        new_google = initial + [
            {"name": "google new", "enabled": True, "categories": ("images",)}
        ]
        with (patch.object(e.on_demand_docker, "_safe_target",
                           return_value=target),
              patch.object(e, "_check_bind_mount"),
              patch.object(e, "live_engines",
                           side_effect=[initial, new_google]) as read,
              patch.object(e, "_restart_verified",
                           side_effect=AssertionError("must not restart")) as restart,
              patch.object(e.os, "geteuid", return_value=1000),
              patch.object(e.sys, "stdin", Mock(isatty=lambda: True)),
              patch("builtins.input", side_effect=["3", "APPLY"]),
              contextlib.redirect_stdout(io.StringIO()),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(e.main(), 1)
        self.assertEqual(read.call_count, 2)
        restart.assert_not_called()
        self.assertEqual(self.file.read_text(), self.original)
        self.assertFalse(e._recovery_path(self.file).exists())
        self.assertIn("Google engine inventory changed", errors.getvalue())

    def test_global_google_inventory_changed_after_restart_rolls_back(self):
        target = Target("searxng", "a" * 64, True, "no")
        initial = [
            {"name": "yahoo", "enabled": True, "categories": ("general",)},
            {"name": "google images", "enabled": True, "categories": ("images",)},
        ]
        for after in (
            initial + [{"name": "google new", "enabled": True}],
            # Even with no new provider, a still-enabled Google result must
            # not be reported as an excluded engine.
            initial,
        ):
            with self.subTest(after=after):
                restarted = []
                with (patch.object(e.on_demand_docker, "_safe_target",
                                   return_value=target),
                      patch.object(e, "_check_bind_mount"),
                      patch.object(e, "live_engines", side_effect=[initial, after]),
                      patch.object(e, "_restart_verified",
                                   side_effect=lambda arg: restarted.append(arg)),
                      patch.object(e.time, "sleep",
                                   side_effect=AssertionError("should fail immediately"))):
                    with self.assertRaisesRegex(
                            e.EngineSetupError, "previous settings restored"):
                        e.apply((), ("google images",), exclude_google=True)
                self.assertEqual(restarted, [target, target])
                self.assertEqual(self.file.read_text(), self.original)
                self.assertFalse(e._recovery_path(self.file).exists())

    def test_global_google_inventory_verified_after_restart(self):
        target = Target("searxng", "a" * 64, True, "no")
        before = [
            {"name": "yahoo", "enabled": True},
            {"name": "google images", "enabled": True},
        ]
        after = [
            {"name": "yahoo", "enabled": True},
            {"name": "google images", "enabled": False},
        ]
        restarts = []
        with (patch.object(e.on_demand_docker, "_safe_target",
                           return_value=target),
              patch.object(e, "_check_bind_mount"),
              patch.object(e, "live_engines", side_effect=[before, after]),
              patch.object(e, "_restart_verified",
                           side_effect=lambda arg: restarts.append(arg))):
            result = e.apply((), ("google images",), exclude_google=True)
        self.assertIn("restarted successfully", result)
        self.assertEqual(restarts, [target])
        self.assertIn('  - name: "google images"\n    disabled: true',
                      self.file.read_text())
        self.assertFalse(e._recovery_path(self.file).exists())

    def test_managed_general_selection_survives_image_google_exclusion(self):
        initial = e.render(self.original, ("yahoo",), ("yahoo", "brave"))
        next_text = e.render(initial, (), ("google cse images", "google images"))
        self.assertIn('  - name: "yahoo"\n    disabled: false', next_text)
        self.assertIn('  - name: "brave"\n    disabled: true', next_text)
        self.assertIn('  - name: "google images"\n    disabled: true', next_text)
        self.assertIn('  - name: "google cse images"\n    disabled: true', next_text)
        self.assertEqual(next_text.count("engines:"), 1)
        self.assertEqual(e.render(next_text, (),
                                 ("google cse images", "google images")), next_text)
        third = e.render(next_text, ("duckduckgo news",), ("duckduckgo news",))
        self.assertIn('  - name: "yahoo"\n    disabled: false', third)
        self.assertIn('  - name: "google images"\n    disabled: true', third)
        self.assertIn('  - name: "duckduckgo news"\n    disabled: false', third)
        # Manual changes inside a previously managed section still fail closed.
        with self.assertRaises(e.EngineSetupError):
            e.render(next_text.replace("google images", "google pictures"),
                     ("yahoo",), ("yahoo",))

    def test_global_google_option_is_explicit_and_preserves_general_settings(self):
        fake = Target("searxng", "a" * 64, True, "no")
        captured = []
        inventory = [
            {"name": "yahoo", "enabled": True, "categories": ("general",)},
            {"name": "google cse images", "enabled": True, "categories": ("images",)},
            {"name": "google scholar", "enabled": True, "categories": ("science",)},
            {"name": "bing images", "enabled": True, "categories": ("images",)},
        ]
        with (patch.object(e.on_demand_docker, "_safe_target", return_value=fake),
              patch.object(e, "_check_bind_mount"),
              patch.object(e, "live_engines", return_value=inventory),
              patch.object(e, "apply", side_effect=lambda selected, controlled, **kwargs:
                           captured.append((selected, controlled)) or "applied"),
              patch.object(e.os, "geteuid", return_value=1000),
              patch.object(e.sys, "stdin", Mock(isatty=lambda: True)),
              patch("builtins.input", side_effect=["3", "APPLY"]),
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(e.main(), 0)
        self.assertEqual(captured, [((), ("google cse images", "google scholar"))])

    def test_category_choice_shows_shared_engines_and_changes_global_flag(self):
        fake = Target("searxng", "a" * 64, True, "no")
        captured = []
        inventory = [
            {"name": "yahoo", "enabled": True, "categories": ("general",)},
            {"name": "example multi", "enabled": True,
             "categories": ("general", "news")},
            {"name": "duckduckgo news", "enabled": False,
             "categories": ("news",)},
            {"name": "google images", "enabled": True, "categories": ("images",)},
        ]
        output = io.StringIO()
        with (patch.object(e.on_demand_docker, "_safe_target", return_value=fake),
              patch.object(e, "_check_bind_mount"),
              patch.object(e, "live_engines", return_value=inventory),
              patch.object(e, "apply", side_effect=lambda selected, controlled, **kwargs:
                           captured.append((selected, controlled)) or "applied"),
              patch.object(e.os, "geteuid", return_value=1000),
              patch.object(e.sys, "stdin", Mock(isatty=lambda: True)),
              patch("builtins.input", side_effect=["4", "3", "2", "APPLY"]),
              contextlib.redirect_stdout(output)):
            self.assertEqual(e.main(), 0)
        self.assertEqual(captured, [(("duckduckgo news",),
                                    ("example multi", "duckduckgo news"))])
        self.assertIn("general, news", output.getvalue())
        self.assertIn("globally", output.getvalue())

    def test_non_general_category_explicit_none_disables_its_engines(self):
        fake = Target("searxng", "a" * 64, True, "no")
        seen = []
        inventory = [
            {"name": "yahoo", "enabled": True, "categories": ("general",)},
            {"name": "bing images", "enabled": True, "categories": ("images",)},
            {"name": "google cse images", "enabled": True, "categories": ("images",)},
        ]
        with (patch.object(e.on_demand_docker, "_safe_target", return_value=fake),
              patch.object(e, "_check_bind_mount"),
              patch.object(e, "live_engines", return_value=inventory),
              patch.object(e, "apply", side_effect=lambda selected, controlled:
                           seen.append((selected, controlled)) or "applied"),
              patch.object(e.os, "geteuid", return_value=1000),
              patch.object(e.sys, "stdin", Mock(isatty=lambda: True)),
              patch("builtins.input", side_effect=["4", "2", "NONE", "APPLY"]),
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(e.main(), 0)
        self.assertEqual(seen, [((), ("bing images", "google cse images"))])

    def test_duplicate_or_invalid_live_engine_cannot_be_used(self):
        conn = Mock()
        reply = Mock(status=200)
        conn.getresponse.return_value = reply
        for data in (
            {"engines": [{"name": "yahoo", "categories": ["general"]},
                         {"name": "yahoo", "categories": ["images"]}]},
            {"engines": [{"name": "google images", "categories": ["images", 5]}]},
            {"engines": [{"name": "google images", "categories": []}]},
        ):
            reply.read.return_value = json.dumps(data).encode()
            with patch.object(e, "HTTPConnection", return_value=conn):
                with self.assertRaises(e.EngineSetupError):
                    e.live_engines()

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
                  {"name": "yahoo", "enabled": False, "categories": ("general",)},
                  {"name": "bing", "enabled": True, "categories": ("general",)},
                  {"name": "brave", "enabled": True, "categories": ("general",)}]),
              patch.object(e, "apply", side_effect=lambda selected, controlled, **kwargs:
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
        for inputs in (["0"], ["1", "CANCEL"], ["2", "1,2", "CANCEL"],
                       ["3", "CANCEL"], ["4", "2", "1", "CANCEL"]):
            with (self.subTest(inputs=inputs),
                patch.object(e.on_demand_docker, "_safe_target", return_value=fake),
                patch.object(e, "_check_bind_mount"),
                patch.object(e, "live_engines", return_value=[
                    {"name": "yahoo", "enabled": False, "categories": ("general",)},
                    {"name": "brave", "enabled": True, "categories": ("general",)},
                    {"name": "google images", "enabled": True,
                     "categories": ("images",)}]),
                patch("builtins.input", side_effect=inputs),
                patch.object(e, "apply", side_effect=AssertionError("cannot mutate")),
                patch.object(e.os, "geteuid", return_value=1000),
                patch.object(e.sys, "stdin", Mock(isatty=lambda: True)),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(e.main(), 0)
            self.assertEqual(self.file.read_text(), self.original)

    def test_sleeping_inventory_requires_separate_operator_wake_consent(self):
        stopped = Target("searxng", "a" * 64, False, "no")
        inventory = [
            {"name": "yahoo", "enabled": True, "categories": ("general",)},
            {"name": "google cse images", "enabled": True,
             "categories": ("images",)},
            {"name": "bing images", "enabled": True, "categories": ("images",)},
        ]
        for inputs, expected_wakes in (
            (["0"], 0),
            (["3", "NO"], 0),
            (["2", ""], 0),
            (["3", "WAKE", "CANCEL"], 1),
            (["3", "WAKE", "APPLY"], 1),
        ):
            with (self.subTest(inputs=inputs),
                  patch.object(e.on_demand_docker, "_safe_target",
                               side_effect=lambda _service: stopped),
                  patch.object(e, "_check_bind_mount"),
                  patch.object(e.on_demand_settings, "read",
                               return_value={"enabled": True, "idle_minutes": 30}),
                  patch.object(e, "_wake_for_engine_inventory",
                               return_value=inventory) as wake,
                  patch.object(e, "apply", return_value="applied") as applied,
                  patch.object(e.os, "geteuid", return_value=1000),
                  patch.object(e.sys, "stdin", Mock(isatty=lambda: True)),
                  patch("builtins.input", side_effect=inputs),
                  contextlib.redirect_stdout(io.StringIO()) as output):
                self.assertEqual(e.main(), 0)
            self.assertEqual(wake.call_count, expected_wakes)
            self.assertEqual(applied.call_count, int(inputs[-1] == "APPLY"))
            if expected_wakes:
                wake.assert_called_once_with(stopped)
                self.assertIn("may remain active", output.getvalue())
            self.assertEqual(self.file.read_text(), self.original)

    def test_sleeping_disabled_on_demand_and_running_invalid_inventory_fail_closed(self):
        for target, enabled in (
            (Target("searxng", "a" * 64, False, "no"), False),
            (Target("searxng", "a" * 64, True, "no"), True),
        ):
            with (self.subTest(running=target.running),
                  patch.object(e.on_demand_docker, "_safe_target",
                               return_value=target),
                  patch.object(e, "_check_bind_mount"),
                  patch.object(e.on_demand_settings, "read",
                               return_value={"enabled": enabled, "idle_minutes": 30}),
                  patch.object(e, "live_engines",
                               side_effect=e.EngineSetupError("private secret")),
                  patch.object(e, "_wake_for_engine_inventory",
                               side_effect=AssertionError("no Docker wake")) as wake,
                  patch.object(e, "apply",
                               side_effect=AssertionError("no mutation")),
                  patch.object(e.os, "geteuid", return_value=1000),
                  patch.object(e.sys, "stdin", Mock(isatty=lambda: True)),
                  patch("builtins.input", side_effect=["3"]),
                  contextlib.redirect_stdout(io.StringIO()),
                  contextlib.redirect_stderr(io.StringIO()) as err):
                self.assertEqual(e.main(), 1)
            wake.assert_not_called()
            self.assertEqual(self.file.read_text(), self.original)
            self.assertNotIn("private secret", err.getvalue())
            self.assertIn(
                "inventory is unavailable" if target.running
                else "on-demand mode is disabled", err.getvalue())

    def test_verified_wake_rechecks_target_ownership_and_never_writes_settings(self):
        stopped = Target("searxng", "a" * 64, False, "no")
        running = Target("searxng", "a" * 64, True, "no")
        response = [{"name": "yahoo", "categories": ("general",),
                     "enabled": True}]
        with (patch.object(e.on_demand_settings, "read",
                           return_value={"enabled": True, "idle_minutes": 30}),
              patch.object(e.on_demand_docker, "_safe_target",
                           return_value=stopped),
              patch.object(e, "_check_bind_mount"),
              patch.object(e.on_demand_docker, "start",
                           return_value=True) as start,
              patch.object(e, "live_engines", return_value=response) as read,
              patch.object(e, "searxng_ok", return_value=True)):
            self.assertEqual(e._wake_for_engine_inventory(stopped), response)
            start.assert_called_once()
            self.assertEqual(start.call_args.args, ("searxng",))
            self.assertEqual(start.call_args.kwargs["timeout"], 30)
            self.assertTrue(start.call_args.kwargs["health"]("searxng"))
            read.assert_called_once()
        self.assertEqual(self.file.read_text(), self.original)

        for actual in (Target("searxng", "b" * 64, False, "no"),
                       Target("searxng", "a" * 64, False, "unless-stopped")):
            with (self.subTest(actual=actual),
                  patch.object(e.on_demand_settings, "read",
                               return_value={"enabled": True}),
                  patch.object(e.on_demand_docker, "_safe_target",
                               return_value=actual),
                  patch.object(e.on_demand_docker, "start",
                               side_effect=AssertionError("no wake")) as start):
                with self.assertRaisesRegex(e.EngineSetupError,
                                            "identity or restart policy"):
                    e._wake_for_engine_inventory(stopped)
                start.assert_not_called()

        with (patch.object(e.on_demand_settings, "read",
                           return_value={"enabled": False}),
              patch.object(e.on_demand_docker, "_safe_target",
                           side_effect=AssertionError("no inspection")),
              patch.object(e.on_demand_docker, "start",
                           side_effect=AssertionError("no wake"))):
            with self.assertRaisesRegex(e.EngineSetupError, "disabled"):
                e._wake_for_engine_inventory(stopped)

    def test_wake_failure_does_not_disclose_docker_error_or_change_settings(self):
        stopped = Target("searxng", "a" * 64, False, "no")
        with (patch.object(e.on_demand_settings, "read",
                           return_value={"enabled": True}),
              patch.object(e.on_demand_docker, "_safe_target",
                           return_value=stopped),
              patch.object(e, "_check_bind_mount"),
              patch.object(e.on_demand_docker, "start",
                           side_effect=e.on_demand_docker.LifecycleError(
                               "PRIVATE DOCKER STDERR"))):
            with self.assertRaisesRegex(e.EngineSetupError,
                                        "wake failed") as error:
                e._wake_for_engine_inventory(stopped)
        self.assertNotIn("PRIVATE", str(error.exception))
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
