"""WG-Gesucht, flats only (1-Zimmer-Wohnung + Wohnung) with open-ended leases.

It rate-limits aggressively, so this source loads one result page per run.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlencode

from ..models import Listing
from ..util import berlin_zip, num, text
from . import Source

BASE = "https://www.wg-gesucht.de"
LIST_PAGE = "1-zimmer-wohnungen-und-wohnungen-in-Berlin.8.1+2.1.0.html"  # 1-Zimmer-Wohnungen + Wohnungen
DESCRIPTION_END = re.compile(r"^(Benötigte Unterlagen|Angaben zum Objekt|Statistiken|Kontakt)")
DEACTIVATED = re.compile(r"Diese Anzeige ist (momentan )?deaktiviert|Die Anzeige in [^<]{0,200} ist deaktiviert")
MONTHS = ["januar", "februar", "märz", "april", "mai", "juni", "juli", "august", "september", "oktober",
          "november", "dezember"]


class WgGesucht(Source):
    name = "wggesucht"
    needs_enrich = True
    can_probe = True

    def search(self, seen):
        params = [("offer_filter", "1"), ("city_id", "8"), ("sort_order", "0"), ("noDeact", "1"),
                  ("categories[]", "1"), ("categories[]", "2"),
                  ("rent_types[]", "2"),  # 2 = unbefristet (1 = befristet, 3 = overnight stays)
                  # WG-Gesucht filters on total rent.
                  ("rMax", str(int(self.fetch_cap)))]
        params += [("ot[]", str(d)) for d in self.opts.get("districts", [126])]
        yield from self._parse_list(self.http.get(f"{BASE}/{LIST_PAGE}?{urlencode(params)}"))

    def _parse_list(self, page: str):
        # Below the real results WG-Gesucht shows rotating promos from all of Berlin.
        page = page.split("Weitere Angebote von verifizierten", 1)[0]
        # Each ad is a link: <a id="liste-details-ad-123" class="wgg-list-card ..." href="..." aria-label="title">
        for card in re.split(r'(?=<a\s+id="liste-details-ad-)', page)[1:]:
            head = re.match(r"<a\s[^>]*>", card).group(0)
            if "data-campaign_partner=" in head or "data-url=" in head:  # partner ads (Wunderflats, HousingAnywhere, ...): furnished, temporary
                continue
            link = re.search(r'href="(/[\w-]*wohnungen-in-[\w.-]+?\.(\d+)\.html)"', head)
            if not link:
                continue
            title = re.search(r'aria-label="([^"]*)"', head)
            photo = re.search(r'data-src="(https://img\.wg-gesucht\.de/[^"]+)"', card[:5000])
            # The text, without the photo slider: "Privat", "1150 € pro Monat", the title,
            # "3-Zimmer-Wohnung · Berlin Kreuzberg · Urbanstraße 30", "68 m²", "01.02.2027",
            # "Name Online: 1 Std.", then most of it again.
            body = re.sub(r'<div class="wgg_card_slider.*?</div>\s*</div>\s*</div>', "", card[:20000], flags=re.S)
            lines = text(body).split("\n")
            parts = [p.strip() for p in next((x for x in lines if " · " in x), "").split(" · ")]
            rooms = re.match(r"([\d,.]+)-Zimmer", parts[0])
            price = next((x for x in lines if re.fullmatch(r"[\d.]+\s*€ pro Monat", x)), None)
            size = next((x for x in lines if re.fullmatch(r"[\d.,]+\s*m²", x)), None)
            dates = next((x for x in lines if re.match(r"\d{2}\.\d{2}\.\d{4}", x)), "")
            listing = Listing(
                source=self.name,
                id=link.group(2),
                url=BASE + link.group(1),
                title=html.unescape(title.group(1)).strip() if title else "",
                warm_rent=num(price),  # WG-Gesucht shows the total rent
                size_sqm=num(size),
                rooms=num(rooms.group(1)) if rooms else None,
                district=parts[1].replace("Berlin", "").strip() or None if len(parts) > 1 else None,
                address=parts[2] if len(parts) > 2 else None,
                image_url=photo.group(1) if photo else None,
            )
            if " - " in dates:
                listing.signals["temporary"] = True
            if lines and lines[0].startswith(("Privat", "Unternehmen")):
                listing.private = lines[0].startswith("Privat")
            online = re.search(r"Online:\s*(?:update\s*)?([^\n]{1,25})", "\n".join(lines))  # "update" is an icon
            if online:
                listing.published = online.group(1).strip()
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
        online = re.search(r"(?m)^Online:\s*\n?([^\n]+)", t)
        if online:
            listing.published = online.group(1).strip()
        if re.search(r"(?m)^frei bis:?\s*\n?\s*\d{2}\.\d{2}\.\d{4}", t):
            listing.signals["temporary"] = True
        abloese = num(after("Ablösevereinbarung"))  # "150€" or "n.a."
        if abloese:
            listing.signals["abloese"] = abloese
        # "Angaben zum Objekt" is one line per icon: Altbau, EG, möbliert / teilmöbliert, Dusche, ...
        details = re.split(r"(?m)^Angaben zum Objekt$", t, maxsplit=1)
        furnished = re.search(r"(?m)^(teil)?möbliert$", details[1][:1000]) if len(details) > 1 else None
        if furnished:
            listing.signals["part_furnished" if furnished.group(1) else "furnished"] = True
        # The poster's box: "Private:r Nutzer:in", "Mitglied seit Juni 2012".
        if re.search(r"(?m)^Private:r Nutzer:in$", t):
            listing.private = True
        since = re.search(r"Mitglied seit (\w+) (\d{4})", t)
        if since and since.group(1).lower() in MONTHS:  # the 1st of the month, so the age is never underestimated
            listing.signals["account_since"] = f"01.{MONTHS.index(since.group(1).lower()) + 1:02d}.{since.group(2)}"

        start = page.find('id="ad_description_text"')
        if start != -1:
            desc = []
            for line in text(page[start - 50: start + 30000]).split("\n"):
                if DESCRIPTION_END.match(line):
                    break
                desc.append(line)
            listing.description = "\n".join(desc)
        # Listing photos appear as ".small." thumbnails (the profile picture only as
        # ".sized."); the same path with ".large." is the full-size photo.
        seen, images = set(), []
        for url in re.findall(r"https://img\.wg-gesucht\.de/media/up/[\d/]+/[0-9a-f]{64}_[^\"'\s]*?\.small\.\w+", page):
            digest = re.search(r"/([0-9a-f]{64})_", url).group(1)
            if digest not in seen:
                seen.add(digest)
                images.append(url.replace(".small.", ".large."))
        listing.images = images
        listing.photos = len(images)
        return listing

    def offline(self, listing: Listing) -> str | None:
        # A deactivated ad still loads, with a notice; the search leaves those out (noDeact=1).
        url, page = self.http.fetch(listing.url)
        if f".{listing.id}.html" not in url:
            return "deleted"
        return "deactivated" if DEACTIVATED.search(page) else None
