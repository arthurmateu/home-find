"""ImmoScout24, via the endpoints its mobile app uses.

The website is behind a bot wall, but api.mobile.immobilienscout24.de answers
plain requests. Swap offers are excluded server-side; in Charlottenburg under
1000 € warm they are ~85% of all results.
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

from ..models import Listing
from ..util import berlin_zip, num
from . import Source

API = "https://api.mobile.immobilienscout24.de"
APP_UA = "ImmoScout_27.12_26.2_._"
HEADERS = {"Accept": "application/json"}


class ImmoScout(Source):
    name = "immoscout"
    needs_enrich = True

    def search(self, seen):
        s = self.cfg["search"]
        geocodes = ",".join(self.opts.get("geocodes", []))
        if geocodes:
            for page in range(1, self.opts.get("max_pages", 3) + 1):
                params = {
                    "searchType": "region",
                    "geocodes": geocodes,
                    "realestatetype": "apartmentrent",
                    "price": f"-{float(s['max_warm_rent']):.1f}",
                    "pricetype": "calculatedtotalrent",
                    "livingspace": f"{float(s['min_size_sqm']):.1f}-",
                    "numberofrooms": f"{float(s['min_rooms']):.1f}-",
                    "exclusioncriteria": "swapflat",
                    "sorting": "-firstactivation",
                    "pagenumber": page,
                }
                data = self.http.json(f"{API}/search/list?{urlencode(params)}", method="POST",
                                      data={"supportedResultListType": [], "userData": {}},
                                      ua=APP_UA, headers=HEADERS)
                items = [self._item(r["item"]) for r in data.get("resultListItems", [])
                         if r.get("type") == "EXPOSE_RESULT"]
                fresh = [l for l in items if not seen(l.key)]
                yield from items
                if not fresh or page >= (data.get("numberOfPages") or 1):
                    break

    def _item(self, it: dict) -> Listing:
        attrs = [a.get("value", "") for a in it.get("attributes", [])]
        line = (it.get("address") or {}).get("line", "")
        district = re.sub(r"\s*\(.*?\)", "", line.rsplit(",", 1)[-1]).strip() if "," in line else None
        listing = Listing(
            source=self.name,
            id=str(it["id"]),
            url=f"https://www.immobilienscout24.de/expose/{it['id']}",
            title=it.get("title", ""),
            cold_rent=next((num(a) for a in attrs if "€" in a), None),  # the list shows cold rent
            size_sqm=next((num(a) for a in attrs if "m²" in a), None),
            rooms=next((num(a) for a in attrs if "Zi" in a), None),
            zip_code=berlin_zip(line),
            district=district,
            address=line,
            private=it.get("isPrivate"),
            published=it.get("published"),
            image_url=(it.get("titlePicture") or {}).get("full"),
        )
        paywall = it.get("paywallListing") or {}
        if paywall.get("active"):
            listing.signals["is24_plus_until"] = (paywall.get("earlyAccessExpiresAt") or "")[:10] or "soon"
        return listing

    def enrich(self, listing: Listing) -> Listing:
        d = self.http.json(f"{API}/expose/{listing.id}", ua=APP_UA, headers=HEADERS)
        a = d.get("adTargetingParameters") or {}
        listing.warm_rent = num(a.get("obj_totalRent")) or listing.warm_rent
        listing.cold_rent = num(a.get("obj_baseRent")) or listing.cold_rent
        listing.size_sqm = num(a.get("obj_livingSpace")) or listing.size_sqm
        listing.rooms = num(a.get("obj_noRooms")) or listing.rooms
        listing.zip_code = a.get("obj_zipCode") or listing.zip_code
        if a.get("geo_ot"):
            listing.district = a["geo_ot"].replace("_", "-").title()
        if "obj_picturecount" in a:
            listing.photos = int(num(a["obj_picturecount"]) or 0)
        if "obj_privateOffer" in a:
            listing.private = a["obj_privateOffer"] == "true"

        texts, attrs = [], {}
        for sec in d.get("sections", []):
            kind = sec.get("type")
            if kind == "TEXT_AREA":
                texts.append(f"{sec.get('title', '')}: {sec.get('text', '')}")
            elif kind == "ATTRIBUTE_LIST":
                for at in sec.get("attributes", []):
                    label = at.get("label", "").rstrip(":").strip()
                    attrs[label] = at.get("text") or ("ja" if at.get("type") == "CHECK" else "")
            elif kind == "AGENTS_INFO":
                listing.landlord = sec.get("company") or sec.get("name") or listing.landlord
                if sec.get("verifiedBy"):
                    listing.signals["verified_landlord"] = True
            elif kind == "MAP":
                addr = ", ".join(x for x in (sec.get("addressLine1"), sec.get("addressLine2")) if x)
                listing.address = addr or listing.address
        listing.description = "\n".join(texts)
        listing.available_from = attrs.get("Bezugsfrei ab") or listing.available_from
        wbs = next((v for k, v in attrs.items() if "WBS" in k or "Wohnberechtigungsschein" in k), None)
        if wbs is not None:
            listing.wbs_required = wbs.strip().lower() in ("ja", "erforderlich")
        return listing
