"""Conservative search URL grouping, stable ranking and source attribution."""
import unittest

from search_results import enrich_search_results, normalized_url


def row(url, engine, title="", content="", engines=None):
    result = {"url": url, "engine": engine, "title": title, "content": content}
    if engines is not None:
        result["engines"] = engines
    return result


class SearchResultTests(unittest.TestCase):
    def test_captured_searxng_image_fields_are_opt_in_and_source_labeled(self):
        # Operator's 2026-10-30 localhost /search?categories=images JSON
        # reported img_src, thumbnail_src, resolution and source as strings
        # in Bing Images and Pinterest rows (no private values captured).
        image = row("https://photos.example.com/gallery/1",
                    "bing images", "Public photo")
        image.update(category="images", img_src="https://cdn.example.com/1.jpg",
                     thumbnail_src="https://thumb.example.com/1.webp",
                     resolution="1920x1080", source="Unverified provider label",
                     img_format="JPEG")
        plain = enrich_search_results([image], 1)
        self.assertEqual(set(plain[0]), {"title", "url", "snippet", "engine"})
        selected = enrich_search_results([image], 1,
                                         include_image_metadata=True)[0]
        self.assertEqual(selected["image_url"], "https://cdn.example.com/1.jpg")
        self.assertEqual(selected["thumbnail_url"],
                         "https://thumb.example.com/1.webp")
        self.assertEqual(selected["source_page_url"],
                         "https://photos.example.com/gallery/1")
        self.assertEqual((selected["image_width"], selected["image_height"]),
                         (1920, 1080))
        self.assertNotIn("source", selected)
        self.assertNotIn("img_format", selected)
        self.assertNotIn("resolution", selected)
        import json
        json.dumps(selected, ensure_ascii=False).encode("utf-8")

        regular = row("https://example.com/news", "yahoo", "News")
        regular.update(category="general", img_src="https://cdn.example.com/a.jpg",
                       thumbnail_src="https://cdn.example.com/a-thumb.jpg")
        self.assertEqual(set(enrich_search_results(
            [regular], 1, include_image_metadata=True)[0]),
            {"title", "url", "snippet", "engine"})

    def test_image_links_reject_unsafe_provider_urls_without_network(self):
        from unittest.mock import patch
        from search_results import _public_image_link
        invalid = (
            "http://cdn.example.com/img.jpg", "data:image/png;base64,AAA",
            "file:///etc/passwd", "javascript:alert(1)",
            "https://localhost/img.jpg", "https://127.0.0.1/img.jpg",
            "https://10.0.0.1/image", "https://[::1]/img.jpg",
            "https://user:pass@cdn.example.com/x",
            "https://@cdn.example.com/x",
            "https://cdn.example.com:8080/x",
            "https://cdn.example.com:/x",
            "https://cdn.example.com./x",
            "https://xn--bcher-kva.example.com/x",
            "https://bücher.example.com/x",
            "https://cdn.example.com/x\\evil",
            "https://cdn.example.com/x\nheader: injected",
            "https://cdn.example.com/x%0Aheader",
            "https://cdn.example.com/x%5cwin",
            "https://cdn.example.com/x%7F",
            "https://cdn.example.com/x image",
            "https://cdn.example.com/x\ud800",
            "https://cdn.example.com/" + "x" * 2200,
            "https://com/x", "https://cdn.local/x",
            "https://cdn.example.com:65536/x",
            None, [], 42,
        )
        with patch("socket.getaddrinfo", side_effect=AssertionError("no DNS")):
            for value in invalid:
                with self.subTest(url=str(value)[:90]):
                    self.assertIsNone(_public_image_link(value))
            self.assertEqual(
                _public_image_link("https://CDN.example.com:443/photo.jpg?q=x&size=2"),
                "https://CDN.example.com:443/photo.jpg?q=x&size=2")

    def test_image_dimensions_strictly_bounded_and_primary_only(self):
        from search_results import _image_dimensions
        self.assertEqual(_image_dimensions("1920x1080"), (1920, 1080))
        for bad in ("0x10", "2x0", "20001x1080", "20x50000",
                    "1920 x 1080", "1920×1080", "80:40", "001x100",
                    "999999999999x10", None, 128):
            with self.subTest(value=bad):
                self.assertIsNone(_image_dimensions(bad))
        first = row("https://example.org/image", "bing images", "Primary")
        first.update(category="images",
                     img_src="https://one.example.com/first.jpg",
                     thumbnail_src="http://bad.example.com/thumb",
                     resolution="1920x1080")
        second = row("https://example.org/image", "pinterest", "Duplicate")
        second.update(category="images",
                      img_src="https://two.example.com/second.jpg",
                      thumbnail_src="https://two.example.com/second-thumb.jpg",
                      resolution="400x200")
        merged = enrich_search_results([first, second], 2, deduplicate=True,
                                       ranking="source_count",
                                       include_image_metadata=True)[0]
        self.assertEqual(merged["duplicate_count"], 2)
        self.assertEqual(merged["source_engines"], ["bing images", "pinterest"])
        self.assertEqual(merged["image_url"],
                         "https://one.example.com/first.jpg")
        self.assertNotIn("thumbnail_url", merged)
        self.assertEqual(merged["image_width"], 1920)
        self.assertEqual(merged["image_height"], 1080)
        self.assertNotIn("image_url", merged["source_matches"][0])
        self.assertNotIn("second.jpg", str(merged))
        swapped = enrich_search_results([second, first], 2, deduplicate=True,
                                        include_image_metadata=True)[0]
        self.assertEqual(swapped["image_url"],
                         "https://two.example.com/second.jpg")
        text = row("https://example.org/image", "yahoo", "Main page")
        text["category"] = "general"
        self.assertNotIn("image_url", enrich_search_results(
            [text, first], 2, deduplicate=True,
            include_image_metadata=True)[0])

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

    def test_overlong_escaped_engine_names_are_not_counted_as_sources(self):
        malformed = "\ud800" * 100
        rows = [row("https://example.com/", malformed, "Page",
                    engines=[malformed])]
        enriched = enrich_search_results(rows, 1, deduplicate=True)
        self.assertEqual(enriched[0]["source_engines"], [])
        self.assertEqual(enriched[0]["source_count"], 0)
        self.assertLessEqual(len(enriched[0]["engine"]), 100)
        self.assertTrue(enriched[0]["engine"].isascii())
        self.assertEqual(enriched[0]["engine"],
                         enriched[0]["engine"].encode("utf-8").decode("utf-8"))

    def test_malformed_primary_and_attribution_names_use_same_representation(self):
        malformed = "engine\ud800"
        results = enrich_search_results(
            [row("https://a.example", malformed, "One")],
            1, deduplicate=True)
        self.assertEqual(results[0]["engine"], r"engine\ud800")
        self.assertEqual(results[0]["source_engines"], [r"engine\ud800"])

    def test_literal_backslash_engine_distinct_from_invalid_unicode_escape(self):
        invalid = "engine\ud800"
        literal = r"engine\ud800"
        rows = [row("https://same.example", invalid, "Article",
                    engines=[invalid, literal])]
        result = enrich_search_results(rows, 1, deduplicate=True)[0]
        self.assertEqual(result["source_count"], 2)
        self.assertEqual(result["source_engines"],
                         [r"engine\ud800", r"engine\\ud800"])
        self.assertEqual(result["engine"], result["source_engines"][0])

    def test_search_enrichment_never_silently_truncates_result_urls(self):
        long_url = "https://example.com/" + "x" * 2100
        # The bridge rejects overlong source links before enrichment. Keep
        # this lower-level formatter from silently returning a *different* URL.
        self.assertEqual(enrich_search_results(
            [row(long_url, "test")], 1)[0]["url"], long_url)

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

    def test_captured_searxng_json_dates_are_opt_in_and_source_labeled(self):
        # Representative real SearXNG 2026.10.19 JSON captured on 2026-10-21:
        # https://gannet.dbhq.uk/searxng-search-api/
        # A result can have publishedDate=null; another has an ISO-like
        # timestamp with no timezone. Never infer UTC from the latter.
        missing = row("https://example.org/first", "brave", "Undated")
        missing["publishedDate"] = None
        dated = row("https://example.org/second", "brave", "Dated")
        dated["publishedDate"] = "2024-10-23T12:00:38"
        defaults = enrich_search_results([missing, dated], 2)
        self.assertTrue(all(set(r) == {"title", "url", "snippet", "engine"}
                            for r in defaults))
        enhanced = enrich_search_results([missing, dated], 2,
                                         include_published_date=True)
        self.assertNotIn("published_date", enhanced[0])
        self.assertEqual(enhanced[1]["published_date"], "2024-10-23")
        self.assertEqual(enhanced[1]["date_source"], "searxng.publishedDate")
        self.assertNotIn("date_source", enhanced[0])

    def test_date_parser_rejects_invalid_untrusted_metadata(self):
        good = ["2024-02-29", "2024-02-29T12:30:40",
                "2024-02-29T12:30:40Z", "2024-02-29T23:59:59+14:00",
                "2024-02-29T00:00:00.012345-05:30",
                "2024-02-29T10:20:30+01:59"]
        for value in good:
            with self.subTest(value=value):
                r = row("https://example.org/post", "news")
                r["publishedDate"] = value
                result = enrich_search_results([r], 1, include_published_date=True)[0]
                self.assertEqual(result["published_date"], "2024-02-29")
                self.assertEqual(result["date_source"], "searxng.publishedDate")
        bad = [None, 12, True, [], {}, "", "now", "2024-02-30",
               "2024-13-01T12:00:00", "2024-10-23T25:00:00",
               "2024-10-23T12:00:00+25:00", "2024-10-23T12:00:00+0200",
               "2024-10-23T12:00:00+01:99", "2024-10-23T12:00:00-01:99",
               "2024-10-23T12:00:00+00:60", "2024-10-23T12:00:00-24:00",
               "2024-10-23T12:00:00+23:60",
               "2024-10-23T12:00:00Z\n", "\u202e2024-10-23",
               "2024-10-23<script>", "\ud800", "x" * 100000]
        for value in bad:
            with self.subTest(value=repr(value)):
                r = row("https://example.org/post", "news")
                r["publishedDate"] = value
                r["pubdate"] = "2020-01-01 00:00:00"
                result = enrich_search_results([r], 1, include_published_date=True)[0]
                self.assertNotIn("published_date", result)
                self.assertNotIn("date_source", result)

    def test_grouping_does_not_attribute_later_engine_date_to_primary_source(self):
        first = row("https://EXAMPLE.org/path#first", "yahoo", "Primary")
        second = row("https://example.org/path#second", "brave", "Alternate")
        second["publishedDate"] = "2026-10-01T12:00:00Z"
        grouped = enrich_search_results([first, second], 2, deduplicate=True,
                                        include_published_date=True)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["source_engines"], ["yahoo", "brave"])
        self.assertNotIn("published_date", grouped[0])
        # If the primary source itself provides a date, retain it across
        # grouping without copying conflicting later-source metadata.
        first["publishedDate"] = "2026-10-02T11:00:00Z"
        grouped = enrich_search_results([first, second], 2, deduplicate=True,
                                        include_published_date=True)
        self.assertEqual(grouped[0]["published_date"], "2026-10-02")
        self.assertEqual(grouped[0]["date_source"], "searxng.publishedDate")


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
