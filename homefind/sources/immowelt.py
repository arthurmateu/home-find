"""Immowelt: only its public SEO result pages work without a browser (the
search API is bot-protected), 30 listings per URL, no paging, and filters in
the path only apply for combinations Immowelt has a landing page for. Treat it
as partial coverage. Results are an LZ-String-compressed JSON blob in the page.
Ad pages, where offline ads are found, are behind DataDome too: only some
lookups get through.
"""

from __future__ import annotations

import json
import re

from ..lzstring import decompress_from_base64
from ..models import Listing
from ..net import Blocked
from ..util import num
from . import Source


class Immowelt(Source):
    name = "immowelt"
    can_probe = True

    def search(self, seen):
        for url in self.opts.get("urls", []):
            yield from parse(self.http.get(url))

    def offline(self, listing: Listing) -> str | None:
        try:
            self.http.get(listing.url)  # 404 ("Diese Anzeige ist nicht mehr verfügbar") or 410 once it's gone
        except Blocked:
            # Ad pages are behind DataDome's bot check, which answers most requests with a 403
            # captcha; the search pages aren't, so this mustn't pause the search (app._blocked).
            raise RuntimeError("its ad pages answered with DataDome's bot check (403)") from None
        return None


def parse(page: str):
    m = re.search(r'window\["__UFRN_FETCHER__"\]=JSON\.parse\((".*?")\);?</script>', page, re.S)
    if not m:
        raise ValueError("result data not found in page (layout changed?)")
    blob = json.loads(json.loads(m.group(1)))["data"]["classified-serp-init-data"]
    props = json.loads(decompress_from_base64(blob))["pageProps"]
    for cid in props.get("classifieds", []):
        c = props["classifiedsData"].get(cid)
        if not c:
            continue
        facts, raw = c.get("hardFacts") or {}, c.get("rawData") or {}
        prices = {p.get("label"): num(p.get("ariaLabel") or p.get("value")) for p in facts.get("prices") or []}
        addr = (c.get("location") or {}).get("address") or {}
        images = (c.get("gallery") or {}).get("images") or []
        first = images[0] if images else {}
        desc = c.get("mainDescription") or {}
        listing = Listing(
            source="immowelt",
            id=c["id"],
            url=c.get("url") or f"https://www.immowelt.de/expose/{c['id'].lower()}",
            title=desc.get("headline", ""),
            description=desc.get("description", ""),
            warm_rent=prices.get("Warmmiete"),
            cold_rent=prices.get("Kaltmiete"),
            size_sqm=num((raw.get("surface") or {}).get("main")),
            rooms=num(raw.get("nbroom")),
            zip_code=addr.get("zipCode"),
            district=addr.get("district"),
            address=addr.get("street"),
            photos=len(images),
            private=c.get("type") == "PRIVATE",
            published=(c.get("metadata") or {}).get("creationDate"),
            image_url=first.get("url") if isinstance(first, dict) else None,
            images=[i["url"] for i in images if isinstance(i, dict) and i.get("url")],
        )
        listing.signals["created"] = listing.published
        listing.signals["provider_city"] = raw.get("providercity")
        if (c.get("tags") or {}).get("hasBrokerageFee"):
            listing.signals["broker_fee"] = True
        yield listing
