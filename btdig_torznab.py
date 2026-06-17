#!/usr/bin/env python3
"""
btdig-torznab — Torznab bridge for btdig.com

Makes BTDig available as a standard Torznab indexer for
Sonarr, Radarr, Lidarr, and other *arr applications.

No external dependencies — Python 3 stdlib only.
"""

import http.server
import urllib.request
import urllib.parse
import html.parser
import xml.etree.ElementTree as ET
import time
import os
import re
import threading
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HOST = os.environ.get("BTDIG_HOST", "0.0.0.0")
PORT = int(os.environ.get("BTDIG_PORT", "5555"))
BTDIG_URL = os.environ.get("BTDIG_URL", "https://btdig.com")
CACHE_TTL = int(os.environ.get("BTDIG_CACHE_TTL", "300"))  # seconds


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class TorrentResult:
    title: str
    magnet: str
    size_bytes: int
    details_url: str
    pub_date: str  # RSS-compatible date string
    seeders: int = 0
    leechers: int = 0


# ---------------------------------------------------------------------------
# HTML parser
# ---------------------------------------------------------------------------


class BTDigParser(html.parser.HTMLParser):
    """Parse btdig.com search results HTML into TorrentResult objects."""

    def __init__(self):
        super().__init__()
        self.results: list[TorrentResult] = []
        self._reset()

    def _reset(self):
        self._in_result = False       # inside a div.one_result
        self._result_depth = 0
        self._in_title_a = False      # directly inside the torrent name <a>
        self._in_size_span = False
        self._in_age_span = False
        self._cur = {}
        self._title_a_depth = 0

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        cls = (d.get("class", "") or "").strip()

        if not self._in_result:
            if tag == "div" and "one_result" in cls.split():
                self._reset()
                self._in_result = True
                self._result_depth = 1
                self._cur = {
                    "title": "",
                    "magnet": "",
                    "size_text": "",
                    "age_text": "",
                    "details_url": "",
                }
            return

        self._result_depth += 1

        if tag == "a":
            href = d.get("href", "") or ""
            if href.startswith("magnet:"):
                self._cur["magnet"] = href
            elif "btdig.com/" in href and not self._cur.get("details_url"):
                self._cur["details_url"] = href

        if cls == "torrent_name":
            self._in_title_a = False  # wait for the <a> inside
            self._title_a_depth = self._result_depth
        elif cls == "torrent_size":
            self._in_size_span = True
        elif cls == "torrent_age":
            self._in_age_span = True

        # The actual title text is in the <a> inside .torrent_name
        if tag == "a" and self._title_a_depth > 0:
            self._in_title_a = True

    def handle_data(self, data):
        if not self._in_result:
            return
        if self._in_title_a:
            self._cur["title"] += data.strip()
        if self._in_size_span:
            self._cur["size_text"] += data.strip()
        if self._in_age_span:
            self._cur["age_text"] += data.strip()

    def handle_endtag(self, tag):
        if not self._in_result:
            return

        self._result_depth -= 1

        if tag == "a" and self._in_title_a:
            self._in_title_a = False
            self._title_a_depth = 0

        if tag == "div" and self._result_depth <= 0:
            self._finalize()
            self._in_result = False

        # Clear span flags on any closing tag
        if tag == "span":
            self._in_size_span = False
            self._in_age_span = False

    def _finalize(self):
        c = self._cur
        if not c.get("magnet") or not c.get("title"):
            return  # skip incomplete

        size = self._parse_size(c.get("size_text", ""))
        pub_date = self._parse_age(c.get("age_text", ""))

        self.results.append(TorrentResult(
            title=c["title"],
            magnet=c["magnet"],
            size_bytes=size,
            details_url=c.get("details_url", ""),
            pub_date=pub_date,
        ))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_size(text: str) -> int:
        text = text.strip().replace("\u00a0", " ")
        if not text:
            return 0
        m = re.match(r"([\d.]+)\s*(B|KB|MB|GB|TB)", text, re.IGNORECASE)
        if not m:
            return 0
        v = float(m.group(1))
        u = m.group(2).upper()
        mul = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
        return int(v * mul.get(u, 1))

    @staticmethod
    def _parse_age(text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        text = re.sub(r"^found\s+", "", text)
        text = re.sub(r"\s+ago$", "", text)
        now = time.time()
        m = re.match(r"(\d+)\s+(year|month|day|hour|minute)s?", text)
        if not m:
            return ""
        amount, unit = int(m.group(1)), m.group(2)
        mul = {"year": 365 * 86400, "month": 30 * 86400,
               "day": 86400, "hour": 3600, "minute": 60}
        pub_ts = now - amount * mul.get(unit, 0)
        return time.strftime("%a, %d %b %Y %H:%M:%S +0000",
                             time.gmtime(pub_ts))


# ---------------------------------------------------------------------------
# Scraper with cache
# ---------------------------------------------------------------------------


@dataclass
class _Cached:
    xml: str
    expiry: float


class BTDigScraper:
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
    REQUEST_DELAY = 1.0

    def __init__(self):
        self._cache: dict[str, _Cached] = {}
        self._last_request = 0.0
        self._lock = threading.Lock()

    def search(self, query: str) -> str:
        key = query.strip().lower()
        with self._lock:
            c = self._cache.get(key)
            if c and time.time() < c.expiry:
                return c.xml
        xml = self._fetch(query)
        with self._lock:
            self._cache[key] = _Cached(xml, time.time() + CACHE_TTL)
        return xml

    def _fetch(self, query: str) -> str:
        self._rate_limit()
        url = f"{BTDIG_URL}/search?q={urllib.parse.quote(query)}&order=0"
        req = urllib.request.Request(url, headers={"User-Agent": self.USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                html = r.read().decode("utf-8", errors="replace")
        except Exception as e:
            return self._error_xml(f"HTTP error: {e}")

        p = BTDigParser()
        try:
            p.feed(html)
        except Exception as e:
            return self._error_xml(f"Parse error: {e}")

        return self._rss(p.results, query)

    def _rate_limit(self):
        e = time.time() - self._last_request
        if e < self.REQUEST_DELAY:
            time.sleep(self.REQUEST_DELAY - e)
        self._last_request = time.time()

    # ------------------------------------------------------------------
    # RSS / Torznab builders
    # ------------------------------------------------------------------

    def _rss(self, results: list[TorrentResult], _query: str) -> str:
        NS = "http://torznab.com/schemas/2015/feed"
        rss = ET.Element("rss", version="2.0",
                         attrib={"xmlns:atom": "http://www.w3.org/2005/Atom",
                                 "xmlns:torznab": NS})
        ch = ET.SubElement(rss, "channel")
        ET.SubElement(ch, "title").text = "BTDig (btdig.com)"
        ET.SubElement(ch, "link").text = BTDIG_URL
        ET.SubElement(ch, "description").text = (
            "BTDig DHT search engine — Torznab bridge")
        ET.SubElement(ch, "language").text = "en-us"

        for r in results:
            item = ET.SubElement(ch, "item")
            ET.SubElement(item, "title").text = r.title
            ET.SubElement(item, "guid", isPermaLink="false").text = r.magnet
            ET.SubElement(item, "link").text = r.details_url or r.magnet
            if r.pub_date:
                ET.SubElement(item, "pubDate").text = r.pub_date
            ET.SubElement(item, "description").text = (
                f"Size: {self._human_size(r.size_bytes)} | "
                f"Magnet: {r.magnet[:60]}...")

            for name, val in [("category", "2000"), ("size", str(r.size_bytes)),
                              ("seeders", str(r.seeders)),
                              ("peers", str(r.seeders + r.leechers))]:
                ET.SubElement(item, f"{{{NS}}}attr", name=name, value=val)

            ET.SubElement(item, "enclosure",
                          url=r.magnet,
                          length=str(r.size_bytes),
                          type="application/x-bittorrent")

        return ET.tostring(rss, encoding="unicode", xml_declaration=True)

    def caps_xml(self) -> str:
        root = ET.Element("caps")
        serv = ET.SubElement(root, "server",
                             title="BTDig (btdig.com)", version="1", email="")
        ET.SubElement(serv, "limits", max="100", default="25")
        src = ET.SubElement(root, "searching")
        for t in ("search", "tv-search", "movie-search", "music-search",
                  "book-search"):
            ET.SubElement(src, t, available="yes", supportedParams="q")
        cats = ET.SubElement(root, "categories")
        c = ET.SubElement(cats, "category", id="2000", name="Other")
        ET.SubElement(c, "subcat", id="2010", name="Other")
        return ET.tostring(root, encoding="unicode", xml_declaration=True)

    @staticmethod
    def _error_xml(msg: str) -> str:
        rss = ET.Element("rss", version="2.0")
        ch = ET.SubElement(rss, "channel")
        ET.SubElement(ch, "title").text = "BTDig — Error"
        ET.SubElement(ch, "description").text = msg
        return ET.tostring(rss, encoding="unicode", xml_declaration=True)

    @staticmethod
    def _human_size(b: float) -> str:
        for u in ("B", "KB", "MB", "GB", "TB"):
            if b < 1024:
                return f"{b:.1f} {u}"
            b /= 1024
        return f"{b:.1f} PB"


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

_scraper = BTDigScraper()


class TorznabHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path in ("/torznab/api", "/api"):
            self._handle(params)
        else:
            self._ok_plain("BTDig Torznab bridge — OK")

    def _handle(self, params: dict):
        t = (params.get("t", [""])[0]).strip()

        if t == "caps":
            return self._send(self._scraper.caps_xml())

        if t in ("search", "tvsearch", "movie", "music", "book"):
            q = params.get("q", [""])[0].strip()
            if not q:
                # Return recent/popular results for Prowlarr test
                return self._send(self._scraper.search("2026"))
            return self._send(self._scraper.search(q))

        return self._send(self._scraper._error_xml(f"Unknown t={t}"))

    def _send(self, xml: str):
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(xml.encode("utf-8"))

    def _ok_plain(self, text: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(text.encode())

    @property
    def _scraper(self):
        return _scraper

    def log_message(self, fmt, *args):
        print(f"[btdig] {self.client_address[0]} - {args[0]} {args[1]}",
              flush=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    sv = http.server.HTTPServer((HOST, PORT), TorznabHandler)
    print(f"[btdig] Torznab bridge listening on {HOST}:{PORT}", flush=True)
    print(f"[btdig] Torznab endpoint: http://<host>:{PORT}/torznab/api", flush=True)
    try:
        sv.serve_forever()
    except KeyboardInterrupt:
        print("\n[btdig] Shutting down...")
        sv.shutdown()


if __name__ == "__main__":
    main()
