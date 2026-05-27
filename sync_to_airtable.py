#!/usr/bin/env python3
"""Sync scraped showtimes into an Airtable table.

Env vars required:
    AIRTABLE_PAT       Personal access token with data.records:read/write scope
    AIRTABLE_BASE_ID   e.g. appXXXXXXXXXXXXXX
    AIRTABLE_TABLE     Table name or ID (default: "Showtimes")

Expected schema (create the table once, then this script keeps it in sync):
    Title       single line text
    Year        number (integer)
    Cinema      single select (4 options listed below)
    Showtime    date with time (display tz: America/Los_Angeles)
    Ticket URL  url                                  <-- upsert key
    Status      single select: Active, Removed
    Last Seen   date with time

Cinema options:
    Roxie
    Alamo Drafthouse New Mission
    Alamo Drafthouse Mountain View
    Alamo Drafthouse Valley Fair

Sync semantics:
    * Each scraped row is upserted by Ticket URL with Status=Active, Last Seen=now.
    * Any record in Airtable whose Ticket URL is NOT in the current scrape is
      patched to Status=Removed (past showtimes and pulled sessions are kept).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from scrapers import alamo, roxie

API_BASE = "https://api.airtable.com/v0"
BATCH_SIZE = 10  # Airtable limit
REQUEST_PAUSE = 0.25  # seconds between requests; stays under 5 req/sec


def _env(name, default=None):
    v = os.environ.get(name, default)
    if v is None:
        sys.exit(f"Missing required env var: {name}")
    return v


def _request(method, url, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        method=method,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            sys.stderr.write(f"{method} {url} → {e.code}: {e.read().decode()[:500]}\n")
            raise


def fetch_existing(base_id, table, token):
    """Return {ticket_url: record_id, ...} for every record in the table."""
    url = f"{API_BASE}/{base_id}/{urllib.parse.quote(table)}"
    out = {}
    params = {"pageSize": "100", "fields[]": "Ticket URL"}
    offset = None
    while True:
        q = dict(params)
        if offset:
            q["offset"] = offset
        # urlencode doesn't repeat fields[] from a dict; build manually
        qs = "&".join(
            f"{urllib.parse.quote(k)}={urllib.parse.quote(v)}"
            for k, v in q.items()
        )
        page = _request("GET", f"{url}?{qs}", token)
        for rec in page.get("records", []):
            tu = rec.get("fields", {}).get("Ticket URL")
            if tu:
                out[tu] = rec["id"]
        offset = page.get("offset")
        if not offset:
            break
        time.sleep(REQUEST_PAUSE)
    return out


def upsert_rows(base_id, table, token, rows, now_iso):
    """Upsert scraped rows keyed on Ticket URL. Returns count upserted."""
    url = f"{API_BASE}/{base_id}/{urllib.parse.quote(table)}"
    total = 0
    for i in range(0, len(rows), BATCH_SIZE):
        chunk = rows[i:i + BATCH_SIZE]
        records = [
            {
                "fields": {
                    "Title": r["title"],
                    "Year": r["year"],
                    "Cinema": r["cinema"],
                    "Showtime": r["datetime"],
                    "Ticket URL": r["ticket_url"],
                    "Status": "Active",
                    "Last Seen": now_iso,
                }
            }
            for r in chunk
        ]
        body = {
            "performUpsert": {"fieldsToMergeOn": ["Ticket URL"]},
            "records": records,
            "typecast": True,
        }
        _request("PATCH", url, token, body)
        total += len(chunk)
        time.sleep(REQUEST_PAUSE)
    return total


def mark_removed(base_id, table, token, record_ids):
    """PATCH the given record IDs to Status=Removed."""
    if not record_ids:
        return 0
    url = f"{API_BASE}/{base_id}/{urllib.parse.quote(table)}"
    total = 0
    for i in range(0, len(record_ids), BATCH_SIZE):
        chunk = record_ids[i:i + BATCH_SIZE]
        body = {
            "records": [{"id": rid, "fields": {"Status": "Removed"}} for rid in chunk],
            "typecast": True,
        }
        _request("PATCH", url, token, body)
        total += len(chunk)
        time.sleep(REQUEST_PAUSE)
    return total


def main():
    token = _env("AIRTABLE_PAT")
    base_id = _env("AIRTABLE_BASE_ID")
    table = os.environ.get("AIRTABLE_TABLE", "Showtimes")

    print("scraping…", file=sys.stderr)
    rows = alamo.fetch() + roxie.fetch()
    # Deduplicate on ticket_url in case a source emits the same URL twice.
    by_url = {}
    for r in rows:
        by_url[r["ticket_url"]] = r
    rows = list(by_url.values())
    print(f"  scraped {len(rows)} unique showtimes", file=sys.stderr)

    print("fetching existing Airtable records…", file=sys.stderr)
    existing = fetch_existing(base_id, table, token)
    print(f"  {len(existing)} existing", file=sys.stderr)

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    print("upserting…", file=sys.stderr)
    n_up = upsert_rows(base_id, table, token, rows, now_iso)

    current_urls = {r["ticket_url"] for r in rows}
    removed_ids = [rid for url, rid in existing.items() if url not in current_urls]
    print(f"marking {len(removed_ids)} as Removed…", file=sys.stderr)
    n_rm = mark_removed(base_id, table, token, removed_ids)

    print(f"done. upserted={n_up} removed={n_rm}", file=sys.stderr)


if __name__ == "__main__":
    main()
