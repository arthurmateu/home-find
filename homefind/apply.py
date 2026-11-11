"""Applying to ads through Firefox. A userscript for Tampermonkey (apply.user.js,
served by the web UI at /homefind.user.js) fills in the contact form on the ad's
page with what's in apply.toml, on the sites in FORMS (ImmoScout24, its German
and English pages, and Immowelt), and tells homefind whenever a form is sent
there, so the ad moves to Applied (also when you apply without it).

Three switches in the header (SWITCHES) say what it does by itself:
- Autofill: ✉ Apply on a card opens the ad in a new tab with #homefind=fill: the
  form gets filled in, for you to check and send. Off, it only opens the ad.
- Autofill + send: ✉ Apply opens it with #homefind=send, which presses the send
  button too.
- Full auto: new flats that aren't reposts (a flat's first ad on ImmoScout24 isn't
  one, see app._first_here) are opened in Firefox by themselves, ImmoScout24's
  (SOURCES) only: a Match (rules.tier) gets sent (#homefind=send), at most
  apply.max_per_day a day and only without ⚠ warnings; a Good one, or a Match
  that can't be sent, gets the form filled in for you (#homefind=auto).
A form that asks for something apply.toml doesn't cover is never sent: it's left
waiting for you.

The userscript reports each step (POST /api/apply-report), which the cards show.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
import tomllib
from datetime import date
from pathlib import Path

from .notify import Notifier, headline
from .store import Store
from .util import stamp

log = logging.getLogger("homefind")
# The sites whose contact form the userscript fills in (✉ Apply), with where their ad pages' address
# has the ad: the userscript reports it that way (Immowelt's isn't homefind's id for the ad).
FORMS = {"immoscout": re.compile(r"immobilienscout24\.de/(?:en/)?expose/(\d+)"),
         "immowelt": re.compile(r"immowelt\.de/expose/([\w-]+)")}
SOURCES = ["immoscout"]  # where you apply: Full auto opens new flats there, and a flat turning up there is news
SWITCHES = {"auto": False, "fill": True, "send": False}  # the header's switches (see above), and how they start
AUTO_TIERS = ("match", "good")  # what Full auto opens: Matches to send, Good ones to fill in
STEPS = ("opening", "filling", "ready", "sending", "sent", "failed")
USERSCRIPT = Path(__file__).with_name("apply.user.js")
FIREFOX = "/mnt/c/Program Files/Mozilla Firefox/firefox.exe"  # Windows' Firefox, from WSL
REPORT_SECONDS = 120  # an ad opened for the userscript that hasn't reported back by then: it isn't there
QUIET_SECONDS = 180   # filling in or sending, with no word from the userscript since: its tab was closed
ACTIVE_SECONDS = 1800  # an application that hasn't moved on since then isn't under way any more (see busy)
NO_HELPER = ("Firefox didn't fill it in: is the homefind helper installed in Tampermonkey? "
             "(Install Firefox helper, in the header)")
# A step the userscript has gone quiet on: how long that takes, and what the card says then (it failed).
GONE_QUIET = {
    "opening": (REPORT_SECONDS, NO_HELPER),
    "filling": (QUIET_SECONDS, "Firefox stopped saying how filling in the form went: was its tab closed? ✉ Apply tries again."),
    "sending": (QUIET_SECONDS, "Firefox stopped saying whether it was sent: check the ad in Firefox before applying again."),
}


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


def switches(store: Store) -> dict[str, bool]:
    """The header's switches, as you last left them (see SWITCHES)."""
    try:
        saved = json.loads(store.meta_get("apply_switches") or "{}")
    except ValueError:
        saved = {}
    return {name: bool(saved.get(name, on)) for name, on in SWITCHES.items()}


def set_switches(store: Store, changes: dict[str, bool]) -> dict[str, bool]:
    now = {**switches(store), **{k: bool(v) for k, v in changes.items() if k in SWITCHES}}
    store.meta_set("apply_switches", json.dumps(now))
    return now


def version() -> str:
    """The userscript's version: Tampermonkey updates it when this goes up."""
    return f"1.{int(USERSCRIPT.stat().st_mtime)}"


def userscript(cfg: dict) -> str:
    return USERSCRIPT.read_text(encoding="utf-8").replace("__PORT__", str(cfg["run"]["web_port"])) \
                                                  .replace("__VERSION__", version())


