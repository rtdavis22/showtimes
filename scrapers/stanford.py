"""Stanford Theatre (Palo Alto) scraper.

The Stanford Theatre's site is static XHTML calendar pages. The home page
links to each currently-active calendar (e.g. "Summer 2026.html"). Each
calendar lists date ranges with one or more films per range, every film
tagged with an IMDB link, year, and showtimes.

Showtime formats observed:
    "7:30"                          -> daily at 7:30 PM
    "5:55, 10:00"                   -> daily at both times
    "7:30 (plus 3:35 Sat/Sun)"      -> daily 7:30; +3:35 on Sat/Sun
    "(Sat/Sun only) 3:30"           -> only Sat/Sun (rare but handled)

Stanford doesn't sell tickets online — ticket_url is a synthetic anchor
URL pointing to the calendar page so the field stays unique per showtime
(needed as the upsert key) and clicking it lands on the right calendar.
"""

import json
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from html import unescape
from zoneinfo import ZoneInfo

from scrapers._http import get as _http_get

BASE_URL = "https://www.stanfordtheatre.org/"
CINEMA_NAME = "Stanford Theatre"

# Cinemas this scraper is responsible for; see scrapers.fetch_all.
CINEMAS = (CINEMA_NAME,)
_PT = ZoneInfo("America/Los_Angeles")

_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        [None, "January", "February", "March", "April", "May", "June",
         "July", "August", "September", "October", "November", "December"]
    ) if m
}

_DOW = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}

_P_TAG_RE = re.compile(r'<p(?:\s+class="([^"]*)")?[^>]*>(.*?)</p>', re.S)
_IMDB_RE = re.compile(r'href="https?://(?:www\.)?imdb\.com/title/(tt\d+)"', re.I)
_FILM_LINK_RE = re.compile(r'<a[^>]*>(.*?)</a>(.*)', re.S)
_TIME_RE = re.compile(r'\b(\d{1,2}):(\d{2})\b')
_YEAR_IN_TITLE_RE = re.compile(r'\((\d{4})\)\s*$')
_YEAR_ANYWHERE_RE = re.compile(r'\((\d{4})\)')
_CALENDAR_LINKS_RE = re.compile(r'href="(calendars/[^"#]+\.html)"', re.I)
_PLUS_QUAL_RE = re.compile(
    r'\(\s*plus\s+(\d{1,2}:\d{2}(?:\s*,\s*\d{1,2}:\d{2})*)\s+([A-Za-z/]+)\s*\)',
    re.I,
)
_ONLY_QUAL_RE = re.compile(
    r'\(\s*([A-Za-z/]+)\s+only\s*\)', re.I,
)


def _current_calendar_urls():
    """Return absolute URLs of the calendars linked from the home page."""
    home = _http_get(BASE_URL)
    out = []
    for path in _CALENDAR_LINKS_RE.findall(home):
        if path.lower().endswith("index.html"):  # skip archive index
            continue
        # Percent-encode the filename portion (spaces etc.) before joining.
        dir_part, _, file_part = path.rpartition("/")
        safe_path = f"{dir_part}/{urllib.parse.quote(file_part)}" if dir_part else urllib.parse.quote(file_part)
        out.append(urllib.parse.urljoin(BASE_URL, safe_path))
    # Dedupe, preserve order.
    seen = set()
    return [u for u in out if not (u in seen or seen.add(u))]


def _series_year(url):
    """Best-effort year extraction from the filename ('Summer 2026.html' -> 2026)."""
    name = urllib.parse.unquote(url.rsplit("/", 1)[-1])
    m = re.search(r'(\d{4})', name)
    return int(m.group(1)) if m else date.today().year


def _parse_date_range(text, default_year):
    """'April 30-May 1' -> [date(Y,4,30), date(Y,5,1)]; 'June 3-7' -> 5 dates.

    Returns [] if it can't parse the range.
    """
    text = text.replace("\xa0", " ").replace("–", "-").replace("—", "-").strip()
    # "Month D-D" or "Month D-Month D"
    m = re.match(
        r"([A-Za-z]+)\s+(\d{1,2})\s*-\s*(?:([A-Za-z]+)\s+)?(\d{1,2})$",
        text,
    )
    if not m:
        return []
    m1, d1, m2, d2 = m.group(1), int(m.group(2)), m.group(3) or m.group(1), int(m.group(4))
    if m1.lower() not in _MONTHS or m2.lower() not in _MONTHS:
        return []
    start = date(default_year, _MONTHS[m1.lower()], d1)
    end = date(default_year, _MONTHS[m2.lower()], d2)
    # Handle year wrap (e.g. Dec 28 - Jan 3).
    if end < start:
        end = date(default_year + 1, _MONTHS[m2.lower()], d2)
    out, d = [], start
    while d <= end:
        out.append(d)
        d += timedelta(days=1)
    return out


