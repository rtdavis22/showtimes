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

import scrapers


def main():
    log = lambda msg: print(msg, file=sys.stderr)
    rows, _covered, failed = scrapers.fetch_all(log)

    rows.sort(key=lambda r: (r["datetime"], r["cinema"], r["title"]))
    json.dump(rows, sys.stdout, indent=2)
    sys.stdout.write("\n")

    # Emit whatever we got, but exit non-zero so a partial scrape is visible
    # to whatever is calling us rather than passing silently as success.
    if failed:
        sys.exit(f"{len(failed)} scraper(s) failed: " + ", ".join(n for n, _ in failed))


if __name__ == "__main__":
    main()