def key_of(store: Store, site: str, ad: str) -> str | None:
    """The listing an ad page is for, from the site and the ad in the page's address (FORMS)."""
    pattern = FORMS.get(site)
    if not pattern:
        return None
    if store.row(f"{site}:{ad}"):  # ImmoScout24's ads go by the same id here
        return f"{site}:{ad}"
    return next((r.listing.key for r in store.all_rows(site)
                 if (m := pattern.search(r.listing.url)) and m.group(1).lower() == ad.lower()), None)


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
    """Where each application is at (for the cards), Full auto, and what the userscript reports."""

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
        if s and s["step"] in GONE_QUIET and time.time() - s["at"] > GONE_QUIET[s["step"]][0]:
            s.update(step="failed", note=GONE_QUIET[s["step"]][1])
        return s and {k: s[k] for k in ("step", "note", "at")}

    def busy(self) -> bool:
        """An application is under way: on its way (opening, filling in, sending; see GONE_QUIET) or waiting
        for you to send it, since less than ACTIVE_SECONDS. Restarting homefind now would lose track of it."""
        now = time.time()
        return any(now - s["at"] < (GONE_QUIET[s["step"]][0] if s["step"] in GONE_QUIET else ACTIVE_SECONDS)
                   for s in list(self.steps.values()) if s["step"] in ("opening", "filling", "ready", "sending"))

    def cancel(self, key: str) -> None:
        """Cancel on the card: it stops following the application. A tab still open in Firefox carries on
        (it's for you to close): a form sent from it still moves the flat to Applied (see report)."""
        self.steps.pop(key, None)

    def opened(self, key: str, auto: bool = False, send: bool = False) -> None:
        """The ad is opening in a new tab, for the userscript to fill in: ✉ Apply on a card
        did that, or Full auto (`auto`); either may be going to send it too (`send`)."""
        self.steps[key] = {"step": "opening", "note": "Opening the ad in Firefox…", "at": time.time(),
                           "auto": auto, "send": send}

    def auto(self, listing, verdict, tier: str | None, store: Store) -> None:
        """A new flat that isn't a repost, in `tier` (rules.tier): with Full auto on, it opens an ImmoScout24
        Match in Firefox to send, a Good one to fill in."""
        if not switches(store)["auto"] or listing.source not in SOURCES or tier not in AUTO_TIERS:
            return
        problem = self.problem()
        if problem:
            log.warning("Full auto is on, but %s", problem)
            return
        send = tier == "match"
        if send and verdict.flags:
            log.info("Full auto: ⚠ %s, only filling it in", "; ".join(verdict.flags))
            send = False
        elif send and self._sent_today(store) >= self.opts["max_per_day"]:
            log.info("Full auto: %d sent today already (apply.max_per_day), only filling it in", self.opts["max_per_day"])
            send = False
        log.info("Full auto (%s, %s): %s", tier, "send" if send else "fill in", listing.url)
        self.opened(listing.key, auto=True, send=send)
        open_in_firefox(f"{listing.url}#homefind={'send' if send else 'auto'}", self.opts.get("firefox") or "")

    def report(self, site: str, ad: str, body: dict, store: Store) -> bool:
        """What the userscript says about an ad on `site` (`ad` as in the page's address, see key_of):
        a step with a note (and `by` for sent: homefind or you), or the form it filled in (step "form",
        kept in data/apply-last.json). False for an ad homefind doesn't know."""
        key = key_of(store, site, ad)
        if body["step"] == "form":
            self._keep(key or f"{site}:{ad}", body)
            return bool(key)
        if not key:
            return False
        row = store.row(key)
        before = self.steps.get(key, {"auto": False, "send": False})
        if before.get("step") == "sent" and body["step"] == "sent":
            return True  # told twice
        s = self.steps[key] = {**before, "step": body["step"], "note": str(body.get("note") or ""), "at": time.time()}
        log.info("apply %s: %s", key, s["note"])
        listing = row.listing
        if s["step"] == "sent":
            by_homefind = body.get("by") == "homefind"
            if row.status != "applied" or by_homefind:
                store.set_status(key, "applied", stamp(), "homefind" if by_homefind else None)
            if by_homefind:
                store.meta_set("apply_sent", f"{date.today().isoformat()} {self._sent_today(store) + 1}")
                self.notifier.push(f"✓ Applied · {headline(listing)}", f"homefind sent your message: {listing.title}",
                                   listing.url)
        elif s["step"] == "ready" and s["send"] and s["auto"]:
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
            self.report_path.write_text(json.dumps({"key": key, "url": body.get("url"), "at": stamp(),
                                                    **(body.get("form") or {})}, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
        except OSError:
            pass
