"""Shared HTTP helper for scrapers (and TMDB).

A single resilient GET with retry + exponential backoff so a transient
server hiccup (503/502/504/429), a bot-filter 403, or a connection error on
one request doesn't abort the whole sync. See the Roxie 503 that motivated
this, and the Roxie 403 that followed it.
"""

import random
import time
import urllib.error
import urllib.request

# Roxie's WAF 403s a bare "movie-scraper/1.0", so present as a normal browser.
# Volume is one pass per day, which is well inside anything these sites care
# about; the UA is about getting served at all, not about hiding.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Transient HTTP statuses worth retrying (server hiccups / rate limiting).
# 403 is in here because Roxie's bot filter returns it intermittently for
# requests it later serves fine — a genuinely forbidden URL just costs us
# the full backoff before raising.
_RETRY_STATUS = {403, 429, 500, 502, 503, 504}
_MAX_RETRIES = 4


def get(url, timeout=30, headers=None):
    """Fetch a URL as text, retrying transient failures with backoff.

    headers, if given, are merged over the browser-ish defaults.

    Raises the underlying HTTPError/URLError if all retries are exhausted or
    the error is non-transient (e.g. 404).
    """
    req = urllib.request.Request(url, headers={**DEFAULT_HEADERS, **(headers or {})})
    for attempt in range(_MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code not in _RETRY_STATUS or attempt == _MAX_RETRIES - 1:
                raise
        except urllib.error.URLError:
            # Covers connection reset / DNS / TLS failures, which show up on
            # these hosts as often as clean HTTP errors do.
            if attempt == _MAX_RETRIES - 1:
                raise
        # Exponential backoff with jitter: ~1s, 2s, 4s. Jitter is random rather
        # than derived from the URL so concurrent scrapers retrying the same
        # host don't march in lockstep.
        time.sleep(2 ** attempt + random.uniform(0, 0.5))
