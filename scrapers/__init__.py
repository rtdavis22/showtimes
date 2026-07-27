"""Scraper registry and a fault-tolerant fan-out across all cinemas."""

from concurrent.futures import ThreadPoolExecutor

from scrapers import alamo, balboa, roxie, stanford

SCRAPERS = (alamo, balboa, roxie, stanford)


def fetch_all(log=None):
    """Run every scraper concurrently, tolerating individual failures.

    Returns (rows, covered, failed):
        rows     deduped showtime dicts from the scrapers that succeeded
        covered  set of cinema names those scrapers are responsible for
        failed   list of (scraper_name, exception) for the ones that died

    `covered` is what makes a partial result safe to sync. It is derived from
    each scraper's declared CINEMAS rather than from the rows it returned, so
    a cinema that genuinely has no showtimes this run is still expirable,
    while a cinema whose scraper crashed is left strictly alone.
    """
    log = log or (lambda msg: None)

    rows, covered, failed = [], set(), []
    with ThreadPoolExecutor(max_workers=len(SCRAPERS)) as pool:
        futures = [(s, pool.submit(s.fetch)) for s in SCRAPERS]
        for scraper, future in futures:
            name = scraper.__name__.rsplit(".", 1)[-1]
            try:
                got = future.result()
            except Exception as e:  # noqa: BLE001 - one bad site must not sink the rest
                failed.append((name, e))
                log(f"  {name}: FAILED ({type(e).__name__}: {e})")
                continue
            rows.extend(got)
            covered.update(scraper.CINEMAS)
            log(f"  {name}: {len(got)} showtimes")

    deduped = list({r["ticket_url"]: r for r in rows}.values())
    return deduped, covered, failed
