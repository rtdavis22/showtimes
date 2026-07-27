"""Roxie Theater (San Francisco) scraper.

Pulls the list of currently-listed films from Roxie's "upcoming events" RSS
feed, then fetches each film page concurrently and parses its #showtimes
section. Ticket links go to Roxie's Veezi ticketing platform.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html import unescape
from zoneinfo import ZoneInfo

from scrapers._http import get as _http_get

_PT = ZoneInfo("America/Los_Angeles")

RSS_URL = "https://roxie.com/upcoming_events"
CINEMA_NAME = "Roxie"

# Cinemas this scraper is responsible for; see scrapers.fetch_all.
CINEMAS = (CINEMA_NAME,)
MAX_WORKERS = 4


def _list_film_urls():
    xml = _http_get(RSS_URL)
    urls = re.findall(r"<link>(https://roxie\.com/film/[^<]+)</link>", xml)
    # The feed repeats most films (currently 246 listings for 89 real
    # showtimes). Dedupe preserving feed order: it drops ~3x of the page
    # fetches we aim at the WAF that keeps 403ing us.
    return list(dict.fromkeys(urls))


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
        # Not-yet-on-sale showtimes render as href="#". Falling back to a bare
        # film_url isn't enough either — a film's showtimes would then share
        # one Ticket URL, and that field is the upsert key, so they'd collapse
        # into a single Airtable record. Qualify with the showtime, as the
        # Stanford scraper does; servers ignore the fragment.
        ticket = unescape(u.group(1)).strip() if u else ""
        if not ticket or ticket.startswith(("#", "javascript:")):
            ticket = f"{film_url}#{dt.strftime('%Y%m%dT%H%M')}"

        rows.append({
            "title": title,
            "year": year,
            "cinema": CINEMA_NAME,
            "datetime": dt.replace(tzinfo=_PT).isoformat(timespec="seconds"),
            "ticket_url": ticket,
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
