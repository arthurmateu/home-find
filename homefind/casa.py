"""The family page (casa.admou.se): a read-only page in Brazilian Portuguese to follow the flats
you've applied to, on a phone.

homefind builds it here as plain files and uploads them to the web server with rsync, so the
page keeps working while this PC is off, and nothing on the internet can reach the real app.
Only what _entry picks out goes on the page. It lives under a long random path: the private
link. See casa.html for the page, and casa-setup.sh for the server's side.
"""

from __future__ import annotations

import fcntl
import hashlib
import http.client
import json
import logging
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from . import geo, web
from .net import BROWSER_UA, Fetcher
from .store import Row, Store

log = logging.getLogger("homefind")
HERE = Path(__file__).parent
EUR_IN_BRL = "https://api.frankfurter.dev/v1/latest?base=EUR&symbols=BRL"  # the ECB's daily rate
PHOTO_TYPES = {"image/webp": ".webp", "image/jpeg": ".jpg", "image/png": ".png"}
PHOTO_GAP_SECONDS = 0.5   # between two photos fetched from the portals
POKE_DELAY_SECONDS = 20   # after a change on your page, in case more follow
PIN_GRID = (0.003, 0.005)  # map pins are rounded to this, in degrees of latitude and longitude: about 330 m
STAGE_ORDER = {"got_it": 0, "viewing": 1, "replied": 2}  # these come first on the page


def enabled(cfg: dict) -> bool:
    return all(cfg["casa"][k] for k in ("host", "remote_dir", "url"))


def _token(store: Store) -> str:
    token = store.meta_get("casa_token")
    if not token:
        token = secrets.token_urlsafe(24)
        store.meta_set("casa_token", token)
    return token


def link(cfg: dict, store: Store) -> str:
    return f"{cfg['casa']['url'].rstrip('/')}/{_token(store)}/"


def _root(cfg: dict) -> Path:
    return Path(cfg["run"]["db_path"]).parent / "casa"


def new_link(cfg: dict, store: Store) -> None:
    """Replaces the private link; the next upload takes the old one off the server."""
    old, site = _token(store), _root(cfg) / "site"
    store.meta_set("casa_token", secrets.token_urlsafe(24))
    if (site / old).exists():
        (site / old).rename(site / _token(store))


def _applied(r: Row) -> bool:
    """On your Applied tab, or with the date you applied. Not the ads that only still carry the
    tag from before there were dates, when the button said Saved: saving wasn't applying."""
    return r.status == "applied" or r.applied_at is not None


def _phone_sized(url: str) -> str:
    """ImmoScout24's photo addresses say the size; the others' are fine as they are."""
    return re.sub(r"/resize/\d+x\d+>?/", "/resize/800x600/", url)


def _photo(url: str, folder: Path, known: dict) -> str | None:
    """The file name of the page's copy of a photo, fetched the first time, so that it outlives
    the ad. `known` remembers them by address (None: the site doesn't have it any more)."""
    if url in known and (known[url] is None or (folder / known[url]).exists()):
        return known[url]
    request = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "image/webp,image/*;q=0.8"})
    try:
        with urllib.request.urlopen(request, timeout=25) as r:
            body, kind = r.read(), r.headers.get_content_type()
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            known[url] = None
        return None
    except (OSError, http.client.HTTPException):  # tried again at the next build
        return None
    finally:
        time.sleep(PHOTO_GAP_SECONDS)
    if kind not in PHOTO_TYPES or not body:
        known[url] = None
        return None
    known[url] = hashlib.sha1(body).hexdigest()[:16] + PHOTO_TYPES[kind]
    (folder / known[url]).write_bytes(body)
    return known[url]


def _photos(listing, folder: Path, known: dict, most: int) -> list[str]:
    names: list[str] = []
    for url in listing.images or ([listing.image_url] if listing.image_url else []):
        if len(names) == most:
            break
        name = "%" not in url and _photo(_phone_sized(url), folder, known)  # "%": a URL template
        if name and name not in names:
            names.append(name)
    return names


def _entry(flat: list[Row], places: dict, folder: Path, known: dict, most: int) -> dict:
    """Everything the page gets to know about a flat. No address, title, landlord, link or text from
    the ad, and nothing about your application but its date: the page is public to whoever has the link."""
    row, listing = flat[0], flat[0].listing
    place = geo.locate(listing, places)
    dates = [r.applied_at for r in flat if r.applied_at]
    rent = row.verdict.warm or listing.warm_rent
    return {
        "id": hashlib.sha1(listing.key.encode()).hexdigest()[:10],
        "area": geo.locality(listing, place) or "Berlim",
        "rooms": listing.rooms,
        "size": round(listing.size_sqm) if listing.size_sqm else None,
        "rent": round(rent) if rent else None,
        "approx": listing.warm_rent is None,  # the ad didn't say: taken from its text or worked out from the cold rent
        "applied": min(dates)[:10] if dates else None,
        "stage": next((r.stage for r in flat if r.stage), None),
        "online": row.gone is None,  # flat[0] is one that's online, if any is (see web.flats)
        "pin": place and [round(round(place[k] / g) * g, 4) for k, g in zip(("lat", "lon"), PIN_GRID)],
        "photos": _photos(listing, folder, known, most),
    }


