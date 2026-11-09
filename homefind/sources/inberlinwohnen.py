"""inberlinwohnen.de: the joint portal of Berlin's six municipal landlords
(degewo, GESOBAU, Gewobag, HOWOGE, STADT UND LAND, WBM).

It is a Laravel Livewire app: each listing's data sits as JSON in a
`wire:snapshot` attribute. Newest first, 10 per page, all of Berlin. Every
`sweep_every_hours` the search goes through all pages (~50), which is how ads
that were taken offline are found.

It has one photo per flat and no description; the ad links to the landlord's
own page, which has the rest (see SITES).
"""

from __future__ import annotations

import html
import json
import logging
import re
from typing import Callable, NamedTuple
from urllib.parse import unquote, urljoin, urlsplit

from ..models import Listing
from ..net import Blocked
from ..util import num, text
from . import Source

log = logging.getLogger("homefind")

URL = "https://www.inberlinwohnen.de/wohnungsfinder/"


class Site(NamedTuple):
    """A landlord's ad page: where its photos are, which parts of its text are the
    ad's own (a heading, up to where the next part starts; both whole lines of
    util.text, as regexes), not the site's menus and boilerplate, and what it says
    once applications are closed (inberlinwohnen drops the ad then)."""
    photos: Callable[[str], list[str]]
    sections: tuple[tuple[str, str], ...]
    closed: str | None = None


def _unique(urls, base: str) -> list[str]:
    return list(dict.fromkeys(urljoin(base, html.unescape(u)) for u in urls))


def _gewobag_photos(page: str) -> list[str]:
    gallery = re.findall(r'<a href="([^"]+/gewo-immo-media/[^"]+\.(?:jpe?g|png|gif|webp))"[^>]*js-gallery-link', page)
    plan = re.findall(r'<img src="([^"]+/gewo-immo-media/[^"]+)" alt="Grundriss"', page)
    return _unique(gallery + plan, "https://www.gewobag.de/")


def _degewo_photos(page: str) -> list[str]:
    # Each photo comes in a dozen sizes (TYPO3's csm_<name>_<hash>), the largest first.
    largest: dict[str, str] = {}
    for m in re.finditer(r'(/fileadmin/user_upload/tx_openimmo/_processed_/[^"\s]*?/csm_([^"/\s]+?)_[0-9a-f]{10}\.(?:jpe?g|png))', page):
        largest.setdefault(m.group(2), m.group(1))
    return _unique(largest.values(), "https://www.degewo.de/")


def _wbm_photos(page: str) -> list[str]:
    return _unique(re.findall(r'(?:https://www\.wbm\.de)?(/fileadmin/user_upload/tx_openimmo/connection-\d+/images/[^"\s]+?\.(?:jpe?g|png))', page),
                   "https://www.wbm.de/")


def _stadtundland_photos(page: str) -> list[str]:
    # Hosted by Wohnungshelden, and only in Next.js' resizer's URLs: url=https%3A%2F%2F...
    return _unique(re.findall(r'https://[\w.-]+/wohnungshelden/[\w-]+\.(?:jpe?g|png|webp)', unquote(page)), "https://stadtundland.de/")


def _berlinovo_photos(page: str) -> list[str]:
    return _unique(re.findall(r'"(/sites/default/files/styles/object_details_main/public/images/wohnungen/[^"]+)"', page),
                   "https://www.berlinovo.de/")


# By host. HOWOGE (its site was down when this was written) and GESOBAU (no ad of theirs
# was online) aren't here: their flats keep inberlinwohnen's one photo.
SITES = {
    # A closed ad stays up, with "Bewerbungsphase abgeschlossen – Angebotsübersicht" over it.
    "www.gewobag.de": Site(_gewobag_photos, (("Merkmale", "Energieausweisdaten"), ("Objektbeschreibung", "Sonstiges$")),
                           closed="Bewerbungsphase abgeschlossen"),
    # Lage, a longer Ausstattung and Wichtige Hinweise come after a map; some ads have no Lage.
    "www.degewo.de": Site(_degewo_photos, (("Ausstattung", "Adresse:"), ("Lage|Ausstattung", "Vermietung$"))),
    "www.wbm.de": Site(_wbm_photos, (("Wohnungsdetails", "Mietkosten"), ("Merkmale", "Grundriss"))),
    "stadtundland.de": Site(_stadtundland_photos, (("Sonstige Informationen", "Kontakt aufnehmen"),)),
    "www.berlinovo.de": Site(_berlinovo_photos, (("Objektnummer", "Vermietungsanfrage"),)),
}


