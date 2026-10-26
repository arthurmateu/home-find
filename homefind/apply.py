"""Applying to ads through Firefox. A userscript for Tampermonkey (apply.user.js,
served by the web UI at /homefind.user.js) fills in ImmoScout24's contact form
on the ad's page with what's in apply.toml, and tells homefind whenever a form
is sent there, so the ad moves to Applied (also when you apply without it).

✉ Apply on a card opens the ad in a new tab with #homefind=fill: the form gets
filled in, for you to check and press Abschicken. Auto-apply (Off / Fill in, I
send / Send by itself, in the header) opens new matches in Firefox by itself:
ImmoScout24 ones scoring at least apply.min_score, without ⚠ warnings and not
reposts. "Send by itself" (#homefind=send) presses Abschicken too, at most
apply.max_per_day a day; a form that asks for something apply.toml doesn't
cover is left waiting for you instead.

The userscript reports each step (POST /api/apply-report), which the cards show.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import tomllib
from datetime import date, datetime
from pathlib import Path

from .notify import Notifier, headline
from .store import Store

log = logging.getLogger("homefind")
SOURCES = ["immoscout"]  # the ones whose contact form the userscript knows
MODES = {"off": "Off", "confirm": "Fill in, I send", "send": "Send by itself"}
STEPS = ("opening", "filling", "ready", "sending", "sent", "failed")
USERSCRIPT = Path(__file__).with_name("apply.user.js")
FIREFOX = "/mnt/c/Program Files/Mozilla Firefox/firefox.exe"  # Windows' Firefox, from WSL
REPORT_SECONDS = 120  # an ad opened for the userscript that hasn't reported back by then: it isn't there
NO_HELPER = ("Firefox didn't fill it in: is the homefind helper installed in Tampermonkey? "
             "(Install Firefox helper, in the header)")


def load_profile(path: str) -> tuple[dict | None, str | None]:
    """apply.toml's message and form fields, or why it can't be used."""
    p = Path(path)
    if not p.exists():
        return None, f"no {p.name} yet: copy apply.example.toml to {p.name} and fill it in"
    try:
        with p.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        return None, f"{p.name} has a mistake in it: {e}"
    message = str(data.get("message") or "").strip()
    if not message:
        return None, f"{p.name} has no message"
    return {"message": message, "fields": data.get("form") or {}}, None


def mode(store: Store) -> str:
    m = store.meta_get("apply_mode")
    return m if m in MODES else "off"


def version() -> str:
    """The userscript's version: Tampermonkey updates it when this goes up."""
    return f"1.{int(USERSCRIPT.stat().st_mtime)}"


def userscript(cfg: dict) -> str:
    return USERSCRIPT.read_text(encoding="utf-8").replace("__PORT__", str(cfg["run"]["web_port"])) \
                                                  .replace("__VERSION__", version())


