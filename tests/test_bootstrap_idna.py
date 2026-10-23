"""Unicode setup validation must run in the prepared environment."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import bootstrap


class BootstrapIDNTests(unittest.TestCase):
    def test_unicode_domains_use_pinned_prepared_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = root / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()
            completed = subprocess.CompletedProcess(
                [], 0, stdout=json.dumps(["xn--caf-dma.fr", "docs.python.org"]))
            with (patch.object(bootstrap, "ROOT", root),
                  patch.object(bootstrap.subprocess, "run",
                               return_value=completed) as run):
                result = bootstrap.validate_domains("café.fr,docs.python.org")
        self.assertEqual(result, "xn--caf-dma.fr,docs.python.org")
        args, keywords = run.call_args
        self.assertEqual(args[0][0], str(python))
        self.assertEqual(args[0][1], "-c")
        self.assertEqual(json.loads(args[0][3]), ["café.fr", "docs.python.org"])
        self.assertTrue(keywords["capture_output"])
        self.assertFalse(keywords["check"])
        self.assertEqual(keywords["cwd"], str(root))

    def test_invalid_unicode_domain_never_accepts_failed_venv_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = root / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()
            failure = subprocess.CompletedProcess([], 1, stdout="",
                                                  stderr="SENSITIVE_ERROR")
            with (patch.object(bootstrap, "ROOT", root),
                  patch.object(bootstrap.subprocess, "run", return_value=failure)):
                with self.assertRaisesRegex(
                        bootstrap.SetupError, "Invalid Unicode domain policy") as error:
                    bootstrap.validate_domains("faß.de")
            self.assertNotIn("SENSITIVE_ERROR", str(error.exception))

    def test_existing_venv_reinstalls_when_dependency_probe_fails(self):
        # An importable idna version outside the required 3.x range must
        # fail the probe, prompting an explicit requirements.txt install.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = root / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()
            probe_failed = subprocess.CompletedProcess([], 1, stdout="", stderr="")
            with (patch.object(bootstrap, "ROOT", root),
                  patch.object(bootstrap.subprocess, "run",
                               return_value=probe_failed) as run,
                  patch.object(bootstrap, "run_command") as command,
                  patch.object(bootstrap, "info")):
                self.assertEqual(bootstrap.make_venv(), python)
        verified_script = run.call_args.args[0][2]
        self.assertIn("3 <= int(m.version('idna').split('.')[0]) < 4",
                      verified_script)
        self.assertEqual(command.call_count, 2)
        self.assertEqual(command.call_args_list[0].args[0],
                         [str(python), "-m", "pip", "install", "-r",
                          str(root / "requirements.txt")])
        self.assertEqual(command.call_args_list[1].args[0],
                         [str(python), "-c", verified_script])

    def test_ascii_domains_never_launch_subprocess(self):
        with patch.object(bootstrap.subprocess, "run",
                          side_effect=AssertionError("subprocess forbidden")):
            self.assertEqual(
                bootstrap.validate_domains("example.com,docs.python.org"),
                "example.com,docs.python.org",
            )


if __name__ == "__main__":
    unittest.main()