def _parse_dow_list(token):
    """'Sat/Sun' -> {5, 6}. Returns None if can't parse."""
    days = set()
    for part in re.split(r'[/,]', token):
        key = part.strip()[:3].title()
        if key not in _DOW:
            return None
        days.add(_DOW[key])
    return days or None


def _to_pm_time(h, mn):
    """Stanford shows are afternoon/evening; map e.g. (3, 30) -> 15:30."""
    if h < 12:
        h += 12
    return h, mn


def _parse_showtimes(line, date_range):
    """Given the trailing text of a film row + the date range, yield datetimes.

    Returns list of (datetime, source_label) where source_label is the raw
    snippet that produced it (for debugging).
    """
    out = []

    # Extract "(plus 3:35 Sat/Sun)" qualifiers first, removing them from the line.
    qualified = []  # list of (times list, dow_set)
    def take_plus(m):
        times = [(int(a), int(b)) for a, b in _TIME_RE.findall(m.group(1))]
        dows = _parse_dow_list(m.group(2))
        if dows:
            qualified.append((times, dows))
        return " "
    line = _PLUS_QUAL_RE.sub(take_plus, line)

    # Extract "(Sat/Sun only)" — applies to whatever times remain in the line.
    only_dows = None
    om = _ONLY_QUAL_RE.search(line)
    if om:
        only_dows = _parse_dow_list(om.group(1))
        line = _ONLY_QUAL_RE.sub(" ", line)

    base_times = [(int(a), int(b)) for a, b in _TIME_RE.findall(line)]

    for d in date_range:
        for h, mn in base_times:
            if only_dows is not None and d.weekday() not in only_dows:
                continue
            ph, pm = _to_pm_time(h, mn)
            out.append(datetime(d.year, d.month, d.day, ph, pm, tzinfo=_PT))
        for times, dows in qualified:
            if d.weekday() not in dows:
                continue
            for h, mn in times:
                ph, pm = _to_pm_time(h, mn)
                out.append(datetime(d.year, d.month, d.day, ph, pm, tzinfo=_PT))
    return out


def _parse_calendar(url, html):
    """Parse a single calendar page; return list of unified showtime rows."""
    year = _series_year(url)
    rows = []
    cur_dates = []
    for cls, body in _P_TAG_RE.findall(html):
        cls = (cls or "").strip()
        if cls == "date":
            txt = unescape(re.sub(r'<[^>]+>', ' ', body)).replace("\xa0", " ").strip()
            cur_dates = _parse_date_range(txt, year)
            continue
        if not cur_dates:
            continue
        link = _FILM_LINK_RE.search(body)
        imdb_m = _IMDB_RE.search(body)
        if not link or not imdb_m:
            continue
        raw_title = unescape(re.sub(r'<[^>]+>', '', link.group(1))).replace("\xa0", " ").strip()
        rest = unescape(link.group(2)).replace("\xa0", " ")
        # Year may live inside the link text ("North by Northwest (1959)") or
        # just after it ("The Big Sleep</a> (1946) ..."). Prefer in-title.
        film_year = None
        ym = _YEAR_IN_TITLE_RE.search(raw_title)
        if ym:
            film_year = int(ym.group(1))
            raw_title = _YEAR_IN_TITLE_RE.sub("", raw_title).strip()
        else:
            ym = _YEAR_ANYWHERE_RE.search(rest)
            if ym:
                film_year = int(ym.group(1))
                rest = _YEAR_ANYWHERE_RE.sub(" ", rest, count=1)

        for dt in _parse_showtimes(rest, cur_dates):
            anchor = f"{imdb_m.group(1)}-{dt.strftime('%Y%m%dT%H%M')}"
            rows.append({
                "title": raw_title,
                "year": film_year,
                "cinema": CINEMA_NAME,
                "datetime": dt.isoformat(timespec="seconds"),
                "ticket_url": f"{url}#{anchor}",
            })
    return rows


def fetch():
    urls = _current_calendar_urls()
    rows = []
    if not urls:
        return rows
    with ThreadPoolExecutor(max_workers=min(4, len(urls))) as pool:
        pages = list(pool.map(_http_get, urls))
    for u, html in zip(urls, pages):
        rows.extend(_parse_calendar(u, html))
    # De-dupe within Stanford (same film can appear in overlapping calendars).
    return list({r["ticket_url"]: r for r in rows}.values())


if __name__ == "__main__":
    import sys
    json.dump(fetch(), sys.stdout, indent=2)
    sys.stdout.write("\n")
