"""WG-Gesucht, flats only (1-Zimmer-Wohnung + Wohnung) with open-ended leases.

It rate-limits aggressively, so this source loads one result page per run.
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

from ..models import Listing
from ..util import berlin_zip, num, text
from . import Source

BASE = "https://www.wg-gesucht.de"
DESCRIPTION_END = re.compile(r"^(Benötigte Unterlagen|Angaben zum Objekt|Statistiken|Kontakt)")


class WgGesucht(Source):
    name = "wggesucht"
    needs_enrich = True

    def search(self, seen):
        params = [("offer_filter", "1"), ("city_id", "8"), ("sort_order", "0"), ("noDeact", "1"),
                  ("categories[]", "1"), ("categories[]", "2"),
                  ("rent_types[]", "2"),  # 2 = unbefristet (1 = befristet, 3 = overnight stays)
                  ("rMax", str(int(self.cfg["search"]["max_warm_rent"])))]
        params += [("ot[]", str(d)) for d in self.opts.get("districts", [126])]
        yield from self._parse_list(self.http.get(f"{BASE}/wohnungen-in-Berlin.8.2.1.0.html?{urlencode(params)}"))

    def _parse_list(self, page: str):
        # Below the real results WG-Gesucht shows rotating promos from all of Berlin.
        page = page.split("Weitere Angebote von verifizierten", 1)[0]
        for chunk in page.split('class="wgg_card offer_list_item')[1:]:
            head = chunk[:400]
            if "data-url=" in head:  # partner ads (Wunderflats, HousingAnywhere, ...): furnished, temporary
                continue
            link = re.search(r'href="(/[\w-]*wohnungen-in-[\w.-]+?\.(\d+)\.html)"', chunk)
            if not link:
                continue
            chunk = chunk[:10000]
            lines = text(chunk).split("\n")
            h2 = re.search(r'class="truncate_title[^"]*"[^>]*>(.*?)</h\d>', chunk, re.S)
            info = next((x for x in lines if "|" in x and "Zimmer" in x), "")
            parts = [p.strip() for p in info.split("|")]
            price = next((x for x in lines if re.fullmatch(r"\d[\d.]*\s*€", x)), None)
            size = next((x for x in lines if re.fullmatch(r"\d+\s*m²", x)), None)
            dates = next((x for x in lines if re.match(r"\d{2}\.\d{2}\.\d{4}", x)), "")
            listing = Listing(
                source=self.name,
                id=link.group(2),
                url=BASE + link.group(1),
                title=text(h2.group(1)) if h2 else "",
                warm_rent=num(price),  # WG-Gesucht shows the total rent
                size_sqm=num(size),
                rooms=num(parts[0]) if parts and parts[0] else None,
                district=parts[1].replace("Berlin", "").strip() or None if len(parts) > 1 else None,
                address=parts[2] if len(parts) > 2 else None,
                available_from=dates[:10] or None,
            )
            if " - " in dates:
                listing.signals["temporary"] = True
            yield listing

    def enrich(self, listing: Listing) -> Listing:
        page = self.http.get(listing.url)
        t = text(page)

        def after(label: str) -> str | None:
            m = re.search(rf"(?m)^{label}\s*:?\s*\n?([^\n]+)", t)
            return m.group(1).strip() if m else None

        listing.warm_rent = num(after("Gesamtmiete")) or listing.warm_rent
        listing.cold_rent = num(after("Miete")) or listing.cold_rent
        addr = re.search(r"(?m)^Adresse\n([^\n]+)\n([^\n]+)", t)
        if addr:
            listing.address = f"{addr.group(1)}, {addr.group(2)}"
            listing.zip_code = berlin_zip(addr.group(2)) or listing.zip_code
        if re.search(r"(?m)^frei bis:?\s*\n?\s*\d{2}\.\d{2}\.\d{4}", t):
            listing.signals["temporary"] = True

        start = page.find('id="ad_description_text"')
        if start != -1:
            desc = []
            for line in text(page[start - 50: start + 30000]).split("\n"):
                if DESCRIPTION_END.match(line):
                    break
                desc.append(line)
            listing.description = "\n".join(desc)
        # Listing photos come in ".small." variants; the profile picture is ".sized." only.
        listing.photos = len(set(re.findall(r"media/up/[\d/]+/([0-9a-f]{64})_[^\"'\s]*?\.small\.", page)))
        return listing
