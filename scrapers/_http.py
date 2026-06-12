"""Shared HTTP helper for scrapers.

A single resilient GET with retry + exponential backoff so a transient
server hiccup (503/502/504/429) or connection error on one request doesn't
abort the whole sync. See the Roxie 503 that motivated this.
"""

import time
import urllib.error
import urllib.request

USER_AGENT = "movie-scraper/1.0"

# Transient HTTP statuses worth retrying (server hiccups / rate limiting).
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 4


def get(url, timeout=30):
    """Fetch a URL as text, retrying transient failures with backoff.

    Raises the underlying HTTPError/URLError if all retries are exhausted or
    the error is non-transient (e.g. 404).
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(_MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code not in _RETRY_STATUS or attempt == _MAX_RETRIES - 1:
                raise
        except urllib.error.URLError:
            if attempt == _MAX_RETRIES - 1:
                raise
        # Exponential backoff: 1s, 2s, 4s … with a little jitter from the URL.
        time.sleep(2 ** attempt + (len(url) % 5) * 0.1)
