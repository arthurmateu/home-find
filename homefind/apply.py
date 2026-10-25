"""Applying to ads: fills in ImmoScout24's contact form in the homefind browser
window (browser.py) with what's in apply.toml, and leaves it for you to check and
press Abschicken; or, with auto-apply on "send", presses it too. Either way, once
ImmoScout says it's sent, the ad moves to Applied.

Auto-apply (Off / Fill in, I send / Send by itself, in the web UI's header) does
this for new matches as they're found: ImmoScout24 ones scoring at least
apply.min_score, without ⚠ warnings and not reposts; "send" sends at most
apply.max_per_day a day. Whatever it can't do by itself (a field apply.toml
doesn't cover, a captcha, being logged out) it leaves waiting for you in the window.

Jobs run one at a time on a thread of their own, so neither the checks nor the
web UI wait for them.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
import tomllib
from datetime import date, datetime
from pathlib import Path

from .browser import Browser, BrowserError, find_chrome
from .notify import Notifier, headline
from .store import Store

log = logging.getLogger("homefind")
SOURCES = ["immoscout"]  # the ones whose contact form apply_form.js knows
MODES = {"off": "Off", "confirm": "Fill in, I send", "send": "Send by itself"}
FORM_JS = Path(__file__).with_name("apply_form.js")
HOME = "https://www.immobilienscout24.de/"
SENDING_SECONDS = 30  # after pressing Abschicken, how long ImmoScout gets to say it's sent
# How the page looks now: ImmoScout's captcha, what it says about messages sent, the form being open.
LOOK_JS = r"""() => {
  const t = document.body.innerText.replace(/\s+/g, " ");
  return { loaded: document.readyState === "complete",
           captcha: document.title.includes("Roboter") || !!document.querySelector("#captcha-container"),
           sent: t.match(/(nachricht|anfrage)[^.]{0,60}(gesendet|versendet|verschickt|übermittelt)|erfolgreich (gesendet|versendet|verschickt|übermittelt)/gi) || [],
           form: [...document.querySelectorAll("textarea")].some((el) => el.getClientRects().length > 0) };
}"""
SEND_JS = """() => { const b = document.querySelector("[data-homefind-submit]"); b?.click(); return !!b; }"""
CONTACT_POST = re.compile(r"^https://[^/]*immobilienscout24\.de/.*(contact|kontakt|message|nachricht|anfrage|inquir)", re.I)
ACTIVE = ("queued", "opening", "filling", "sending")  # steps that move on by themselves


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


class Applier:
    def __init__(self, cfg: dict, notifier: Notifier):
        self.cfg, self.opts, self.notifier = cfg, cfg["apply"], notifier
        data = Path(cfg["run"]["db_path"]).parent
        self.profile_dir, self.report_path = data / "browser", data / "apply-last.json"
        self.steps: dict[str, dict] = {}  # listing key -> where its application is at, for the web UI
        self._jobs: queue.Queue = queue.Queue()
        self._open: dict[str, dict] = {}  # listing key -> its form, open in a tab (see _watch)
        self._browser: Browser | None = None
        self._store: Store | None = None  # the worker thread's own
        threading.Thread(target=self._work, name="apply", daemon=True).start()

    # ---- called from the checks and the web UI
    def problem(self) -> str | None:
        """Why it can't apply, or None."""
        if not find_chrome(self.opts.get("chrome") or ""):
            return "it needs Chrome: see Applying in the README"
        return load_profile(self.opts["profile"])[1]

    def fill(self, key: str) -> None:
        """Apply on a card: fills in the form and leaves it for you to send."""
        self._queue(key, send=False, auto=False)

    def show(self, key: str) -> None:
        self._jobs.put(("show", key, False, False))

    def window(self) -> None:
        """Opens the window on ImmoScout24, to log in."""
        self._jobs.put(("window", None, False, False))

    def auto(self, listing, verdict, score: int, store: Store) -> None:
        """A new match that isn't a repost: auto-apply fills in its form (or sends it) if it's on and the match qualifies."""
        how = mode(store)
        if how == "off" or listing.source not in SOURCES or verdict.flags or score < self.opts["min_score"]:
            return
        problem = self.problem()
        if problem:
            log.warning("auto-apply is on, but %s", problem)
            return
        log.info("auto-apply (%s): %s, score %d", MODES[how].lower(), listing.url, score)
        self._queue(listing.key, send=how == "send", auto=True)

    def _queue(self, key: str, send: bool, auto: bool) -> None:
        if self.steps.get(key, {}).get("step") not in ACTIVE:
            self._step(key, "queued", "Waiting for the homefind browser…", quiet=True)
        self._jobs.put(("fill", key, send, auto))

    # ---- the worker thread
    def _work(self) -> None:
        self._store = Store(self.cfg["run"]["db_path"])
        while True:
            try:
                job = self._jobs.get(timeout=2)
            except queue.Empty:
                job = None
            if job:
                kind, key, send, auto = job
                try:
                    if kind == "window":
                        self._chrome().tab(HOME).front()
                    elif kind == "show" and key in self._open and self._open[key]["tab"].open:
                        self._open[key]["tab"].front()
                    elif kind == "fill":
                        self._start(key, send, auto)
                except Exception as e:  # noqa: BLE001 - one job going wrong must not stop the next
                    if not isinstance(e, BrowserError):
                        log.exception("apply %s: failed", key)
                    if key:
                        self._open.pop(key, None)
                        self._step(key, "failed", f"Couldn't fill it in: {e}")
                    else:
                        log.warning("apply: couldn't open the homefind browser: %s", e)
            for key in list(self._open):
                try:
                    self._watch(key)
                except BrowserError as e:  # e.g. the page was loading: next time
                    log.debug("apply %s: %s", key, e)
                except Exception:  # noqa: BLE001
                    log.exception("apply %s: failed", key)
                    self._open.pop(key, None)

    def _chrome(self) -> Browser:
        if not (self._browser and self._browser.alive):
            exe = find_chrome(self.opts.get("chrome") or "")
            if not exe:
                raise BrowserError("no Chrome found")
            try:
                self._browser = Browser(exe, self.profile_dir)
            except BrowserError:
                raise BrowserError("Chrome didn't start (is the homefind browser open in another homefind?)") from None
        return self._browser

    def _start(self, key: str, send: bool, auto: bool) -> None:
        if key in self._open and self._open[key]["tab"].open:  # its form is open already
            if not auto:
                self._open[key]["tab"].front()
            return
        row = self._store.row(key)
        if not row:
            return self._step(key, "failed", "It's not in the database any more.")
        self._step(key, "opening", "Opening the ad in the homefind browser…")
        tab = self._chrome().tab(row.listing.url, background=auto)
        if not auto:
            tab.front()
        self._open[key] = {"tab": tab, "send": send, "auto": auto, "listing": row.listing, "phase": "filling"}
        self._fill(key)

    def _fill(self, key: str) -> None:
        w = self._open[key]
        tab, listing = w["tab"], w["listing"]
        profile, problem = load_profile(self.opts["profile"])
        if problem:
            w["phase"] = "ready"
            return self._step(key, "failed", f"Couldn't fill it in: {problem}.")
        self._step(key, "filling", "Filling in the form…", quiet=True)
        r = tab.call(FORM_JS.read_text(encoding="utf-8"), {**profile, "auto": w["auto"]}) or \
            {"error": "the page changed while it was being filled in"}
        if r.get("captcha"):
            w["phase"] = "captcha"
            self._step(key, "captcha", "ImmoScout wants you to prove you're not a robot: solve its puzzle in the "
                                       "homefind window, and homefind carries on from there.")
            if w["auto"]:
                self._push("ImmoScout wants a captcha solved", f"before homefind can apply: {headline(listing)}. "
                           "It's in the homefind browser window.", listing.url)
            return
        self._report(key, listing.url, r)
        if r.get("already"):  # auto only: by hand, it fills in the form anyway
            self._open.pop(key)
            tab.close()
            return self._step(key, "failed", f"Not applied: ImmoScout says you've been in touch already (“{r['already']}”).")
        problems = [*([r["error"]] if r.get("error") else []),
                    *(["you're not logged in to ImmoScout24 there"] if r.get("loggedOut") else []),
                    *(f"{label}: {why}" for label, why in r.get("unset", [])),
                    *(f"{label}: apply.toml doesn't say" for label in r.get("missing", []))]
        look = tab.call(LOOK_JS, None)
        w["seen"], w["posts"] = set(look["sent"]), len(tab.posts)
        full = w["send"] and self._sent_today() >= self.opts["max_per_day"]
        if w["send"] and not problems and not full and tab.js(f"({SEND_JS})()"):
            w["phase"], w["since"] = "sending", time.monotonic()
            return self._step(key, "sending", "Pressed Abschicken, waiting for ImmoScout to confirm…")
        w["phase"] = "ready"
        if problems:
            note = "Fill in the rest in the homefind window, then press Abschicken there: " + "; ".join(problems) + "."
        else:
            note = "Check the form in the homefind window and press Abschicken there."
        if full:
            note = f"Not sent by itself: {self.opts['max_per_day']} sent today already (apply.max_per_day). " + note
        self._step(key, "ready", note)
        if w["auto"] and w["send"]:
            self._push("Couldn't apply by itself", f"{headline(listing)}: {note}", listing.url)

    def _watch(self, key: str) -> None:
        """A form open in a tab: sent yet? (by homefind, or by you). Tab closed?"""
        w = self._open[key]
        tab = w["tab"]
        if not tab.open:
            del self._open[key]
            if w["phase"] == "sending":
                self._step(key, "failed", "The tab was closed before ImmoScout said it was sent: check on ImmoScout24.")
            elif self.steps.get(key, {}).get("step") != "failed":
                self._step(key, "closed", "Closed without sending.")
            return
        look = tab.call(LOOK_JS, None)
        if w["phase"] == "captcha":
            if look["loaded"] and not look["captcha"]:  # solved: the ad's page is back
                self._fill(key)
            return
        if "seen" not in w:
            return
        posted = [(url, status) for url, status in tab.posts[w["posts"]:] if CONTACT_POST.match(url)]
        if set(look["sent"]) - w["seen"] or (not look["form"] and any(s and s < 400 for _, s in posted)):
            return self._sent(key)
        if w["phase"] == "sending" and time.monotonic() - w["since"] > SENDING_SECONDS:
            w["phase"] = "ready"
            log.info("apply %s: POSTs after Abschicken: %s", key, tab.posts[w["posts"]:])
            note = "Pressed Abschicken, but ImmoScout didn't say it was sent: check the homefind window."
            self._step(key, "ready", note)
            if w["auto"]:
                self._push("Did homefind apply?", f"{headline(w['listing'])}: {note}", w["listing"].url)

    def _sent(self, key: str) -> None:
        w = self._open.pop(key)
        by_homefind = w["phase"] == "sending"
        self._store.set_status(key, "applied", datetime.now().isoformat(timespec="seconds"),
                               "homefind" if by_homefind else None)
        self._step(key, "sent", "Sent by homefind." if by_homefind else "Sent.")
        if by_homefind:
            today = date.today().isoformat()
            self._store.meta_set("apply_sent", f"{today} {self._sent_today() + 1}")
            w["tab"].close()
            listing = w["listing"]
            self._push(f"✓ Applied · {headline(listing)}", f"homefind sent your message: {listing.title}", listing.url)

    # ---- helpers
    def _sent_today(self) -> int:
        day, _, n = (self._store.meta_get("apply_sent") or "").partition(" ")
        return int(n) if day == date.today().isoformat() else 0

    def _step(self, key: str, step: str, note: str, quiet: bool = False) -> None:
        self.steps[key] = {"step": step, "note": note, "at": time.time()}
        if not quiet:
            log.info("apply %s: %s", key, note)

    def _report(self, key: str, url: str, r: dict) -> None:
        """What the last form asked for and how it went: data/apply-last.json, for when a field isn't filled in."""
        try:
            self.report_path.write_text(json.dumps({"key": key, "url": url, "at": datetime.now().isoformat(timespec="seconds"),
                                                    **r}, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    def _push(self, title: str, body: str, url: str) -> None:
        self.notifier.push(title, body, url, warn=not title.startswith("✓"))
