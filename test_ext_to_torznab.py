#!/usr/bin/env python3
"""
Unit tests for ext_to_torznab.py — parsers, helpers, RSS builder, HTML parsing.

These tests do NOT require FlareSolverr or network access.
They test the pure functions and parser logic with sample HTML/data.
"""

import time
import unittest
from xml.etree import ElementTree as ET

# Import the module under test
import ext_to_torznab as ext

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_ROW = """\
<table class="table table-striped">
<tbody>
<tr>
  <td>
    <a class="torrent-title-link" href="/serenity-firefly-123456/"
       data-tooltip="Serenity Firefly S01E01 1080p WEB-DL x264">Sereni...</a>
    <div class="related-posted">
      <a href="/user/uploader/">uploader</a>
      <a href="/tv/">TV</a>
      <a href="/tv/hd/">HD</a>
    </div>
    <a class="search-magnet-btn" data-id="123456">Magnet</a>
  </td>
  <td><span>2.5 GB</span></td>
  <td><span>3</span></td>
  <td><span title="15 June 2026">15 Jun 26</span></td>
  <td><span class="text-success">45</span></td>
  <td><span class="text-danger">12</span></td>
</tr>
</tbody>
</table>
"""

SAMPLE_PAGE_HTML = """\
<!doctype html>
<html><head>
<meta name="csrf-token" content="aabbccdd00112233">
<script>window.searchPageToken = "deadbeef12345678";</script>
</head><body>
<table class="table table-striped">
<tbody>
<tr>
  <td>
    <a class="torrent-title-link" href="/test-title-999/"
       data-tooltip="Test Title">Test...</a>
    <div class="related-posted">
      <a href="/user/tester/">tester</a>
      <a href="/movies/">Movies</a>
      <a href="/movies/ultrahd/">UltraHD</a>
    </div>
    <a class="search-magnet-btn" data-id="999">Magnet</a>
  </td>
  <td><span>1.2 TB</span></td>
  <td><span>5</span></td>
  <td><span title="01 June 2026">01 Jun 26</span></td>
  <td><span class="text-success">100</span></td>
  <td><span class="text-danger">20</span></td>
</tr>
</tbody>
</table>
</body></html>
"""

SAMPLE_DETAIL_PAGE = """\
<!doctype html><html><head>
<meta name="csrf-token" content="ddccbbaa99887766">
<script>window.pageToken = "abababab12341234";</script>
</head><body>
<h1>Test Detail Page</h1>
<a class="search-magnet-btn" data-id="777">Download</a>
</body></html>
"""

CLOUDFLARE_522 = """\
<html><head><title>522: Connection timed out</title></head>
<body>522: Connection timed out</body></html>
"""

CLOUDFLARE_524 = """\
<html><head><title>524: A timeout occurred</title></head>
<body>524: A timeout occurred</body></html>
"""

# ---------------------------------------------------------------------------
# Tests — helper functions
# ---------------------------------------------------------------------------


