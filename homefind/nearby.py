"""What's near a flat, for the score: U-Bahn and S-Bahn stations (and their
entrances), bus and tram stops, supermarkets, parks, woods and water; and places
to eat or drink, for the card's note. From OpenStreetMap, through the Overpass API:
each layer is fetched for all of Berlin into data/nearby.json, and again once
it's REFRESH_DAYS old, by the web UI's map thread (geo.start) or --geocode.
Until a layer is there, the score just leaves it out.

Minutes on foot come from the straight line: streets make a walk about DETOUR
times as long, at WALK_M_PER_MIN. Data © OpenStreetMap contributors (ODbL).
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from .net import OSM_UA, Fetcher
from .util import stamp

API = "https://overpass-api.de/api/interpreter"
REFRESH_DAYS = 30
DETOUR, WALK_M_PER_MIN = 1.3, 80  # 4.8 km/h
M_PER_DEG = 111_320               # metres per degree of latitude
LON_SCALE = math.cos(math.radians(52.5))  # ... and of longitude, in Berlin
CELL = 300                        # grid cells, in metres
EDGE_STEP = 50                    # a park's edge, as points this far apart


def _station(name: str) -> str:
    """ "Berlin-Charlottenburg" -> "Charlottenburg", "Berlin Hauptbahnhof (tief)" -> "Hauptbahnhof"."""
    return re.sub(r"\s*\(.*?\)", "", re.sub(r"^Berlin[- ]", "", name)).strip()


def _stops(elements: list[dict]) -> list[list]:
    """U and S stations, each entrance as its station, bus and tram stops: [kind, lat, lon, name]."""
    stations, entrances, stops = [], [], []
    for e in elements:
        t = e.get("tags") or {}
        lat, lon = e.get("lat", (e.get("center") or {}).get("lat")), e.get("lon", (e.get("center") or {}).get("lon"))
        if lat is None or lon is None:
            continue
        rail = t.get("railway")
        if rail in ("station", "halt"):
            # Parkeisenbahn and museum lines aren't; big DB stations (Zoo, Charlottenburg) only say train=yes
            if t.get("usage") == "tourism" or t.get("station") == "miniature" or not t.get("name") or "[" in t["name"]:
                continue
            kind = "U" if t.get("station") == "subway" else "S"
            stations.append([kind, lat, lon, f"{kind} {_station(t['name'])}"])
        elif rail in ("subway_entrance", "train_station_entrance"):
            entrances.append(("U" if rail == "subway_entrance" else "S", lat, lon))
        elif t.get("highway") == "bus_stop" or rail == "tram_stop":
            stops.append(["bus" if t.get("highway") == "bus_stop" else "tram", lat, lon, t.get("name") or ""])
    index = Index(stations)
    for kind, lat, lon in entrances:  # entrances' own names are a mixed bag: name them after their station
        station = index.nearest(lat, lon, {kind}, 500)
        if station:
            stations.append([kind, lat, lon, station[1][3]])
    return [[k, round(lat, 5), round(lon, 5), name] for k, lat, lon, name in stations + stops]


def _places(kind: str):
    """A parser for places that are a point or a building's middle: [kind, lat, lon, name]."""
    def parse(elements: list[dict]) -> list[list]:
        out = []
        for e in elements:
            t, at = e.get("tags") or {}, e.get("center") or e
            if at.get("lat") is not None:
                out.append([kind, round(at["lat"], 5), round(at["lon"], 5), t.get("name") or t.get("brand") or ""])
        return out
    return parse


def _green(elements: list[dict]) -> list[list]:
    """Parks, woods and lakes that are named and over 800 m round, rivers and the Landwehrkanal
    (the query sees to that), as points every EDGE_STEP metres along their edges, so a flat by
    Tiergarten is by it, not 1 km from its middle: ["green", lat, lon, name]."""
    out, seen = [], set()
    for e in elements:
        name = (e.get("tags") or {}).get("name")
        if not name:
            continue
        for line in [e.get("geometry") or [], *((m.get("geometry") or []) for m in e.get("members") or [])]:
            for a, b in zip(line, line[1:]):
                dy, dx = b["lat"] - a["lat"], b["lon"] - a["lon"]
                steps = max(1, math.ceil(math.hypot(dy * M_PER_DEG, dx * M_PER_DEG * LON_SCALE) / EDGE_STEP))
                for k in range(steps):
                    lat, lon = round(a["lat"] + dy * k / steps, 5), round(a["lon"] + dx * k / steps, 5)
                    if (lat, lon, name) not in seen:
                        seen.add((lat, lon, name))
                        out.append(["green", lat, lon, name])
    return out


