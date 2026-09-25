"""SQLite state: every listing ever seen (with its verdict) and watched pages."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from .models import Listing
from .rules import Verdict

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    key        TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    ok         INTEGER NOT NULL,
    notified   INTEGER NOT NULL DEFAULT 0,
    data       TEXT NOT NULL,
    verdict    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS listings_ok ON listings (ok, first_seen);
CREATE TABLE IF NOT EXISTS pages (url TEXT PRIMARY KEY, name TEXT, lines TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS fingerprints (fp TEXT PRIMARY KEY, key TEXT, url TEXT, first_seen TEXT);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Row(NamedTuple):
    listing: Listing
    verdict: Verdict
    first_seen: str
    last_seen: str        # last time it was seen online
    status: str | None    # None, 'saved' or 'hidden'
    gone: str | None      # the ad was taken offline: 'deactivated' or 'deleted'
    gone_at: str | None   # when that was noticed
    status_if_back: str | None  # where it goes if it comes back online


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15)
        self.db.execute("PRAGMA journal_mode=WAL")  # the web UI reads while the poller writes
        self.db.executescript(SCHEMA)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(listings)")}
        # Added later: status (the web UI: NULL, 'saved' or 'hidden'), then the offline check's columns.
        for column in ("status", "gone", "gone_at", "status_if_back"):
            if column not in columns:
                self.db.execute(f"ALTER TABLE listings ADD COLUMN {column} TEXT")

    def seen(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM listings WHERE key = ?", (key,)).fetchone() is not None

    def touch(self, key: str) -> None:
        """Seen online just now. If it had been taken offline, it's back: it returns
        to where it was (or wherever you moved it since)."""
        with self.db:
            self.db.execute("UPDATE listings SET last_seen = ?,"
                            " status = CASE WHEN gone IS NULL THEN status ELSE status_if_back END,"
                            " gone = NULL, gone_at = NULL WHERE key = ?", (_now(), key))

    def mark_gone(self, keys, why: str) -> None:
        """The ads were taken offline ('deactivated' or 'deleted'): move them to Hidden."""
        with self.db:
            self.db.executemany("UPDATE listings SET gone = ?, gone_at = ?, status_if_back = status, status = 'hidden'"
                                " WHERE key = ? AND gone IS NULL", [(why, _now(), key) for key in keys])

    def online_keys(self, source: str) -> set[str]:
        """Keys of the source's ads not known to be offline."""
        return {k for (k,) in self.db.execute("SELECT key FROM listings WHERE source = ? AND gone IS NULL", (source,))}

    def add(self, listing: Listing, verdict: Verdict, notified: bool = False) -> None:
        now = _now()
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO listings (key, source, first_seen, last_seen, ok, notified, data, verdict)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (listing.key, listing.source, now, now, int(verdict.ok), int(notified),
                 json.dumps(listing.to_dict(), ensure_ascii=False),
                 json.dumps(verdict.to_dict(), ensure_ascii=False)),
            )

    def _rows(self, ok: bool, limit: int):
        rows = self.db.execute(
            "SELECT data, verdict, first_seen, last_seen, notified FROM listings"
            " WHERE ok = ? ORDER BY first_seen DESC LIMIT ?", (int(ok), limit))
        for data, verdict, first_seen, last_seen, notified in rows:
            yield (Listing.from_dict(json.loads(data)), Verdict(**json.loads(verdict)),
                   first_seen, last_seen, bool(notified))

    def matches(self, limit: int = 500):
        return list(self._rows(True, limit))

    def rejected(self, limit: int = 100):
        return list(self._rows(False, limit))

    def set_status(self, key: str, status: str | None) -> bool:
        with self.db:
            return self.db.execute("UPDATE listings SET status = ?, status_if_back = ? WHERE key = ?",
                                   (status, status, key)).rowcount == 1

    def update_listing(self, listing: Listing) -> None:
        with self.db:
            self.db.execute("UPDATE listings SET data = ? WHERE key = ?",
                            (json.dumps(listing.to_dict(), ensure_ascii=False), listing.key))

    def all_rows(self, source: str | None = None):
        """Everything (or everything from one source), newest first."""
        rows = self.db.execute("SELECT data, verdict, first_seen, last_seen, status, gone, gone_at, status_if_back"
                               " FROM listings WHERE ? IS NULL OR source = ? ORDER BY first_seen DESC, rowid DESC",
                               (source, source))
        for data, verdict, *rest in rows:
            yield Row(Listing.from_dict(json.loads(data)), Verdict(**json.loads(verdict)), *rest)

    def update_verdict(self, key: str, verdict: Verdict) -> None:
        with self.db:
            self.db.execute("UPDATE listings SET ok = ?, verdict = ? WHERE key = ?",
                            (int(verdict.ok), json.dumps(verdict.to_dict(), ensure_ascii=False), key))

    def forget(self, key: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM listings WHERE key = ?", (key,))

    def repost_of(self, listing: Listing, fingerprint: str, days: int = 30) -> str | None:
        """URL of an earlier match with the same fingerprint in the last `days`
        days (a repost, or the same flat on another portal); else records this one."""
        row = self.db.execute("SELECT key, url, first_seen FROM fingerprints WHERE fp = ?", (fingerprint,)).fetchone()
        if row and row[0] != listing.key and (datetime.now() - datetime.fromisoformat(row[2])).days < days:
            return row[1]
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO fingerprints VALUES (?, ?, ?, ?)",
                            (fingerprint, listing.key, listing.url, _now()))
        return None

    def meta_values(self, prefix: str) -> list[str]:
        return [v for (v,) in self.db.execute("SELECT v FROM meta WHERE k LIKE ?", (prefix + "%",))]

    def close(self) -> None:
        self.db.close()

    def meta_get(self, k: str) -> str | None:
        row = self.db.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return row[0] if row else None

    def meta_set(self, k: str, v: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, v))

    def page_lines(self, url: str) -> list[str] | None:
        row = self.db.execute("SELECT lines FROM pages WHERE url = ?", (url,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_page(self, url: str, name: str, lines: list[str]) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO pages VALUES (?, ?, ?, ?)",
                            (url, name, json.dumps(lines, ensure_ascii=False), _now()))
