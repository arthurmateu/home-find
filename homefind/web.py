"""Local web UI at http://localhost:<web_port>: browse listings, flip through
photos, apply (apply.py), mark the ones you applied to or hide them. A flat on
several sites gets one card (see flats). Runs inside --loop (or --serve) on
127.0.0.1 only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import app, apply, geo
from .notify import headline
from .rating import band, rate
from .rules import REASONS, street_budget, street_key, street_stretch
from .util import parse_published, street_of
from .store import Row, Store

log = logging.getLogger("homefind")
PAGE = Path(__file__).with_name("ui.html")
CSP = ("default-src 'self'; img-src * data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'")
STATUSES = (None, "applied", "hidden")
FIRST_CHOICE = "immoscout"  # a flat on several sites: its card shows and opens this site's ad, while it's online
COPY_DAYS = 30              # ads first seen this far apart aren't taken for the same flat (as in Store.repost_of)


def _address(listing) -> tuple[str | None, str | None, str | None]:
    """Street, house number and its letter, spelling-proof ("Schlangenbader Str. 20 C" ->
    "schlangenbader strasse", "20", "c"); None for what the address doesn't say."""
    street = street_of(listing.address)
    if not street:
        return None, None, None
    m = re.match(r"(.+?)\s+(\d+)\s*([a-z]\b)?", street_key(street))
    return (m.group(1), m.group(2), m.group(3)) if m else (street_key(street), None, None)


def _copies(a: Row, b: Row) -> bool:
    """Two ads with the same fingerprint (app.fingerprint), on two sites: the same flat, unless their
    cold rents, streets or house numbers differ, or they were first seen COPY_DAYS or more apart."""
    la, lb = a.listing, b.listing
    if la.source == lb.source or (la.cold_rent and lb.cold_rent and abs(la.cold_rent - lb.cold_rent) > 1):
        return False
    if any(x and y and x != y for x, y in zip(_address(la), _address(lb))):
        return False
    return abs(datetime.fromisoformat(a.first_seen) - datetime.fromisoformat(b.first_seen)) < timedelta(days=COPY_DAYS)


def _lead(r: Row) -> tuple:
    """Of a flat's ads, the one its card shows and opens: online, then FIRST_CHOICE's, then the first seen."""
    return r.gone is not None, r.listing.source != FIRST_CHOICE, r.first_seen


def flats(cfg: dict, store: Store) -> list[list[Row]]:
    """The listings the web UI shows (all but drop_reasons), newest first, grouped by flat: the
    same flat on several sites (see _copies) is one group, its lead first (see _lead). Where one
    site has two ads that could be it (identical flats in one building), all of them stay apart."""
    drop = set(cfg["search"].get("drop_reasons") or [])
    rows = [r for r in store.all_rows() if not drop & set(r.verdict.codes)]
    alike = defaultdict(list)
    for r in rows:
        if r.verdict.warm and r.listing.size_sqm and (r.listing.zip_code or r.listing.district):
            alike[app.fingerprint(r.listing, r.verdict)].append(r)
    grouped = {}  # listing key -> its flat
    for left in alike.values():
        while left:
            flat, todo = [], [left.pop()]
            while todo:  # every ad that's a copy of one in the flat
                r = todo.pop()
                flat.append(r)
                rest = []
                for o in left:
                    (todo if _copies(r, o) else rest).append(o)
                left = rest
            if len(flat) > 1 and len({r.listing.source for r in flat}) == len(flat):
                flat.sort(key=_lead)
                grouped.update((r.listing.key, flat) for r in flat)
    out, done = [], set()
    for r in rows:
        flat = grouped.get(r.listing.key, [r])
        if flat[0].listing.key not in done:
            done.add(flat[0].listing.key)
            out.append(flat)
    return out


CHOICE_RANK = {"applied": 2, "hidden": 1}


def _state(flat: list[Row]) -> dict:
    """Where the flat's card goes: where you put any of its ads, Applied before Hidden (Mark
    applied and Hide go to all of them, but an ad that came later, or the one the Firefox helper
    marked applied, may be alone in it). Taken offline once every ad is."""
    s = max(flat, key=lambda r: (CHOICE_RANK.get(r.choice, 0), r.applied_at is not None))
    state = {k: getattr(s, k) for k in ("status", "gone", "gone_at", "status_if_back", "hide_at", "applied_at",
                                        "applied_by")}
    if s.gone and not flat[0].gone:  # still online on another site
        state.update(status=s.choice, status_if_back=s.choice, gone=None, gone_at=None, hide_at=None)
    return state


def build() -> str:
    """Changes whenever ui.html does, so an open page can tell it's out of date."""
    return hashlib.sha1(PAGE.read_bytes()).hexdigest()[:12]


