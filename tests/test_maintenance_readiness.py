"""Read-only host maintenance gate; all systemd interactions are faked."""
from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
import io
import json
import shutil
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import maintenance_readiness as gate


class MaintenanceGateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.units = self.home / "systemd" / "user"
        self.units.mkdir(parents=True)
        for name in gate.UNITS:
            path = self.units / name
            path.write_text(gate.stop_stack.MARKER + "\n[Unit]\nDescription=Managed\n")
            path.chmod(0o644)
        self.uid = patch.object(gate.os, "geteuid", return_value=1000)
        self.uid.start()
        self.addCleanup(self.uid.stop)
        self.preference = patch.object(gate.on_demand_settings, "read",
                                       return_value={"enabled": False, "idle_minutes": 30})
        self.preference.start()
        self.addCleanup(self.preference.stop)
        self.states = {}
        self.calls = []

    def state(self, name):
        path = self.units / name
        result = {
            "LoadState": "loaded", "ActiveState": "inactive",
            "UnitFileState": "disabled",
            "FragmentPath": str(path), "DropInPaths": "", "NeedDaemonReload": "no",
        }
        result.update(self.states.get(name, {}))
        return result

    def inspect(self, *, candidate=None):
        with patch.object(gate, "_unit_state", side_effect=self.state) as state:
            if candidate is None:
                report = gate.inspect(config_home=self.home)
            else:
                with patch.object(gate.rollback_candidate, "inspect",
                                  return_value={"ready_for_live_use": False}):
                    report = gate.inspect(config_home=self.home, candidate=candidate)
            self.assertEqual(state.call_count, len(gate.UNITS))
        return report

    def test_quiescent_managed_install_is_read_only(self):
        report = self.inspect(candidate=self.home / "candidate")
        self.assertTrue(report["ready_for_offline_maintenance"])
        self.assertFalse(report["live_rollback_authorized"])
        self.assertFalse(report["services_changed"])
        self.assertEqual(report["managed_units"], len(gate.UNITS))
        self.assertEqual(report["checked_units"], len(gate.UNITS))
        self.assertTrue(report["candidate_integrity_checked"])
        self.assertEqual(report["blocked"], [])

    def test_symlink_foreign_missing_and_overbroad_units_fail_closed(self):
        name = gate.UNITS[0]
        path = self.units / name
        path.unlink()
        path.symlink_to(self.units / gate.UNITS[1])
        self.assertIn("unverified", "\n".join(self.inspect()["blocked"]))
        path.unlink()
        path.write_text("[Unit]\nDescription=Foreign\n")
        self.assertIn("unverified", "\n".join(self.inspect()["blocked"]))
        path.write_text(gate.stop_stack.MARKER + "\n[Unit]\n")
        path.chmod(0o666)
        self.assertIn("unverified", "\n".join(self.inspect()["blocked"]))
        path.unlink()
        self.states[name] = {"LoadState": "not-found",
                             "FragmentPath": "", "UnitFileState": ""}
        report = self.inspect()
        self.assertTrue(report["ready_for_offline_maintenance"])
        self.states[name]["ActiveState"] = "active"
        self.assertFalse(self.inspect()["ready_for_offline_maintenance"])
        self.states[name]["ActiveState"] = "inactive"
        self.states[name]["LoadState"] = "loaded"
        self.assertIn("unmanaged", "\n".join(self.inspect()["blocked"]))

    def test_active_services_restart_triggers_dropins_and_foreign_fragments_block(self):
        cases = [
            ("local-search-mcp-bridge.service", "ActiveState", "active"),
            ("local-search-mcp-tunnel.service", "ActiveState", "activating"),
            ("local-search-mcp-update.timer", "UnitFileState", "enabled"),
            ("local-search-mcp-update.path", "ActiveState", "active"),
            ("local-search-mcp-tunnel.service", "DropInPaths", "/tmp/secret.conf"),
            ("local-search-mcp-bridge.service", "FragmentPath", "/etc/systemd/user/foreign"),
            ("local-search-mcp-tunnel.service", "NeedDaemonReload", "yes"),
        ]
        for name, key, value in cases:
            with self.subTest(name=name, key=key):
                self.states[name] = {key: value}
                report = self.inspect()
                self.assertFalse(report["ready_for_offline_maintenance"])
                self.assertNotIn("secret.conf", json.dumps(report))
                self.states.clear()

    def test_exact_repository_guard_dropin_is_accepted_but_foreign_is_not(self):
        guard = self.units / "local-search-mcp-tunnel.service.d" / "10-crawler-egress.conf"
        guard.parent.mkdir()
        source = Path(gate.__file__).resolve().parents[1] / "config" / "systemd" / "guards" / guard.name
        shutil.copyfile(source, guard)
        guard.chmod(0o644)
        unit = "local-search-mcp-tunnel.service"
        self.states[unit] = {"DropInPaths": str(guard)}
        self.assertTrue(self.inspect()["ready_for_offline_maintenance"])
        guard.write_text("[Service]\nExecStart=/bin/false\n")
        self.assertIn("unreviewed systemd drop-in", "\n".join(self.inspect()["blocked"]))
        shutil.copyfile(source, guard)
        guard.chmod(0o666)
        self.assertIn("unreviewed systemd drop-in", "\n".join(self.inspect()["blocked"]))
        guard.chmod(0o644)
        self.states[unit]["DropInPaths"] = str(guard) + " /etc/systemd/foreign.conf"
        self.assertFalse(self.inspect()["ready_for_offline_maintenance"])

    def test_failed_oneshot_is_stopped_but_activation_remains_blocked(self):
        self.states["local-search-mcp-update.service"] = {
            "ActiveState": "failed", "UnitFileState": "static"}
        self.assertTrue(self.inspect()["ready_for_offline_maintenance"])
        self.states["local-search-mcp-update.timer"] = {"UnitFileState": "enabled"}
        self.assertFalse(self.inspect()["ready_for_offline_maintenance"])

    def test_fifo_at_unit_path_fails_without_waiting_for_writer(self):
        name = gate.UNITS[0]
        path = self.units / name
        path.unlink()
        os.mkfifo(path, 0o600)
        self.assertIn("unverified", "\n".join(self.inspect()["blocked"]))

    def test_enabled_on_demand_or_failed_candidate_never_passes(self):
        with patch.object(gate.on_demand_settings, "read",
                          return_value={"enabled": True, "idle_minutes": 30}):
            report = self.inspect()
        self.assertIn("on-demand wake remains enabled", report["blocked"])
        with (patch.object(gate, "_unit_state", side_effect=self.state),
              patch.object(gate.rollback_candidate, "inspect",
                           side_effect=ValueError("PRIVATE_SECRET_AND_ARCHIVE_PATH"))):
            report = gate.inspect(config_home=self.home, candidate=self.home / "secret")
        self.assertFalse(report["ready_for_offline_maintenance"])
        self.assertNotIn("PRIVATE_SECRET", str(report))
        self.assertNotIn(str(self.home), str(report))

    def test_effective_systemd_query_is_strict_and_never_mutating(self):
        def run(argv, **kwargs):
            self.calls.append(tuple(argv))
            self.assertTrue(kwargs["capture_output"])
            self.assertFalse(kwargs["check"])
            self.assertEqual(kwargs["timeout"], 15)
            self.assertEqual(argv[:3], ["systemctl", "--user", "show"])
            values = self.state(argv[3])
            output = "".join(f"{k}={v}\n" for k, v in values.items())
            return subprocess.CompletedProcess(argv, 0, stdout=output.encode(),
                                               stderr=b"PRIVATE_SECRET")
        with patch.object(gate.subprocess, "run", side_effect=run):
            result = gate.inspect(config_home=self.home)
        self.assertTrue(result["ready_for_offline_maintenance"])
        self.assertEqual(len(self.calls), len(gate.UNITS))
        self.assertFalse(any(any(x in args for x in ("stop", "disable", "start", "restart"))
                             for args in self.calls))

    def test_ambiguous_systemd_output_blocks_without_leaking(self):
        for payload in (b"ActiveState=inactive\n", b"LoadState=loaded\n" * 9000,
                        b"LoadState=loaded\nActiveState=inactive\nExtra=bogus\n",
                        b"LoadState=\xff\n"):
            with self.subTest(payload=payload[:30]), patch.object(
                    gate.subprocess, "run",
                    return_value=subprocess.CompletedProcess(["systemctl"], 0,
                                                              stdout=payload, stderr=b"SECRET")):
                self.assertFalse(gate.inspect(config_home=self.home)[
                    "ready_for_offline_maintenance"])

    def test_cli_failure_is_redacted_and_nonzero(self):
        with (patch.object(gate, "inspect",
                           return_value={"ready_for_offline_maintenance": False,
                                         "blocked": ["bridge: active"]}),
              redirect_stdout(io.StringIO()) as stdout,
              redirect_stderr(io.StringIO()) as stderr):
            self.assertEqual(gate.main([]), 1)
        self.assertIn('"ready_for_offline_maintenance": false', stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        with (patch.object(gate, "inspect",
                           side_effect=ValueError("PRIVATE_TUNNEL_TOKEN")),
              redirect_stdout(io.StringIO()) as stdout,
              redirect_stderr(io.StringIO()) as stderr):
            self.assertEqual(gate.main([]), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("PRIVATE_TUNNEL_TOKEN", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
