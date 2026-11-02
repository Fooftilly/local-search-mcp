"""On-host bounded cross-process pacing acceptance (no Docker or HTTP)."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class EnginePacingAcceptanceTests(unittest.TestCase):
    def _invoke(self, args, env):
        return subprocess.run(
            [sys.executable, "-m", "scripts.engine_pacing_acceptance", *args],
            cwd=ROOT, env=env, capture_output=True, text=True,
            check=False, timeout=20)

    def test_isolated_real_process_admission_and_no_live_policy_mutation(self):
        with tempfile.TemporaryDirectory() as home:
            config = Path(home) / "local-search-mcp"
            config.mkdir(mode=0o700)
            actual_policy = config / "engine-pacing.json"
            actual_policy.write_text('{"THIS":"is an intentionally invalid host policy"}')
            actual_policy.chmod(0o600)
            original = actual_policy.read_bytes()
            env = dict(os.environ, XDG_CONFIG_HOME=home)
            result = self._invoke([], env)
            self.assertEqual(result.returncode, 0, result.stdout[-800:])
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["checks_passed"], 4)
            self.assertTrue(all(report["checks"].values()))
            self.assertEqual(report["provider_searches_sent"], 0)
            self.assertFalse(report["persisted_policy_modified"])
            self.assertTrue(report["sandbox_removed"])
            self.assertNotIn("operator_pacing_enabled", report)
            self.assertEqual(actual_policy.read_bytes(), original)
            self.assertEqual(set(config.iterdir()), {actual_policy})

    def test_worker_cannot_use_operator_xdg_directory_directly(self):
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, XDG_CONFIG_HOME=home)
            env.pop("_LSM_PACING_ACCEPTANCE_ROOT", None)
            env.pop("_LSM_PACING_ACCEPTANCE_TOKEN", None)
            invalid = self._invoke(
                ["--child", "acceptance-engine-a", "hold"], env)
            self.assertEqual(invalid.returncode, 2)
            self.assertEqual(json.loads(invalid.stdout)["status"], "sandbox_refused")
            self.assertEqual(list(Path(home).iterdir()), [])

    def test_invalid_arguments_never_launch_probe(self):
        with tempfile.TemporaryDirectory() as home:
            result = self._invoke(["--unexpected"], dict(os.environ, XDG_CONFIG_HOME=home))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["status"], "invalid_arguments")
            self.assertEqual(list(Path(home).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