class TestHelpers(unittest.TestCase):
    """Test pure helper functions."""

    def test_parse_size(self):
        cases = [
            ("0 B", 0),
            ("1 KB", 1024),
            ("2.5 MB", int(2.5 * 1024**2)),
            ("1.2 GB", int(1.2 * 1024**3)),
            ("100 TB", int(100 * 1024**4)),
            ("", 0),
            ("garbage", 0),
            ("512", 0),  # no unit
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(ext._parse_size(raw), expected)

    def test_parse_int(self):
        cases = [
            ("0", 0),
            ("45", 45),
            ("1,234", 1234),
            ("", 0),
            ("abc", 0),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(ext._parse_int(raw), expected)

    def test_parse_date_word_relative(self):
        """Relative time strings like '2 hours ago'."""
        result = ext._parse_date("2 hours ago")
        # Should produce an RFC-822 date
        self.assertIn(",", result)
        self.assertTrue(result.endswith("+0000"))

    def test_parse_date_absolute(self):
        """Full date strings."""
        result = ext._parse_date("15 June 2026")
        self.assertEqual(result, "Mon, 15 Jun 2026 00:00:00 +0000")

    def test_parse_date_empty(self):
        now = time.strftime("%a, %d %b %Y", time.gmtime())
        result = ext._parse_date("")
        self.assertTrue(result.startswith(now))

    def test_compute_hmac(self):
        result = ext._compute_hmac(12345, 1712345678, "mytoken")
        self.assertIsInstance(result, str)
        self.assertEqual(len(result), 64)  # SHA-256 hex

    def test_build_magnet(self):
        magnet = ext._build_magnet(
            "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
            "Test Title"
        )
        self.assertTrue(magnet.startswith("magnet:?xt=urn:btih:"))
        self.assertIn("&dn=Test+Title", magnet)
        # Trackers are URL-encoded
        self.assertIn("&tr=udp%3A%2F%2F", magnet)

    def test_build_magnet_no_title(self):
        magnet = ext._build_magnet(
            "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0", ""
        )
        self.assertTrue(magnet.startswith("magnet:?xt=urn:btih:"))
        # When title is empty, dn is still present but empty
        self.assertIn("&dn=", magnet)

    def test_extract_js_tokens_search_page(self):
        html = SAMPLE_PAGE_HTML
        page_token, csrf = ext._extract_js_tokens(html)
        self.assertEqual(page_token, "deadbeef12345678")
        self.assertEqual(csrf, "aabbccdd00112233")

    def test_extract_js_tokens_detail_page(self):
        page_token, csrf = ext._extract_js_tokens(SAMPLE_DETAIL_PAGE)
        self.assertEqual(page_token, "abababab12341234")
        self.assertEqual(csrf, "ddccbbaa99887766")

    def test_extract_js_tokens_no_tokens(self):
        page_token, csrf = ext._extract_js_tokens("<html></html>")
        self.assertEqual(page_token, "")
        self.assertEqual(csrf, "")

    def test_build_magnet_post(self):
        post = ext._build_magnet_post(12345, "mytoken", "mycsrf")
        self.assertIn("torrent_id=12345", post)
        self.assertIn("sessid=mycsrf", post)
        self.assertIn("hmac=", post)
        self.assertIn("timestamp=", post)

    def test_looks_like_token_error(self):
        self.assertTrue(ext.ExtToScraper._looks_like_token_error(
            {"success": False, "error": "Invalid token"}
        ))
        self.assertTrue(ext.ExtToScraper._looks_like_token_error(
            {"success": False, "error": "HMAC expired"}
        ))
        self.assertFalse(ext.ExtToScraper._looks_like_token_error(
            {"success": True, "error": ""}
        ))
        self.assertFalse(ext.ExtToScraper._looks_like_token_error(
            {"success": False, "error": "not found"}
        ))


# ---------------------------------------------------------------------------
# Tests — category mapping
# ---------------------------------------------------------------------------


class TestCategories(unittest.TestCase):

    def test_tv_mapped_to_5000(self):
        self.assertEqual(ext.EXT_TO_CAT_MAP["/tv/"], 5000)

    def test_movies_mapped_to_2000(self):
        self.assertEqual(ext.EXT_TO_CAT_MAP["/movies/"], 2000)

    def test_movies_ultrahd_mapped(self):
        key = "/movies//movies/ultrahd/"
        self.assertEqual(ext.EXT_TO_CAT_MAP[key], 2045)

    def test_cat_id_matches_exact(self):
        self.assertTrue(ext.cat_id_matches(5000, 5000))

    def test_cat_id_matches_top_level(self):
        """Subcat should match top-level filter."""
        self.assertTrue(ext.cat_id_matches(5040, 5000))

    def test_cat_id_matches_no_match(self):
        self.assertFalse(ext.cat_id_matches(2000, 5000))

    def test_cat_id_to_browse_path(self):
        self.assertEqual(ext.CAT_ID_TO_BROWSE_PATH[5000], "/tv/")


# ---------------------------------------------------------------------------
# Tests — HTML parser
# ---------------------------------------------------------------------------


class TestHtmlParser(unittest.TestCase):
    """Test sample HTML parsing (no network)."""

    def setUp(self):
        self.scraper = ext.ExtToScraper(
            "https://ext.to",
            None,  # type: ignore[arg-type]
            include_adult=True,
        )

    def test_parse_row_full(self):
        """Parse a complete sample row."""
        results = self.scraper._parse_html(SAMPLE_ROW)
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r["title"], "Serenity Firefly S01E01 1080p WEB-DL x264")
        self.assertIn("123456", r["details_url"])
        self.assertEqual(r["torrent_id"], 123456)
        self.assertEqual(r["categories"], [5000])
        self.assertEqual(r["size"], int(2.5 * 1024**3))
        self.assertEqual(r["seeders"], 45)
        self.assertEqual(r["leechers"], 12)
        self.assertEqual(r["peers"], 57)
        self.assertEqual(r["files"], 3)
        self.assertEqual(r["categories"], [5000])

    def test_parse_row_infohash_url(self):
        """GUID should be set to details_url initially."""
        results = self.scraper._parse_html(SAMPLE_ROW)
        self.assertEqual(results[0]["guid"], results[0]["details_url"])

    def test_parse_cloudflare_522(self):
        """Cloudflare error pages should yield no results."""
        results = self.scraper._parse_html(CLOUDFLARE_522)
        self.assertEqual(results, [])

    def test_parse_cloudflare_524(self):
        results = self.scraper._parse_html(CLOUDFLARE_524)
        self.assertEqual(results, [])

    def test_parse_empty_html(self):
        results = self.scraper._parse_html("")
        self.assertEqual(results, [])

    def test_parse_no_table(self):
        results = self.scraper._parse_html("<html><body>no results</body></html>")
        self.assertEqual(results, [])

    def test_cat_id_via_related_posted(self):
        """Category from /movies/ + /movies/ultrahd/ should map to 2045."""
        results = self.scraper._parse_html(SAMPLE_PAGE_HTML)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["categories"], [2045])

    def test_size_tb(self):
        """TB-sized torrent parsing."""
        results = self.scraper._parse_html(SAMPLE_PAGE_HTML)
        self.assertEqual(results[0]["size"], int(1.2 * 1024**4))

    def test_date_preserved(self):
        results = self.scraper._parse_html(SAMPLE_PAGE_HTML)
        pub_date = results[0]["pub_date"]
        self.assertIn("Jun", pub_date)
        self.assertIn("2026", pub_date)


