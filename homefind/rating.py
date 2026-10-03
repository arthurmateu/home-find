"""A 0-100 score for which listing to look at first.

Computed whenever it's shown (freshness changes by the hour), for rejected
listings too: a flat that's great apart from being 5 % over budget still
scores well, so it stands out on the Rejected tab. Hard rejections (swap, WBS,
...) don't lower the score; they're shown separately.

Points:  value (warm €/m²) 35 · fits the budget 15 (negative when over) · size 20 + rooms 5 ·
         freshness 10 · photos / trusted landlord / description 10 ·
         features (balcony, kitchen, ...) up to 8,
         minus: scam signs (8 per point), Ablöse, estimated rent, semi-basement.
IS24 Plus-only listings aren't marked down: you're a Plus member.
"""

from __future__ import annotations

import re
from datetime import datetime

from .models import Listing
from .rules import ABLOESE, Verdict
from .util import parse_published

FEATURES = [  # (points, label, pattern); "kein Balkon" / "ohne EBK" don't count
    (3, "balcony", r"balkon|loggia|terrasse"),
    (2, "fitted kitchen", r"einbauküche|\bebk\b"),
    (1, "lift", r"aufzug|fahrstuhl|\blift\b"),
    (1, "Altbau", r"altbau|dielenboden|dielen\b"),
    (1, "garden", r"\bgarten\b|gartenanteil|gartennutzung"),
]
MAX_FEATURES = 8
BANDS = [(75, "great"), (60, "good"), (45, "ok"), (0, "weak")]


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _has(pattern: str, text: str) -> bool:
    for m in re.finditer(pattern, text, re.I):
        if not re.search(r"(kein\w*|ohne|nicht)\s+(\w+\s+)?$", text[max(0, m.start() - 20): m.start()], re.I):
            return True
    return False


def _ago(hours: float) -> str:
    if hours < 1:
        return f"{max(1, round(hours * 60))} min ago"
    if hours < 48:
        return f"{round(hours)} h ago"
    days = hours / 24
    if days < 60:
        return f"{round(days)} days ago"
    return f"{round(days / 30)} months ago" if days < 730 else f"{days / 365:.0f} years ago"


def rate(listing: Listing, verdict: Verdict, first_seen: str, cfg: dict,
         now: datetime | None = None) -> tuple[int, list[tuple[int, str]]]:
    """Returns (score, [(points, reason), ...])."""
    now = now or datetime.now()
    s, sig = cfg["search"], listing.signals
    parts: list[tuple[int, str]] = []
    warm, size = verdict.warm or listing.warm_rent, listing.size_sqm

    # value for money and budget fit
    if warm and size:
        per_sqm = warm / size
        parts.append((round(35 * _clamp((30 - per_sqm) / 17)), f"{per_sqm:.1f} €/m² warm"))
    if warm:
        r = warm / s["max_warm_rent"]
        if r <= 0.8:
            pts = 15
        elif r <= 1:
            pts = 15 - (r - 0.8) / 0.2 * 5
        else:
            pts = 10 - (r - 1) * 100  # 10 at the budget, 0 at 10 % over, -10 at 20 % over
        label = f"{(1 - r) * 100:.0f} % under budget" if r <= 1 else f"{(r - 1) * 100:.0f} % over budget"
        parts.append((round(pts), label))

    # space
    if size:
        parts.append((round(20 * _clamp((size - 25) / 45)), f"{size:g} m²"))
    if listing.rooms and listing.rooms >= 1.5:
        parts.append((5 if listing.rooms >= 2 else 2, f"{listing.rooms:g} rooms"))

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

    # minus
    if verdict.score:
        parts.append((-8 * verdict.score, "scam warning signs"))
    if ABLOESE.search(text) or sig.get("abloese"):
        parts.append((-6, "asks for Ablöse"))
    if any("estimated" in n for n in verdict.notes):
        parts.append((-2, "warm rent is an estimate"))
    if _has(r"souterrain|tiefparterre|kellerwohnung", text):
        parts.append((-5, "semi-basement (Souterrain)"))

    score = round(_clamp(sum(p for p, _ in parts), 0, 100))
    return score, [p for p in parts if p[0] != 0 or p[1].startswith(("posted", "first seen"))]


def band(score: int) -> str:
    return next(label for threshold, label in BANDS if score >= threshold)