# Layer name -> (Overpass query for Berlin, parser); each one is fetched and kept on its own.
LAYERS = {
    "stops": ("""(
  nwr["railway"~"^(station|halt)$"](area.b);
  node["railway"~"^(subway_entrance|train_station_entrance)$"](area.b);
  node["highway"="bus_stop"](area.b);
  node["railway"="tram_stop"](area.b);
); out center tags;""", _stops),
    "shops": ("""nwr["shop"="supermarket"](area.b); out center tags;""", _places("supermarket")),  # discounters and organic ones too
    "food": ("""nwr["amenity"~"^(restaurant|fast_food|bar|pub|nightclub|biergarten)$"](area.b); out center tags;""", _places("food")),
    "green": ("""(
  wr["leisure"="park"]["name"](if: length() > 800)(area.b);
  wr["landuse"="forest"]["name"](if: length() > 800)(area.b);
  wr["natural"~"^(wood|water)$"]["name"](if: length() > 800)(area.b);
  way["waterway"="river"]["name"](area.b);
  way["waterway"="canal"]["name"="Landwehrkanal"](area.b);
); out geom;""", _green),
}


class Index:
    """Points ([kind, lat, lon, name]) on a grid, to find the nearest one quickly."""

    def __init__(self, points: list[list]):
        self.cells: dict[tuple[int, int], list] = defaultdict(list)
        self.kinds = {p[0] for p in points}
        for p in points:
            self.cells[self._cell(p[1], p[2])].append(p)

    @staticmethod
    def _cell(lat: float, lon: float) -> tuple[int, int]:
        return int(lat * M_PER_DEG // CELL), int(lon * M_PER_DEG * LON_SCALE // CELL)

    def around(self, lat: float, lon: float, kinds, metres: float):
        """(distance in metres, point) of each point of `kinds` within `metres`."""
        ci, cj = self._cell(lat, lon)
        r = math.ceil(metres / CELL)
        for i in range(ci - r, ci + r + 1):
            for j in range(cj - r, cj + r + 1):
                for p in self.cells.get((i, j), ()):
                    if p[0] in kinds:
                        d = distance((p[1], p[2]), (lat, lon))
                        if d <= metres:
                            yield d, p

    def nearest(self, lat: float, lon: float, kinds, metres: float) -> tuple[float, list] | None:
        """(distance in metres, point) of the nearest point of `kinds` within `metres`."""
        return min(self.around(lat, lon, kinds, metres), key=lambda found: found[0], default=None)


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Straight-line distance between two (lat, lon) in Berlin."""
    return math.hypot((a[0] - b[0]) * M_PER_DEG, (a[1] - b[1]) * M_PER_DEG * LON_SCALE)


def on_foot(metres: float) -> int:
    """Minutes on foot for a straight-line distance, rounded up."""
    return max(1, math.ceil(metres * DETOUR / WALK_M_PER_MIN))


def path(cfg: dict) -> Path:
    return Path(cfg["run"]["db_path"]).with_name("nearby.json")


def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def refresh(cfg: dict, http: Fetcher | None = None) -> list[str]:
    """Fetches the layers that are missing or REFRESH_DAYS old; returns their names.
    Raises what the fetch raises (Blocked when Overpass is busy)."""
    p = path(cfg)
    data = _read(p)
    layers = data.setdefault("layers", {})
    stale = (datetime.now() - timedelta(days=REFRESH_DAYS)).isoformat()
    due = [name for name in LAYERS if (layers.get(name) or {}).get("fetched", "") < stale]
    http = http or Fetcher(delay=5, timeout=200)
    for name in due:
        body, parse = LAYERS[name]
        query = f'[out:json][timeout:180];\narea["name"="Berlin"]["admin_level"="4"]->.b;\n{body}'
        found = http.json(f"{API}?{urlencode({'data': query})}", ua=OSM_UA)
        layers[name] = {"fetched": stamp(), "points": parse(found["elements"])}
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        os.replace(tmp, p)
    return due


_cache: dict = {}


def _index(cfg: dict) -> Index | None:
    """data/nearby.json's points, loaded again whenever the file changes."""
    p = path(cfg)
    try:
        key = (str(p), p.stat().st_mtime)
    except OSError:
        return None
    if _cache.get("key") != key:
        layers = _read(p).get("layers", {})
        _cache.update(key=key, index=Index([pt for layer in layers.values() for pt in layer["points"]]), counts={})
    return _cache["index"]


def walk(cfg: dict, lat: float, lon: float, kinds, minutes: float) -> tuple[int, str, str, float] | None:
    """The nearest of `kinds` (see LAYERS' parsers) at most `minutes` away on foot:
    (minutes, rounded up; its name; its kind; metres in a straight line). None if
    there's none, or no data yet (see known)."""
    index = _index(cfg)
    found = index and index.nearest(lat, lon, set(kinds), minutes * WALK_M_PER_MIN / DETOUR)
    if not found:
        return None
    metres, (kind, _, _, name) = found
    return on_foot(metres), name, kind, metres


def known(cfg: dict, kinds) -> bool:
    """Whether there's data on any of `kinds` yet."""
    index = _index(cfg)
    return bool(index and index.kinds & set(kinds))


def count(cfg: dict, lat: float, lon: float, kinds, metres: float) -> int | None:
    """How many of `kinds` are within `metres` in a straight line. None until there's data on them."""
    if not known(cfg, kinds):
        return None
    index = _index(cfg)
    key, counts = (lat, lon, tuple(kinds), metres), _cache["counts"]
    if key not in counts:
        counts[key] = sum(1 for _ in index.around(lat, lon, set(kinds), metres))
    return counts[key]
