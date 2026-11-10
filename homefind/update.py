"""homefind restarts by itself when it's updated. Its Python code, config.toml and
config.local.toml are only read at startup, so a change to them needs a restart (ui.html
is read on every page load, so for that open pages just offer a Reload).

It restarts in the same terminal, with the same options (but --open: the page is open
already), once the files have stopped changing for SETTLE_SECONDS (an update is often a few
files, saved one after another), between two checks, and not while that would cut something
short (see Updater.busy). First it makes sure the new version starts, in a process of its own
(PREFLIGHT): if it doesn't, the one running keeps going and the web page says why, until the
files change again.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config

log = logging.getLogger("homefind")
SETTLE_SECONDS = 30
LOOK_EVERY_SECONDS = 5
PACKAGE = Path(__file__).resolve().parent
# What starting the new version would do first: import all of homefind and read the config (a
# mistake in a config file is told with its name). Nothing else, so it can't touch the database
# under the version still running.
PREFLIGHT = """
import sys, tomllib
from pathlib import Path
from homefind import __main__, config
for p in (Path(sys.argv[1]), config.local_path(sys.argv[1])):
    try:
        if p.exists():
            tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"{p.name}: {e}")
config.load(sys.argv[1])
"""


def code(cfg: dict) -> str:
    """The Python code and the config files as they are on disk now."""
    h = hashlib.sha1()
    files = [Path(cfg["path"]), config.local_path(cfg["path"])] if cfg.get("path") else []
    for f in [*sorted(PACKAGE.rglob("*.py")), *files]:
        h.update(f.name.encode())
        try:
            h.update(f.read_bytes())
        except OSError:  # not there (config.local.toml is optional), or deleted just now
            pass
    return h.hexdigest()[:12]


def _why(stderr: str, returncode: int) -> str:
    """The new version's error in a line, with where it is: "SyntaxError: ... (homefind/web.py, line 12)"."""
    lines = stderr.strip().splitlines()
    if not lines:
        return f"it stopped with exit code {returncode}"
    where = [(Path(path).resolve(), line) for path, line in re.findall(r'File "([^"]+)", line (\d+)', stderr)]
    ours = [(path.relative_to(PACKAGE.parent), line) for path, line in where if path.is_relative_to(PACKAGE)]
    if not ours:  # a config file (see PREFLIGHT), or the error has no file
        return lines[-1]
    path, line = ours[-1]  # in homefind's code, not in the standard library it called
    return f"{lines[-1]} ({path}, line {line})"


class Updater:
    """Watches homefind's files (code) on a thread of its own, and restarts into an update
    once it's ready: settled, starting, and nothing under way (see busy). `wake` tells the
    main thread to have a look (restart_if_ready), which only it does: with --loop, it's
    the one doing the checks, so it's never in the middle of one when it looks."""

    def __init__(self, cfg: dict, applier=None, casa=None, wake=None):
        self.cfg, self.applier, self.casa = cfg, applier, casa
        self._wake = wake
        self.started = self.seen = code(cfg)  # what's running, and what's on disk
        self.seen_at = time.monotonic()        # when what's on disk last changed
        self.tried: tuple[str, str | None] | None = None  # code() of the last update tried, and why it doesn't start
        self.waiting: str | None = None        # code() of the update a restart is waiting on (busy), said once
        self.nudged = threading.Event()        # without `wake`: restart_if_ready's cue

    def start(self) -> None:
        threading.Thread(target=self._run, name="update", daemon=True).start()

    def _run(self) -> None:
        while True:
            time.sleep(LOOK_EVERY_SECONDS)
            try:
                if self._look():
                    self._wake() if self._wake else self.nudged.set()
            except Exception:  # noqa: BLE001 - never stop watching
                log.exception("update: couldn't look for an update")

    def _look(self) -> bool:
        """Notes what's on disk; True when there's an update ready to restart into."""
        now = code(self.cfg)
        if now != self.seen:
            self.seen, self.seen_at = now, time.monotonic()
        if now == self.started or time.monotonic() - self.seen_at < SETTLE_SECONDS:
            return False
        if not self.tried or self.tried[0] != now:
            self.tried = now, self._preflight()
            if self.tried[1]:
                log.warning("homefind was updated, but the new version doesn't start, so this one keeps going "
                            "until it's fixed: %s", self.tried[1])
        if self.tried[1]:
            return False
        busy = self.busy()
        if busy and self.waiting != now:
            log.info("homefind was updated: restarting once %s", busy)
            self.waiting = now
        return not busy

    def _preflight(self) -> str | None:
        """Why the version on disk doesn't start, or None."""
        try:
            r = subprocess.run([sys.executable, "-c", PREFLIGHT, self.cfg["path"]], capture_output=True,
                               text=True, timeout=120, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"couldn't try it ({e})"
        if r.returncode:
            log.debug("update: the new version said:\n%s", r.stderr)
        return _why(r.stderr, r.returncode) if r.returncode else None

    def busy(self) -> str | None:
        """What a restart would cut short now, as in "restarting once ...", or None."""
        if self.applier and self.applier.busy():
            return "the application under way is done (sent, or no longer waiting for you)"
        if self.casa and self.casa.uploading:
            return "the family page is uploaded"
        return None

    def status(self) -> dict | None:
        """For the web page: None while it runs what's on disk; else {"problem": why the update
        doesn't start, or None while it's on its way (it restarts by itself)}."""
        if self.seen == self.started:
            return None
        return {"problem": self.tried[1] if self.tried and self.tried[0] == self.seen else None}

    def restart_if_ready(self, server=None) -> None:
        """Restarts into the update if it's ready (see _look). Only from the main thread, between checks."""
        self.nudged.clear()
        if not self.tried or self.tried[1] or self.busy():
            return
        if code(self.cfg) != self.tried[0]:  # changed again since (or back): wait for that to settle
            return
        log.info("homefind was updated: restarting")
        if server:  # finish the requests under way, take no more; the new one listens on the same port
            server.shutdown()
            server.server_close()
        for h in logging.getLogger().handlers:
            h.flush()
        sys.stdout.flush()
        sys.stderr.flush()
        # The new version, as this one was started (sys.orig_argv: python3 -m homefind ...). Everything
        # it opened (the lock, the database, the port) is closed on the way (Python's files don't outlive exec).
        os.execv(sys.executable, [sys.executable, *[a for a in sys.orig_argv[1:] if a != "--open"]])
