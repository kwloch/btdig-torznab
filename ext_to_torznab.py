#!/usr/bin/env python3
"""
ext-to-torznab — Torznab bridge for ext.to

Makes EXT Torrents available as a standard Torznab indexer for
Sonarr, Radarr, Lidarr, and other *arr applications.

REQUIRES:
  - FlareSolverr instance (bypasses ext.to Cloudflare protection)
  - pip: requests, beautifulsoup4

Usage:
  # Set env vars, then:
  python3 ext_to_torznab.py

See README.md for Docker Compose setup with FlareSolverr.
"""

import hashlib
import http.server
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HOST = os.environ.get("EXT_TO_HOST", "0.0.0.0")
PORT = int(os.environ.get("EXT_TO_PORT", "5556"))
EXT_TO_URL = os.environ.get("EXT_TO_URL", "https://ext.to")
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191")
FLARESOLVERR_TIMEOUT = int(os.environ.get("FLARESOLVERR_TIMEOUT", "60000"))
INCLUDE_ADULT = os.environ.get("INCLUDE_ADULT", "true").lower() == "true"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
CACHE_TTL = int(os.environ.get("EXT_TO_CACHE_TTL", "300"))

logger = logging.getLogger("ext.to")

# ---------------------------------------------------------------------------
# Trackers — appended to constructed magnet links
# ---------------------------------------------------------------------------

_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
]

_RESULTS_PER_PAGE = 50
_MAGNET_API_PATH = "/ajax/getSearchMagnet.php"
_DETAIL_MAGNET_API_PATH = "/ajax/getTorrentMagnet.php"
_MAGNET_WORKERS = 3

