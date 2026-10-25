"""Conservative search URL grouping, stable ranking and source attribution."""
import unittest

from search_results import enrich_search_results, normalized_url


def row(url, engine, title="", content="", engines=None):
    result = {"url": url, "engine": engine, "title": title, "content": content}
    if engines is not None:
        result["engines"] = engines
    return result


class SearchResultTests(unittest.TestCase):
    def test_invalid_unicode_search_metadata_cannot_break_mcp_json(self):
        import json
        value = "bad\ud800text"
        rows = [row("https://example.com/path", value, value, value)]
        plain = enrich_search_results(rows, 1)
        grouped = enrich_search_results(rows + rows, 1, deduplicate=True)
        for answer in (plain, grouped):
            self.assertNotIn("\ud800", str(answer))
            self.assertIn("bad?text", str(answer))
            json.dumps(answer, ensure_ascii=False).encode("utf-8", "strict")

    def test_malformed_engine_identifiers_do_not_collapse_source_count(self):
        rows = [row("https://a.example", "engine\ud800", "A",
                    engines=["engine\ud800", "engine\ud801"]),
                row("https://b.example", "ordinary", "B")]
        results = enrich_search_results(rows, 2, deduplicate=True,
                                        ranking="source_count")
        self.assertEqual([item["title"] for item in results], ["A", "B"])
        self.assertEqual(results[0]["source_count"], 2)
        self.assertEqual(results[0]["source_engines"],
                         [r"engine\ud800", r"engine\ud801"])
        for name in results[0]["source_engines"]:
            name.encode("utf-8", "strict")

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

    def test_ipv6_zone_spelling_is_not_merged(self):
        original = "http://[FE80::ABCD%25Eth0]:80/path"
        other_zone = "http://[fe80::abcd%25eth0]/path"
        normalized = "http://[fe80::abcd%25Eth0]/path"
        self.assertEqual(normalized_url(original), normalized)
        self.assertEqual(normalized_url(normalized), normalized)
        self.assertNotEqual(normalized_url(original),
                            normalized_url(other_zone))
        rows = [row(original, "qwant", "Upper zone"),
                row(normalized, "bing", "Same zone"),
                row(other_zone, "yahoo", "Lower zone")]
        grouped = enrich_search_results(rows, 4, deduplicate=True)
        self.assertEqual([x["title"] for x in grouped],
                         ["Upper zone", "Lower zone"])
        self.assertEqual([x["duplicate_count"] for x in grouped], [2, 1])
        self.assertEqual(grouped[0]["source_engines"], ["qwant", "bing"])

    def test_more_than_sixteen_engines_count_for_ranking(self):
        engines = [f"engine-{i:02}" for i in range(24)]
        rows = [
            row("https://one.example", "solo", "First"),
            row("https://many.example", engines[0], "Many",
                engines=engines + [engines[0], None, 123, "x" * 101]),
            row("https://third.example", "third", "Third",
                engines=["third", "other"]),
        ]
        grouped = enrich_search_results(rows, 3, deduplicate=True,
                                        ranking="source_count")
        self.assertEqual([x["title"] for x in grouped],
                         ["Many", "Third", "First"])
        self.assertEqual([x["source_count"] for x in grouped], [24, 2, 1])
        self.assertEqual(grouped[0]["source_engines"], engines)

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
                         ["Alternate"])
        self.assertEqual(result[1]["source_matches"], [])
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
        self.assertEqual(result[0]["source_matches"][0]["title"], "Title 1")
        self.assertEqual(len(result[0]["source_engines"]), 20)
        self.assertEqual(len(result[0]["source_matches"][0]["snippet"]), 1100)

    def test_unmerged_sources_do_not_duplicate_snippet_text(self):
        rows = [row(f"https://site.example/item/{i}", "qwant",
                    f"Item {i}", "S" * 1100) for i in range(12)]
        results = enrich_search_results(rows, 12, deduplicate=True)
        self.assertEqual(len(results), 12)
        self.assertTrue(all(r["source_matches"] == [] and r["duplicate_count"] == 1
                            and r["source_count"] == 1 for r in results))
        self.assertEqual([r["snippet"] for r in results], ["S" * 1100] * 12)

    def test_missing_source_engine_does_not_gain_unearned_count(self):
        rows = [row("https://site.example", "", "No attribution"),
                row("https://site.example#2", None, "Still unknown")]
        result = enrich_search_results(rows, 3, deduplicate=True)
        self.assertEqual(result[0]["source_count"], 0)
        self.assertEqual(result[0]["duplicate_count"], 2)


if __name__ == "__main__":
    unittest.main()
