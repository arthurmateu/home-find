"""Local web UI at http://localhost:<web_port>: browse listings, flip through
photos, apply (apply.py), mark the ones you applied to or hide them. Runs inside
--loop (or --serve) on 127.0.0.1 only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import app, apply, geo
from .notify import headline
from .rating import band, rate
from .rules import REASONS, street_budget, street_stretch
from .util import parse_published
from .store import Store

log = logging.getLogger("homefind")
PAGE = Path(__file__).with_name("ui.html")
CSP = ("default-src 'self'; img-src * data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'")
STATUSES = (None, "applied", "hidden")


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
    drop = set(s.get("drop_reasons") or [])
    now = datetime.now()
    places = store.places()
    listings = []
    for row in store.all_rows():
        listing, verdict, first_seen = row.listing, row.verdict, row.first_seen
        if drop & set(verdict.codes):
            continue
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
            "first_seen": first_seen,
            "last_seen": row.last_seen,
            "images": [u for u in images if u and "%" not in u],  # skip URL templates
            "description": listing.description,
            "ok": verdict.ok,
            "status": row.status,
            "gone": row.gone,
            "gone_at": row.gone_at,
            "status_if_back": row.status_if_back,
            "hide_at": row.hide_at,
            "applied_at": row.applied_at,
            "applied_by": row.applied_by,
            "applying": applier and applier.steps.get(listing.key),
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
                                  "max_per_day": cfg["apply"]["max_per_day"]},
            "outdated": code(cfg) != Handler.started}


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
        if path in ("/api/apply", "/api/apply-mode", "/api/browser"):
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
        try:
            found = store.set_status(key, status, applied_at)
        finally:
            store.close()
        self._send(204 if found else 404)

    def _apply(self, path: str, body: dict):
        """Apply on a card ({key}, or {key, show} to bring its form up), auto-apply's
        mode ({mode}), and the window itself, to log in to ImmoScout24."""
        if not self.applier:
            return self._send(409, b"applying is off")
        if path == "/api/apply-mode":
            if body.get("mode") not in apply.MODES:
                return self._send(400, b"bad mode")
            store = Store(self.cfg["run"]["db_path"])
            try:
                store.meta_set("apply_mode", body["mode"])
            finally:
                store.close()
            log.info("auto-apply: %s", apply.MODES[body["mode"]].lower())
            return self._send(204)
        problem = self.applier.problem()
        if problem:
            return self._send(409, problem.encode())
        if path == "/api/browser":
            self.applier.window()
        elif not isinstance(body.get("key"), str):
            return self._send(400, b"bad request")
        elif body.get("show"):
            self.applier.show(body["key"])
        else:
            self.applier.fill(body["key"])
        self._send(202)


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