def code(cfg: dict) -> str:
    """The Python code and the config file as they are on disk now. Both are only read
    at startup (ui.html on every page load), so when this changes homefind needs a restart."""
    h = hashlib.sha1()
    for f in [*sorted(Path(__file__).parent.rglob("*.py")), Path(cfg.get("path") or "")]:
        h.update(f.read_bytes() if f.is_file() else b"")
    return h.hexdigest()[:12]


def payload(cfg: dict, store: Store, loop: app.Loop | None = None, applier: apply.Applier | None = None) -> dict:
    s = cfg["search"]
    reasons = dict(REASONS)
    if s.get("near_miss_cold_rent"):
        reasons["near_miss"] = f"Near miss (cold ≤ {s['near_miss_cold_rent']} €)"
    if s.get("street_stretch_warm_rent"):
        reasons["stretch"] = " / ".join(f"{street} (warm ≤ {budget:g} €)"
                                        for street, budget in s["street_stretch_warm_rent"].items())
    now = datetime.now()
    places = store.places()
    listings = []
    for flat in flats(cfg, store):
        row = flat[0]
        listing, verdict, first_seen = row.listing, row.verdict, row.first_seen
        images = listing.images or ([listing.image_url] if listing.image_url else [])
        score, parts = rate(listing, verdict, first_seen, cfg, now)
        posted = parse_published(listing.published, first_seen)
        place = geo.locate(listing, places)
        listings.append({
            "key": listing.key,
            "source": listing.source,
            "url": listing.url,
            "title": listing.title,
            "headline": headline(listing, verdict),
            "landlord": listing.landlord,
            "first_seen": first_seen,  # this ad's: one on ImmoScout24 after another site had the flat is news
            "last_seen": row.last_seen,
            "images": [u for u in images if u and "%" not in u],  # skip URL templates
            "description": listing.description,
            "ok": verdict.ok,
            **_state(flat),  # status, gone, gone_at, status_if_back, hide_at, applied_at, applied_by
            # On several sites: each one's ad, this card's first. Empty when it's only on one.
            "ads": [{"key": r.listing.key, "source": r.listing.source, "url": r.listing.url, "gone": r.gone,
                     "gone_at": r.gone_at, "ok": r.verdict.ok, "reasons": r.verdict.reasons}
                    for r in flat] if len(flat) > 1 else [],
            "applying": applier and applier.step(listing.key),
            "reasons": verdict.reasons,
            "codes": verdict.codes,
            "flags": verdict.flags,
            "notes": verdict.notes,
            "score": score,
            "band": band(score),
            "why": [[p, label] for p, label in parts],
            "warm": verdict.warm or listing.warm_rent,
            "size": listing.size_sqm,
            "posted": (posted.isoformat(timespec="seconds") if posted else first_seen),
            "geo": place and {k: place[k] for k in ("lat", "lon", "precision", "radius")},  # None until looked up
            "locality": geo.locality(listing, place),
            "street": street_stretch(listing, cfg)[1] or street_budget(listing, cfg)[1],
        })
    # Streets with a budget or stretch of their own, for the Where filter.
    streets = list(dict.fromkeys([*(s.get("street_stretch_warm_rent") or {}), *(s.get("street_max_warm_rent") or {})]))
    return {"listings": listings, "reasons": reasons, "streets": streets,
            "highlight": cfg["run"].get("highlight_sources") or [],
            "checks": app.status(cfg, store, loop), "build": build(),
            "apply": applier and {"mode": apply.mode(store), "modes": apply.MODES, "problem": applier.problem(),
                                  "sources": apply.SOURCES, "min_score": cfg["apply"]["min_score"],
                                  "max_per_day": cfg["apply"]["max_per_day"], "helper": helper(store)},
            "outdated": code(cfg) != Handler.started}


def helper(store: Store) -> str | None:
    """The Firefox helper (apply.user.js) as last heard from: None (never), "current" or "outdated"."""
    seen = json.loads(store.meta_get("apply_helper") or "null")
    return seen and ("current" if seen["version"] == apply.version() else "outdated")


