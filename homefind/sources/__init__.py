"""Listing sources. Each yields `Listing`s from search results; sources with
`needs_enrich` also load the detail page, but only for new listings that pass
the cheap pre-check, to keep request counts low.

Ads that were taken offline are found one of two ways: `offline()` looks up a
single ad (`can_probe`), or a search that went through every ad on the site
sets `complete`, so any stored ad it didn't yield is gone."""

from __future__ import annotations

from typing import Callable, Iterator

from ..models import Listing


class Source:
    name = ""
    needs_enrich = False
    can_probe = False  # offline() works
    complete = False   # set by search(): it just yielded every ad that's online

    def __init__(self, cfg: dict, http):
        self.cfg, self.http = cfg, http
        self.opts = cfg["sources"].get(self.name, {})

    @property
    def fetch_cap(self) -> float:
        """Price cap for searching: also covers near misses (cold rent within
        search.near_miss_cold_rent), which the rules then file under Rejected."""
        s = self.cfg["search"]
        return float(max(s["max_warm_rent"], s.get("near_miss_cold_rent") or 0))

    @property
    def near_miss(self) -> bool:
        return bool(self.cfg["search"].get("near_miss_cold_rent"))

    def search(self, seen: Callable[[str], bool]) -> Iterator[Listing]:
        """`seen(key)` lets paginating sources stop once they reach known listings."""
        raise NotImplementedError

    def enrich(self, listing: Listing) -> Listing:
        return listing

    def offline(self, listing: Listing) -> str | None:
        """'deactivated' or 'deleted' if the ad was taken offline, None if it's
        still up. HTTP 404/410 count as deleted; anything else it can't tell raises."""
        raise NotImplementedError


def enabled_sources(cfg: dict, http) -> list[Source]:
    from .charlotte1907 import Charlotte1907
    from .immoscout import ImmoScout
    from .immowelt import Immowelt
    from .inberlinwohnen import InBerlinWohnen
    from .kleinanzeigen import Kleinanzeigen
    from .wggesucht import WgGesucht

    classes = [InBerlinWohnen, Charlotte1907, ImmoScout, Kleinanzeigen, WgGesucht, Immowelt]
    return [c(cfg, http) for c in classes if cfg["sources"].get(c.name, {}).get("enabled", True)]


SOURCE_NAMES = ["inberlinwohnen", "charlotte1907", "immoscout", "kleinanzeigen", "wggesucht", "immowelt"]