def open_in_firefox(url: str, configured: str = "") -> None:
    """In a new tab of Firefox (Windows' one under WSL), or else the default browser."""
    exe = configured or next((p for p in (FIREFOX, shutil.which("firefox")) if p and os.access(p, os.X_OK)), None)
    try:
        if exe:  # in a session of its own: Ctrl+C on homefind mustn't close a Firefox it started
            subprocess.Popen([exe, "-new-tab", url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return
    except OSError as e:
        log.warning("apply: couldn't start Firefox (%s); trying the default browser", e.strerror)
    from .web import open_browser
    open_browser(url)


class Applier:
    """Where each application is at (for the cards), auto-apply, and what the userscript reports."""

    def __init__(self, cfg: dict, notifier: Notifier):
        self.cfg, self.opts, self.notifier = cfg, cfg["apply"], notifier
        self.report_path = Path(cfg["run"]["db_path"]).parent / "apply-last.json"
        self.steps: dict[str, dict] = {}  # listing key -> {step, note, at, auto, send}

    def problem(self) -> str | None:
        """Why it can't apply, or None."""
        return load_profile(self.opts["profile"])[1]

    def step(self, key: str) -> dict | None:
        """Where `key`'s application is at: {step, note, at}, or None."""
        s = self.steps.get(key)
        if s and s["step"] == "opening" and time.time() - s["at"] > REPORT_SECONDS:
            s.update(step="failed", note=NO_HELPER)
        return s and {k: s[k] for k in ("step", "note", "at")}

    def opened(self, key: str) -> None:
        """✉ Apply on a card: the page opened the ad in a new tab, for the userscript to fill in."""
        self.steps[key] = {"step": "opening", "note": "Opening the ad in Firefox…", "at": time.time(),
                           "auto": False, "send": False}

    def auto(self, listing, verdict, score: int, store: Store) -> None:
        """A new match that isn't a repost: auto-apply opens it in Firefox if it's on and the match qualifies."""
        how = mode(store)
        if how == "off" or listing.source not in SOURCES or verdict.flags or score < self.opts["min_score"]:
            return
        problem = self.problem()
        if problem:
            log.warning("auto-apply is on, but %s", problem)
            return
        send = how == "send" and self._sent_today(store) < self.opts["max_per_day"]
        if how == "send" and not send:
            log.info("auto-apply: %d sent today already (apply.max_per_day), only filling it in", self.opts["max_per_day"])
        log.info("auto-apply (%s): %s, score %d", "send" if send else "fill in", listing.url, score)
        self.steps[listing.key] = {"step": "opening", "note": "Opening the ad in Firefox…", "at": time.time(),
                                   "auto": True, "send": send}
        open_in_firefox(f"{listing.url}#homefind={'send' if send else 'auto'}", self.opts.get("firefox") or "")

    def report(self, key: str, body: dict, store: Store) -> bool:
        """What the userscript says about an ad: a step with a note (and `by` for sent: homefind or
        you), or the form it filled in (step "form", kept in data/apply-last.json). False for an ad
        homefind doesn't know."""
        row = store.row(key)
        if body["step"] == "form":
            self._keep(key, body)
            return bool(row)
        if not row:
            return False
        before = self.steps.get(key, {"auto": False, "send": False})
        if before.get("step") == "sent" and body["step"] == "sent":
            return True  # told twice
        s = self.steps[key] = {**before, "step": body["step"], "note": str(body.get("note") or ""), "at": time.time()}
        log.info("apply %s: %s", key, s["note"])
        listing = row.listing
        if s["step"] == "sent":
            by_homefind = body.get("by") == "homefind"
            if row.status != "applied" or by_homefind:
                store.set_status(key, "applied", datetime.now().isoformat(timespec="seconds"),
                                 "homefind" if by_homefind else None)
            if by_homefind:
                store.meta_set("apply_sent", f"{date.today().isoformat()} {self._sent_today(store) + 1}")
                self.notifier.push(f"✓ Applied · {headline(listing)}", f"homefind sent your message: {listing.title}",
                                   listing.url)
        elif s["step"] == "ready" and s["send"]:
            self.notifier.push("Couldn't apply by itself", f"{headline(listing)}: {s['note']}", listing.url, warn=True)
        elif s["step"] == "failed" and s["auto"]:
            self.notifier.push("Couldn't apply", f"{headline(listing)}: {s['note']}", listing.url, warn=True)
        return True

    def _sent_today(self, store: Store) -> int:
        day, _, n = (store.meta_get("apply_sent") or "").partition(" ")
        return int(n) if day == date.today().isoformat() else 0

    def _keep(self, key: str, body: dict) -> None:
        """What the last form asked for and how it went, for when a field doesn't get filled in."""
        try:
            self.report_path.write_text(json.dumps({"key": key, "url": body.get("url"),
                                                    "at": datetime.now().isoformat(timespec="seconds"),
                                                    **(body.get("form") or {})}, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
        except OSError:
            pass
