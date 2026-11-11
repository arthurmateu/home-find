"""Every photo of the flats you've applied to, and the PDFs their ad links to (ImmoScout24's
floor plans, see Listing.documents), kept in data/photos/, so they outlive the ad.
Landlords often take an ad down within hours, sometimes right after inviting you to a
viewing, and the portals delete its photos with it.

While --loop or --serve runs, it fetches the ones it doesn't have yet: right after you mark
a flat applied or the Firefox helper sends an application (see Keeper.poke), and every
SWEEP_MINUTES for any a site didn't answer for. That's for the flat's ad on every site it's
on. Each ad gets a folder (immoscout-172530239) with its photos, 01.webp, 02.jpg, ..., in the
ad's order, and kept.json: each photo's address and its file there, or None if the site
didn't have it any more and neither did the family page (casa.copies). Its PDFs are there too,
named after their label on the ad (JG_12_GR_Vermietung.pdf), listed in documents.json the same
way. The web UI shows the kept ones (see urls and documents).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import unicodedata
from pathlib import Path

from . import casa, net, web
from .models import Listing
from .store import Store

log = logging.getLogger("homefind")
SWEEP_MINUTES = 15       # how often it looks again, besides right after you apply
PHOTO_GAP_SECONDS = 0.5  # between two photos fetched from the portals
INDEX = "kept.json"
DOCUMENTS = "documents.json"


def root(cfg: dict) -> Path:
    return Path(cfg["run"]["db_path"]).parent / "photos"


def folder_name(key: str) -> str:
    """The ad's folder: "immoscout:172530239" -> "immoscout-172530239" (charlotte1907's have slashes)."""
    return re.sub(r"[^\w.-]", "_", key.replace(":", "-", 1))


def kept(cfg: dict, key: str, index: str = INDEX) -> dict[str, str | None]:
    """The ad's photos (or with DOCUMENTS, PDFs) looked for so far: address -> file name, or None if
    the site didn't have it any more."""
    try:
        return json.loads((root(cfg) / folder_name(key) / index).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def urls(cfg: dict, listing: Listing) -> list[str]:
    """The ad's photos for the web UI: the kept copy where there is one (web.py serves them under
    /photos/), the site's where it hasn't been fetched yet, and none for one that's lost."""
    have, folder = kept(cfg, listing.key), folder_name(listing.key)
    return [f"/photos/{folder}/{have[u]}" if have.get(u) else u
            for u in listing.photo_urls() if u not in have or have[u]]


def documents(cfg: dict, listing: Listing) -> list[dict]:
    """The ad's PDFs for the web UI, [{label, url}]: the kept copy where there is one, else the site's."""
    have, folder = kept(cfg, listing.key, DOCUMENTS), folder_name(listing.key)
    return [{"label": d["label"], "url": f"/photos/{folder}/{have[d['url']]}" if have.get(d["url"]) else d["url"]}
            for d in listing.documents]


def document_name(label: str, taken) -> str:
    """A file name for a PDF, from its label on the ad: "Grundriß 2. OG.pdf" -> "Grundri_2._OG.pdf"
    ("Grundriss" in ASCII as far as it goes); not one of `taken`."""
    ascii_label = unicodedata.normalize("NFKD", re.sub(r"\.pdf$", "", label, flags=re.I)).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^A-Za-z0-9.-]+", "_", ascii_label).strip("._-")[:80] or "document"
    name, n = f"{stem}.pdf", 2
    while name in taken:
        name, n = f"{stem}-{n}.pdf", n + 1
    return name


def _write(path: Path, body: bytes) -> None:
    """All of it or nothing, should homefind restart halfway (see update.py)."""
    part = path.with_name(path.name + ".part")
    part.write_bytes(body)
    os.replace(part, path)


def keep(cfg: dict, listing: Listing, copy) -> int:
    """Fetches the ad's photos and PDFs it doesn't have yet; how many it got. `copy(url)`: the family
    page's copy of a photo the site doesn't have any more (see casa.copies). That's looked for
    again for the lost ones every time: it's only a look on disk, and the family page may have
    been writing its list just then."""
    have = kept(cfg, listing.key)
    folder = root(cfg) / folder_name(listing.key)
    got = 0
    for url in listing.photo_urls():
        if have.get(url):
            continue
        if url in have:  # lost
            photo = copy(url)
            if not photo:
                continue
        else:
            try:
                photo = net.photo(url)
            except OSError:  # tried again at the next sweep
                continue
            finally:
                time.sleep(PHOTO_GAP_SECONDS)
            photo = photo or copy(url)
        folder.mkdir(parents=True, exist_ok=True)
        if photo:
            body, extension = photo
            have[url] = f"{sum(1 for name in have.values() if name) + 1:02d}{extension}"
            _write(folder / have[url], body)
            got += 1
        else:
            have[url] = None
        _write(folder / INDEX, json.dumps(have, indent=1).encode())
    return got + _keep_documents(cfg, listing)


def _keep_documents(cfg: dict, listing: Listing) -> int:
    """Fetches the ad's PDFs it doesn't have yet (one the site didn't have is given up on); how many it got."""
    have = kept(cfg, listing.key, DOCUMENTS)
    folder = root(cfg) / folder_name(listing.key)
    got = 0
    for doc in listing.documents:
        url = doc["url"]
        if url in have:
            continue
        try:
            body = net.document(url)
        except OSError:  # tried again at the next sweep
            continue
        finally:
            time.sleep(PHOTO_GAP_SECONDS)
        folder.mkdir(parents=True, exist_ok=True)
        if body:
            have[url] = document_name(doc.get("label") or "", set(filter(None, have.values())))
            _write(folder / have[url], body)
            got += 1
        else:
            have[url] = None
        _write(folder / DOCUMENTS, json.dumps(have, indent=1, ensure_ascii=False).encode())
    return got


def sweep(cfg: dict, poked=lambda: False) -> int:
    """Keeps the photos of every flat you've applied to, the ads still online first; how many
    it fetched. Stops early once `poked()`, so a flat you've just applied to goes first."""
    with Store(cfg["run"]["db_path"]) as store:
        rows = [r for flat in web.flats(cfg, store) if any(r.applied for r in flat) for r in flat]
        copy = casa.copies(cfg, store)
    rows.sort(key=lambda r: r.gone is not None)  # otherwise newest first, as web.flats has them
    got = 0
    for r in rows:
        if poked():
            break
        got += keep(cfg, r.listing, copy)
    return got


class Keeper:
    """Keeps them while homefind runs: at the start, every SWEEP_MINUTES, and right after you apply."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._poke = threading.Event()

    def poke(self) -> None:
        """You may have marked a flat applied (web.py), or the helper sent an application."""
        self._poke.set()

    def _run(self) -> None:
        while True:
            self._poke.clear()
            try:
                got = sweep(self.cfg, self._poke.is_set)
                if got:
                    log.info("kept %d photo%s or PDF%s of flats you applied to, in %s", got, "s"[got == 1:],
                             "s"[got == 1:], root(self.cfg))
            except Exception:  # noqa: BLE001 - the flat hunt goes on without it
                log.exception("couldn't keep the photos of the flats you applied to")
            self._poke.wait(SWEEP_MINUTES * 60)


def start(cfg: dict) -> Keeper:
    keeper = Keeper(cfg)
    threading.Thread(target=keeper._run, name="keep", daemon=True).start()
    return keeper
