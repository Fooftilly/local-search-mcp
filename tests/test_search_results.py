"""Conservative search URL grouping, stable ranking and source attribution."""
import unittest

from search_results import enrich_search_results, normalized_url


def row(url, engine, title="", content="", engines=None):
    result = {"url": url, "engine": engine, "title": title, "content": content}
    if engines is not None:
        result["engines"] = engines
    return result


class SearchResultTests(unittest.TestCase):
    def test_conservative_url_canonicalization(self):
        good = "https://example.com/path?x=1&y=2"
        self.assertEqual(normalized_url("HTTPS://EXAMPLE.COM:443/path?x=1&y=2#fragment"),
                         good)
        self.assertEqual(normalized_url("https://example.com"), "https://example.com/")
        self.assertEqual(normalized_url("http://EXAMPLE.COM:80/a"), "http://example.com/a")
        self.assertEqual(normalized_url("http://[::1]:80/"), "http://[::1]/")
        self.assertEqual(normalized_url("https://example.com/path?#anchor"),
                         "https://example.com/path?")
        self.assertNotEqual(normalized_url("https://example.com/path?"),
                            normalized_url("https://example.com/path"))
        self.assertEqual(normalized_url("https://example.com/path#one"),
                         normalized_url("https://example.com/path#two"))
        self.assertNotEqual(normalized_url("http://example.com/path"),
                            normalized_url("https://example.com/path"))
        for variant in ("https://example.com:444/path", "https://example.com/path/",
                        "https://example.com/path?x=2",
                        "https://example.com/path?y=2&x=1",
                        "https://example.com/%7e"):
            with self.subTest(variant=variant):
                self.assertNotEqual(normalized_url(variant), good)
        for bad in ("ftp://example.com/a", "not a URL", "",
                    "https://user:pw@example.com/a",
                    "https://example.com:99999/a",
                    "https://[not-valid-ipv6/a", None):
            with self.subTest(bad=bad):
                self.assertIsNone(normalized_url(bad))

    def test_default_keeps_legacy_shape_first_n_and_order(self):
        rows = [
            row("https://example.com/a", "qwant", "First", "1"),
            row("https://example.com/a", "bing", "Second", "2"),
            row("https://example.com/b", "brave", "Third", "3"),
        ]
        result = enrich_search_results(rows, 2)
        self.assertEqual(result, [
            {"url": "https://example.com/a", "engine": "qwant",
             "title": "First", "snippet": "1"},
            {"url": "https://example.com/a", "engine": "bing",
             "title": "Second", "snippet": "2"},
        ])
        self.assertTrue(all("source_count" not in r for r in result))

    def test_deduplicate_before_limit_and_keep_variants(self):
        rows = [
            row("https://EXAMPLE.com:443/a#one", "qwant", "Original", "Snippet A"),
            row("https://different.example/z", "bing", "Other", "Other text"),
            row("https://example.com/a#two", "bing", "Alternate", "Snippet B"),
            row("https://new.example/p", "mojeek", "Third", "Three"),
        ]
        result = enrich_search_results(rows, 2, deduplicate=True)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["url"], "https://EXAMPLE.com:443/a#one")
        self.assertEqual(result[0]["title"], "Original")
        self.assertEqual(result[0]["snippet"], "Snippet A")
        self.assertEqual(result[0]["engine"], "qwant")
        self.assertEqual(result[0]["source_engines"], ["qwant", "bing"])
        self.assertEqual(result[0]["source_count"], 2)
        self.assertEqual(result[0]["duplicate_count"], 2)
        self.assertEqual([r["title"] for r in result[0]["source_matches"]],
                         ["Original", "Alternate"])
        self.assertEqual(result[1]["title"], "Other")

    def test_source_count_ranking_is_stable_on_ties(self):
        rows = [
            row("https://a.example", "qwant", "A"),
            row("https://b.example", "yahoo", "B", engines=["bing", "brave", "yahoo"]),
            row("https://c.example", "bing", "C"),
            row("https://a.example#t", "mojeek", "A2"),
            row("https://c.example#t", "brave", "C2"),
        ]
        result = enrich_search_results(rows, 3, deduplicate=True,
                                       ranking="source_count")
        self.assertEqual([x["title"] for x in result], ["B", "A", "C"])
        self.assertEqual([x["source_count"] for x in result], [3, 2, 2])
        # First-seen order is retained for the tie between A and C,
        # while B's upstream multi-engine attribution remains explicit.
        self.assertEqual(result[0]["source_engines"], ["yahoo", "bing", "brave"])

    def test_does_not_conflate_distinct_queries_or_paths_or_invalid_urls(self):
        rows = [
            row("https://site.example/x?order=1", "a"),
            row("https://site.example/x?order=2", "b"),
            row("https://site.example/x", "c"),
            row("http://site.example/x", "d"),
            row("malformed", "e"),
            row("malformed", "f"),
        ]
        result = enrich_search_results(rows, 12, deduplicate=True)
        self.assertEqual(len(result), len(rows))
        self.assertTrue(all(x["duplicate_count"] == 1 for x in result))

    def test_bound_source_variants_and_preserve_stable_order(self):
        rows = [row("https://site.example/article#"+str(i), "engine"+str(i),
                    "Title "+str(i), "s" * 1500) for i in range(20)]
        result = enrich_search_results(rows, 12, deduplicate=True)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["duplicate_count"], 20)
        self.assertEqual(len(result[0]["source_matches"]), 8)
        self.assertEqual(len(result[0]["source_engines"]), 20)
        self.assertEqual(len(result[0]["source_matches"][0]["snippet"]), 1100)

    def test_missing_source_engine_does_not_gain_unearned_count(self):
        rows = [row("https://site.example", "", "No attribution"),
                row("https://site.example#2", None, "Still unknown")]
        result = enrich_search_results(rows, 3, deduplicate=True)
        self.assertEqual(result[0]["source_count"], 0)
        self.assertEqual(result[0]["duplicate_count"], 2)


if __name__ == "__main__":
    unittest.main()
