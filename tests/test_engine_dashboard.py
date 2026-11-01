"""Operator-only engine selector: data validation and actual dashboard routing."""
import unittest
from unittest.mock import patch

import engine_dashboard


INVENTORY = [
    {"name": "bing", "enabled": False, "categories": ("general", "web")},
    {"name": "brave", "enabled": True, "categories": ("general", "web")},
    {"name": "duckduckgo", "enabled": True, "categories": ("general", "web")},
    {"name": "google", "enabled": False, "categories": ("general", "web")},
]


class EngineDashboardTests(unittest.TestCase):
    def test_page_escapes_inventory_and_keeps_google_disabled(self):
        with patch.object(engine_dashboard, "snapshot", return_value=(
                INVENTORY, tuple(r["name"] for r in INVENTORY), "a" * 64)):
            page = engine_dashboard.page("CSRF_PRIVATE", message="<not HTML>")
        self.assertIn('name="engine" value="duckduckgo" checked', page)
        self.assertIn('name="engine" value="google" disabled', page)
        self.assertIn("&lt;not HTML&gt;", page)
        self.assertIn('name="state" value="' + "a" * 64, page)
        self.assertNotIn("<not HTML>", page)
        self.assertNotIn("<script", page)

    def test_form_changes_only_operator_selected_general_engines(self):
        with (patch.object(engine_dashboard, "snapshot", return_value=(
                INVENTORY, tuple(r["name"] for r in INVENTORY), "b" * 64)),
              patch.object(engine_dashboard.engines, "apply", return_value="saved") as writer,
              patch.object(engine_dashboard.request_cache, "invalidate_search")
              as invalidate):
            result = engine_dashboard.apply_form({
                "engine": ["duckduckgo", "bing"], "confirm": ["APPLY"],
                "state": ["b" * 64],
            })
        self.assertEqual(result, "saved")
        invalidate.assert_called_once_with()
        writer.assert_called_once_with(
            ("duckduckgo", "bing"), ("bing", "brave", "duckduckgo", "google"),
            expected_state="b" * 64)

    def test_rejects_untrusted_and_stale_browser_inputs_without_writes(self):
        good = {"engine": ["duckduckgo"], "confirm": ["APPLY"],
                "state": ["c" * 64]}
        invalid = [
            {**good, "engine": []},
            {**good, "engine": ["duckduckgo", "duckduckgo"]},
            {**good, "engine": ["google"]},
            {**good, "engine": ["unlisted"]},
            {**good, "engine": ["duckduckgo", "<script>"]},
            {**good, "confirm": ["APPLY", "APPLY"]},
            {**good, "confirm": ["confirm"]},
            {**good, "state": ["d" * 64]},
            {**good, "state": ["c" * 64, "c" * 64]},
        ]
        with (patch.object(engine_dashboard, "snapshot", return_value=(
                INVENTORY, tuple(r["name"] for r in INVENTORY), "c" * 64)),
              patch.object(engine_dashboard.engines, "apply") as writer):
            for attempt in invalid:
                with self.subTest(attempt=attempt), self.assertRaises(ValueError):
                    engine_dashboard.apply_form(attempt)
            writer.assert_not_called()

    def test_full_inventory_and_failed_update_does_not_invalidate(self):
        large = [{"name": f"e-{i}", "enabled": i % 2 == 0,
                  "categories": ("general",)} for i in range(512)]
        state = "a" * 64
        with (patch.object(engine_dashboard, "snapshot", return_value=(
                large, tuple(r["name"] for r in large), state)),
              patch.object(engine_dashboard.engines, "apply",
                           side_effect=engine_dashboard.engines.EngineSetupError(
                               "safe failure")),
              patch.object(engine_dashboard.request_cache, "invalidate_search")
              as invalidate):
            page = engine_dashboard.page("csrf")
            self.assertIn('value="e-511"', page)
            with self.assertRaises(engine_dashboard.engines.EngineSetupError):
                engine_dashboard.apply_form({
                    "engine": ["e-0", "e-511"],
                    "state": [state], "confirm": ["APPLY"],
                })
            invalidate.assert_not_called()

    def test_search_cache_invalidation_releases_old_keys_and_preserves_fetch(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from unittest.mock import patch
        import request_cache

        request_cache.reset_for_test()
        self.addCleanup(request_cache.reset_for_test)
        begun, resume = Event(), Event()

        def old_loader():
            begun.set()
            resume.wait(timeout=3)
            return [{"engine": "brave", "title": "Stale"}]

        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(
                request_cache.run, "search", ("search", "old"),
                old_loader, ttl=90)
            self.assertTrue(begun.wait(timeout=2))
            request_cache.run("fetch", ("fetch", "safe"),
                              lambda: {"success": True}, ttl=90)
            request_cache.invalidate_search()
            # A new call after the selection change must not coalesce with
            # the pre-change loader and must not get its old cached value.
            fresh = request_cache.run(
                "search", ("search", "old"),
                lambda: [{"engine": "duckduckgo", "title": "Fresh"}], ttl=90)
            resume.set()
            self.assertEqual(pending.result(timeout=3)[0]["engine"], "brave")
        self.assertEqual(fresh[0]["engine"], "duckduckgo")
        result = request_cache.run("search", ("search", "old"),
                                   lambda: [], ttl=90)
        self.assertEqual(result, fresh)
        self.assertEqual(request_cache.run(
            "fetch", ("fetch", "safe"),
            lambda: {"success": False}, ttl=90), {"success": True})

    def test_sleeping_inventory_is_read_only_and_cannot_apply(self):
        with (patch.object(engine_dashboard, "snapshot",
                           side_effect=engine_dashboard.engines.EngineSetupError(
                               "SearXNG sleeping")),
              patch.object(engine_dashboard.engines, "apply") as writer):
            self.assertIn("SearXNG sleeping", engine_dashboard.page("csrf"))
            with self.assertRaises(engine_dashboard.engines.EngineSetupError):
                engine_dashboard.apply_form({
                    "engine": ["duckduckgo"], "confirm": ["APPLY"],
                    "state": ["a" * 64],
                })
            writer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
