"""Balboa Theater (San Francisco) scraper.

Balboa's ticketing lives on Veezi (same platform as the Roxie's, different
site token). The "sessions" page renders all upcoming films + showtimes
as static HTML — no separate API needed.

Year is inferred: the page renders dates as "Friday 29, May" without a
year. We assume current year and bump to next year for dates that would
land more than a week in the past.
"""

import json
import re
from datetime import date, datetime, timedelta
from html import unescape
from zoneinfo import ZoneInfo

from scrapers._http import get as _http_get

SITE_TOKEN = "52wkfzmjpwjjfpz3ye7tz8wscg"
SESSIONS_URL = f"https://ticketing.uswest.veezi.com/sessions/?siteToken={SITE_TOKEN}"
CINEMA_NAME = "Balboa Theater"
_PT = ZoneInfo("America/Los_Angeles")

_FILM_SPLIT = re.compile(r'<div\s+class="film\s')
_TITLE_RE = re.compile(r'<h3 class="title">\s*(.*?)\s*</h3>', re.S)
_DATE_BLOCK_RE = re.compile(
    r'<h4 class="date">\s*([^<]+?)\s*</h4>(.*?)</ul>', re.S,
)
_SESSION_RE = re.compile(
    r'<a\s+href="([^"]+/purchase/\d+[^"]*)"[^>]*>\s*<time[^>]*>\s*([^<]+?)\s*</time>',
    re.S,
)


def _infer_year(month, day, today):
    """Pick a year for a (month, day) so the resulting date isn't long-past."""
    for year in (today.year, today.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if d >= today - timedelta(days=7):
            return year
    return today.year


def _parse_datetime(date_str, time_str, today):
    """date_str like 'Friday 29, May'; time_str like '7:00 PM'."""
    parts = re.match(r"\w+\s+(\d{1,2}),\s+(\w+)", date_str)
    if not parts:
        return None
    day, month_name = int(parts.group(1)), parts.group(2)
    try:
        month = datetime.strptime(month_name, "%B").month
    except ValueError:
        return None
    year = _infer_year(month, day, today)
    try:
        return datetime.strptime(
            f"{year}-{month:02d}-{day:02d} {time_str}",
            "%Y-%m-%d %I:%M %p",
        ).replace(tzinfo=_PT)
    except ValueError:
        return None


def fetch():
    html = _http_get(SESSIONS_URL)
    today = date.today()
    rows = []

    chunks = _FILM_SPLIT.split(html)[1:]  # drop the pre-first-film header
    for chunk in chunks:
        t = _TITLE_RE.search(chunk)
        if not t:
            continue
        title = unescape(re.sub(r"<[^>]+>", "", t.group(1))).strip()
        if not title:
            continue
        for date_match in _DATE_BLOCK_RE.finditer(chunk):
            date_str, sessions_html = date_match.group(1), date_match.group(2)
            for url, time_str in _SESSION_RE.findall(sessions_html):
                dt = _parse_datetime(date_str, time_str, today)
                if dt is None:
                    continue
                rows.append({
                    "title": title,
                    "year": None,  # not in Veezi markup; TMDB enrichment fills this
                    "cinema": CINEMA_NAME,
                    "datetime": dt.isoformat(timespec="seconds"),
                    "ticket_url": unescape(url),
                })
    return rows


if __name__ == "__main__":
    import sys
    json.dump(fetch(), sys.stdout, indent=2)
    sys.stdout.write("\n")
