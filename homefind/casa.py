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

from . import geo, rating, web
from .net import BROWSER_UA
from .store import Row, Store

log = logging.getLogger("homefind")
HERE = Path(__file__).parent
PHOTO_TYPES = {"image/webp": ".webp", "image/jpeg": ".jpg", "image/png": ".png"}
PHOTO_GAP_SECONDS = 0.5   # between two photos fetched from the portals
POKE_DELAY_SECONDS = 20   # after a change on your page, in case more follow
PIN_GRID = (0.003, 0.005)  # map pins are rounded to this, in degrees of latitude and longitude: about 330 m
STAGE_ORDER = {"got_it": 0, "viewing": 1, "replied": 2}  # these come first on the page

# What rating.py and rules.py write (the start of it), in the page's words; None: known, but not for the page.
# Whatever isn't here stays off the page, so a street, a station or a link in the English can't get onto it.
# In Portuguese, keeping the German terms for what its reader knows from renting in Germany.
SAID = [
    # the points under a score
    (r"([\d.]+) €/m² warm", r"\1 €/m² warm"),
    (r"(\d+) % under budget", r"\1% abaixo do orçamento"),
    (r"(\d+) % over budget", r"\1% acima do orçamento"),
    (r"([\d.]+) m²", r"Tamanho de \1 m²"),
    (r"([\d.]+) rooms", r"\1 Zimmer"),
    (r"posted |first seen ", "Anúncio recente"),
    (r"(\d+) photo", r"\1 fotos"),
    (r"municipal / co-op landlord", "Dono municipal ou Genossenschaft"),
    (r"verified landlord", "Dono verificado"),
    (r"detailed description", "Descrição detalhada"),
    (r"in the old area", "Na região preferida"),
    (r"on [^:]+$", "Numa rua de que gosto"),
    (r"(~?[\d.]+) km from work", r"\1 km do trabalho"),
    (r"(~?[\d.]+) km from ", r"\1 km de um lugar frequente"),
    (r"work (\d+) min walk", r"Trabalho a \1 min a pé"),
    (r".+ (\d+) min walk", r"Lugar frequente a \1 min a pé"),
    (r"scam warning signs", "Sinais de alerta"),
    (r"warm rent is an estimate", "Warmmiete estimada"),
    # the chips that go with them
    (r"balcony", "Varanda"),
    (r"fitted kitchen", "Einbauküche"),
    (r"lift", "Elevador"),
    (r"Altbau", "Altbau"),
    (r"garden", "Jardim"),
    (r"floor (\d+) of (\d+)", r"\1º andar de \2"),
    (r"floor (\d+)", r"\1º andar"),
    (r"top floor", "Último andar"),
    (r"(Hinterhaus|Seitenflügel|Quergebäude|Gartenhaus)", r"\1"),  # the page explains these: they're very Berlin
    (r"faces the courtyard", "Virado para o pátio"),
    (r"semi-basement", "Souterrain"),
    (r"too residential\?", "Talvez bem residencial"),
    (r"too residential", "Região bem residencial"),
    # the notes
    (r"heads-up: only ([\d.]+) room", r"Só \1 Zimmer"),
    (r"Nachmieter", "Nachmieter: o dono ainda precisa aceitar"),
    (r"asks for Ablöse", "Pede Ablöse"),
    (r"warm rent estimated", "Warmmiete estimada"),
    (r"partly furnished", "Parcialmente mobiliado"),
    (r"furnished", "Mobiliado"),
    (r"WBS", "Precisa de WBS"),
    (r"co-op members", "Só para sócios da Genossenschaft"),
    (r"ImmoScout Plus members", "Só para assinantes Plus, por enquanto"),
    (r"identity-verified landlord", "Dono verificado"),
    (r"warm rent taken from|on [^:]+: budget|same rent/size/postcode|new on \w+, same", None),
    # the scam signs
    (r"only one photo", "Só uma foto"),
    (r"e-mail address in the text", "E-mail no texto do anúncio"),
    (r"mentions a broker fee", "Menciona Provision"),
    (r"unusually cheap", "Preço baixo demais"),
    (r"implausible", "Cômodos e tamanho não batem"),
    (r"rent marked as negotiable", "Aluguel negociável"),
    (r"seller account is", "Conta nova do anunciante"),
    (r"online for", "No ar há muito tempo"),
    (r"private landlord based in", "Dono mora em outra cidade"),
    (r"asks for money up front", "Pede dinheiro adiantado"),
    (r"keys by post", "Chaves pelo correio ou sem visita"),
    (r"charges a viewing", "Cobra taxa de visita ou reserva"),
    (r"landlord says they are abroad", "Dono diz que está no exterior"),
    (r"mentions Airbnb", "Menciona Airbnb ou Booking"),
    (r"wants to move to WhatsApp", "Quer ir para WhatsApp ou Telegram"),
    (r"private landlord", "Dono particular"),
]
WALK = {rating.RAIL: "U/S-Bahn", rating.STOP: "Ônibus ou Tram", rating.SHOP: "Supermercado", rating.GREEN: "Parque ou lago"}
HEADS_UP = re.compile(r"heads-up|Nachmieter|asks for Ablöse|warm rent estimated|furnished|partly furnished|WBS|co-op")  # as in ui.html
NICE = re.compile(r"ImmoScout Plus|identity-verified")
_unsaid: set[str] = set()  # told about once each


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
    """Marked applied, whether it's still on your Applied tab or not (ones that went offline
    before dates were kept only have status_if_back to say so)."""
    return r.status == "applied" or r.applied_at is not None or r.status_if_back == "applied"


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
    for url in listing.photo_urls():
        if len(names) == most:
            break
        name = _photo(_phone_sized(url), folder, known)
        if name and name not in names:
            names.append(name)
    return names


