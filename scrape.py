#!/usr/bin/env python3
"""Scrape showtimes across 6 SF Bay Area cinemas and emit unified JSON.

Cinemas:
- Roxie (San Francisco)
- Balboa Theater (San Francisco)
- Stanford Theatre (Palo Alto)
- Alamo Drafthouse New Mission (San Francisco)
- Alamo Drafthouse Mountain View
- Alamo Drafthouse Valley Fair (Santa Clara)

Each row has: title, year, cinema, datetime, ticket_url.
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor

from scrapers import alamo, balboa, roxie, stanford

SCRAPERS = [alamo, balboa, roxie, stanford]


def main():
    with ThreadPoolExecutor(max_workers=len(SCRAPERS)) as pool:
        futures = [pool.submit(s.fetch) for s in SCRAPERS]
        rows = [row for f in futures for row in f.result()]

    rows.sort(key=lambda r: (r["datetime"], r["cinema"], r["title"]))
    json.dump(rows, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
