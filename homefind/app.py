"""Polling: each source (and the co-op page watch) on its own schedule."""

from __future__ import annotations

import logging
import time
import urllib.error
from datetime import datetime, timedelta

from .net import Blocked
from .rating import rate
from .notify import Notifier, print_match, print_reject
from .rules import evaluate
from .sources import enabled_sources
from .store import SAVED_OFFLINE_DAYS, Store
from .util import text

log = logging.getLogger("homefind")
MAX_BACKOFF_MINUTES = 6 * 60
CHECK_AFTER_HOURS = 6  # an ad seen online more recently than this isn't looked up to see if it's gone


def _every(cfg: dict, name: str) -> float:
    return float(cfg["sources"].get(name, {}).get("every_minutes", 10)) * 60


def _due_at(cfg: dict, store: Store, name: str) -> float:
    last = float(store.meta_get(f"last:{name}") or 0)
    paused_until = float(store.meta_get(f"pause:{name}") or 0)
    return max(last + _every(cfg, name) - 5, paused_until)


def _names(cfg: dict, http, only: set[str] | None) -> list[str]:
    names = [s.name for s in enabled_sources(cfg, http)] + (["watch"] if cfg.get("watch") else [])
    return [n for n in names if not only or n in only]


def seconds_until_due(cfg: dict, store: Store, only: set[str] | None = None) -> float:
    soonest = min(_due_at(cfg, store, n) for n in _names(cfg, None, only))
    return min(max(soonest - time.time(), 15), 3600)


def _blocked(store: Store, name: str, every: float, err: Exception) -> None:
    """Back off from a site that blocked us: 2x, 4x, ... its interval, capped at 6 hours."""
    strikes = int(store.meta_get(f"strikes:{name}") or 0) + 1
    pause = min(every * 2**strikes, MAX_BACKOFF_MINUTES * 60)
    store.meta_set(f"strikes:{name}", str(strikes))
    store.meta_set(f"pause:{name}", str(time.time() + pause))
    log.warning("%s: blocked by the site (%s), pausing it for %.0f min", name, err, pause / 60)


def run_once(cfg: dict, store: Store, http, notifier: Notifier, only: set[str] | None = None,
             dry_run: bool = False, scheduled: bool = False) -> None:
    """One pass. `scheduled` (the --loop mode) only runs sources that are due."""
    now = time.time()
    if not dry_run and store.hide_due():
        log.info("saved ads offline for %d days moved to Hidden", SAVED_OFFLINE_DAYS)
    results = []
    for src in enabled_sources(cfg, http):
        if (only and src.name not in only) or (scheduled and _due_at(cfg, store, src.name) > now):
            continue
        results.append(_run_source(src, cfg, store, notifier, dry_run, quiet=scheduled))
        if not dry_run:
            store.meta_set(f"last:{src.name}", str(time.time()))
    if cfg.get("watch") and (not only or "watch" in only) and not (scheduled and _due_at(cfg, store, "watch") > now):
        run_watches(cfg, store, http, notifier, dry_run)
        if not dry_run:
            store.meta_set("last:watch", str(time.time()))
        results.append({"name": "watch", "new": 0, "matches": 0})
    if dry_run or not results:
        return
    if scheduled:
        new = sum(r["new"] for r in results)
        matched = sum(r["matches"] for r in results)
        names = ", ".join(r["name"] for r in results)
        log.info("checked %s: %s", names, f"{new} new, {matched} matching" if new else "nothing new")


def _sweep_due(cfg: dict, store: Store, name: str) -> bool:
    hours = cfg["sources"].get(name, {}).get("sweep_every_hours")
    return bool(hours) and time.time() - float(store.meta_get(f"sweep:{name}") or 0) >= hours * 3600


def _run_source(src, cfg, store, notifier, dry_run, quiet=False) -> dict:
    first_run = not dry_run and store.meta_get(f"init:{src.name}") is None
    handled: set[str] = set()
    online: set[str] = set()  # every ad the search came across
    seen = (lambda key: key in handled) if dry_run else store.seen
    # A sweep pages through everything (sources stop paging at ads they've seen before).
    sweep = not dry_run and _sweep_due(cfg, store, src.name)
    if sweep:
        store.meta_set(f"sweep:{src.name}", str(time.time()))
    matches: set[str] = set()
    scanned = new = 0
    details_blocked = False
    started = time.monotonic()
    try:
        for listing in src.search((lambda key: False) if sweep else seen):
            scanned += 1
            online.add(listing.key)
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
                score, _ = rate(listing, verdict, datetime.now().isoformat(timespec="seconds"), cfg)
                print_match(listing, verdict, score)
                if not first_run and not earlier:
                    pushed = notifier.listing(listing, verdict, score)
            store.add(listing, verdict, notified=pushed)
    except Blocked as e:
        if not dry_run:
            _blocked(store, src.name, _every(cfg, src.name), e)
        else:
            log.warning("%s: blocked by the site (%s)", src.name, e)
        return {"name": src.name, "new": new, "matches": len(matches)}
    except Exception:  # noqa: BLE001
        log.exception("%s: failed (the site may have changed its layout)", src.name)
        return {"name": src.name, "new": new, "matches": len(matches)}
    if not dry_run and store.meta_get(f"strikes:{src.name}") not in (None, "0"):
        store.meta_set(f"strikes:{src.name}", "0")
    if sweep and not src.complete:
        log.warning("%s: the sweep missed some ads (they shift pages as ads come and go); trying again in %s h",
                    src.name, cfg["sources"][src.name]["sweep_every_hours"])
    if not dry_run:
        offline = _find_offline(src, cfg, store, online)
        if offline:
            log.info("%-14s %d ad%s taken offline", src.name, len(offline), "s" * (len(offline) > 1))
    if first_run:
        store.meta_set(f"init:{src.name}", datetime.now().isoformat(timespec="seconds"))
    if not quiet or new or first_run:
        log.info("%-14s scanned %3d  new %3d  matches %d  (%.0fs)%s", src.name, scanned, new, len(matches),
                 time.monotonic() - started,
                 "  [first run: stored, not pushed]" if first_run and matches else "")
    return {"name": src.name, "new": new, "matches": len(matches)}


