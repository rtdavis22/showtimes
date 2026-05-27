#!/usr/bin/env python3
"""Scrape showtimes across 4 SF Bay Area cinemas and emit unified JSON.

Cinemas:
- Roxie (San Francisco)
- Alamo Drafthouse New Mission (San Francisco)
- Alamo Drafthouse Mountain View
- Alamo Drafthouse Valley Fair (Santa Clara)

Each row has: title, year, cinema, datetime_local, ticket_url.
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor

from scrapers import alamo, roxie


def main():
    with ThreadPoolExecutor(max_workers=2) as pool:
        alamo_future = pool.submit(alamo.fetch)
        roxie_future = pool.submit(roxie.fetch)
        rows = alamo_future.result() + roxie_future.result()

    rows.sort(key=lambda r: (r["datetime"], r["cinema"], r["title"]))
    json.dump(rows, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