def _site(listing: Listing) -> Site | None:
    return SITES.get(urlsplit(listing.url).hostname or "")


def _sections(page: str, sections) -> str:
    lines, parts, at = text(page).split("\n"), [], 0
    for start, end in sections:
        first = next((i for i in range(at, len(lines)) if re.fullmatch(start, lines[i])), None)
        if first is None:
            continue
        last = next((i for i in range(first + 1, len(lines)) if re.match(end, lines[i])), len(lines))
        parts += lines[first:last]
        at = last
    return "\n".join(parts)


def fill_in(listing: Listing, page: str) -> bool:
    """Takes the photos and the ad's text from the landlord's page, when its site is
    one SITES knows and the page has them. Says whether it found either."""
    site = _site(listing)
    if not site:
        return False
    photos, description = site.photos(page), _sections(page, site.sections)
    if photos:
        listing.images, listing.image_url, listing.photos = photos, photos[0], len(photos)
    if description:
        listing.description = description
    return bool(photos or description)


class InBerlinWohnen(Source):
    name = "inberlinwohnen"
    needs_enrich = True  # the landlord's page, for the photos and the text
    # Between sweeps, ads are looked up on the landlord's page: Gewobag's says when
    # applications are closed, and every page fills in ads stored before it was loaded.
    can_probe = True

    def can_look_up(self, listing: Listing) -> bool:
        return _site(listing) is not None

    def offline(self, listing: Listing) -> str | None:
        try:
            page = self.http.get(listing.url)
        except Blocked as e:  # the landlord's site: inberlinwohnen mustn't be paused for it
            raise RuntimeError(e) from None
        site = _site(listing)
        if site.closed and site.closed in page:
            return "deactivated"
        if fill_in(listing, page):
            listing.signals["enriched"] = True
        return None

    def enrich(self, listing: Listing) -> Listing:
        """Never fails: if the landlord's site is down or blocks us, the listing keeps what
        inberlinwohnen shows (one photo, no text), rather than waiting for it."""
        if not _site(listing):
            return listing
        try:
            page = self.http.get(listing.url)
        except Exception as e:  # noqa: BLE001 - Blocked included: it's the landlord's site, not inberlinwohnen
            log.warning("inberlinwohnen: could not load the landlord's page %s (%s); keeping its one photo",
                        listing.url, e)
            return listing
        if not fill_in(listing, page):
            log.warning("inberlinwohnen: found no photos or text on %s (has the page changed?)", listing.url)
        return listing

    def search(self, seen):
        keys, total = set(), None
        for page in range(1, self.opts.get("max_pages", 100) + 1):
            html_page = self.http.get(URL if page == 1 else f"{URL}?page={page}")
            items = list(parse(html_page))
            if total is None:
                m = re.search(r"von (\d+) Angeboten", html_page)
                total = int(m.group(1)) if m else None
            if not items:
                break
            keys.update(l.key for l in items)
            fresh = [l for l in items if not seen(l.key)]
            yield from items
            if not fresh or total is not None and len(keys) >= total:
                break
        # Paging moves under you when ads come and go, so it only counts if every one went past.
        self.complete = total is not None and len(keys) >= total


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
        listing = Listing(
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
            published=it.get("createdAt"),
            landlord=(company.get("name") or "").strip() or None,
            image_url=f"https://www.inberlinwohnen.de/img/{img}" if img else None,
            images=[f"https://www.inberlinwohnen.de/img/{img}"] if img else [],
            trusted=True,
        )
        for key, signal in (("level", "floor"), ("levelsTotal", "floors")):  # level 0 = ground floor
            if isinstance(it.get(key), int):
                listing.signals[signal] = it[key]
        yield listing
