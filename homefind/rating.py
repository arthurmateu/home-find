"""A score for which listing to look at first: 0-100 for the basics, and the
bonuses (floor, quiet side, [bonus] places, what's nearby) can take it past 100.

Computed whenever it's shown (freshness changes by the hour), for rejected
listings too: a flat that's great apart from being 5 % over budget still
scores well, so it stands out on the Rejected tab. Hard rejections (swap, WBS,
...) don't lower the score; they're shown separately.

Points:  value (warm €/m²) 35 · fits the budget 15 (negative when over; streets in
         street_max_warm_rent have their own budget, and their €/m² count for less to match) · size 20 + rooms 10 (2+; 1.5 gets 4) ·
         freshness 10 · photos / trusted landlord / description 10 ·
         features (balcony 5, kitchen, ...) up to 10 · not on the ground floor 3 ·
         quiet side (Hinterhaus, Seitenflügel, facing the courtyard) 2 ·
         where it is ([bonus] in config.toml): the old area 5 · a street you like 4 ·
         U/S-Bahn within 5 min on foot 4 · bus/tram stop within 2 min 2 ·
         supermarket within 5 min 10 · park or water within 5 min 3 (nearby.py) ·
         near a place you go daily up to 6, 9 within a 15-minute walk ([bonus] daily, in config.local.toml),
         minus: scam signs (8 per point), Ablöse, estimated rent, semi-basement.
IS24 Plus-only listings aren't marked down: you're a Plus member.
Notes, worth no points: too residential (see residential), Altbau.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import NamedTuple

from . import nearby, transit
from .models import Listing
from .rules import ABLOESE, Verdict, on_street, street_budget
from .util import parse_published

FEATURES = [  # (points, label, pattern); "kein Balkon" / "ohne EBK" don't count
    (5, "balcony", r"balkon|loggia|terrasse"),
    (2, "fitted kitchen", r"einbauküche|\bebk\b"),
    (1, "lift", r"aufzug|fahrstuhl|\blift\b"),
    (1, "garden", r"\bgarten\b|gartenanteil|gartennutzung"),
]
MAX_FEATURES = 10
ALTBAU = r"altbau|dielenboden|dielen\b"  # a heads-up, not a feature: a note under Heads up, no points
UPPER_FLOOR = 3  # not on the ground floor
OLD_AREA = 5     # [bonus] old_area: a postcode you were after before
BIG_STREET = 4   # [bonus] streets
RAIL, RAIL_MINUTES = 4, 5  # a U-Bahn or S-Bahn station at most this many minutes' walk away
STOP, STOP_MINUTES = 2, 2  # a bus or tram stop
SHOP, SHOP_MINUTES = 10, 5  # a supermarket: worth a lot to you
GREEN, GREEN_MINUTES = 3, 5  # a park, woods, a lake or the river
QUIET = 2        # the quiet side: back buildings, rooms facing the courtyard
DAILY, DAILY_KM = 6, (2, 8)  # [bonus] daily: all points up to 2 km away (straight line), none from 8 km
DAILY_WALK, DAILY_WALK_MINUTES = 3, 15  # ... these on top, and a chip on the card, when it's at most this many minutes on foot
INFO_MINUTES = 30  # how far on foot the card's Info row looks for a supermarket or a station (see info)
RESIDENTIAL_PER_KM2, RESIDENTIAL_M = 18, 300  # too residential: fewer places to eat or drink than this, in this radius (5 or fewer)
BANDS = [(95, "great"), (75, "good"), (55, "ok"), (0, "weak")]  # each as rare as 75 / 60 / 45 were before the bonuses

# The floor in the ad's text, when the portal doesn't say: "im 3. OG", "Etage: 2", "zweite Etage",
# "Dachgeschoss". Plain "im Erdgeschoss" is often about a shop downstairs, so a numbered floor
# wins over it; "Erdgeschosswohnung" / "EG-Wohnung" / "Hochparterre" describe the flat itself.
_ORDINALS = ["erste", "zweite", "dritte", "vierte", "fünfte", "sechste", "siebte"]
FLOOR_TEXT = re.compile(
    r"\b(?P<n>\d{1,2})\s*\.\s*(?:og\b|obergeschoss|etage\b|stock\b|stockwerk)"
    r"|\b(?:etage|stockwerk|geschoss)\s*:?\s*(?P<n2>\d{1,2})\b"
    rf"|\b(?P<w>{'|'.join(_ORDINALS)})[nrsm]?\s+(?:og\b|obergeschoss|etage\b|stock\b|stockwerk)"
    r"|\b(?P<en>\d{1,2})(?:st|nd|rd|th) floor"
    r"|\b(?P<top>dachgeschoss\w*|dg\b|penthouse|top floor|unterm dach)", re.I)
GROUND_FLAT = re.compile(r"erdgeschosswohnung|\beg-wohnung|hochparterre|ground[- ]floor (flat|apartment)", re.I)
GROUND_TEXT = re.compile(r"erdgeschoss|\bparterre\b|ground floor|\bim eg\b", re.I)
UPPER_TYPES = re.compile(r"roof|penthouse|dach", re.I)        # the portal's flat type
GROUND_TYPES = re.compile(r"ground|erdgeschoss|parterre", re.I)
BASEMENT_TYPES = re.compile(r"basement|souterrain", re.I)

# Away from the street: the first building the text names (not the Vorderhaus), or rooms that
# face the courtyard ("Balkon zum ruhigen Innenhof", "Schlafzimmer liegen nach hinten zum Hof").
BUILDING = re.compile(r"\b(vorderhaus|hinterhaus|seitenflügel|gartenhaus|quergebäude|quergebaeude)", re.I)
COURTYARD = re.compile(
    r"\b(fenster|balkon|loggia|terrasse|\w*zimmer|räume|zimmern|ausrichtung|ausgerichtet|orientiert|liegt|liegen"
    r"|gelegen|lage)\s+(\w+\s+){0,3}(zum|zur|in den|richtung)\s+(ruhigen\s+)?(innen|garten)?(hof|hofseite|gartenseite)\b"
    r"|hofseitig\w*|\bzur hofseite|ruhige[nr]? (innen)?hoflage|\b(hof|garten)seitige[nrs]?\b"
    r"|(facing|overlooks?|overlooking|towards?) the (inner |quiet )?(court)?yard|courtyard[- ]facing|rear building", re.I)


class Part(NamedTuple):
    """A line under a score. `chip`: how it reads on the card, under "Info" (or under "Heads up",
    when it costs points or is only a note, 0 points); empty for the price, size, freshness and
    the like. `in_info`: the card's Info row says it already, with info()'s own distance, so the
    web UI leaves its chip out (the family page still shows it)."""
    points: int
    label: str
    chip: str = ""
    in_info: bool = False


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _has(pattern: str, text: str) -> bool:
    for m in re.finditer(pattern, text, re.I):
        if not re.search(r"(kein\w*|ohne|nicht)\s+(\w+\s+)?$", text[max(0, m.start() - 20): m.start()], re.I):
            return True
    return False


def storey(listing: Listing) -> tuple[int | None, str]:
    """Which floor the flat is on (0 = ground floor or Hochparterre, below 0 = semi-basement),
    and how to put it; (None, "") when nothing says. From the portal's floor, else its flat
    type, else the title or text. A Dachgeschoss with no number counts as floor 1: above the
    ground floor, anyway."""
    sig = listing.signals
    floor, floors = sig.get("floor"), sig.get("floors")
    if isinstance(floor, int):
        if floor <= 0:
            return floor, "ground floor" if floor == 0 else "semi-basement"
        return floor, f"floor {floor}" + (f" of {floors}" if isinstance(floors, int) and floors >= floor else "")
    kind = sig.get("flat_type") or ""
    if UPPER_TYPES.search(kind):
        return 1, "top floor"
    if BASEMENT_TYPES.search(kind):
        return -1, "semi-basement"
    if GROUND_TYPES.search(kind):
        return 0, "ground floor"
    for text in (listing.title or "", listing.description or ""):
        if GROUND_FLAT.search(text):
            return 0, "ground floor"
        m = FLOOR_TEXT.search(text)
        if m:
            g = m.groupdict()
            n = g["n"] or g["n2"] or g["en"]
            n = int(n) if n else _ORDINALS.index(g["w"].lower()) + 1 if g["w"] else None
            if n is None:
                return 1, "top floor (says the ad)"
            if n == 0:
                return 0, "ground floor"
            if n <= 30:
                return n, f"floor {n} (says the ad)"
        if GROUND_TEXT.search(text):
            return 0, "ground floor"
    return None, ""


def quiet_side(listing: Listing) -> str | None:
    """Why the flat is on the quiet side of the house, if the ad says it is."""
    text = f"{listing.title}\n{listing.description}"
    building = BUILDING.search(text)
    if building and building.group(1).lower() != "vorderhaus":
        return f"{building.group(1).capitalize()}: the quiet side"
    return "faces the courtyard" if COURTYARD.search(text) else None


def _ago(hours: float) -> str:
    if hours < 1:
        return f"{max(1, round(hours * 60))} min ago"
    if hours < 48:
        return f"{round(hours)} h ago"
    days = hours / 24
    if days < 60:
        return f"{round(days)} days ago"
    return f"{round(days / 30)} months ago" if days < 730 else f"{days / 365:.0f} years ago"


def spot(listing: Listing, place: dict | None) -> tuple[float, float] | None:
    """Where the flat is, when that's known to the house: ImmoScout24's pin for an ad with
    a house number, else the address as looked up on OpenStreetMap (geo.locate's `place`)."""
    pin = listing.signals.get("geo")
    if pin:
        return pin[0], pin[1]
    if place and place.get("precision") == "address":
        return place["lat"], place["lon"]
    return None


def residential(cfg: dict, at: tuple[float, float] | None, place: dict | None) -> Part | None:
    """A note when there's next to nothing to eat or drink around: no one's out at night there,
    so a courier at 1am is one the neighbours notice. Counted around the house; or with only the
    postcode known, over all of it, which tells less ("too residential?"). None if it's lively,
    or not known."""
    rough = not at and bool(place and place.get("radius"))
    if not at and not rough:
        return None
    metres = max(RESIDENTIAL_M, place["radius"]) if rough else RESIDENTIAL_M
    n = nearby.count(cfg, *(at or (place["lat"], place["lon"])), ("food",), metres)
    per_km2 = None if n is None else n / (math.pi * (metres / 1000) ** 2)
    if per_km2 is None or per_km2 >= RESIDENTIAL_PER_KM2:
        return None
    if rough:
        return Part(0, f"maybe too residential: only the area is known, and it has about {per_km2:.0f} places "
                       "to eat or drink per km², so a courier at 1am may get noticed", "too residential?")
    return Part(0, f"too residential: {n or 'no'} place{'s' * (n != 1)} to eat or drink within {RESIDENTIAL_M} m, "
                   "so a courier at 1am may get noticed", "too residential")


def rate(listing: Listing, verdict: Verdict, first_seen: str, cfg: dict,
         now: datetime | None = None, place: dict | None = None) -> tuple[int, list[Part]]:
    """Returns (score, [Part(points, reason, chip, in_info), ...]). `place` is geo.locate's for the listing."""
    now = now or datetime.now()
    s, sig = cfg["search"], listing.signals
    parts: list[tuple[int, str]] = []
    warm, size = verdict.warm or listing.warm_rent, listing.size_sqm

    # value for money and budget fit; on a street with its own budget, prices count
    # for less in the same proportion (1200 € there scores like 1000 € elsewhere)
    cap, street = street_budget(listing, cfg)
    leeway = cap / s["max_warm_rent"]
    if warm and size:
        per_sqm = warm / size
        parts.append((round(35 * _clamp((30 - per_sqm / leeway) / 17)),
                      f"{per_sqm:.1f} €/m² warm" + (f" (on {street}, counts as {per_sqm / leeway:.1f})" if street else "")))
    if warm:
        r = warm / cap
        if r <= 0.8:
            pts = 15
        elif r <= 1:
            pts = 15 - (r - 0.8) / 0.2 * 5
        else:
            pts = 10 - (r - 1) * 100  # 10 at the budget, 0 at 10 % over, -10 at 20 % over
        label = f"{(1 - r) * 100:.0f} % under budget" if r <= 1 else f"{(r - 1) * 100:.0f} % over budget"
        parts.append((round(pts), label + (f" ({cap:g} € on {street})" if street else "")))

    # space
    if size:
        parts.append((round(20 * _clamp((size - 25) / 45)), f"{size:g} m²"))
    if listing.rooms and listing.rooms >= 1.5:
        parts.append((10 if listing.rooms >= 2 else 4, f"{listing.rooms:g} rooms"))  # 2+ is what you want

    # freshness: the first hours after posting are when you still get a viewing
    posted = parse_published(listing.published, first_seen)
    seen = posted or datetime.fromisoformat(first_seen)
    hours = max(0.0, (now - seen).total_seconds() / 3600)
    if hours < 3:
        pts = 10
    elif hours < 24:
        pts = 10 - 4 * (hours - 3) / 21
    elif hours < 72:
        pts = 6 - 3 * (hours - 24) / 48
    else:
        pts = max(0, 3 - 3 * (hours - 72) / 96)
    parts.append((round(pts), f"{'posted' if posted else 'first seen'} {_ago(hours)}"))

    # how much you can tell (and trust) before going there
    photos = len(listing.images) or listing.photos or 0
    if photos:
        parts.append((5 if photos >= 8 else 4 if photos >= 4 else 2 if photos >= 2 else 0, f"{photos} photo{'s' * (photos > 1)}"))
    if listing.trusted:
        parts.append((4, "municipal / co-op landlord", "municipal / co-op landlord"))
    elif sig.get("verified_landlord"):
        parts.append((3, "verified landlord"))  # its chip is the verdict's note
    if len(listing.description or "") > 300:
        parts.append((1, "detailed description"))

    # features, from structured data where the portal has it, else the text
    text = f"{listing.title}\n{listing.description}"
    found = [(p, label) for p, label, rx in FEATURES if label in sig.get("features", []) or _has(rx, text)]
    budget = MAX_FEATURES
    for p, label in found:
        if budget > 0:
            parts.append((min(p, budget), label, label))
            budget -= p
    if _has(ALTBAU, text):
        parts.append((0, "Altbau: an old building, says the ad", "Altbau"))
    floor, floor_label = storey(listing)
    if floor and floor > 0:
        parts.append((UPPER_FLOOR, floor_label, floor_label.split(" (")[0]))
    quiet = quiet_side(listing)
    if quiet:
        parts.append((QUIET, quiet, quiet.split(":")[0]))  # "Hinterhaus", "faces the courtyard"

    # where it is ([bonus] in the config)
    bonus = cfg.get("bonus") or {}
    if listing.zip_code and listing.zip_code in bonus.get("old_area", []):
        parts.append((OLD_AREA, f"in the old area ({listing.zip_code})"))
    street = on_street(listing, bonus.get("streets", []))
    if street:
        parts.append((BIG_STREET, f"on {street}"))
    at = spot(listing, place)  # what's in walking distance (nearby.py)
    if at:
        rail = nearby.walk(cfg, *at, ("U", "S"), RAIL_MINUTES)
        if rail:
            parts.append((RAIL, f"{rail[1]}, {rail[0]} min walk", f"{rail[1]} {rail[0]} min", True))
        stop = nearby.walk(cfg, *at, ("bus", "tram"), STOP_MINUTES)
        if stop:  # Info has the nearest bus stop, not tram stops
            parts.append((STOP, f"{stop[2]} stop {stop[1]}, {stop[0]} min walk".replace("  ", " "),
                          f"{stop[2]} stop {stop[0]} min", stop[2] == "bus"))
        shop = nearby.walk(cfg, *at, ("supermarket",), SHOP_MINUTES)
        if shop:
            parts.append((SHOP, f"supermarket {shop[1]}, {shop[0]} min walk".replace("  ", " "), f"supermarket {shop[0]} min", True))
        green = nearby.walk(cfg, *at, ("green",), GREEN_MINUTES)
        if green:
            parts.append((GREEN, f"{green[1]}, {green[0]} min walk", f"{green[1]} {green[0]} min"))
    quiet_area = residential(cfg, at, place)
    if quiet_area:
        parts.append(quiet_area)
    # kilometres are fine by the postcode's middle too, if that's all there is ("~")
    rough = None if at or not place else (place["lat"], place["lon"])
    for daily in bonus.get("daily", []) if at or rough else []:
        metres = nearby.distance(at or rough, (daily["lat"], daily["lon"]))
        km, walk = metres / 1000, nearby.on_foot(metres)
        near, far = DAILY_KM
        points = round(DAILY * _clamp((far - km) / (far - near)))
        label = f"{'~' if rough else ''}{km:.1f} km from {daily['name']} (straight line)"
        if at and walk <= DAILY_WALK_MINUTES:  # a postcode's middle says nothing about a walk
            parts.append((points + DAILY_WALK, f"{label}, about {walk} min on foot", f"{daily['name']} {walk} min walk", True))
        else:
            parts.append((points, label))

    # minus
    if verdict.score:
        parts.append((-8 * verdict.score, "scam warning signs"))
    if ABLOESE.search(text) or sig.get("abloese"):
        parts.append((-6, "asks for Ablöse"))
    if any("estimated" in n for n in verdict.notes):
        parts.append((-2, "warm rent is an estimate"))
    if _has(r"souterrain|tiefparterre|kellerwohnung", text) or (floor or 0) < 0:
        parts.append((-5, "semi-basement (Souterrain)", "semi-basement"))

    score = max(0, round(sum(p[0] for p in parts)))  # no ceiling: the points under a score add up, and the best stay in order
    return score, [q for q in (Part(*p) for p in parts) if q.points or q.chip or q.label.startswith(("posted", "first seen"))]


def _duration(minutes: int) -> str:
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h" + (f" {minutes % 60} min" if minutes % 60 else "")


def _far(metres: float) -> str:
    return f"{round(metres, -1):.0f} m" if metres < 950 else f"{metres / 1000:.1f} km"


def info(cfg: dict, listing: Listing, place: dict | None, trips: dict | None = None) -> list[tuple[str, str]]:
    """The card's Info row, whatever the score makes of it: how far each [bonus] daily place (work)
    is on foot and by public transport (`trips`: Store.trips), and the nearest supermarket, U-Bahn,
    S-Bahn and bus stop on foot; as (chip, full text). The walks need the house (see spot): with only
    the street or postcode known, work is "~" kilometres and minutes by public transport from there."""
    at = spot(listing, place)
    rough = None if at or not place else (place["lat"], place["lon"])
    out = []
    for daily in (cfg.get("bonus") or {}).get("daily", []) if at or rough else []:
        there = (daily["lat"], daily["lon"])
        name, metres = daily["name"], nearby.distance(at or rough, there)
        if rough:
            chip, full = f"{name} ~{_far(metres)}", f"{name}: about {_far(metres)} in a straight line"
        else:
            walk = _duration(nearby.on_foot(metres))
            chip, full = f"{name} {walk} walk", f"{name}: {_far(metres)} in a straight line, about {walk} on foot"
        trip = (trips or {}).get(transit.key(at or rough, there), False)  # False: not asked yet
        if trip:
            by = _duration(trip["minutes"])
            chip += f" · {'~' * bool(rough)}{by} transit"
            full += (f"; {'about ' * bool(rough)}{by} by public transport ({trip['how']}, "
                     f"leaving {transit.LEAVE:%H:%M} on a weekday)")
        else:
            full += "; BVG's journey planner found no connection" if trip is None else "; by public transport: not looked up yet"
        if rough:
            full += (" (only the street is known, so from somewhere along it)" if place["precision"] == "street"
                     else " (only the area is known, so from its middle)")
        out.append((chip, full))
    for kinds, what in ((("supermarket",), "supermarket"), (("U",), "U-Bahn"), (("S",), "S-Bahn"), (("bus",), "bus stop")):
        if not at or not nearby.known(cfg, kinds):
            continue
        found = nearby.walk(cfg, *at, kinds, INFO_MINUTES)
        if not found:
            out.append((f"no {what} within {INFO_MINUTES} min", f"no {what} within {INFO_MINUTES} min on foot"))
            continue
        minutes, name, _, metres = found
        chip = name if what in ("U-Bahn", "S-Bahn") else what.split()[0]  # "U Wilmersdorfer Straße", "bus"
        full = f"{what} {name}" if what in ("supermarket", "bus stop") and name else name or f"a {what}"
        out.append((f"{chip} {minutes} min", f"{full}: about {minutes} min on foot ({_far(metres)} in a straight line)"))
    return out


def band(score: int) -> str:
    return next(label for threshold, label in BANDS if score >= threshold)