def _said(text: str) -> str | None:
    """`text` in the page's words; None if it isn't for the page (see SAID)."""
    for pattern, words in SAID:
        m = re.match(pattern, text)
        if m:
            return words and re.sub(r"(\d)\.(\d)", r"\1,\2", m.expand(words))
    shape = re.sub(r"\d+", "#", text)
    if shape not in _unsaid:
        _unsaid.add(shape)
        log.info("family page: left out, no Portuguese for it in casa.SAID: %s", shape)
    return None


def _part(p: rating.Part) -> str | None:
    """A line under the score. What's within walking distance is said by its kind, not its name."""
    walk = re.search(r", (\d+) min walk$", p.label)
    if walk and p.points in WALK:
        return f"{WALK[p.points]} a {walk.group(1)} min a pé"
    return _said(p.chip or p.label)


def _chips(verdict, parts: list[rating.Part]) -> dict[str, list[str]]:
    """The notes on the card, in the web UI's rows: Atenção its Cons (scam signs first), Tem its Pros, Bom saber its Info."""
    heads = [_said(flag) or "Sinal de alerta" for flag in verdict.flags]
    nice, info = [], []
    for p in parts:
        if p.chip:
            (nice if p.points > 0 else heads).append(_part(p))
    for note in verdict.notes:
        plus = re.match(r"ImmoScout Plus members only until (\S+)", note)
        if not (plus and plus.group(1) < date.today().isoformat()):  # that's over
            (heads if HEADS_UP.match(note) else nice if NICE.match(note) else info).append(_said(note))
    return {name: list(dict.fromkeys(filter(None, group))) for name, group in (("heads", heads), ("nice", nice), ("info", info))}


def _entry(flat: list[Row], cfg: dict, places: dict, folder: Path, known: dict) -> dict:
    """Everything the page gets to know about a flat. No address, title, landlord, link or text from the
    ad, and nothing about your application but its date: the page is public to whoever has the link."""
    row, listing = flat[0], flat[0].listing
    place = geo.locate(listing, places)
    dates = [r.applied_at for r in flat if r.applied_at]
    rent = row.verdict.warm or listing.warm_rent
    score, parts = rating.rate(listing, row.verdict, row.first_seen, cfg, place=place)
    return {
        "score": score,
        "band": rating.band(score),
        "why": sorted(([p.points, t] for p in parts if p.points and (t := _part(p))), key=lambda w: -w[0]),
        **_chips(row.verdict, parts),
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
        "photos": _photos(listing, folder, known, int(cfg["casa"]["max_photos"])),
    }


def snapshot(cfg: dict, store: Store, folder: Path, known: dict) -> dict:
    """What the page shows: active applications first, then the latest."""
    places = store.places()
    flats = [f for f in web.flats(cfg, store) if any(_applied(r) for r in f)]
    entries = [_entry(f, cfg, places, folder, known) for f in flats]
    entries.sort(key=lambda e: e["applied"] or "", reverse=True)
    entries.sort(key=lambda e: STAGE_ORDER.get(e["stage"], len(STAGE_ORDER)))
    return {"built": int(time.time() * 1000), "flats": entries}


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
        self.uploading = False  # homefind waits for it to be done before restarting into an update
        self._poke = threading.Event()

    def poke(self) -> None:
        self._poke.set()

    def _once(self) -> None:
        with Store(self.cfg["run"]["db_path"]) as store:
            try:
                self.uploading = True
                publish(self.cfg, store)
                if self.failing:
                    log.info("family page: uploading works again")
                self.failing = False
            except Exception as e:  # noqa: BLE001 - the flat hunt goes on without it
                if not self.failing:  # said once, not at every try
                    log.warning("family page: couldn't upload it (%s); will keep trying", problem(e))
                self.failing = True
            finally:
                self.uploading = False

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
