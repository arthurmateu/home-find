"""Charlottenburger Baugenossenschaft ("Charlotte"): the big housing co-op in
Charlottenburg (~6,500 flats, cold rents around 5-6 €/m²). It posts a batch of
offers every week; most are for members only.
"""

from __future__ import annotations

import re

from ..models import Listing
from ..util import berlin_zip, num, text
from . import Source

URL = "https://charlotte1907.de/wohnungsangebote/woechentliche-angebote"


class Charlotte1907(Source):
    name = "charlotte1907"

    def search(self, seen):
        yield from parse(self.http.get(URL))


def _field(block: str, label: str) -> str | None:
    m = re.search(rf"{label}\s*:\s*([^\n]+)", block)
    return m.group(1).strip() if m else None


def parse(page: str):
    for block in re.split(r"(?=ANGEBOT Nr\.\s*\d+)", text(page)):
        head = re.match(r"ANGEBOT Nr\.\s*(\d+)\s*WOHNUNGS-Nr\.\s*([\d/]+)", block)
        if not head:
            continue
        # A map-marker line like: "Spandau","Schwendyweg 43","0. EG","52.56","13.20",...
        marker = re.search(r'"([^"]+)","([^"]+)","[^"]*","[\d.]+","[\d.]+"', block)
        district, street = (marker.group(1), marker.group(2)) if marker else (None, None)
        rooms = num(_field(block, "Zimmer"))
        title = f"{rooms:g}-Zimmer-Wohnung" if rooms else "Wohnung"
        yield Listing(
            source="charlotte1907",
            id=head.group(2),
            url=URL,
            title=f"{title}, {street}" if street else title,
            warm_rent=num(_field(block, "Gesamtmiete in Euro")),
            size_sqm=num(_field(block, "Wohnfläche")),
            rooms=rooms,
            zip_code=berlin_zip((re.search(r"\b1\d{4} Berlin\b", block) or [None])[0]),
            district=district,
            address=street,
            wbs_required=bool(re.search(r"\bWBS\b[^\n]{0,30}(erforderlich|notwendig)|\bmit WBS\b", block)),
            available_from=_field(block, "Voraus. frei ab"),
            landlord="Charlottenburger Baugenossenschaft eG",
            trusted=True,
            signals={"members_only": "nur für mitglieder" in block.lower()},
        )