def _find_offline(src, cfg: dict, store: Store, online: set[str]) -> list[str]:
    """Finds ads that were taken offline, files them away (see Store.mark_gone)
    and returns their keys. After a search that saw every ad on the
    site, that's all the stored ones it didn't see. Otherwise a few older ads
    get looked up, `check_per_run` per run (see _to_check)."""
    if src.complete:
        gone = store.online_keys(src.name) - online
        store.mark_gone(gone, "deleted")
        return sorted(gone)
    if not src.can_probe:
        return []
    gone = []
    for r in _to_check(src, cfg, store)[: int(src.opts.get("check_per_run", 5))]:
        try:
            if _look_up(src, cfg, store, r):
                gone.append(r.listing.key)
        except Blocked:
            break
        except Exception as e:  # noqa: BLE001 - leave the rest for next run
            log.warning("%s: could not check %s (%s); trying again next run", src.name, r.listing.url, e)
            break
    return gone


def _to_check(src, cfg: dict, store: Store) -> list:
    """Ads to look up, most useful first: saved, matches, rejected ones in the
    area, ones you hid, the rest; least recently seen first. Leaves out ads seen
    online in the last CHECK_AFTER_HOURS and ones never shown (drop_reasons)."""
    drop = set(cfg["search"].get("drop_reasons") or [])
    cutoff = (datetime.now() - timedelta(hours=CHECK_AFTER_HOURS)).isoformat(timespec="seconds")

    def rank(r) -> int:
        if r.status == "saved":
            return 0
        if r.status == "hidden":
            return 3
        if r.verdict.ok:
            return 1
        return 4 if {"area", "avoid"} & set(r.verdict.codes) else 2

    due = [r for r in store.all_rows(src.name)
           if not r.gone and r.last_seen < cutoff and not drop & set(r.verdict.codes)]
    return sorted(due, key=lambda r: (rank(r), r.last_seen))


def _look_up(src, cfg: dict, store: Store, row) -> bool:
    """Looks one ad up. Taken offline: files it away and returns True.
    Raises when it can't tell; a site that blocks us is paused first."""
    try:
        why = src.offline(row.listing)
    except urllib.error.HTTPError as e:
        if e.code not in (404, 410):
            raise
        why = "deleted"
    except Blocked as e:
        _blocked(store, src.name, _every(cfg, src.name), e)
        raise
    if why:
        store.mark_gone([row.listing.key], why)
    else:
        store.touch(row.listing.key)
    return bool(why)


def check_offline(cfg: dict, store: Store, http, notifier: Notifier) -> None:
    """--check-offline: finds every ad taken offline now, instead of
    `check_per_run` per hourly check. Lookups take turns between the sites, so
    none gets many requests in a row; sources that can't look up a single ad
    go through all their pages instead (a sweep)."""
    queues, sweepers = {}, set()
    for src in enabled_sources(cfg, http):
        if src.can_probe:
            queues[src] = _to_check(src, cfg, store)
        else:
            sweepers.add(src.name)
            store.meta_set(f"sweep:{src.name}", "0")  # due now
    if sweepers:
        run_once(cfg, store, http, notifier, only=sweepers)
    total = sum(len(q) for q in queues.values())
    log.info("looking up %d ads (%s)", total, ", ".join(f"{s.name} {len(q)}" for s, q in queues.items()))
    stats = {src.name: [0, 0] for src in queues}  # looked up, offline
    while queues:
        for src in list(queues):
            if not queues[src]:
                del queues[src]
                continue
            row = queues[src].pop(0)
            try:
                stats[src.name][1] += _look_up(src, cfg, store, row)
                stats[src.name][0] += 1
            except Blocked:
                del queues[src]
            except Exception as e:  # noqa: BLE001 - the hourly checks pick up the rest
                log.warning("%s: could not check %s (%s); skipping the rest of %s", src.name, row.listing.url, e, src.name)
                del queues[src]
    for name, (looked, gone) in stats.items():
        log.info("%-14s looked up %3d  taken offline %d", name, looked, gone)


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
    if not dry_run and changed:
        log.info("%-14s %d of %d co-op pages changed", "watch", changed, checked)
