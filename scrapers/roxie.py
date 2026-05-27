"""Roxie Theater (San Francisco) scraper.

Pulls the list of currently-listed films from Roxie's "upcoming events" RSS
feed, then fetches each film page concurrently and parses its #showtimes
section. Ticket links go to Roxie's Veezi ticketing platform.
"""

import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html import unescape
from zoneinfo import ZoneInfo

_PT = ZoneInfo("America/Los_Angeles")

RSS_URL = "https://roxie.com/upcoming_events"
USER_AGENT = "movie-scraper/1.0"
CINEMA_NAME = "Roxie"
MAX_WORKERS = 8


def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _list_film_urls():
    xml = _http_get(RSS_URL)
    return re.findall(r"<link>(https://roxie\.com/film/[^<]+)</link>", xml)


_SHOWTIME_RE = re.compile(
    r'<div class="content-film__showtimes-list-item">(.*?)</div>\s*</div>\s*</div>',
    re.S,
)
_DATE_RE = re.compile(r'content-film__showtimes__date">\s*([^<]+?)\s*</span>')
_TIME_RE = re.compile(r'content-film__showtimes__time">\s*([^<]+?)\s*</span>')
_TICKET_RE = re.compile(r'content-film__showtimes-tickets.*?href="([^"]+)"', re.S)
_YEAR_RE = re.compile(r'\bYear\s+((?:19|20)\d{2})\b')
_TITLE_RE = re.compile(r'<h1[^>]*>\s*(.*?)\s*</h1>', re.S)


def _parse_film_page(html, film_url):
    title_m = _TITLE_RE.search(html)
    title = unescape(re.sub(r"<[^>]+>", "", title_m.group(1))).strip() if title_m else None
    if not title:
        return []

    year_m = _YEAR_RE.search(re.sub(r"<[^>]+>", " ", html))
    year = int(year_m.group(1)) if year_m else None

    rows = []
    for block in _SHOWTIME_RE.finditer(html):
        b = block.group(1)
        d, t, u = _DATE_RE.search(b), _TIME_RE.search(b), _TICKET_RE.search(b)
        if not (d and t):
            continue
        try:
            dt = datetime.strptime(f"{d.group(1)} {t.group(1)}", "%B %d, %Y %I:%M %p")
        except ValueError:
            continue
        rows.append({
            "title": title,
            "year": year,
            "cinema": CINEMA_NAME,
            "datetime": dt.replace(tzinfo=_PT).isoformat(timespec="seconds"),
            "ticket_url": unescape(u.group(1)) if u else film_url,
        })
    return rows


def fetch():
    film_urls = _list_film_urls()
    rows = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for film_url, html in zip(film_urls, pool.map(_http_get, film_urls)):
            rows.extend(_parse_film_page(html, film_url))
    return rows


if __name__ == "__main__":
    import sys
    json.dump(fetch(), sys.stdout, indent=2)
    sys.stdout.write("\n")
