#!/usr/bin/env python3
"""Sync scraped showtimes (+ enriched Movies) into Airtable.

Env vars:
    AIRTABLE_PAT          PAT with data.records:read/write on the base
    AIRTABLE_BASE_ID      e.g. appXXXXXXXXXXXXXX
    AIRTABLE_TABLE        Showtimes table name (default: "Showtimes")
    AIRTABLE_MOVIES_TABLE Movies table name    (default: "Movies")
    TMDB_BEARER_TOKEN     TMDB v4 API Read Access Token

Schema:
    Movies table
        Title          single line text  (canonical from TMDB)
        Year           number (integer)
        TMDB ID        number (integer)   <-- upsert key, must be unique
        Poster URL     url
        Source Titles  long text          (newline-separated, used as a lookup
                                           cache so we don't re-query TMDB)
        Showtimes      linked record      (reverse of Showtimes.Movie)

    Showtimes table
        Title       single line text     (as advertised by the cinema)
        Year        number (integer)
        Cinema      single select: Roxie, Balboa Theater, Stanford Theatre,
                                   Alamo Drafthouse New Mission,
                                   Alamo Drafthouse Mountain View,
                                   Alamo Drafthouse Valley Fair
        Showtime    date with time (display tz: America/Los_Angeles)
        Ticket URL  url                  <-- upsert key, must be unique
        Status      single select: Active, Removed
        Last Seen   date with time
        Movie       linked record -> Movies

Sync semantics:
    1. Scrape all showtimes.
    2. For each unique source title:
       - If it appears in any Movie's Source Titles → reuse that TMDB ID.
       - Else → query TMDB. Cache the (source_title → tmdb_id) result by
         appending to that Movie's Source Titles.
       - If TMDB returns no match → skip the Movie link (link stays empty).
    3. Upsert Movies (keyed on TMDB ID).
    4. Upsert Showtimes (keyed on Ticket URL), setting Movie link.
    5. Any Showtime whose Ticket URL is absent from the current scrape gets
       Status=Removed; the record itself is kept for history.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import scrapers
import tmdb

API_BASE = "https://api.airtable.com/v0"
BATCH_SIZE = 10  # Airtable bulk-write limit
REQUEST_PAUSE = 0.25  # ~4 req/sec, under Airtable's 5/sec/base cap


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


def _table_url(base_id, table):
    return f"{API_BASE}/{base_id}/{urllib.parse.quote(table)}"


def _paginate(base_id, table, token, fields):
    url = _table_url(base_id, table)
    base_params = [("pageSize", "100")] + [("fields[]", f) for f in fields]
    offset = None
    while True:
        params = list(base_params)
        if offset:
            params.append(("offset", offset))
        qs = urllib.parse.urlencode(params)
        page = _request("GET", f"{url}?{qs}", token)
        for rec in page.get("records", []):
            yield rec
        offset = page.get("offset")
        if not offset:
            break
        time.sleep(REQUEST_PAUSE)


def fetch_existing_showtimes(base_id, table, token):
    """Return {ticket_url: {id, cinema, status}} for every Showtime row.

    Cinema and Status come back too because expiry is scoped per-cinema and
    skips rows already marked Removed.
    """
    out = {}
    for rec in _paginate(base_id, table, token, ["Ticket URL", "Cinema", "Status"]):
        f = rec.get("fields", {})
        tu = f.get("Ticket URL")
        if tu:
            out[tu] = {
                "id": rec["id"],
                "cinema": f.get("Cinema"),
                "status": f.get("Status"),
            }
    return out


def fetch_existing_movies(base_id, table, token):
    """Return (tmdb_id -> {record_id, source_titles set}, source_title_lower -> tmdb_id)."""
    movies = {}
    by_source_title = {}
    for rec in _paginate(base_id, table, token, ["TMDB ID", "Source Titles"]):
        f = rec.get("fields", {})
        tmdb_id = f.get("TMDB ID")
        if not tmdb_id:
            continue
        src = {s.strip() for s in (f.get("Source Titles") or "").splitlines() if s.strip()}
        movies[int(tmdb_id)] = {"record_id": rec["id"], "source_titles": src}
        for s in src:
            by_source_title[s.lower()] = int(tmdb_id)
    return movies, by_source_title


def resolve_movies(rows, existing_movies, by_source_title, tmdb_token):
    """For each unique source title in rows, find or create a TMDB match.

    Returns (source_title_to_tmdb_id, tmdb_id_to_movie_data, unmatched_titles).
    tmdb_id_to_movie_data has all TMDB-matched movies we need to upsert
    (newly discovered + existing-with-newly-added source titles).
    """
    unique_sources = {}  # title -> sample year (Roxie may know it)
    for r in rows:
        unique_sources.setdefault(r["title"], r.get("year"))

    src_to_tmdb = {}
    to_upsert = {}  # tmdb_id -> {tmdb_id, title, year, poster_url, source_titles set}
    unmatched = []

    for source_title, hint_year in unique_sources.items():
        # Cache hit: this source title already lives in some Movie's Source Titles.
        if source_title.lower() in by_source_title:
            src_to_tmdb[source_title] = by_source_title[source_title.lower()]
            continue

        try:
            match = tmdb.search(source_title, tmdb_token, year=hint_year)
        except (urllib.error.URLError, ValueError) as e:
            # Enrichment is best-effort per the module docstring: on a TMDB
            # failure the Movie link just stays empty and the showtime still
            # syncs. Retries are already exhausted inside tmdb.search.
            print(f"  tmdb lookup failed for {source_title!r}: {e}", file=sys.stderr)
            unmatched.append(source_title)
            continue
        if match is None:
            unmatched.append(source_title)
            continue

        tmdb_id = match["tmdb_id"]
        src_to_tmdb[source_title] = tmdb_id

        if tmdb_id in existing_movies:
            # Existing Movie; just append the new source title.
            entry = existing_movies[tmdb_id]
            new_sources = entry["source_titles"] | {source_title}
            if new_sources != entry["source_titles"]:
                to_upsert[tmdb_id] = {
                    **match,
                    "source_titles": new_sources,
                }
        else:
            to_upsert[tmdb_id] = {
                **match,
                "source_titles": {source_title},
            }

    return src_to_tmdb, to_upsert, unmatched


def upsert_movies(base_id, table, token, to_upsert):
    """Upsert Movie records keyed on TMDB ID. Returns {tmdb_id: record_id}."""
    url = _table_url(base_id, table)
    items = list(to_upsert.values())
    out = {}
    for i in range(0, len(items), BATCH_SIZE):
        chunk = items[i:i + BATCH_SIZE]
        records = [
            {
                "fields": {
                    "Title": m["title"],
                    "Year": m["year"],
                    "TMDB ID": m["tmdb_id"],
                    "Poster URL": m["poster_url"],
                    "Source Titles": "\n".join(sorted(m["source_titles"])),
                }
            }
            for m in chunk
        ]
        body = {
            "performUpsert": {"fieldsToMergeOn": ["TMDB ID"]},
            "records": records,
            "typecast": True,
        }
        resp = _request("PATCH", url, token, body)
        for rec in resp.get("records", []):
            tmdb_id = rec.get("fields", {}).get("TMDB ID")
            if tmdb_id is not None:
                out[int(tmdb_id)] = rec["id"]
        time.sleep(REQUEST_PAUSE)
    return out


def upsert_showtimes(base_id, table, token, rows, src_to_movie_record, now_iso):
    """Upsert Showtimes; attach Movie link when we have one."""
    url = _table_url(base_id, table)
    total = 0
    for i in range(0, len(rows), BATCH_SIZE):
        chunk = rows[i:i + BATCH_SIZE]
        records = []
        for r in chunk:
            fields = {
                "Title": r["title"],
                "Year": r["year"],
                "Cinema": r["cinema"],
                "Showtime": r["datetime"],
                "Ticket URL": r["ticket_url"],
                "Status": "Active",
                "Last Seen": now_iso,
            }
            mid = src_to_movie_record.get(r["title"])
            if mid:
                fields["Movie"] = [mid]
            records.append({"fields": fields})
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
    if not record_ids:
        return 0
    url = _table_url(base_id, table)
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
    air_token = _env("AIRTABLE_PAT")
    base_id = _env("AIRTABLE_BASE_ID")
    showtimes_table = os.environ.get("AIRTABLE_TABLE", "Showtimes")
    movies_table = os.environ.get("AIRTABLE_MOVIES_TABLE", "Movies")
    tmdb_token = _env("TMDB_BEARER_TOKEN")

    log = lambda msg: print(msg, file=sys.stderr)

    log("scraping…")
    rows, covered_cinemas, failed = scrapers.fetch_all(log)
    log(f"  {len(rows)} unique showtimes across {len(covered_cinemas)} cinemas")
    if not rows:
        sys.exit("every scraper failed; aborting without touching Airtable")

    log("fetching existing Movies…")
    existing_movies, by_source_title = fetch_existing_movies(base_id, movies_table, air_token)
    log(f"  {len(existing_movies)} existing movies, {len(by_source_title)} source-title aliases")

    log("resolving movies via TMDB…")
    src_to_tmdb, to_upsert, unmatched = resolve_movies(
        rows, existing_movies, by_source_title, tmdb_token,
    )
    log(f"  {len(to_upsert)} movies to upsert, {len(unmatched)} unmatched")
    if unmatched:
        for t in unmatched[:10]:
            log(f"    unmatched: {t}")
        if len(unmatched) > 10:
            log(f"    …and {len(unmatched) - 10} more")

    log("upserting Movies…")
    tmdb_to_record = upsert_movies(base_id, movies_table, air_token, to_upsert)
    # Merge with existing record IDs so showtime linking covers everything.
    for tmdb_id, entry in existing_movies.items():
        tmdb_to_record.setdefault(tmdb_id, entry["record_id"])
    src_to_movie_record = {
        src: tmdb_to_record[tmdb_id]
        for src, tmdb_id in src_to_tmdb.items()
        if tmdb_id in tmdb_to_record
    }

    log("fetching existing Showtimes…")
    existing_showtimes = fetch_existing_showtimes(base_id, showtimes_table, air_token)
    log(f"  {len(existing_showtimes)} existing")

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log("upserting Showtimes…")
    n_up = upsert_showtimes(
        base_id, showtimes_table, air_token, rows, src_to_movie_record, now_iso,
    )

    # Expire only within cinemas actually scraped this run: if Roxie's site was
    # down, its showtimes must stay Active rather than being declared gone.
    # Rows already Removed are skipped so we don't re-PATCH the whole archive
    # every day for no reason.
    current_urls = {r["ticket_url"] for r in rows}
    removed_ids = [
        rec["id"]
        for url, rec in existing_showtimes.items()
        if url not in current_urls
        and rec["cinema"] in covered_cinemas
        and rec["status"] != "Removed"
    ]
    log(f"marking {len(removed_ids)} Showtimes as Removed…")
    n_rm = mark_removed(base_id, showtimes_table, air_token, removed_ids)

    log(f"done. movies_upserted={len(to_upsert)} showtimes_upserted={n_up} removed={n_rm}")

    # The sync above is real work worth keeping, but a scraper outage still
    # needs to surface as a red run rather than a quiet partial success.
    if failed:
        sys.exit(
            f"completed with {len(failed)} scraper failure(s): "
            + ", ".join(f"{n} ({type(e).__name__})" for n, e in failed)
        )


if __name__ == "__main__":
    main()
