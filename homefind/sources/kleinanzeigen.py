"""Kleinanzeigen (ex eBay Kleinanzeigen): many private landlords and Nachmieter
ads, and the most scams. Server-rendered HTML.

The listed price may be cold or warm, so the detail page (Warmmiete,
Nebenkosten, photos, seller account age) is loaded for every candidate.
"""

from __future__ import annotations

import re

from ..models import Listing
from ..util import berlin_zip, num, text
from . import Source

BASE = "https://www.kleinanzeigen.de"


class Kleinanzeigen(Source):
    name = "kleinanzeigen"
    needs_enrich = True

    def search(self, seen):
        cap = int(self.fetch_cap)  # the listed price is usually the cold rent
        for loc in self.opts.get("locations", []):
            for page in range(1, self.opts.get("max_pages", 2) + 1):
                seite = f"seite:{page}/" if page > 1 else ""
                url = f"{BASE}/s-wohnung-mieten/{loc['slug']}/anzeige:angebote/preis::{cap}/{seite}c203l{loc['id']}"
                items = list(self._parse_list(self.http.get(url)))
                fresh = [l for l in items if not seen(l.key)]
                yield from items
                if not fresh or len(items) < 20:
                    break

    def _parse_list(self, page: str):
        for art in re.findall(r"<article\b.*?</article>", page, re.S):
            adid = re.search(r'data-adid="(\d+)"', art)
            href = re.search(r'data-href="([^"]+)"', art)
            if not (adid and href):
                continue
            h3 = re.search(r"<h3\b[^>]*>(.*?)</h3>", art, re.S)
            lines = text(art).split("\n")
            loc = next((x for x in lines if re.match(r"1[0-4]\d{3}\b", x)), "")
            facts = next((x for x in lines if "m²" in x or "Zi." in x), "")
            price = next((x for x in reversed(lines) if "€" in x), "")
            paras = [text(p) for p in re.findall(r"<p\b[^>]*>(.*?)</p>", art, re.S)]
            size = re.search(r"([\d.,]+)\s*m²", facts)
            rooms = re.search(r"([\d.,]+)\s*Zi", facts)
            img = re.search(r'<img[^>]+src="(https://img\.kleinanzeigen\.de[^"]+)"', art)
            listing = Listing(
                source=self.name,
                id=adid.group(1),
                url=BASE + href.group(1),
                title=text(h3.group(1)) if h3 else "",
                size_sqm=num(size.group(1)) if size else None,
                rooms=num(rooms.group(1)) if rooms else None,
                zip_code=berlin_zip(loc),
                district=loc[5:].strip() or None,
                description=next((p for p in paras if len(p) > 25 and "€" not in p and "m²" not in p), ""),
                image_url=img.group(1) if img else None,
            )
            listing.signals["list_price"] = num(price)
            if "VB" in price:
                listing.signals["negotiable"] = True
            if img:
                listing.signals["has_photo"] = True
            yield listing

    def enrich(self, listing: Listing) -> Listing:
        page = self.http.get(listing.url)
        attrs = {}
        for m in re.finditer(r'<li class="addetailslist--detail">(.*?)</li>', page, re.S):
            parts = text(m.group(1).replace("<span", "\n<span")).split("\n")
            if len(parts) >= 2:
                attrs[parts[0].strip()] = parts[1].strip()

        def grab(element_id: str) -> str | None:
            m = re.search(rf'id="{element_id}"[^>]*>(.*?)</(?:p|h1|span|div)>', page, re.S)
            return text(m.group(1)) if m else None

        listing.title = grab("viewad-title") or listing.title
        listing.description = grab("viewad-description-text") or listing.description
        locality = grab("viewad-locality")
        if locality:
            listing.address = locality
            listing.zip_code = berlin_zip(locality) or listing.zip_code
        listed = num(grab("viewad-price")) or listing.signals.get("list_price")

        listing.size_sqm = num(attrs.get("Wohnfläche")) or listing.size_sqm
        listing.rooms = num(attrs.get("Zimmer")) or listing.rooms
        listing.available_from = attrs.get("Verfügbar ab")
        warm = num(attrs.get("Warmmiete"))
        cold = num(attrs.get("Kaltmiete")) or listed
        extra, heating = num(attrs.get("Nebenkosten")), num(attrs.get("Heizkosten"))
        if warm is None and extra is not None and cold is not None:
            warm = cold + extra + (heating or 0)
        listing.warm_rent, listing.cold_rent = warm, cold
        if "nur tausch" in attrs.get("Tauschangebot", "").lower():
            listing.signals["swap_only"] = True

        # Gallery slides only: the page also shows other ads' thumbnails.
        ids = []
        for slide in re.split(r'class="galleryimage-element(?=[\s"])', page)[1:]:
            m = re.search(r"prod-ads/images/(\w\w/[0-9a-f-]{36})", slide[:3000])
            if m and m.group(1) not in ids:
                ids.append(m.group(1))
        listing.images = [f"https://img.kleinanzeigen.de/api/v1/prod-ads/images/{i}?rule=$_59.AUTO" for i in ids]
        listing.photos = max(len(ids), 1 if listing.signals.get("has_photo") else 0)
        if "Privater Nutzer" in page:
            listing.private = True
        elif "Gewerblicher Nutzer" in page:
            listing.private = False
        since = re.search(r"Aktiv seit (\d{2}\.\d{2}\.\d{4})", page)
        if since:
            listing.signals["account_since"] = since.group(1)
        return listing
