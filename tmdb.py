"""Minimal TMDB lookup for enriching scraped titles.

Uses TMDB's /search/movie endpoint. Authenticates with a v4 Bearer token
(create at themoviedb.org → Settings → API → API Read Access Token).
"""

import json
import re
import urllib.parse
import urllib.request

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
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        results = json.load(resp).get("results", [])
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
