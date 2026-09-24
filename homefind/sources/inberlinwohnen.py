"""inberlinwohnen.de: the joint portal of Berlin's six municipal landlords
(degewo, GESOBAU, Gewobag, HOWOGE, STADT UND LAND, WBM).

It is a Laravel Livewire app: each listing's data sits as JSON in a
`wire:snapshot` attribute. Newest first, 10 per page, all of Berlin.
"""

from __future__ import annotations

import html
import json
import re

from ..models import Listing
from ..util import num
from . import Source

URL = "https://www.inberlinwohnen.de/wohnungsfinder/"


class InBerlinWohnen(Source):
    name = "inberlinwohnen"

    def search(self, seen):
        for page in range(1, self.opts.get("max_pages", 45) + 1):
            items = list(parse(self.http.get(URL if page == 1 else f"{URL}?page={page}")))
            if not items:
                break
            fresh = [l for l in items if not seen(l.key)]
            yield from items
            if not fresh:
                break


def _unwrap(x):
    """Livewire encodes arrays as [value, {"s": "arr"}]; strip that metadata."""
    if isinstance(x, list):
        if len(x) == 2 and isinstance(x[1], dict) and "s" in x[1]:
            return _unwrap(x[0])
        return [_unwrap(i) for i in x]
    if isinstance(x, dict):
        return {k: _unwrap(v) for k, v in x.items()}
    return x


def parse(page: str):
    for raw in re.findall(r'wire:snapshot="([^"]*)"', page):
        snap = json.loads(html.unescape(raw))
        if snap.get("memo", {}).get("name") != "apartment-finder.item.apartment-item":
            continue
        it = _unwrap(snap["data"]["item"])
        details = {d["label"]: d["value"] for group in it.get("details") or []
                   for d in group if isinstance(d, dict) and "label" in d}
        addr, company, img = it.get("address") or {}, it.get("company") or {}, it.get("imagePath")
        wbs = str(details.get("WBS", "")).strip().lower()
        yield Listing(
            source="inberlinwohnen",
            id=str(it["id"]),
            url=it.get("deeplink") or URL,
            title=(it.get("title") or "").strip(),
            warm_rent=num(details.get("Gesamtmiete")) or num(it.get("rentGross")),
            cold_rent=num(it.get("rentNet")),
            size_sqm=num(it.get("area")),
            rooms=num(it.get("rooms")),
            zip_code=addr.get("zipCode"),
            district=addr.get("district"),
            address=" ".join(x for x in (addr.get("street"), addr.get("number")) if x),
            # "erforderlich", "nicht erforderlich" or "unbekannt"; rules.py also reads the title
            wbs_required={"erforderlich": True, "nicht erforderlich": False}.get(wbs),
            available_from=it.get("occupationDate"),
            published=it.get("createdAt"),
            landlord=(company.get("name") or "").strip() or None,
            image_url=f"https://www.inberlinwohnen.de/img/{img}" if img else None,
            images=[f"https://www.inberlinwohnen.de/img/{img}"] if img else [],
            trusted=True,
        )
