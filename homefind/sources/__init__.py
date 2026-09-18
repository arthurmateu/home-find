"""Listing sources. Each yields `Listing`s from search results; sources with
`needs_enrich` also load the detail page, but only for new listings that pass
the cheap pre-check, to keep request counts low."""

from __future__ import annotations

from typing import Callable, Iterator

from ..models import Listing


class Source:
    name = ""
    needs_enrich = False

    def __init__(self, cfg: dict, http):
        self.cfg, self.http = cfg, http
        self.opts = cfg["sources"].get(self.name, {})

    def search(self, seen: Callable[[str], bool]) -> Iterator[Listing]:
        """`seen(key)` lets paginating sources stop once they reach known listings."""
        raise NotImplementedError

    def enrich(self, listing: Listing) -> Listing:
        return listing


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
