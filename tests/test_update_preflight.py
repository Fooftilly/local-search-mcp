"""Offline tests for safe local update inspection in the setup wizard."""
from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from scripts import restart_on_update, update_preflight


class LocalUpdatePreflightTests(unittest.TestCase):
    def test_local_change_inspection_is_reused_without_mutation(self):
        message = ("Local revision differs from saved baseline (0123456789ab -> "
                   "abcdef012345). 1 runtime file(s) changed.")
        with (patch.object(restart_on_update, "inspect_status",
                           return_value=message) as read,
              patch.object(restart_on_update, "apply",
                           side_effect=AssertionError("must not apply")),
              patch.object(restart_on_update, "write_state",
                           side_effect=AssertionError("must not update baseline")),
              patch.object(restart_on_update, "reset_baseline",
                           side_effect=AssertionError("must not reset baseline")),
              patch.object(restart_on_update, "restart",
                           side_effect=AssertionError("must not restart"))):
            result = update_preflight.report()
        read.assert_called_once_with()
        self.assertEqual(result["state"], "inspected")
        self.assertEqual(result["local_revision"], message)
        self.assertFalse(result["remote_checked"])
        self.assertFalse(result["updates_applied"])

    def test_missing_baseline_is_reported_as_local_status(self):
        with patch.object(restart_on_update, "inspect_status",
                          return_value="No local update baseline; nothing changed."):
            result = update_preflight.report()
        self.assertEqual(result["state"], "inspected")
        self.assertNotIn("git fetch", result["local_revision"])

    def test_unexpected_string_is_not_echoed(self):
        with patch.object(restart_on_update, "inspect_status",
                          return_value="SECRET=private token in Git stderr"):
            result = update_preflight.report()
        self.assertEqual(result["state"], "unavailable")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_errors_from_git_never_expose_private_info(self):
        with patch.object(restart_on_update, "inspect_status",
                          side_effect=subprocess.CalledProcessError(
                              1, ["git"], stderr="TOKEN=sensitive")):
            result = update_preflight.report()
        self.assertEqual(result["state"], "unavailable")
        self.assertNotIn("TOKEN", json.dumps(result))

    def test_read_only_status_fails_closed_on_git_permission_error(self):
        with patch.object(restart_on_update, "inspect_status",
                          side_effect=PermissionError("/private/config")):
            result = update_preflight.report()
        self.assertEqual(result["state"], "unavailable")
        self.assertNotIn("/private/config", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