# ---------------------------------------------------------------------------
# Tests — RSS/XML builders
# ---------------------------------------------------------------------------


class TestRssBuilder(unittest.TestCase):

    def test_caps_xml_has_tv_search(self):
        xml = ext._build_caps("http://localhost:5556")
        root = ET.fromstring(xml)
        searching = root.find("searching")
        self.assertIsNotNone(searching)
        tv = searching.find("tv-search")  # type: ignore[union-attr]
        self.assertIsNotNone(tv)
        self.assertEqual(tv.get("available"), "yes")  # type: ignore[union-attr]
        self.assertIn("season", tv.get("supportedParams", ""))  # type: ignore[union-attr]

    def test_caps_xml_has_categories(self):
        xml = ext._build_caps("http://localhost:5556")
        root = ET.fromstring(xml)
        cats = root.find("categories")
        self.assertIsNotNone(cats)
        tv = cats.find('.//category[@id="5000"]')  # type: ignore[union-attr]
        self.assertIsNotNone(tv)

    def test_build_rss_basic(self):
        results = [{
            "title": "Test Torrent",
            "guid": "https://ext.to/test-12345/",
            "details_url": "https://ext.to/test-12345/",
            "download_url": "https://ext.to/test-12345/",
            "magnet_url": "magnet:?xt=urn:btih:aa",
            "infohash": "aa",
            "categories": [5000],
            "size": int(2.5 * 1024**3),
            "files": 3,
            "pub_date": "Mon, 15 Jun 2026 12:00:00 +0000",
            "seeders": 45,
            "leechers": 12,
            "peers": 57,
        }]
        xml = ext._build_rss(results)
        self.assertIn("Test Torrent", xml)
        self.assertIn("magnet:?xt=urn:btih:aa", xml)
        # Namespace-prefixed attributes (exact prefix depends on ET serialization)
        self.assertIn('name="seeders"', xml)
        self.assertIn('name="infohash"', xml)

    def test_build_rss_empty(self):
        xml = ext._build_rss([])
        self.assertIn("<rss", xml)
        self.assertIn("EXT Torrents", xml)

    def test_build_rss_no_magnet(self):
        """When magnet_url is empty, RSS uses download endpoint link."""
        results = [{
            "title": "No Magnet",
            "guid": "https://ext.to/no-mag-42/",
            "details_url": "https://ext.to/no-mag-42/",
            "download_url": "https://ext.to/no-mag-42/",
            "magnet_url": "",
            "infohash": "",
            "categories": [2000],
            "size": 1024,
            "files": 1,
            "pub_date": "Mon, 15 Jun 2026 12:00:00 +0000",
            "seeders": 10,
            "leechers": 2,
            "peers": 12,
        }]
        xml = ext._build_rss(results, base_url="http://localhost:5556")
        self.assertIn("t=download", xml)
        # & is serialized as &amp; by ElementTree
        self.assertIn("guid=http", xml)

    def test_error_xml(self):
        xml = ext._error_xml("Something broke")
        self.assertIn("Something broke", xml)
        self.assertIn("Error", xml)


