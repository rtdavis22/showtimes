"""Alamo Drafthouse San Francisco Bay Area scraper.

Returns unified showtime records for all 3 Bay Area cinemas:
new-mission (SF), mountain-view, valley-fair (Santa Clara).
"""

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from scrapers._http import get as _http_get

MARKET_SLUG = "sf"
API_URL = f"https://drafthouse.com/s/mother/v2/schedule/market/{MARKET_SLUG}"
TICKET_URL_TEMPLATE = (
    "https://drafthouse.com/" + MARKET_SLUG +
    "/show/{slug}?cinemaId={cinema_id}&sessionId={session_id}"
)


def fetch():
    data = json.loads(_http_get(API_URL))["data"]

    titles = {p["slug"]: p["show"]["title"] for p in data["presentations"]}
    # Map cinema slug to a display name unambiguous alongside other SF cinemas.
    # Alamo's API labels New Mission as just "San Francisco" — too generic.
    cinema_names = {
        "new-mission": "Alamo Drafthouse New Mission",
        "mountain-view": "Alamo Drafthouse Mountain View",
        "valley-fair": "Alamo Drafthouse Valley Fair",
    }
    cinemas = {
        c["id"]: cinema_names.get(c["slug"], f"Alamo Drafthouse {c['name']}")
        for market in data["market"]
        for c in market["cinemas"]
    }

    rows = []
    for s in data["sessions"]:
        if s.get("isHidden") or s.get("status") != "ONSALE":
            continue
        cinema = cinemas.get(s["cinemaId"])
        if cinema is None:
            continue
        # showTimeUtc is naive ISO in UTC per the API; attach Z and convert to PT.
        utc_dt = datetime.fromisoformat(s["showTimeUtc"]).replace(tzinfo=timezone.utc)
        local_dt = utc_dt.astimezone(ZoneInfo(s["cinemaTimeZoneName"]))
        rows.append({
            "title": titles.get(s["presentationSlug"], s["presentationSlug"]),
            "year": None,  # not provided by the schedule API
            "cinema": cinema,
            "datetime": local_dt.isoformat(timespec="seconds"),
            "ticket_url": TICKET_URL_TEMPLATE.format(
                slug=s["presentationSlug"],
                cinema_id=s["cinemaId"],
                session_id=s["sessionId"],
            ),
        })
    return rows


if __name__ == "__main__":
    import sys
    json.dump(fetch(), sys.stdout, indent=2)
    sys.stdout.write("\n")
