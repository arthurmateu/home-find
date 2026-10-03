"""Where each listing is: coordinates and neighbourhood (Ortsteil), for the web
UI's mini-maps and its locality filter.

Looked up on OpenStreetMap's Nominatim, at most one request every second or so
as its usage policy asks, and kept in the database (table `places`), so each
address is only asked once. While the web UI runs, a background thread places
new listings every couple of minutes; `--geocode` does everything at once.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from urllib.parse import urlencode

from .net import Blocked, Fetcher
from .store import Store
from .util import street_of

log = logging.getLogger("homefind")
API = "https://nominatim.openstreetmap.org/search"
UA = "homefind/1.0 (personal flat search in Berlin)"  # Nominatim wants the app named, not a browser
BERLIN = (52.33, 52.68, 13.08, 13.77)  # south, north, west, east: answers outside are wrong matches
EVERY_SECONDS = 120


def lookups(listing) -> list[dict]:
    """Queries that could place the listing, most precise first: the street (and
    house number) within its postcode or neighbourhood, so it can't land on a
    namesake across town; then the postcode, or else the neighbourhood."""
    street, zip_code, district = street_of(listing.address), listing.zip_code, listing.district
    tries = []
    if street:
        if zip_code:
            tries.append({"street": street, "postalcode": zip_code, "city": "Berlin"})
        else:
            tries.append({"q": f"{street}, {district}, Berlin"} if district else {"street": street, "city": "Berlin"})
    if zip_code:
        tries.append({"postalcode": zip_code})  # with city=Berlin, Nominatim answers with all of Berlin
    elif district:
        tries.append({"q": f"{district}, Berlin"})
    return tries


def key(params: dict) -> str:
    return json.dumps(params, ensure_ascii=False, sort_keys=True).lower()


def locate(listing, places: dict) -> dict | None:
    """The most precise place known for the listing (`places` is Store.places())."""
    return next((places[k] for k in map(key, lookups(listing)) if places.get(k)), None)


def locality(listing, place: dict | None) -> str | None:
    """The listing's neighbourhood. Placed only by its postcode, the portal's own
    label is the better guess: 14055's middle is in the Grunewald forest, but
    most of its flats are in Westend."""
    if place and (place["precision"] != "area" or not listing.district):
        return place["locality"]
    return listing.district


def _parse(item: dict) -> dict | None:
    lat, lon = float(item["lat"]), float(item["lon"])
    if not (BERLIN[0] <= lat <= BERLIN[1] and BERLIN[2] <= lon <= BERLIN[3]):
        return None
    a = item.get("address") or {}
    radius = None
    if a.get("house_number"):
        precision = "address"
    elif item.get("category") == "highway":
        precision = "street"  # somewhere along it
    else:  # a postcode or neighbourhood: its middle, and roughly how far it reaches
        precision = "area"
        s, n, w, e = map(float, item["boundingbox"])
        radius = round(max(n - s, (e - w) * math.cos(math.radians(lat))) * 111_320 / 2)
    return {"lat": lat, "lon": lon, "precision": precision, "radius": radius,
            "locality": a.get("suburb") or a.get("city_district") or a.get("borough")}


class Geocoder:
    def __init__(self, http: Fetcher | None = None):
        self.http = http or Fetcher(delay=1.5)  # 1.2-2.25 s apart

    def ask(self, params: dict) -> dict | None:
        q = {**params, "format": "jsonv2", "addressdetails": 1, "limit": 1, "countrycodes": "de"}
        found = self.http.json(f"{API}?{urlencode(q)}", ua=UA)
        return _parse(found[0]) if found else None


def _shown(cfg: dict, store: Store) -> list:
    """Listings the web UI shows, the ones you're likeliest to look at first:
    saved, matches, rejected ones in the area, hidden, the rest."""
    drop = set(cfg["search"].get("drop_reasons") or [])

    def rank(r) -> int:
        if r.status == "saved":
            return 0
        if r.status == "hidden":
            return 3
        if r.verdict.ok:
            return 1
        return 4 if {"area", "avoid"} & set(r.verdict.codes) else 2

    return sorted((r for r in store.all_rows() if not drop & set(r.verdict.codes)), key=rank)


def fill(cfg: dict, store: Store, geocoder: Geocoder) -> int:
    """Looks up the listings that haven't been placed yet; returns how many
    lookups it made. Raises Blocked if Nominatim turns us away."""
    places = store.places()
    asked = 0
    for row in _shown(cfg, store):
        for params in lookups(row.listing):
            k = key(params)
            if k not in places:
                places[k] = geocoder.ask(params)
                store.save_place(k, places[k])
                asked += 1
            if places[k]:
                break
    return asked


def coverage(cfg: dict, store: Store) -> tuple[int, int]:
    """(listings on the map, listings shown)."""
    places = store.places()
    rows = _shown(cfg, store)
    return sum(locate(r.listing, places) is not None for r in rows), len(rows)


def start(cfg: dict) -> threading.Thread:
    """Places new listings in the background, every EVERY_SECONDS."""
    def run():
        geocoder = Geocoder()
        while True:
            wait = EVERY_SECONDS
            store = Store(cfg["run"]["db_path"])
            try:
                n = fill(cfg, store, geocoder)
                if n:
                    log.info("map: looked up %d place%s", n, "s" * (n > 1))
            except Blocked as e:
                log.warning("map: Nominatim refused (%s); trying again in an hour", e)
                wait = 3600
            except Exception as e:  # noqa: BLE001 - maps are a nice-to-have; never stop for them
                log.warning("map: lookup failed (%s); trying again in 10 min", e)
                wait = 600
            finally:
                store.close()
            time.sleep(wait)

    t = threading.Thread(target=run, name="geo", daemon=True)
    t.start()
    return t
