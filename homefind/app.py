"""One polling run: every source, then the watched pages, then the report."""

from __future__ import annotations

import logging
import time
from datetime import datetime

from .net import Blocked
from .notify import Notifier, print_match, print_reject
from .report import write_report
from .rules import evaluate
from .sources import enabled_sources
from .store import Store
from .util import text

log = logging.getLogger("homefind")


def run_once(cfg: dict, store: Store, http, notifier: Notifier,
             only: set[str] | None = None, dry_run: bool = False) -> set[str]:
    new_matches: set[str] = set()
    for src in enabled_sources(cfg, http):
        if not only or src.name in only:
            new_matches |= _run_source(src, cfg, store, notifier, dry_run)
    if cfg.get("watch") and (not only or "watch" in only):
        run_watches(cfg, store, http, notifier, dry_run)
    if not dry_run:
        write_report(store, cfg["run"]["report_path"], new_matches)
    return new_matches


def _run_source(src, cfg, store, notifier, dry_run) -> set[str]:
    first_run = not dry_run and store.meta_get(f"init:{src.name}") is None
    handled: set[str] = set()
    seen = (lambda key: key in handled) if dry_run else store.seen
    matches: set[str] = set()
    scanned = new = 0
    details_blocked = False
    started = time.monotonic()
    try:
        for listing in src.search(seen):
            scanned += 1
            if listing.key in handled or seen(listing.key):
                if not dry_run:
                    store.touch(listing.key)
                continue
            verdict = evaluate(listing, cfg, final=not src.needs_enrich)
            if verdict.ok and src.needs_enrich:
                if details_blocked:
                    continue  # not stored: retried next run
                try:
                    listing = src.enrich(listing)
                except Blocked as e:
                    log.warning("%s: detail pages blocked (%s); will retry next run", src.name, e)
                    details_blocked = True
                    continue
                except Exception as e:  # noqa: BLE001 - one broken listing must not stop the source
                    log.warning("%s: could not load %s (%s)", src.name, listing.url, e)
                    continue
                listing.signals["enriched"] = True
                verdict = evaluate(listing, cfg, final=True)
            handled.add(listing.key)
            new += 1
            if dry_run:
                (print_match if verdict.ok else print_reject)(listing, verdict)
                continue
            pushed = False
            if verdict.ok:
                matches.add(listing.key)
                earlier = store.repost_of(listing, fingerprint(listing, verdict))
                if earlier:
                    verdict.notes.append(f"same rent/size/postcode as {earlier}")
                print_match(listing, verdict)
                if not first_run and not earlier:
                    pushed = notifier.listing(listing, verdict)
            store.add(listing, verdict, notified=pushed)
    except Blocked as e:
        log.warning("%s: blocked by the site (%s), skipped this run", src.name, e)
        return matches
    except Exception:  # noqa: BLE001
        log.exception("%s: failed (the site may have changed its layout)", src.name)
        return matches
    if first_run:
        store.meta_set(f"init:{src.name}", datetime.now().isoformat(timespec="seconds"))
    log.info("%-14s scanned %3d  new %3d  matches %d  (%.0fs)%s", src.name, scanned, new, len(matches),
             time.monotonic() - started,
             "  [first run: stored, not pushed]" if first_run and matches else "")
    return matches


def recheck(cfg: dict, store: Store) -> tuple[int, int, int]:
    """Re-apply the rules to every stored listing, offline. Listings that now
    pass but were rejected before their detail page was loaded are dropped, so
    the next run fetches them properly."""
    needs_enrich = {s.name for s in enabled_sources(cfg, None) if s.needs_enrich}
    changed = dropped = 0
    rows = store.matches(100_000) + store.rejected(100_000)
    for listing, old, *_ in rows:
        complete = listing.source not in needs_enrich or listing.signals.get("enriched")
        verdict = evaluate(listing, cfg, final=bool(complete))
        if verdict.ok and not complete:
            store.forget(listing.key)
            dropped += 1
        elif verdict.to_dict() != old.to_dict():
            store.update_verdict(listing.key, verdict)
            changed += 1
    return len(rows), changed, dropped


def fingerprint(listing, verdict) -> str:
    """Same flat reposted, or listed on two portals: same rent, size and postcode."""
    return f"{round(verdict.warm or 0)}|{round(listing.size_sqm or 0)}|{listing.zip_code or listing.district}"


def run_watches(cfg, store, http, notifier, dry_run=False) -> None:
    """Page-change watch for landlords without a parser: reports lines of text
    that weren't on the page last time."""
    checked = changed = 0
    for w in cfg["watch"]:
        name, url = w["name"], w["url"]
        try:
            page = http.get(url)
        except Exception as e:  # noqa: BLE001
            log.warning("watch %s: %s", name, e)
            continue
        lines = list(dict.fromkeys(l for l in text(page).split("\n") if len(l) >= 12))
        if dry_run:
            print(f"  ◆ watch {name}: {len(lines)} lines of text")
            continue
        checked += 1
        old = store.page_lines(url)
        store.save_page(url, name, lines)
        if old is None:
            log.info("watch %-26s baseline saved (%d lines)", name, len(lines))
            continue
        previous = set(old)
        added = [l for l in lines if l not in previous]
        if added:
            changed += 1
            print(f"  ◆ {name} changed: {url}")
            for line in added[:6]:
                print(f"      + {line[:110]}")
            notifier.page_changed(name, url, added)
    if not dry_run:
        log.info("%-14s checked %d co-op pages, %d changed", "watch", checked, changed)
