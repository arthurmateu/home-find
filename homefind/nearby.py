"""What's near a flat, for the score: U-Bahn and S-Bahn stations (and their
entrances), bus and tram stops. From OpenStreetMap, through the Overpass API:
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

from .net import Fetcher

API = "https://overpass-api.de/api/interpreter"
UA = "homefind/1.0 (personal flat search in Berlin)"
REFRESH_DAYS = 30
DETOUR, WALK_M_PER_MIN = 1.3, 80  # 4.8 km/h
M_PER_DEG = 111_320               # metres per degree of latitude
LON_SCALE = math.cos(math.radians(52.5))  # ... and of longitude, in Berlin
CELL = 300                        # grid cells, in metres


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


# Layer name -> (Overpass query for Berlin, parser); each one is fetched and kept on its own.
LAYERS = {
    "stops": ("""(
  nwr["railway"~"^(station|halt)$"](area.b);
  node["railway"~"^(subway_entrance|train_station_entrance)$"](area.b);
  node["highway"="bus_stop"](area.b);
  node["railway"="tram_stop"](area.b);
); out center tags;""", _stops),
}


class Index:
    """Points ([kind, lat, lon, name]) on a grid, to find the nearest one quickly."""

    def __init__(self, points: list[list]):
        self.cells: dict[tuple[int, int], list] = defaultdict(list)
        for p in points:
            self.cells[self._cell(p[1], p[2])].append(p)

    @staticmethod
    def _cell(lat: float, lon: float) -> tuple[int, int]:
        return int(lat * M_PER_DEG // CELL), int(lon * M_PER_DEG * LON_SCALE // CELL)

    def nearest(self, lat: float, lon: float, kinds, metres: float) -> tuple[float, list] | None:
        """(distance in metres, point) of the nearest point of `kinds` within `metres`."""
        ci, cj = self._cell(lat, lon)
        r = math.ceil(metres / CELL)
        best = None
        for i in range(ci - r, ci + r + 1):
            for j in range(cj - r, cj + r + 1):
                for p in self.cells.get((i, j), ()):
                    if p[0] in kinds:
                        d = math.hypot((p[1] - lat) * M_PER_DEG, (p[2] - lon) * M_PER_DEG * LON_SCALE)
                        if d <= metres and (best is None or d < best[0]):
                            best = (d, p)
        return best


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
        found = http.json(f"{API}?{urlencode({'data': query})}", ua=UA)
        layers[name] = {"fetched": datetime.now().isoformat(timespec="seconds"), "points": parse(found["elements"])}
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
        _cache.update(key=key, index=Index([pt for layer in layers.values() for pt in layer["points"]]))
    return _cache["index"]


def walk(cfg: dict, lat: float, lon: float, kinds, minutes: float) -> tuple[int, str, str] | None:
    """The nearest of `kinds` (see LAYERS' parsers) at most `minutes` away on foot:
    (minutes, rounded up; its name; its kind). None if there's none, or no data yet."""
    index = _index(cfg)
    found = index and index.nearest(lat, lon, set(kinds), minutes * WALK_M_PER_MIN / DETOUR)
    if not found:
        return None
    metres, (kind, _, _, name) = found
    return max(1, math.ceil(metres * DETOUR / WALK_M_PER_MIN)), name, kind
