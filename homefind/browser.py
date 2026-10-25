"""A Chrome window homefind can drive, for applying to ads (see apply.py).

It's a normal Chrome with a profile of its own (data/browser), so a login there
sticks, talked to over the DevTools protocol on a pipe: no network port, so
nothing else on the machine can drive it. Under WSL the window shows up on the
Windows desktop (WSLg).
"""

from __future__ import annotations

import fcntl
import glob
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

CANDIDATES = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
              *sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")),
                      reverse=True)]


class BrowserError(Exception):
    pass


def find_chrome(configured: str = "") -> str | None:
    for c in [configured] if configured else CANDIDATES:
        path = shutil.which(c) or (c if os.access(c, os.X_OK) else None)
        if path:
            return path
    return None


class Browser:
    def __init__(self, exe: str, profile: str | Path):
        Path(profile).mkdir(parents=True, exist_ok=True)
        # Chrome reads commands from fd 3 and writes answers to fd 4, each a JSON
        # message ending in a NUL byte. Its ends are moved to 10 and up first, so
        # the redirections can't clobber them (bash: dash only takes fds up to 9).
        to_chrome, self._out = os.pipe()
        self._in, from_chrome = os.pipe()
        r, w = (fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10) for fd in (to_chrome, from_chrome))
        os.close(to_chrome), os.close(from_chrome)
        # Tabs in the background run at full speed (Chrome slows their timers down otherwise).
        args = [exe, "--remote-debugging-pipe", f"--user-data-dir={Path(profile).resolve()}",
                "--no-first-run", "--no-default-browser-check", "--lang=de-DE", "--window-size=1100,1000",
                "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows", "about:blank"]
        self.proc = subprocess.Popen(["bash", "-c", f'exec "$@" 3<&{r} 4>&{w} {r}<&- {w}>&-', "bash", *args],
                                     pass_fds=(r, w), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(r), os.close(w)
        self._id = 0
        self._lock = threading.Lock()
        self._waiting: dict[int, list] = {}  # id -> [Event, answer]
        self._tabs: dict[str, Tab] = {}      # session id -> tab
        self.pages: set[str] = set()         # target ids of the tabs open now
        threading.Thread(target=self._read, name="browser", daemon=True).start()
        self.send("Target.setDiscoverTargets", {"discover": True})

    def _read(self) -> None:
        buf = b""
        with os.fdopen(self._in, "rb", buffering=0) as f:
            while chunk := f.read(65536):
                buf += chunk
                *msgs, buf = buf.split(b"\0")
                for raw in msgs:
                    self._got(json.loads(raw))
        self.pages.clear()  # Chrome is gone (closed, or crashed)
        for slot in list(self._waiting.values()):
            slot[0].set()

    def _got(self, msg: dict) -> None:
        if "id" in msg:
            slot = self._waiting.pop(msg["id"], None)
            if slot:
                slot[1] = msg
                slot[0].set()
            return
        method, p = msg["method"], msg.get("params", {})
        if method in ("Target.targetCreated", "Target.targetInfoChanged") and p["targetInfo"]["type"] == "page":
            self.pages.add(p["targetInfo"]["targetId"])
        elif method == "Target.targetDestroyed":
            self.pages.discard(p["targetId"])
        elif msg.get("sessionId") in self._tabs:
            self._tabs[msg["sessionId"]]._event(method, p)

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def send(self, method: str, params: dict | None = None, session: str | None = None,
             timeout: float = 30) -> dict:
        with self._lock:
            self._id += 1
            mid = self._id
            slot = self._waiting[mid] = [threading.Event(), None]
            msg = {"id": mid, "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            try:
                os.write(self._out, json.dumps(msg).encode() + b"\0")
            except BrokenPipeError:
                slot[0].set()
        if not slot[0].wait(timeout) or slot[1] is None:
            self._waiting.pop(mid, None)
            raise BrowserError(f"{method}: no answer from Chrome" + ("" if self.alive else " (it was closed)"))
        if "error" in slot[1]:
            raise BrowserError(f"{method}: {slot[1]['error'].get('message')}")
        return slot[1].get("result", {})

    def tab(self, url: str, background: bool = False) -> "Tab":
        """Opens `url` in a new tab (or the blank one Chrome starts with) and waits for it to load."""
        mine = {tab.target for tab in self._tabs.values()}
        blank = [t["targetId"] for t in self.send("Target.getTargets")["targetInfos"]
                 if t["type"] == "page" and t["url"] in ("about:blank", "chrome://newtab/") and t["targetId"] not in mine]
        target = blank[0] if blank else self.send("Target.createTarget",
                                                  {"url": "about:blank", "background": background})["targetId"]
        session = self.send("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        tab = self._tabs[session] = Tab(self, target, session)
        tab.go(url)
        return tab

    def close(self) -> None:
        try:
            self.send("Browser.close", timeout=5)
        except BrowserError:
            pass
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class Tab:
    def __init__(self, browser: Browser, target: str, session: str):
        self.browser, self.target, self.session = browser, target, session
        self.posts: list[list] = []  # [URL, HTTP status (None until it answers)] of each POST the page sent
        self._post_ids: dict[str, list] = {}
        self.send("Network.enable")

    def _event(self, method: str, p: dict) -> None:
        if method == "Network.requestWillBeSent" and p["request"]["method"] == "POST":
            self._post_ids[p["requestId"]] = post = [p["request"]["url"], None]
            self.posts.append(post)
        elif method == "Network.responseReceived" and p["requestId"] in self._post_ids:
            self._post_ids.pop(p["requestId"])[1] = p["response"]["status"]

    def send(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        return self.browser.send(method, params, self.session, timeout)

    @property
    def open(self) -> bool:
        return self.browser.alive and self.target in self.browser.pages

    def go(self, url: str, timeout: float = 45) -> None:
        """Goes to `url` and waits (up to `timeout`) for the page to finish loading."""
        self.send("Page.navigate", {"url": url})
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if self.js("document.readyState === 'complete' && location.href !== 'about:blank'"):
                    return
            except BrowserError:  # between the two pages
                pass
            time.sleep(0.3)

    def js(self, expression: str, timeout: float = 60):
        """Evaluates `expression` in the page (awaiting it if it's a promise) and returns its value."""
        r = self.send("Runtime.evaluate", {"expression": expression, "awaitPromise": True,
                                           "returnByValue": True, "userGesture": True}, timeout)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise BrowserError((d.get("exception") or {}).get("description") or d.get("text"))
        return r["result"].get("value")

    def call(self, function: str, arg):
        """Calls the JavaScript function expression `function` with `arg` (passed as JSON)."""
        return self.js(f"({function})({json.dumps(arg, ensure_ascii=False)})")

    def front(self) -> None:
        self.send("Page.bringToFront")

    def close(self) -> None:
        try:
            self.browser.send("Target.closeTarget", {"targetId": self.target}, timeout=5)
        except BrowserError:
            pass