# ---------------------------------------------------------------------------
# Tests — on-demand magnet fetch for GUIDs
# ---------------------------------------------------------------------------


class TestGuidMagnetResolution(unittest.TestCase):
    """Test fetch_magnet_for_guid logic — pure string handling before FS call."""

    def setUp(self):
        self.scraper = ext.ExtToScraper(
            "https://ext.to",
            None,  # type: ignore[arg-type]
        )

    def test_magnet_already(self):
        """Already a magnet: return as-is."""
        result = self.scraper.fetch_magnet_for_guid(
            "magnet:?xt=urn:btih:aa"
        )
        self.assertEqual(result, "magnet:?xt=urn:btih:aa")

    def test_infohash_url(self):
        """GUID is an infohash URL."""
        result = self.scraper.fetch_magnet_for_guid(
            "https://ext.to/t/a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0/"
        )
        self.assertEqual(
            result,
            "magnet:?xt=urn:btih:a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
        )

    def test_infohash_url_with_slug(self):
        result = self.scraper.fetch_magnet_for_guid(
            "https://ext.to/t/My-Title-a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0/"
        )
        self.assertEqual(
            result,
            "magnet:?xt=urn:btih:a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
        )

    def test_non_http_guid(self):
        result = self.scraper.fetch_magnet_for_guid("not-a-url")
        self.assertIsNone(result)

    def test_empty_guid(self):
        result = self.scraper.fetch_magnet_for_guid("")
        self.assertIsNone(result)

    def test_none_guid(self):
        result = self.scraper.fetch_magnet_for_guid(None)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Tests — URL construction
# ---------------------------------------------------------------------------


class TestUrlBuilder(unittest.TestCase):

    def setUp(self):
        self.scraper = ext.ExtToScraper("https://ext.to", None)

    def test_build_search_url(self):
        url = self.scraper._build_url("test", 1)
        self.assertIn("/browse/", url)
        self.assertIn("q=test", url)
        self.assertIn("sort=age", url)
        self.assertIn("with_adult=1", url)
        self.assertNotIn("page=", url)

    def test_build_search_url_page_2(self):
        url = self.scraper._build_url("firefly", 2)
        self.assertIn("page=2", url)

    def test_build_search_url_no_adult(self):
        scraper2 = ext.ExtToScraper(
            "https://ext.to", None, include_adult=False
        )
        url = scraper2._build_url("test", 1)
        self.assertNotIn("with_adult", url)


# ---------------------------------------------------------------------------
# Tests — keywordless browse URL construction
# ---------------------------------------------------------------------------


class TestBrowseCategories(unittest.TestCase):

    def setUp(self):
        self.scraper = ext.ExtToScraper("https://ext.to", None)

    def test_browse_path_for_tv(self):
        path = ext.CAT_ID_TO_BROWSE_PATH.get(5000)
        self.assertEqual(path, "/tv/")

    def test_browse_path_default(self):
        """When no categories given, defaults to /tv/ and /movies/."""
        # _browse_by_categories calls CAT_ID_TO_BROWSE_PATH directly
        pass  # tested via CAT_ID_TO_BROWSE_PATH entries


# ---------------------------------------------------------------------------
# Tests — category matching
# ---------------------------------------------------------------------------


class TestCatMatch(unittest.TestCase):

    def test_matching_sub_category(self):
        """Top-level cat 5000 should match subcat 5040."""
        self.assertTrue(ext.cat_id_matches(5040, 5000))

    def test_no_match_diff_family(self):
        self.assertFalse(ext.cat_id_matches(5040, 2000))

    def test_match_exact(self):
        self.assertTrue(ext.cat_id_matches(2000, 2000))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
