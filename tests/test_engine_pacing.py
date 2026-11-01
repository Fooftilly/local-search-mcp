"""Cross-process engine admission is conservative, private and disabled by default."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
import json
import os
import select
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import engine_pacing as pacing


BOOT_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BOOT_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@contextmanager
def fixed_clocks(epoch: int, monotonic: int, *, boot: str | None = None):
    """Keep timing tests deterministic across runner uptimes and reboot states."""
    with ExitStack() as patches:
        patches.enter_context(patch.object(pacing.time, "time", return_value=epoch))
        patches.enter_context(patch.object(pacing.time, "monotonic",
                                          return_value=monotonic))
        if boot is not None:
            patches.enter_context(patch.object(pacing, "_boot_id", return_value=boot))
        yield


class EnginePacingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    @property
    def folder(self):
        return self.root / "local-search-mcp"

    def write_policy(self, data):
        self.folder.mkdir(mode=0o700, exist_ok=True)
        path = self.folder / pacing.FILENAME
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        return path

    def sample(self, enabled=True):
        return {"schema": pacing.SCHEMA, "enabled": enabled, "engines": {
            "duckduckgo": {"min_spacing_seconds": 15},
            "qwant": {"min_spacing_seconds": 60}}}

    def test_missing_policy_disabled_and_read_does_not_write(self):
        self.assertIsNone(pacing.policy())
        self.assertFalse(self.folder.exists())
        self.write_policy(self.sample(False))
        self.assertIsNone(pacing.policy())
        self.assertEqual(list(self.folder.iterdir()), [self.folder / pacing.FILENAME])

    def test_explicit_policy_validates_names_and_exact_schema(self):
        self.write_policy(self.sample())
        self.assertEqual(pacing.policy(), {"duckduckgo": 15, "qwant": 60})
        bad = [
            {**self.sample(), "extra": "invalid"},
            {**self.sample(), "enabled": 1},
            {**self.sample(), "engines": {"Google": {"min_spacing_seconds": 20}}},
            {**self.sample(), "engines": {"duckduckgo": {"min_spacing_seconds": True}}},
            {**self.sample(), "engines": {"duckduckgo": {"min_spacing_seconds": -1}}},
            {**self.sample(), "engines": {"duckduckgo": {"min_spacing_seconds": 3601}}},
            {**self.sample(), "engines": {"duckduckgo": {"min_spacing_seconds": 1},
                                          "DuckDuckGo": {"min_spacing_seconds": 1}}},
            {**self.sample(), "engines": {}},
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                self.write_policy(payload)
                with self.assertRaises(pacing.PaceUnavailable):
                    pacing.policy()
        self.write_policy(self.sample())
        with (self.folder / pacing.FILENAME).open("w") as stream:
            stream.write('{"schema":"' + pacing.SCHEMA + '","enabled":true,'
                         '"engines":{},"engines":{}}')
        with self.assertRaises(pacing.PaceUnavailable):
            pacing.policy()

    def test_policy_can_preserve_exact_configured_engine_case(self):
        payload = self.sample()
        payload["engines"] = {
            "Bing Images": {"min_spacing_seconds": 30},
            "duckduckgo": {"min_spacing_seconds": 15},
        }
        self.write_policy(payload)
        self.assertEqual(pacing.policy(), {"bing images": 30, "duckduckgo": 15})
        self.assertEqual(pacing.policy(preserve_case=True),
                         {"Bing Images": 30, "duckduckgo": 15})
        payload["engines"]["bing images"] = {"min_spacing_seconds": 10}
        self.write_policy(payload)
        for preserve_case in (False, True):
            with self.subTest(preserve_case=preserve_case):
                with self.assertRaises(pacing.PaceUnavailable):
                    pacing.policy(preserve_case=preserve_case)

    def test_symlink_and_permissions_fail_closed_without_modifying_target(self):
        path = self.write_policy(self.sample())
        path.chmod(0o644)
        with self.assertRaises(pacing.PaceUnavailable):
            pacing.policy()
        path.unlink()
        target = self.root / "outside"
        target.write_text("external")
        path.symlink_to(target)
        with self.assertRaises(OSError):
            pacing.policy()
        self.assertEqual(target.read_text(), "external")

    def test_cooldown_persists_but_different_engines_have_independent_slots(self):
        with fixed_clocks(10000, 5000):
            with pacing.reserve("duckduckgo", 15):
                with pacing.reserve("qwant", 60):
                    pass
            with self.assertRaises(pacing.PaceUnavailable) as caught:
                with pacing.reserve("duckduckgo", 15):
                    pass
            self.assertEqual(caught.exception.retry_after_seconds, 15)
        with fixed_clocks(10015, 5015):
            with pacing.reserve("duckduckgo", 15):
                pass
            with self.assertRaises(pacing.PaceUnavailable):
                with pacing.reserve("qwant", 60):
                    pass
        for path in self.folder.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_concurrent_same_engine_fail_fast_different_engine_allowed(self):
        arrived, release = threading.Event(), threading.Event()
        def first():
            with pacing.reserve("duckduckgo", 0):
                arrived.set()
                self.assertTrue(release.wait(timeout=3))
        with ThreadPoolExecutor(max_workers=2) as pool:
            job = pool.submit(first)
            self.assertTrue(arrived.wait(timeout=2))
            with self.assertRaisesRegex(pacing.PaceUnavailable, "active search"):
                with pacing.reserve("duckduckgo", 0):
                    pass
            with pacing.reserve("qwant", 0):
                pass
            release.set()
            job.result(timeout=3)
        with pacing.reserve("duckduckgo", 0):
            pass

    def test_separate_process_cannot_overlap_selected_engine(self):
        child_code = (
            "import engine_pacing as p, sys\n"
            "with p.reserve('duckduckgo', 0):\n"
            "    print('ADMITTED', flush=True)\n"
            "    sys.stdin.readline()\n"
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(pacing.__file__).resolve().parent)
        child = subprocess.Popen(
            [sys.executable, "-c", child_code], env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        try:
            readable, _, _ = select.select([child.stdout], [], [], 5)
            self.assertTrue(readable, "Child admission process did not respond")
            self.assertEqual(child.stdout.readline().strip(), "ADMITTED")
            with self.assertRaisesRegex(pacing.PaceUnavailable, "active search"):
                with pacing.reserve("duckduckgo", 0):
                    pass
            child.stdin.write("\n")
            child.stdin.flush()
            child.stdin.close()
            self.assertEqual(child.wait(timeout=5), 0, child.stderr.read())
        finally:
            # An assertion before the stdin write must never hang CI while
            # Popen's context manager waits on a child blocked in readline.
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
        with pacing.reserve("duckduckgo", 0):
            pass

    def test_forward_wall_clock_jump_cannot_bypass_monotonic_cooldown(self):
        with fixed_clocks(10000, 5000):
            with pacing.reserve("duckduckgo", 60):
                pass
        with fixed_clocks(10100, 5001):
            with self.assertRaises(pacing.PaceUnavailable) as caught:
                with pacing.reserve("duckduckgo", 60):
                    pass
            self.assertEqual(caught.exception.retry_after_seconds, 59)
        with fixed_clocks(10101, 5060):
            with pacing.reserve("duckduckgo", 60):
                pass

    def test_reboot_requires_new_full_cooldown_before_admission(self):
        with fixed_clocks(10000, 5000, boot=BOOT_A):
            with pacing.reserve("duckduckgo", 60):
                pass
        with fixed_clocks(20000, 100, boot=BOOT_B):
            with self.assertRaises(pacing.PaceUnavailable) as caught:
                with pacing.reserve("duckduckgo", 60):
                    pass
            self.assertEqual(caught.exception.retry_after_seconds, 60)
        with fixed_clocks(20060, 160, boot=BOOT_B):
            with pacing.reserve("duckduckgo", 60):
                pass

    def test_reboot_with_zero_gap_admits_first_operation(self):
        with fixed_clocks(10000, 5000, boot=BOOT_A):
            with pacing.reserve("duckduckgo", 0):
                pass
        with fixed_clocks(20000, 100, boot=BOOT_B):
            # Zero spacing must not emit an immediate-retry failure, even
            # when a reservation file exists from the previous boot.
            with pacing.reserve("duckduckgo", 0):
                pass
            with pacing.reserve("duckduckgo", 0):
                pass

    def test_reboot_preserves_old_positive_gap_if_new_policy_is_zero(self):
        with fixed_clocks(10000, 5000, boot=BOOT_A):
            with pacing.reserve("duckduckgo", 60):
                pass
        with fixed_clocks(20000, 100, boot=BOOT_B):
            with self.assertRaises(pacing.PaceUnavailable) as caught:
                with pacing.reserve("duckduckgo", 0):
                    pass
            self.assertEqual(caught.exception.retry_after_seconds, 60)
        with fixed_clocks(20060, 160, boot=BOOT_B):
            with pacing.reserve("duckduckgo", 0):
                pass

    def test_invalid_state_and_clock_rollback_fail_closed(self):
        # Fix both clocks: otherwise host uptime changes the exception path.
        with fixed_clocks(10000, 5000):
            with pacing.reserve("duckduckgo", 100):
                pass
        state = next(self.folder.glob("*.json"))
        # State is intentionally hidden, not a visible user policy file.
        self.assertIn("next_allowed_epoch", state.read_text())
        with fixed_clocks(1000, 100):
            with self.assertRaisesRegex(pacing.PaceUnavailable, "clock skew"):
                with pacing.reserve("duckduckgo", 100):
                    pass
        state.write_text('{"next_allowed_epoch": "oops"}')
        with fixed_clocks(10100, 5100):
            with self.assertRaisesRegex(pacing.PaceUnavailable, "state"):
                with pacing.reserve("duckduckgo", 100):
                    pass


if __name__ == "__main__":
    unittest.main()