class Handler(BaseHTTPRequestHandler):
    server_version = "homefind"
    cfg: dict = {}
    loop: app.Loop | None = None  # the --loop run this UI belongs to; None with --serve
    applier: apply.Applier | None = None
    started = ""                  # code() when it started

    def log_message(self, fmt, *args):
        log.debug("web: " + fmt, *args)

    def _local(self) -> bool:
        # Rejects requests addressed to any other host name (DNS rebinding).
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("localhost", "127.0.0.1", "::1")

    def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read(self, listings: bool = True) -> dict:
        """The payload, or (listings=False) just the status for the header."""
        store = Store(self.cfg["run"]["db_path"])
        try:
            return payload(self.cfg, store, self.loop, self.applier) if listings else app.status(self.cfg, store, self.loop)
        finally:
            store.close()

    def do_GET(self):
        if not self._local():
            return self._send(403, b"forbidden")
        path = urlsplit(self.path).path
        if path == "/":
            data = json.dumps(self._read(), ensure_ascii=False).replace("</", "<\\/")
            page = PAGE.read_text(encoding="utf-8").replace("__DATA__", data)
            return self._send(200, page.encode(), "text/html; charset=utf-8", {"Content-Security-Policy": CSP})
        if path in ("/api/listings", "/api/checks"):  # /api/checks: polled every few seconds while a check runs
            data = self._read(listings=path == "/api/listings")
            return self._send(200, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        if self.applier and path == "/homefind.user.js":  # the Firefox helper, for Tampermonkey to install
            return self._send(200, apply.userscript(self.cfg).encode(), "text/javascript; charset=utf-8")
        if self.applier and path == "/api/apply-profile":  # the helper, about to fill in a form
            profile, problem = apply.load_profile(self.cfg["apply"]["profile"])
            if problem:
                return self._send(409, problem.encode())
            return self._send(200, json.dumps(profile, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        if self.applier and path == "/api/apply-hello":  # the helper, on an ad: it's installed, and which version
            version = parse_qs(urlsplit(self.path).query).get("version", [""])[0]
            store = Store(self.cfg["run"]["db_path"])
            try:
                store.meta_set("apply_helper", json.dumps({"version": version, "at": datetime.now().isoformat()}))
            finally:
                store.close()
            return self._send(204)
        self._send(404, b"not found")

    def do_POST(self):
        # JSON only: a cross-site form can't send that without a CORS preflight, which we never answer.
        if not self._local() or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._send(403, b"forbidden")
        path = urlsplit(self.path).path
        if path == "/api/check":
            if not self.loop:
                return self._send(409, b"not checking for ads: homefind runs with --serve")
            self.loop.ask()
            return self._send(202)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return self._send(400, b"bad request")
        if path in ("/api/apply", "/api/apply-mode", "/api/apply-report"):
            return self._apply(path, body)
        if path != "/api/status":
            return self._send(404, b"not found")
        try:
            key, status, applied_at = body["key"], body.get("status"), body.get("applied_at")
            if applied_at is not None:
                datetime.fromisoformat(applied_at)
        except (ValueError, KeyError, TypeError):
            return self._send(400, b"bad request")
        if status not in STATUSES:
            return self._send(400, b"bad status")
        store = Store(self.cfg["run"]["db_path"])
        try:  # the flat's ad on every site it's on
            flat = next((f for f in flats(self.cfg, store) if any(r.listing.key == key for r in f)), [])
            found = sum(store.set_status(k, status, applied_at) for k in [r.listing.key for r in flat] or [key])
        finally:
            store.close()
        self._send(204 if found else 404)

    def _apply(self, path: str, body: dict):
        """✉ Apply on a card ({key}: it opened the ad for the Firefox helper), auto-apply's
        mode ({mode}), and what the helper reports ({key, step, ...}, see Applier.report)."""
        if not self.applier:
            return self._send(409, b"applying is off")
        if path == "/api/apply-mode" and body.get("mode") not in apply.MODES:
            return self._send(400, b"bad mode")
        if path != "/api/apply-mode" and not isinstance(body.get("key"), str):
            return self._send(400, b"bad request")
        if path == "/api/apply-report" and body.get("step") not in (*apply.STEPS, "form"):
            return self._send(400, b"bad step")
        store = Store(self.cfg["run"]["db_path"])
        try:
            if path == "/api/apply-mode":
                store.meta_set("apply_mode", body["mode"])
                log.info("auto-apply: %s", apply.MODES[body["mode"]].lower())
            elif path == "/api/apply-report":
                return self._send(204 if self.applier.report(body["key"], body, store) else 404)
            else:
                self.applier.opened(body["key"])
        finally:
            store.close()
        self._send(204)


def url(cfg: dict) -> str:
    return f"http://localhost:{cfg['run']['web_port']}"


def serve(cfg: dict, loop: app.Loop | None = None, applier: apply.Applier | None = None) -> ThreadingHTTPServer | None:
    """Start the UI in a background thread. Raises OSError if the port is taken."""
    if not cfg["run"].get("web_port"):
        return None
    Handler.cfg, Handler.loop, Handler.applier, Handler.started = cfg, loop, applier, code(cfg)
    server = ThreadingHTTPServer(("127.0.0.1", int(cfg["run"]["web_port"])), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    return server


def open_browser(target: str) -> None:
    # Best effort: failing here must not look like the server failing (e.g. WSL with a broken /mnt/c).
    try:
        if os.environ.get("WSL_DISTRO_NAME") and shutil.which("powershell.exe"):  # WSL: use the Windows browser
            subprocess.Popen(["powershell.exe", "-NoProfile", "-Command", f"Start-Process '{target}'"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            webbrowser.open(target)
    except OSError as e:
        log.warning("could not open a browser (%s); go to %s yourself", e.strerror, target)
