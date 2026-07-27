"""Minimal TMDB lookup for enriching scraped titles.

Uses TMDB's /search/movie endpoint. Authenticates with a v4 Bearer token
(create at themoviedb.org → Settings → API → API Read Access Token).
"""

import json
import re
import urllib.parse

from scrapers._http import get as _http_get

_SEARCH_URL = "https://api.themoviedb.org/3/search/movie"
_POSTER_BASE = "https://image.tmdb.org/t/p/w500"

# Strip parenthesized format markers like "(35mm)", "(70mm)", "(4K Restoration)"
# before querying — TMDB matches on canonical title.
_FORMAT_SUFFIX = re.compile(
    r"\s*\([^)]*(?:mm|4K|IMAX|Restoration|Digital|Print)[^)]*\)\s*$",
    re.IGNORECASE,
)


def _clean(title):
    prev = None
    while prev != title:
        prev = title
        title = _FORMAT_SUFFIX.sub("", title).strip()
    return title


def search(title, token, year=None):
    """Return {tmdb_id, title, year, poster_url} for the best match, or None."""
    query = _clean(title)
    params = {"query": query, "include_adult": "false"}
    if year:
        params["year"] = str(year)
    url = f"{_SEARCH_URL}?{urllib.parse.urlencode(params)}"
    # Shared helper so TMDB gets the same retry/backoff as the scrapers; an
    # unretried connection reset here used to abort the entire sync.
    body = _http_get(
        url,
        timeout=15,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    results = json.loads(body).get("results", [])
    if not results:
        return None
    best = results[0]
    release = best.get("release_date") or ""
    return {
        "tmdb_id": best["id"],
        "title": best.get("title") or best.get("original_title") or query,
        "year": int(release[:4]) if release[:4].isdigit() else None,
        "poster_url": f"{_POSTER_BASE}{best['poster_path']}" if best.get("poster_path") else None,
    }