def _brl(store: Store) -> dict | None:
    """What a euro is in reais, asked for once a day; the last known one if that fails."""
    saved = json.loads(store.meta_get("casa_brl") or "null")
    today = date.today().isoformat()
    if saved and saved["asked"] == today:
        return saved
    try:
        got = Fetcher(delay=0, retries=1).json(EUR_IN_BRL)
        saved = {"rate": float(got["rates"]["BRL"]), "date": got["date"], "asked": today}
        store.meta_set("casa_brl", json.dumps(saved))
    except Exception as e:  # noqa: BLE001 - the page does without reais
        log.debug("casa: no exchange rate (%s)", e)
    return saved


def snapshot(cfg: dict, store: Store, folder: Path, known: dict) -> dict:
    """What the page shows: active applications first, then the latest."""
    places = store.places()
    flats = [f for f in web.flats(cfg, store) if any(_applied(r) for r in f)]
    entries = [_entry(f, places, folder, known, int(cfg["casa"]["max_photos"])) for f in flats]
    entries.sort(key=lambda e: e["applied"] or "", reverse=True)
    entries.sort(key=lambda e: STAGE_ORDER.get(e["stage"], len(STAGE_ORDER)))
    brl = _brl(store)
    return {"built": int(time.time() * 1000), "flats": entries, "brl": brl and {k: brl[k] for k in ("rate", "date")}}


def build(cfg: dict, store: Store) -> Path:
    """Writes the page under data/casa/site/<private link>/ and returns data/casa/site."""
    root = _root(cfg)
    site = root / "site"
    folder = site / _token(store)
    (folder / "photos").mkdir(parents=True, exist_ok=True)
    index = root / "photos.json"
    known = json.loads(index.read_text()) if index.exists() else {}
    data = snapshot(cfg, store, folder / "photos", known)
    index.write_text(json.dumps(known))
    used = {name for f in data["flats"] for name in f["photos"]}
    for p in (folder / "photos").iterdir():
        if p.name not in used:  # a flat that came off the page
            p.unlink()
    page = (HERE / "casa.html").read_text(encoding="utf-8")
    (folder / "index.html").write_text(page.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")),
                                       encoding="utf-8")
    for size in (180, 512):
        shutil.copyfile(HERE / f"casa-icon-{size}.png", folder / f"icon-{size}.png")
    (folder / "manifest.webmanifest").write_text(json.dumps({
        "name": "A busca pelo apê", "short_name": "Apê em Berlim", "lang": "pt-BR", "start_url": "./", "scope": "./",
        "display": "standalone", "background_color": "#f6f2ea", "theme_color": "#f6f2ea",
        "icons": [{"src": f"icon-{s}.png", "sizes": f"{s}x{s}", "type": "image/png", "purpose": "any maskable"}
                  for s in (180, 512)],
    }, ensure_ascii=False), encoding="utf-8")
    return site


def publish(cfg: dict, store: Store) -> None:
    """Builds the page and uploads it. Raises if the upload fails."""
    c = cfg["casa"]
    _root(cfg).mkdir(parents=True, exist_ok=True)
    with (_root(cfg) / "lock").open("w") as lock:  # one upload at a time: --loop's and --casa's
        fcntl.flock(lock, fcntl.LOCK_EX)
        site = build(cfg, store)
        subprocess.run(["rsync", "-rltp", "--chmod=D755,F644", "--delete-delay", "--delay-updates",
                        "-e", "ssh -o BatchMode=yes -o ConnectTimeout=15", f"{site}/", f"{c['host']}:{c['remote_dir'].rstrip('/')}/"],
                       check=True, capture_output=True, text=True, timeout=600)


def problem(e: Exception) -> str:
    """What went wrong with an upload, in a line."""
    if isinstance(e, subprocess.CalledProcessError) and e.stderr.strip():
        return e.stderr.strip().splitlines()[0]  # ssh's or rsync's own explanation; the last line is just its exit code
    return str(e)


class Publisher:
    """Keeps the page up to date while homefind runs: every `every_minutes`, so "updated x
    minutes ago" says the PC is on, and soon after you change something on your own page."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.failing = False
        self._poke = threading.Event()

    def poke(self) -> None:
        self._poke.set()

    def _once(self) -> None:
        store = Store(self.cfg["run"]["db_path"])
        try:
            publish(self.cfg, store)
            if self.failing:
                log.info("family page: uploading works again")
            self.failing = False
        except Exception as e:  # noqa: BLE001 - the flat hunt goes on without it
            if not self.failing:  # said once, not at every try
                log.warning("family page: couldn't upload it (%s); will keep trying", problem(e))
            self.failing = True
        finally:
            store.close()

    def _run(self) -> None:
        while True:
            self._once()
            if self._poke.wait(float(self.cfg["casa"]["every_minutes"]) * 60):
                time.sleep(POKE_DELAY_SECONDS)
                self._poke.clear()


def start(cfg: dict) -> Publisher | None:
    """Starts keeping the page up to date in the background, if it's set up in [casa]."""
    if not enabled(cfg):
        return None
    publisher = Publisher(cfg)
    threading.Thread(target=publisher._run, name="casa", daemon=True).start()
    return publisher
