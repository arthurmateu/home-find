"""A 0-100 score for which listing to look at first.

Computed whenever it's shown (freshness changes by the hour), for rejected
listings too: a flat that's great apart from being 5 % over budget still
scores well, so it stands out on the Rejected tab. Hard rejections (swap, WBS,
...) don't lower the score; they're shown separately.

Points:  value (warm €/m²) 35 · fits the budget 15 (negative when over; streets in
         street_max_warm_rent have their own budget, and their €/m² count for less to match) · size 20 + rooms 10 (2+; 1.5 gets 4) ·
         freshness 10 · photos / trusted landlord / description 10 ·
         features (balcony 5, kitchen, ...) up to 10 · not on the ground floor 3 ·
         where it is ([bonus] in config.toml): the old area 5 · a street you like 4 ·
         U/S-Bahn within 5 min on foot 4 · bus/tram stop within 2 min 2 ·
         supermarket within 5 min 10 · park or water within 5 min 3 (nearby.py),
         minus: scam signs (8 per point), Ablöse, estimated rent, semi-basement.
IS24 Plus-only listings aren't marked down: you're a Plus member.
"""

from __future__ import annotations

import re
from datetime import datetime

from . import nearby
from .models import Listing
from .rules import ABLOESE, Verdict, on_street, street_budget
from .util import parse_published

FEATURES = [  # (points, label, pattern); "kein Balkon" / "ohne EBK" don't count
    (5, "balcony", r"balkon|loggia|terrasse"),
    (2, "fitted kitchen", r"einbauküche|\bebk\b"),
    (1, "lift", r"aufzug|fahrstuhl|\blift\b"),
    (1, "Altbau", r"altbau|dielenboden|dielen\b"),
    (1, "garden", r"\bgarten\b|gartenanteil|gartennutzung"),
]
MAX_FEATURES = 10
UPPER_FLOOR = 3  # not on the ground floor
OLD_AREA = 5     # [bonus] old_area: a postcode you were after before
BIG_STREET = 4   # [bonus] streets
RAIL, RAIL_MINUTES = 4, 5  # a U-Bahn or S-Bahn station at most this many minutes' walk away
STOP, STOP_MINUTES = 2, 2  # a bus or tram stop
SHOP, SHOP_MINUTES = 10, 5  # a supermarket: worth a lot to you
GREEN, GREEN_MINUTES = 3, 5  # a park, woods, a lake or the river
BANDS = [(75, "great"), (60, "good"), (45, "ok"), (0, "weak")]

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


def rate(listing: Listing, verdict: Verdict, first_seen: str, cfg: dict,
         now: datetime | None = None, place: dict | None = None) -> tuple[int, list[tuple[int, str]]]:
    """Returns (score, [(points, reason), ...]). `place` is geo.locate's for the listing."""
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
        parts.append((4, "municipal / co-op landlord"))
    elif sig.get("verified_landlord"):
        parts.append((3, "verified landlord"))
    if len(listing.description or "") > 300:
        parts.append((1, "detailed description"))

    # features, from structured data where the portal has it, else the text
    text = f"{listing.title}\n{listing.description}"
    found = [(p, label) for p, label, rx in FEATURES if label in sig.get("features", []) or _has(rx, text)]
    budget = MAX_FEATURES
    for p, label in found:
        if budget > 0:
            parts.append((min(p, budget), label))
            budget -= p
    floor, floor_label = storey(listing)
    if floor and floor > 0:
        parts.append((UPPER_FLOOR, floor_label))

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
            parts.append((RAIL, f"{rail[1]}, {rail[0]} min walk"))
        stop = nearby.walk(cfg, *at, ("bus", "tram"), STOP_MINUTES)
        if stop:
            parts.append((STOP, f"{stop[2]} stop {stop[1]}, {stop[0]} min walk".replace("  ", " ")))
        shop = nearby.walk(cfg, *at, ("supermarket",), SHOP_MINUTES)
        if shop:
            parts.append((SHOP, f"supermarket {shop[1]}, {shop[0]} min walk".replace("  ", " ")))
        green = nearby.walk(cfg, *at, ("green",), GREEN_MINUTES)
        if green:
            parts.append((GREEN, f"{green[1]}, {green[0]} min walk"))

    # minus
    if verdict.score:
        parts.append((-8 * verdict.score, "scam warning signs"))
    if ABLOESE.search(text) or sig.get("abloese"):
        parts.append((-6, "asks for Ablöse"))
    if any("estimated" in n for n in verdict.notes):
        parts.append((-2, "warm rent is an estimate"))
    if _has(r"souterrain|tiefparterre|kellerwohnung", text) or (floor or 0) < 0:
        parts.append((-5, "semi-basement (Souterrain)"))

    score = round(_clamp(sum(p for p, _ in parts), 0, 100))
    return score, [p for p in parts if p[0] != 0 or p[1].startswith(("posted", "first seen"))]


def band(score: int) -> str:
    return next(label for threshold, label in BANDS if score >= threshold)