# Pre-compiled regex patterns
_RE_HTML_TAGS = re.compile(r"<[^>]+>")
_RE_INFOHASH = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_RE_SEARCH_TOKEN = re.compile(
    r"window\.searchPageToken\s*=\s*['\"]([a-fA-F0-9]+)['\"]"
)
_RE_PAGE_TOKEN = re.compile(
    r"window\.pageToken\s*=\s*['\"]([a-fA-F0-9]+)['\"]"
)
_RE_CSRF = re.compile(
    r'<meta\s[^>]*name=["\']csrf-token["\']\s[^>]*content=["\']([a-fA-F0-9]+)["\']'
)
_RE_CSRF_FALLBACK = re.compile(
    r'csrf-token[^>]*content=["\']([a-fA-F0-9]+)["\']'
)
_RE_SIZE = re.compile(r"([\d.,]+)\s*(B|KB|MB|GB|TB)\b", re.IGNORECASE)
_RE_GUID_INFOHASH = re.compile(r"/t/(?:[^/]+-)?([0-9a-fA-F]{40,64})/")
_RE_URL_TORRENT_ID = re.compile(r"-(\d+)/?$")
_RE_WORD_TIME = re.compile(
    r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s*(?:ago)?",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Category maps — ext.to URL path fragment → Torznab category ID
# ---------------------------------------------------------------------------

EXT_TO_CAT_MAP: dict[str, int] = {
    "/anime/": 5070,
    "/anime//anime/audio-lossless/": 3040,
    "/anime//anime/english-translated/": 5070,
    "/anime//anime/raw": 5070,
    "/anime//anime/raw/": 5070,
    "/anime//anime/subs/": 5070,
    "/anime/raw": 5070,
    "/anime/raw/": 5070,
    "/applications/": 4000,
    "/applications//applications/android/": 4070,
    "/applications//applications/ios/": 4060,
    "/applications//applications/linux/": 4000,
    "/applications//applications/mac/": 4030,
    "/applications//applications/other-applications/": 4040,
    "/applications//applications/windows/": 4010,
    "/books/": 7000,
    "/books//books/audio-books/": 3030,
    "/books//books/comics/": 7030,
    "/books//books/ebooks/": 7020,
    "/games/": 4050,
    "/games//games/mac/": 4030,
    "/games//games/nds/": 1000,
    "/games//games/other-games/": 1000,
    "/games//games/pc-games/": 4050,
    "/games//games/ps3/": 1000,
    "/games//games/ps4/": 1000,
    "/games//games/psp/": 1000,
    "/games//games/switch/": 1000,
    "/games//games/wii/": 1000,
    "/games//games/xbox360/": 1000,
    "/movies/": 2000,
    "/movies//movies/3d-movies/": 2060,
    "/movies//movies/bollywood/": 2000,
    "/movies//movies/documentary/": 2000,
    "/movies//movies/dubbed-movies/": 2000,
    "/movies//movies/dvd/": 2030,
    "/movies//movies/highres-movies/": 2040,
    "/movies//movies/movie-clips/": 2020,
    "/movies//movies/mp4/": 2000,
    "/movies//movies/music-videos/": 3020,
    "/movies//movies/other-movies/": 2020,
    "/movies//movies/ultrahd/": 2045,
    "/music/": 3000,
    "/music//music/aac/": 3000,
    "/music//music/lossless/": 3040,
    "/music//music/mp3/": 3010,
    "/music//music/other-music/": 3050,
    "/music//music/radio-shows/": 3000,
    "/other/": 8000,
    "/tv/": 5000,
    "/video/": 6000,
    "/xxx/": 6000,
    "/xxx//xxx/games/": 6050,
    "/xxx//xxx/hentai/": 6050,
    "/xxx//xxx/magazines/": 6050,
    "/xxx//xxx/pictures/": 6060,
    "/xxx//xxx/video/": 6000,
}

# Reverse: Torznab top-level → ext.to browse path (keywordless fallback)
CAT_ID_TO_BROWSE_PATH: dict[int, str] = {
    1000: "/games/",
    2000: "/movies/",
    3000: "/music/",
    4000: "/applications/",
    5000: "/tv/",
    5070: "/anime/",
    6000: "/xxx/",
    7000: "/books/",
    8000: "/other/",
}

# Torznab caps category tree
TORZNAB_CATEGORIES: list[tuple[int, str, list[tuple[int, str]]]] = [
    (1000, "Console", [(1010, "NDS"), (1020, "PSP"), (1030, "Wii"),
                        (1040, "XBox"), (1050, "XBox 360"), (1060, "PS3"),
                        (1070, "Other"), (1080, "PS4"), (1090, "Switch")]),
    (2000, "Movies", [(2010, "Foreign"), (2020, "Other"), (2030, "SD"),
                       (2040, "HD"), (2045, "UHD"), (2050, "BluRay"),
                       (2060, "3D")]),
    (3000, "Audio", [(3010, "MP3"), (3020, "Video"), (3030, "Audiobook"),
                      (3040, "Lossless"), (3050, "Other")]),
    (4000, "PC", [(4010, "0day"), (4020, "ISO"), (4030, "Mac"),
                   (4040, "Mobile-Other"), (4050, "Games"),
                   (4060, "Mobile-iOS"), (4070, "Mobile-Android")]),
    (5000, "TV", [(5020, "Foreign"), (5030, "SD"), (5040, "HD"),
                   (5045, "UHD"), (5050, "Other"), (5060, "Sport"),
                   (5070, "Anime"), (5080, "Documentary")]),
    (6000, "XXX", [(6010, "DVD"), (6020, "WMV"), (6030, "XviD"),
                    (6040, "x264"), (6050, "Other"), (6060, "Imageset"),
                    (6070, "Pack"), (6080, "BluRay")]),
    (7000, "Books", [(7010, "Mags"), (7020, "EBook"), (7030, "Comics"),
                      (7040, "Technical"), (7050, "Other")]),
    (8000, "Other", [(8010, "Misc"), (8020, "Hashed")]),
]


def cat_id_matches(result_cat: int, filter_cat: int) -> bool:
    """Return True if *result_cat* should be included under *filter_cat*."""
    if result_cat == filter_cat:
        return True
    if filter_cat % 1000 == 0:
        return (result_cat // 1000) * 1000 == filter_cat
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_hmac(torrent_id: int, timestamp: int, page_token: str) -> str:
    data = f"{torrent_id}|{timestamp}|{page_token}"
    return hashlib.sha256(data.encode()).hexdigest()


def _build_magnet_post(torrent_id: int, page_token: str,
                       csrf_token: str) -> str:
    ts = int(time.time())
    return urllib.parse.urlencode({
        "torrent_id": torrent_id,
        "hash": "",
        "name": "",
        "timestamp": ts,
        "hmac": _compute_hmac(torrent_id, ts, page_token),
        "sessid": csrf_token,
    })


def _build_magnet(infohash: str, title: str) -> str:
    dn = urllib.parse.quote_plus(title) if title else ""
    tr_params = "".join(
        f"&tr={urllib.parse.quote_plus(t)}" for t in _TRACKERS
    )
    return f"magnet:?xt=urn:btih:{infohash.lower()}&dn={dn}{tr_params}"


def _parse_size(raw: str) -> int:
    raw = raw.strip()
    m = _RE_SIZE.match(raw)
    if not m:
        return 0
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    unit = m.group(2).upper()
    mult = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    return int(value * mult.get(unit, 1))


def _parse_int(raw: str) -> int:
    raw = raw.strip().replace(",", "").replace(".", "")
    try:
        return int(raw) if raw else 0
    except (ValueError, AttributeError):
        return 0


def _parse_date(raw: str) -> str:
    raw = raw.strip()
    now = datetime.now(timezone.utc)

    m = _RE_WORD_TIME.match(raw)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {
            "second": n, "minute": n * 60, "hour": n * 3600,
            "day": n * 86400, "week": n * 604800,
            "month": n * 2592000, "year": n * 31536000,
        }.get(unit, 0)
        ts = time.time() - delta
        return time.strftime("%a, %d %b %Y %H:%M:%S +0000",
                             time.gmtime(ts))

    for fmt in ("%d %B %Y", "%B %d, %Y", "%d %b %Y",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%d-%m-%Y", "%m/%d/%Y", "%d.%m.%Y"):
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except ValueError:
            pass

    return now.strftime("%a, %d %b %Y %H:%M:%S +0000")


def _extract_js_tokens(html: str) -> tuple[str, str]:
    """Extract (page_token, csrf_token) from ext.to HTML."""
    m = _RE_SEARCH_TOKEN.search(html)
    page_token = m.group(1) if m else ""
    if not page_token:
        m = _RE_PAGE_TOKEN.search(html)
        page_token = m.group(1) if m else ""

    m = _RE_CSRF.search(html)
    if not m:
        m = _RE_CSRF_FALLBACK.search(html)
    csrf_token = m.group(1) if m else ""

    return page_token, csrf_token


# ---------------------------------------------------------------------------
# FlareSolverr client
# ---------------------------------------------------------------------------


class FlareSolverrError(Exception):
    """Raised when FlareSolverr returns an error or is unreachable."""


class FlareSolverrClient:
    """Thin wrapper around the FlareSolverr REST API v1."""

    def __init__(self, base_url: str, timeout_ms: int = 60000):
        self._api_url = base_url.rstrip("/") + "/v1"
        self._timeout_ms = timeout_ms
        self._http_timeout = timeout_ms / 1000 + 15
        self._session_id: Optional[str] = None
        self._session_lock = threading.Lock()

        if requests is None:
            raise RuntimeError(
                "Missing 'requests' library. Install with: pip install requests"
            )

    def create_session(self) -> str:
        with self._session_lock:
            if self._session_id:
                return self._session_id
            result = self._post({"cmd": "sessions.create"})
            session_id = result.get("session")
            if not session_id:
                raise FlareSolverrError(
                    "FlareSolverr did not return a session ID"
                )
            self._session_id = session_id
            logger.info("FlareSolverr session created: %s", session_id)
            return session_id

    def destroy_session(self) -> None:
        if not self._session_id:
            return
        sid = self._session_id
        self._session_id = None
        try:
            self._post({"cmd": "sessions.destroy", "session": sid})
        except FlareSolverrError:
            pass

    def get_page(self, url: str) -> str:
        html, _cookies, _ua = self.get_page_with_cookies(url)
        return html

    def get_page_with_cookies(self, url: str) -> tuple[str, dict, str]:
        if not self._session_id:
            self.create_session()
        return self._do_request("request.get", url)

    def post_form(self, url: str, post_data: str) -> dict:
        if not self._session_id:
            self.create_session()

        payload = {
            "cmd": "request.post",
            "url": url,
            "postData": post_data,
            "maxTimeout": self._timeout_ms,
            "session": self._session_id,
        }

        result = self._post(payload)
        status = result.get("status")
        if status != "ok":
            msg = result.get("message", "Unknown error")
            raise FlareSolverrError(
                f"FlareSolverr POST returned status={status!r}: {msg}"
            )

        solution = result.get("solution", {})
        http_status = solution.get("status", 200)
        if http_status >= 400:
            raise FlareSolverrError(
                f"POST to {url} returned HTTP {http_status}"
            )

        raw = solution.get("response", "{}")
        # FlareSolverr wraps JSON in <pre> tags
        m = re.search(r"<pre[^>]*>(.*?)</pre>", raw, re.DOTALL)
        if m:
            raw = m.group(1).strip()

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"success": False, "error": f"Non-JSON response"}

    def _do_request(self, cmd: str, url: str) -> tuple[str, dict, str]:
        payload = {
            "cmd": cmd,
            "url": url,
            "maxTimeout": self._timeout_ms,
            "session": self._session_id,
        }
        result = self._post(payload)
        status = result.get("status")
        if status != "ok":
            msg = result.get("message", "Unknown error")
            raise FlareSolverrError(
                f"FlareSolverr returned status={status!r}: {msg}"
            )

        solution = result.get("solution", {})
        http_status = solution.get("status", 200)
        if http_status >= 400:
            raise FlareSolverrError(
                f"ext.to returned HTTP {http_status} for {url}"
            )

        html = solution.get("response", "")
        ua = solution.get("userAgent", "")
        cookies: dict = {}
        for c in solution.get("cookies", []):
            name = c.get("name", "")
            value = c.get("value", "")
            if name:
                cookies[name] = value

        return html, cookies, ua

    def _post(self, payload: dict) -> dict:
        try:
            resp = requests.post(
                self._api_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self._http_timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.ConnectionError as exc:
            raise FlareSolverrError(
                f"Cannot connect to FlareSolverr at {self._api_url}: {exc}"
            )
        except requests.exceptions.Timeout as exc:
            raise FlareSolverrError(
                f"FlareSolverr request timed out after "
                f"{self._http_timeout:.0f}s"
            )
        except requests.exceptions.HTTPError as exc:
            raise FlareSolverrError(
                f"FlareSolverr HTTP error: "
                f"{exc.response.status_code} {exc.response.text[:200]}"
            )


# ---------------------------------------------------------------------------
# Ext.to HTML scraper
# ---------------------------------------------------------------------------


class ExtToScraper:
    """Scrape ext.to search results via FlareSolverr."""

    def __init__(self, base_url: str, flaresolverr: FlareSolverrClient,
                 include_adult: bool = True):
        self._base = base_url.rstrip("/")
        self._fs = flaresolverr
        self._include_adult = include_adult
        self._cache: dict[str, tuple[list[dict], float]] = {}
        self._cache_lock = threading.Lock()

        if BeautifulSoup is None:
            raise RuntimeError(
                "Missing 'beautifulsoup4' library. "
                "Install with: pip install beautifulsoup4"
            )

    def search(self, query: str = "", categories: Optional[list[int]] = None,
               season: Optional[int] = None, episode: Optional[int] = None,
               offset: int = 0, limit: int = 25) -> list[dict]:
        effective_query = query.strip()
        if effective_query and season is not None:
            if episode is not None:
                effective_query = f"{effective_query} S{season:02d}E{episode:02d}"
            else:
                effective_query = f"{effective_query} S{season:02d}"

        # Keywordless → browse category pages to avoid 522
        if not effective_query:
            return self._browse_by_categories(
                categories or [], offset, limit
            )

        start_page = (offset // _RESULTS_PER_PAGE) + 1
        pages_needed = max(1, -(-limit // _RESULTS_PER_PAGE))

        all_results: list[dict] = []
        last_html = ""
        last_url = ""

        for page in range(start_page, start_page + pages_needed):
            url = self._build_url(effective_query, page)
            logger.info("Fetching ext.to page %d: %s", page, url)
            try:
                html, _cookies, _ua = self._fs.get_page_with_cookies(url)
            except FlareSolverrError as exc:
                logger.error("FlareSolverr error: %s", exc)
                break

            last_html = html
            last_url = url
            page_results = self._parse_html(html)
            all_results.extend(page_results)

            if len(page_results) < _RESULTS_PER_PAGE:
                break

        # Enrich with magnets
        if last_html and all_results:
            self._enrich_with_magnets(all_results, last_url, last_html)

        local_offset = offset % _RESULTS_PER_PAGE if offset > 0 else 0
        return all_results[local_offset:local_offset + limit]

    def _browse_by_categories(self, categories: list[int], offset: int,
                               limit: int) -> list[dict]:
        paths_seen: set = set()
        browse_paths: list[str] = []
        for cat in (categories or []):
            path = CAT_ID_TO_BROWSE_PATH.get(cat)
            if not path:
                parent = (cat // 1000) * 1000
                path = CAT_ID_TO_BROWSE_PATH.get(parent)
            if path and path not in paths_seen:
                paths_seen.add(path)
                browse_paths.append(path)

        if not browse_paths:
            browse_paths = ["/tv/", "/movies/"]

        all_results: list[dict] = []
        last_html = ""
        last_url = ""

        for path in browse_paths:
            page_param = (offset // _RESULTS_PER_PAGE) + 1
            url = self._base + path
            if page_param > 1:
                url += f"?page={page_param}"
            logger.info("Keywordless browse: %s", url)
            try:
                html, _cookies, _ua = self._fs.get_page_with_cookies(url)
            except FlareSolverrError as exc:
                logger.error("FlareSolverr error browsing %s: %s", path, exc)
                continue
            last_html = html
            last_url = url
            page_results = self._parse_html(html)
            all_results.extend(page_results)
            if len(all_results) >= limit:
                break

        if categories:
            all_results = [
                r for r in all_results
                if any(cat_id_matches(rc, fc)
                       for rc in r.get("categories", [8000])
                       for fc in categories)
            ]

        if last_html and all_results:
            self._enrich_with_magnets(all_results, last_url, last_html)

        local_offset = offset % _RESULTS_PER_PAGE if offset > 0 else 0
        return all_results[local_offset:local_offset + limit]

    def _build_url(self, query: str, page: int) -> str:
        params: dict[str, str] = {"sort": "age", "order": "desc", "q": query}
        if self._include_adult:
            params["with_adult"] = "1"
        if page > 1:
            params["page"] = str(page)
        return f"{self._base}/browse/?{urllib.parse.urlencode(params)}"

    def _parse_html(self, html: str) -> list[dict]:
        if not html:
            return []
        if ("522: Connection timed out" in html
                or "524: A timeout occurred" in html):
            logger.warning("ext.to returned a Cloudflare error page")
            return []

        soup = BeautifulSoup(html, "html.parser")
        table = soup.select_one("table.table-striped")
        if not table:
            logger.warning("Result table not found (snippet): %s",
                           html[:400].replace("\n", " "))
            return []

        results = []
        for row in table.select("tbody > tr"):
            try:
                item = self._parse_row(row)
                if item:
                    results.append(item)
            except Exception as exc:
                logger.debug("Skipping malformed row: %s", exc)

        logger.debug("Parsed %d results", len(results))
        return results

    def _parse_row(self, row) -> Optional[dict]:
        td1 = row.select_one("td:nth-child(1)")
        if not td1:
            return None

        title_link = td1.select_one("a.torrent-title-link")
        if not title_link:
            return None

        title = _RE_HTML_TAGS.sub(
            "", title_link.get("data-tooltip", "")
        ).strip()
        if not title:
            title = title_link.get_text(strip=True)
        if not title:
            return None

        details_href = title_link.get("href", "")
        details_url = (
            self._base + details_href
            if details_href and not details_href.startswith("http")
            else details_href or self._base
        )

        # Category
        related = td1.select_one("div.related-posted")
        cat_id = 8000
        if related:
            cat_links = [
                a.get("href", "")
                for a in related.find_all("a")
                if a.get("href", "").startswith("/")
                and not a.get("href", "").startswith("/user/")
            ]
            if len(cat_links) >= 2:
                key = cat_links[0] + cat_links[1]
                cat_id = EXT_TO_CAT_MAP.get(
                    key, EXT_TO_CAT_MAP.get(cat_links[0], 8000)
                )
            elif len(cat_links) == 1:
                cat_id = EXT_TO_CAT_MAP.get(cat_links[0], 8000)

        # Torrent ID
        magnet_btn = td1.select_one("a.search-magnet-btn")
        torrent_id: Optional[int] = None
        if magnet_btn:
            try:
                torrent_id = int(magnet_btn.get("data-id", ""))
            except (ValueError, TypeError):
                pass

        # Size
        td2 = row.select_one("td:nth-child(2)")
        size = 0
        if td2:
            val = td2.select_one("span:not(.add-block)")
            if val:
                size = _parse_size(val.get_text(strip=True))

        # Files
        td3 = row.select_one("td:nth-child(3)")
        files = 1
        if td3:
            val = td3.select_one("span:not(.add-block)")
            if val:
                files = max(1, _parse_int(val.get_text(strip=True)))

        # Date
        td4 = row.select_one("td:nth-child(4)")
        pub_date = ""
        if td4:
            span_t = td4.select_one("span[title]")
            pub_date = _parse_date(
                span_t["title"] if span_t else td4.get_text(strip=True)
            )

        # Seeders
        td5 = row.select_one("td:nth-child(5)")
        seeders = 0
        if td5:
            val = td5.select_one("span.text-success")
            if val:
                seeders = _parse_int(val.get_text(strip=True))

        # Leechers
        td6 = row.select_one("td:nth-child(6)")
        leechers = 0
        if td6:
            val = td6.select_one("span.text-danger")
            if val:
                leechers = _parse_int(val.get_text(strip=True))

        return {
            "title": title,
            "guid": details_url,
            "details_url": details_url,
            "download_url": details_url,
            "magnet_url": "",
            "infohash": "",
            "torrent_id": torrent_id,
            "categories": [cat_id],
            "size": size,
            "files": files,
            "pub_date": pub_date,
            "seeders": seeders,
            "leechers": leechers,
            "peers": seeders + leechers,
        }

    # -------------------------------------------------------------------
    # Magnet enrichment
    # -------------------------------------------------------------------

    def _enrich_with_magnets(self, results: list[dict], page_url: str,
                              page_html: str) -> None:
        page_token, csrf_token = _extract_js_tokens(page_html)
        if not page_token or not csrf_token:
            logger.warning(
                "Missing tokens; magnet links unavailable"
            )
            return

        api_url = self._base + _MAGNET_API_PATH
        token_state = {"page_token": page_token, "csrf_token": csrf_token}
        token_lock = threading.Lock()
        refresh_done = threading.Event()

        def _get_tokens():
            return token_state["page_token"], token_state["csrf_token"]

        def _do_refresh():
            with token_lock:
                if refresh_done.is_set():
                    return
                new_tok, new_csrf = self._fetch_fresh_tokens(page_url)
                if new_tok and new_csrf:
                    token_state["page_token"] = new_tok
                    token_state["csrf_token"] = new_csrf
                    refresh_done.set()

        def fetch_one(item: dict) -> None:
            tid = item.get("torrent_id")
            if not tid:
                return
            tok, csrf = _get_tokens()
            try:
                data = self._fs.post_form(
                    api_url, _build_magnet_post(tid, tok, csrf)
                )
            except FlareSolverrError as exc:
                logger.debug("Magnet API error id=%s: %s", tid, exc)
                return

            # Token expired → refresh once and retry
            if self._looks_like_token_error(data):
                logger.info("Token error id=%s – refreshing", tid)
                _do_refresh()
                tok, csrf = _get_tokens()
                if not tok:
                    return
                try:
                    data = self._fs.post_form(
                        api_url, _build_magnet_post(tid, tok, csrf)
                    )
                except FlareSolverrError as exc:
                    logger.debug("Magnet retry error id=%s: %s", tid, exc)
                    return

            if not data.get("success"):
                return

            infohash = data.get("hash", "").lower()
            magnet_url = data.get("url", "")

            if not magnet_url and infohash:
                if _RE_INFOHASH.fullmatch(infohash):
                    magnet_url = _build_magnet(infohash, item["title"])
                else:
                    infohash = ""

            if magnet_url:
                item["magnet_url"] = magnet_url
                item["download_url"] = magnet_url
                item["infohash"] = infohash
                if infohash:
                    item["guid"] = f"https://ext.to/t/{infohash}/"

        with ThreadPoolExecutor(max_workers=_MAGNET_WORKERS) as pool:
            pool.map(fetch_one, results)

        success_count = sum(1 for r in results if r.get("magnet_url"))
        logger.info("Fetched %d/%d magnet links", success_count, len(results))

    @staticmethod
    def _looks_like_token_error(data: dict) -> bool:
        err = str(data.get("error", "")).lower()
        return not data.get("success") and any(
            kw in err for kw in ("invalid", "expired", "token",
                                 "hmac", "auth", "forbidden", "blocked")
        )

    def _fetch_fresh_tokens(self, page_url: str) -> tuple[str, str]:
        try:
            html, _cookies, _ua = self._fs.get_page_with_cookies(page_url)
        except FlareSolverrError as exc:
            logger.debug("Token refresh failed: %s", exc)
            return "", ""
        return _extract_js_tokens(html)

    # -------------------------------------------------------------------
    # On-demand magnet fetch (used by t=download)
    # -------------------------------------------------------------------

    def fetch_magnet_for_guid(self, guid: str) -> Optional[str]:
        if not guid:
            return None
        if guid.startswith("magnet:"):
            return guid

        m = _RE_GUID_INFOHASH.search(guid)
        if m:
            return f"magnet:?xt=urn:btih:{m.group(1).lower()}"

        if not guid.startswith("http"):
            return None

        logger.info("t=download: fetching detail page %s", guid)
        try:
            html, _cookies, _ua = self._fs.get_page_with_cookies(guid)
        except FlareSolverrError as exc:
            logger.error("t=download: page fetch failed: %s", exc)
            return None

        # Torrent ID from URL slug
        torrent_id: Optional[int] = None
        m = _RE_URL_TORRENT_ID.search(guid.rstrip("/"))
        if m:
            try:
                torrent_id = int(m.group(1))
            except ValueError:
                pass

        # Fallback: button element
        if not torrent_id:
            soup = BeautifulSoup(html, "html.parser")
            magnet_btn = soup.select_one("a.search-magnet-btn[data-id]")
            if magnet_btn:
                try:
                    torrent_id = int(magnet_btn.get("data-id", ""))
                except (ValueError, TypeError):
                    pass

        if not torrent_id:
            logger.warning("t=download: no torrent_id on %s", guid)
            return None

        page_token, csrf_token = _extract_js_tokens(html)
        if not page_token or not csrf_token:
            logger.warning("t=download: missing tokens on %s", guid)
            return None

        post_data = _build_magnet_post(torrent_id, page_token, csrf_token)
        for api_path in (_DETAIL_MAGNET_API_PATH, _MAGNET_API_PATH):
            api_url = self._base + api_path
            try:
                data = self._fs.post_form(api_url, post_data)
            except FlareSolverrError:
                continue

            if data.get("success"):
                infohash = data.get("hash", "").lower()
                magnet_url = data.get("url", "")
                if not magnet_url and _RE_INFOHASH.fullmatch(infohash):
                    magnet_url = _build_magnet(infohash, "")
                if magnet_url:
                    logger.info("t=download: got magnet for id=%s",
                                torrent_id)
                    return magnet_url

        return None


# ---------------------------------------------------------------------------
# Torznab RSS builder
# ---------------------------------------------------------------------------


def _build_caps(bind_url: str) -> str:
    root = ET.Element("caps")
    serv = ET.SubElement(root, "server",
                         title="EXT Torrents (ext.to)",
                         strapline="EXT Torrents Torznab Proxy",
                         url=bind_url, version="1.0")
    ET.SubElement(serv, "limits", max="100", default="25")
    src = ET.SubElement(root, "searching")
    for t in ("search", "tv-search", "movie-search", "music-search",
              "book-search", "audio-search"):
        params = "q"
        if t == "tv-search":
            params = "q,season,ep"
        elif t == "movie-search":
            params = "q"
        ET.SubElement(src, t, available="yes", supportedParams=params)
    cats = ET.SubElement(root, "categories")
    for cid, cname, subs in TORZNAB_CATEGORIES:
        c = ET.SubElement(cats, "category", id=str(cid), name=cname)
        for sid, sname in subs:
            ET.SubElement(c, "subcat", id=str(sid), name=sname)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _build_rss(results: list[dict], offset: int = 0,
               base_url: str = "") -> str:
    NS = "http://torznab.com/schemas/2015/feed"
    rss = ET.Element("rss", version="2.0",
                     attrib={"xmlns:atom": "http://www.w3.org/2005/Atom",
                             "xmlns:torznab": NS})
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = "EXT Torrents"
    ET.SubElement(ch, "link").text = "https://ext.to"
    ET.SubElement(ch, "description").text = "EXT Torrents — Torznab proxy"
    ET.SubElement(ch, "language").text = "en-us"
    ET.SubElement(ch, "category").text = "search"

    now_rfc = datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )

    for r in results:
        item = ET.SubElement(ch, "item")
        ET.SubElement(item, "title").text = r.get("title", "Unknown")
        guid = r.get("guid", r.get("details_url", ""))
        g = ET.SubElement(item, "guid", isPermaLink="true")
        g.text = guid

        magnet = r.get("magnet_url", "")
        details = r.get("details_url", guid)

        if magnet:
            link = details
        elif base_url and details.startswith("http"):
            link = (f"{base_url}/api?t=download&guid="
                    f"{urllib.parse.quote(details)}")
        else:
            link = details

        ET.SubElement(item, "link").text = link
        ET.SubElement(item, "pubDate").text = r.get("pub_date", now_rfc)
        ET.SubElement(item, "size").text = str(r.get("size", 0))
        ET.SubElement(item, "description").text = r.get("title", "")

        encl = ET.SubElement(item, "enclosure",
                             url=(magnet or link),
                             length=str(r.get("size", 0)),
                             type="application/x-bittorrent")

        for name, val in [("category", ",".join(
            str(c) for c in r.get("categories", [8000]))),
                ("size", str(r.get("size", 0))),
                ("seeders", str(r.get("seeders", 0))),
                ("leechers", str(r.get("leechers", 0))),
                ("peers", str(r.get("peers", 0))),
                ("files", str(r.get("files", 1)))]:
            ET.SubElement(item, f"{{{NS}}}attr", name=name, value=val)

        if r.get("infohash"):
            ET.SubElement(item, f"{{{NS}}}attr",
                          name="infohash", value=r["infohash"])
        if magnet:
            ET.SubElement(item, f"{{{NS}}}attr",
                          name="magneturl", value=magnet)

    return ET.tostring(rss, encoding="unicode", xml_declaration=True)


def _error_xml(msg: str) -> str:
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = "EXT Torrents — Error"
    ET.SubElement(ch, "description").text = msg
    return ET.tostring(rss, encoding="unicode", xml_declaration=True)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

_scraper: Optional[ExtToScraper] = None
_server_base_url = f"http://{HOST}:{PORT}"


class ExtTorznabHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path in ("/torznab/api", "/api"):
            self._handle(params)
        elif parsed.path == "/healthz":
            self._ok_plain("OK")
        else:
            self._ok_plain("EXT Torrents Torznab bridge — OK")

    def _handle(self, params: dict):
        t = (params.get("t", [""])[0]).strip()

        if t == "caps":
            xml = _build_caps(_server_base_url)
            return self._send(xml)

        if t in ("search", "tvsearch", "movie", "music", "book"):
            q = params.get("q", [""])[0].strip()
            cat = params.get("cat", [None])[0]
            season_s = params.get("season", [None])[0]
            ep_s = params.get("ep", [None])[0]
            offset = int(params.get("offset", ["0"])[0])
            limit = min(int(params.get("limit", ["25"])[0]), 100)

            categories = []
            if cat:
                for part in cat.split(","):
                    part = part.strip()
                    if part.isdigit():
                        categories.append(int(part))

            season: Optional[int] = None
            if season_s and season_s.isdigit():
                season = int(season_s)
            episode: Optional[int] = None
            if ep_s and ep_s.isdigit():
                episode = int(ep_s)

            try:
                results = _scraper.search(
                    query=q, categories=categories or None,
                    season=season, episode=episode,
                    offset=offset, limit=limit,
                )
            except FlareSolverrError as exc:
                logger.error("FlareSolverr error: %s", exc)
                return self._send(
                    _error_xml(f"FlareSolverr error: {exc}"),
                    status=500,
                )
            except Exception as exc:
                logger.exception("Unexpected error: %s", exc)
                return self._send(
                    _error_xml(f"Internal error: {exc}"),
                    status=500,
                )

            xml = _build_rss(results, offset=offset, base_url=_server_base_url)
            return self._send(xml)

        if t == "download":
            guid = params.get("guid", [""])[0]
            if not guid:
                return self._send(_error_xml("Missing guid"), status=400)

            if guid.startswith("magnet:"):
                return self._redirect(guid)

            magnet = _scraper.fetch_magnet_for_guid(guid)
            if magnet:
                return self._redirect(magnet)

            if guid.startswith("http"):
                return self._redirect(guid)
            return self._send(_error_xml("Could not resolve download URL"),
                              status=404)

        return self._send(
            _error_xml(f"Unknown function: {t}"), status=400
        )

    def _send(self, xml: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type",
                         "application/rss+xml; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(xml.encode("utf-8"))

    def _redirect(self, url: str):
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def _ok_plain(self, text: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(text.encode())

    def log_message(self, fmt, *args):
        logger.info("%s - %s %s", self.client_address[0], args[0], args[1])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    global _scraper, _server_base_url
    logger.info("═══ EXT Torrents Torznab proxy ═══")
    logger.info("FlareSolverr: %s", FLARESOLVERR_URL)
    logger.info("ext.to URL  : %s", EXT_TO_URL)
    logger.info("Include adult: %s", INCLUDE_ADULT)
    logger.info("Listen: %s:%d", HOST, PORT)

    fs = FlareSolverrClient(FLARESOLVERR_URL, FLARESOLVERR_TIMEOUT)
    _scraper = ExtToScraper(EXT_TO_URL, fs, INCLUDE_ADULT)
    _server_base_url = f"http://{HOST}:{PORT}"

    sv = http.server.HTTPServer((HOST, PORT), ExtTorznabHandler)
    logger.info("Torznab endpoint: http://%s:%d/torznab/api", HOST, PORT)
    try:
        sv.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        fs.destroy_session()
        sv.shutdown()


if __name__ == "__main__":
    main()
