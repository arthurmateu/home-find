"""Local web UI at http://localhost:<web_port>: browse listings, flip through
photos, save or hide them. Runs inside --loop (or --serve) on 127.0.0.1 only.
"""

from __future__ import annotations

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

from .notify import headline
from .rating import band, rate
from .rules import REASONS
from .util import parse_published
from .store import Store

log = logging.getLogger("homefind")
PAGE = Path(__file__).with_name("ui.html")
CSP = ("default-src 'self'; img-src * data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'")
STATUSES = (None, "saved", "hidden")


def payload(cfg: dict, store: Store) -> dict:
    s = cfg["search"]
    reasons = dict(REASONS)
    if s.get("near_miss_cold_rent"):
        reasons["near_miss"] = f"Near miss (cold ≤ {s['near_miss_cold_rent']} €)"
    checks = [float(v) for v in store.meta_values("last:")]
    checked = datetime.fromtimestamp(max(checks)).strftime("%H:%M") if checks else "never"
    drop = set(s.get("drop_reasons") or [])
    now = datetime.now()
    listings = []
    for row in store.all_rows():
        listing, verdict, first_seen = row.listing, row.verdict, row.first_seen
        if drop & set(verdict.codes):
            continue
        images = listing.images or ([listing.image_url] if listing.image_url else [])
        score, parts = rate(listing, verdict, first_seen, cfg, now)
        posted = parse_published(listing.published, first_seen)
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
        })
    area = " · ".join(cfg["area"]["names"])
    return {"listings": listings, "reasons": reasons,
            "subtitle": f"{area} · up to {s['max_warm_rent']} € warm · last check {checked}"}


class Handler(BaseHTTPRequestHandler):
    server_version = "homefind"
    cfg: dict = {}

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

    def _payload(self) -> dict:
        store = Store(self.cfg["run"]["db_path"])
        try:
            return payload(self.cfg, store)
        finally:
            store.close()

    def do_GET(self):
        if not self._local():
            return self._send(403, b"forbidden")
        path = urlsplit(self.path).path
        if path == "/":
            data = json.dumps(self._payload(), ensure_ascii=False).replace("</", "<\\/")
            page = PAGE.read_text(encoding="utf-8").replace("__DATA__", data)
            return self._send(200, page.encode(), "text/html; charset=utf-8", {"Content-Security-Policy": CSP})
        if path == "/api/listings":
            return self._send(200, json.dumps(self._payload(), ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        self._send(404, b"not found")

    def do_POST(self):
        # JSON only: a cross-site form can't send that without a CORS preflight, which we never answer.
        if not self._local() or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._send(403, b"forbidden")
        if urlsplit(self.path).path != "/api/status":
            return self._send(404, b"not found")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            key, status = body["key"], body.get("status")
        except (ValueError, KeyError, TypeError):
            return self._send(400, b"bad request")
        if status not in STATUSES:
            return self._send(400, b"bad status")
        store = Store(self.cfg["run"]["db_path"])
        try:
            found = store.set_status(key, status)
        finally:
            store.close()
        self._send(204 if found else 404)


def url(cfg: dict) -> str:
    return f"http://localhost:{cfg['run']['web_port']}"


def serve(cfg: dict) -> ThreadingHTTPServer | None:
    """Start the UI in a background thread. Raises OSError if the port is taken."""
    if not cfg["run"].get("web_port"):
        return None
    Handler.cfg = cfg
    server = ThreadingHTTPServer(("127.0.0.1", int(cfg["run"]["web_port"])), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    return server


def open_browser(target: str) -> None:
    if os.environ.get("WSL_DISTRO_NAME") and shutil.which("powershell.exe"):  # WSL: use the Windows browser
        subprocess.Popen(["powershell.exe", "-NoProfile", "-Command", f"Start-Process '{target}'"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        webbrowser.open(target)
