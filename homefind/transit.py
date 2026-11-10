"""How long the [bonus] daily places (work) take by public transport from a flat, for
the card's Info row. Asked of BVG's journey planner through transport.rest
(v6.bvg.transport.rest: free, no key, up to 100 requests a minute), door to door:
leaving on the next weekday at LEAVE, the fastest of the first few connections, by
the timetable (delays left out). The planner gets both ends' coordinates, nothing else.

Kept in the database (table `trips`), so each flat is asked about once per place.
While the web UI runs, its map thread (geo.start) asks about new flats a few at a
time, after placing them; `--geocode` asks about all of them at once. From the
house when it's known (rating.spot), else from the street or the postcode's middle,
which the card marks as rough ("~"); those are asked about once for all their flats.
"""

from __future__ import annotations

import math
import urllib.error
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .net import OSM_UA, Fetcher

API = "https://v6.bvg.transport.rest/journeys"
BERLIN = ZoneInfo("Europe/Berlin")
LEAVE = time(8, 30)
PER_ROUND = 30  # trips asked about per round of the map thread


def key(a: tuple[float, float], b: tuple[float, float]) -> str:
    """A trip's key in the database: both ends, (lat, lon) each, to about 10 m."""
    return f"{a[0]:.4f},{a[1]:.4f}>{b[0]:.4f},{b[1]:.4f}"


def departure(today: date) -> datetime:
    """The next weekday at LEAVE, Berlin time; never today, which may be past it."""
    day = today + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, LEAVE, BERLIN)


def _at(leg: dict, end: str) -> datetime | None:
    when = leg.get("planned" + end.capitalize()) or leg.get(end)
    return datetime.fromisoformat(when) if when else None


def _line(leg: dict) -> str:
    line = leg["line"]
    return f"bus {line['name']}" if line.get("product") == "bus" else line["name"]  # "S41", "U7", "M10", "bus 109"


def ask(http: Fetcher, a: tuple[float, float], b: tuple[float, float], leave: datetime) -> dict | None:
    """The fastest way from a to b leaving at `leave` or a little later: {"minutes", "how"}, `how`
    being the lines taken ("U7 + S41") or "on foot". None when the planner finds no connection."""
    q = {"from.latitude": a[0], "from.longitude": a[1], "from.address": "start",
         "to.latitude": b[0], "to.longitude": b[1], "to.address": "destination",
         "departure": leave.isoformat(), "results": 3, "stopovers": "false", "remarks": "false",
         "polylines": "false", "language": "en"}
    try:
        found = http.json(f"{API}?{urlencode(q)}", ua=OSM_UA)
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):  # e.g. "no stations found close to the address"
            return None
        raise
    best = None
    for journey in found.get("journeys") or []:
        legs = journey.get("legs") or []
        start, end = legs and _at(legs[0], "departure"), legs and _at(legs[-1], "arrival")
        if not (start and end):
            continue
        minutes = max(1, math.ceil((end - start).total_seconds() / 60))
        if best is None or minutes < best["minutes"]:
            best = {"minutes": minutes, "how": " + ".join(_line(leg) for leg in legs if leg.get("line")) or "on foot"}
    return best


def fill(cfg: dict, store, homes: list[tuple[float, float]], http: Fetcher, limit: int | None = None) -> int:
    """Asks about the trips not known yet, from each of `homes` (the first ones first) to each
    [bonus] daily place, at most `limit` of them; returns how many it asked about. Raises what
    the fetch raises (Blocked when the planner turns us away)."""
    daily = (cfg.get("bonus") or {}).get("daily") or []
    trips = store.trips() if daily else {}
    leave = departure(datetime.now(BERLIN).date())
    asked = 0
    for home in homes:
        for place in daily:
            there = (place["lat"], place["lon"])
            k = key(home, there)
            if k in trips:
                continue
            if limit is not None and asked >= limit:
                return asked
            trips[k] = ask(http, home, there, leave)
            store.save_trip(k, trips[k])
            asked += 1
    return asked
