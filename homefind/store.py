"""SQLite state: every listing ever seen (with its verdict) and watched pages."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
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
-- Geocoder answers (see geo.py), misses included (lat NULL) so they aren't asked again.
CREATE TABLE IF NOT EXISTS places (q TEXT PRIMARY KEY, lat REAL, lon REAL, precision TEXT, locality TEXT,
                                   radius REAL, at TEXT);
"""


SAVED_OFFLINE_DAYS = 2  # a saved ad taken offline stays on Saved this long, then moves to Hidden


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
    hide_at: str | None   # saved and offline: when it moves to Hidden


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15)
        self.db.execute("PRAGMA journal_mode=WAL")  # the web UI reads while the poller writes
        self.db.executescript(SCHEMA)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(listings)")}
        # Added later: status (the web UI: NULL, 'saved' or 'hidden'), then the offline check's columns.
        for column in ("status", "gone", "gone_at", "status_if_back", "hide_at"):
            if column not in columns:
                self.db.execute(f"ALTER TABLE listings ADD COLUMN {column} TEXT")
        if "gone" in columns and "hide_at" not in columns:
            # Offline ads marked when all of them went to Hidden: apply today's rules (see mark_gone).
            with self.db:
                self.db.execute("DELETE FROM listings WHERE gone IS NOT NULL AND ok = 0 AND status = 'hidden'"
                                " AND status_if_back IS NULL")
                self.db.execute("UPDATE listings SET status = 'saved', hide_at = strftime('%Y-%m-%dT%H:%M:%S', gone_at, ?)"
                                " WHERE gone IS NOT NULL AND status = 'hidden' AND status_if_back = 'saved'",
                                (f"+{SAVED_OFFLINE_DAYS} days",))

    def seen(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM listings WHERE key = ?", (key,)).fetchone() is not None

    def touch(self, key: str) -> None:
        """Seen online just now. If it had been taken offline, it's back: it returns
        to where it was (or wherever you moved it since)."""
        with self.db:
            self.db.execute("UPDATE listings SET last_seen = ?,"
                            " status = CASE WHEN gone IS NULL THEN status ELSE status_if_back END,"
                            " gone = NULL, gone_at = NULL, hide_at = NULL WHERE key = ?", (_now(), key))

    def mark_gone(self, keys, why: str) -> None:
        """The ads were taken offline ('deactivated' or 'deleted'). Rejected ones
        are deleted for good; matches move to Hidden; saved ones stay on Saved for
        SAVED_OFFLINE_DAYS (see hide_due); ones you hid stay hidden. All but the
        deleted ones are tagged with `why`."""
        now = datetime.now()
        hide_at = (now + timedelta(days=SAVED_OFFLINE_DAYS)).isoformat(timespec="seconds")
        with self.db:
            self.db.executemany("DELETE FROM listings WHERE key = ? AND ok = 0 AND status IS NULL",
                                [(key,) for key in keys])
            self.db.executemany(
                "UPDATE listings SET gone = ?, gone_at = ?, status_if_back = status,"
                " status = CASE WHEN status IS NULL THEN 'hidden' ELSE status END,"
                " hide_at = CASE WHEN status = 'saved' THEN ? END WHERE key = ? AND gone IS NULL",
                [(why, now.isoformat(timespec="seconds"), hide_at, key) for key in keys])

    def hide_due(self) -> int:
        """Saved ads that were taken offline SAVED_OFFLINE_DAYS ago move to Hidden."""
        with self.db:
            return self.db.execute("UPDATE listings SET status = 'hidden', hide_at = NULL"
                                   " WHERE hide_at <= ? AND status = 'saved'", (_now(),)).rowcount

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
            return self.db.execute("UPDATE listings SET status = ?, status_if_back = ?, hide_at = NULL WHERE key = ?",
                                   (status, status, key)).rowcount == 1

    def update_listing(self, listing: Listing) -> None:
        with self.db:
            self.db.execute("UPDATE listings SET data = ? WHERE key = ?",
                            (json.dumps(listing.to_dict(), ensure_ascii=False), listing.key))

    def all_rows(self, source: str | None = None):
        """Everything (or everything from one source), newest first."""
        rows = self.db.execute("SELECT data, verdict, first_seen, last_seen, status, gone, gone_at, status_if_back, hide_at"
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

    def places(self) -> dict[str, dict | None]:
        """Every geocoder answer: query -> {lat, lon, precision, locality, radius}, or None if it found nothing."""
        rows = self.db.execute("SELECT q, lat, lon, precision, locality, radius FROM places")
        return {q: (dict(lat=lat, lon=lon, precision=p, locality=loc, radius=r) if lat is not None else None)
                for q, lat, lon, p, loc, r in rows}

    def save_place(self, q: str, place: dict | None) -> None:
        p = place or {}
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO places VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (q, p.get("lat"), p.get("lon"), p.get("precision"), p.get("locality"), p.get("radius"),
                             _now()))

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
