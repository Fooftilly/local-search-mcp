"""Offline design-gate corpus for future bounded document comparison (#213).

No HTTP, MCP registrations, user data or host-service operations.
"""
import unittest

from scripts import document_compare_benchmark as benchmark


class ComparisonBenchmarkDesignTests(unittest.TestCase):
    def test_synthetic_cases_preserve_expected_change_signals(self):
        for name, (left, right) in benchmark._cases().items():
            with self.subTest(name=name):
                result = benchmark._compare(left, right)
                self.assertEqual(result["equal"], name == "unchanged")
                self.assertEqual(result["changes"] == 0, name == "unchanged")
                self.assertLessEqual(len(left), benchmark.MAX_CHARS_PER_SIDE)
                self.assertLessEqual(len(right), benchmark.MAX_CHARS_PER_SIDE)
                if name == "interleaved_repetition":
                    # A single full replacement would not test alignment.
                    self.assertGreater(result["changes"], 20)

    def test_input_limits_are_explicit_and_not_silent_truncation(self):
        with self.assertRaises(ValueError):
            benchmark._compare("x" * (benchmark.MAX_CHARS_PER_SIDE + 1), "")
        with self.assertRaises(ValueError):
            benchmark._compare("a\n" * (benchmark.MAX_LINES_PER_SIDE + 1), "")
        with self.assertRaises(ValueError):
            benchmark._compare("", "a\n" * (benchmark.MAX_LINES_PER_SIDE + 1))

    def test_unicode_is_compared_as_source_text_not_encoded_bytes(self):
        self.assertTrue(benchmark._compare("🧭 Život\n", "🧭 Život\n")["equal"])
        self.assertFalse(benchmark._compare("🧭 Život\n", "🧭 Zivot\n")["equal"])


if __name__ == "__main__":
    unittest.main()
