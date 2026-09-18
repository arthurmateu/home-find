"""CLI entry point: python3 -m homefind --help"""

from __future__ import annotations

import argparse
import fcntl
import logging
import random
import sys
import time
from pathlib import Path

from . import config
from .app import recheck, run_once, seconds_until_due
from .report import write_report
from .net import Fetcher
from .notify import Notifier, print_reject
from .sources import SOURCE_NAMES
from .store import Store

log = logging.getLogger("homefind")
PROJECT_CONFIG = Path(__file__).resolve().parent.parent / "config.toml"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="homefind", description="Poll Berlin rental portals for matching flats.")
    ap.add_argument("-c", "--config", help=f"config file (default: {PROJECT_CONFIG})")
    ap.add_argument("--loop", action="store_true",
                    help="keep running; each source on its own schedule (sources.*.every_minutes)")
    ap.add_argument("--only", help="comma-separated: " + ",".join(SOURCE_NAMES + ["watch"]))
    ap.add_argument("--dry-run", action="store_true",
                    help="evaluate everything online now and print each verdict; store and push nothing")
    ap.add_argument("--no-push", action="store_true", help="store and print, but send no notifications")
    ap.add_argument("--rejected", type=int, metavar="N", help="show the last N rejected listings and why")
    ap.add_argument("--recheck", action="store_true",
                    help="re-apply the rules to all stored listings (after editing config.toml)")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    cfg = config.load(args.config or PROJECT_CONFIG)
    store = Store(cfg["run"]["db_path"])
    http = Fetcher(delay=cfg["run"]["request_delay_seconds"])
    notifier = Notifier(cfg, http, enabled=not (args.no_push or args.dry_run))

    if args.test_notify:
        if not notifier.configured:
            print("No notification channel configured: set notify.ntfy_topic (or Telegram) in config.toml")
            return 1
        ok = notifier.push("homefind test", "Notifications work.", "https://www.inberlinwohnen.de/wohnungsfinder/")
        print("sent" if ok else "failed, see warnings above")
        return 0 if ok else 1
    if args.rejected:
        for listing, verdict, *_ in store.rejected(args.rejected):
            print_reject(listing, verdict)
        return 0
    if args.recheck:
        total, changed, dropped = recheck(cfg, store)
        write_report(store, cfg["run"]["report_path"])
        print(f"rechecked {total} listings: {changed} changed verdict, {dropped} will be re-fetched next run")
        return 0

    if args.loop and args.dry_run:
        ap.error("--dry-run is a one-off check; drop --loop")
    only = {s.strip() for s in args.only.split(",")} if args.only else None
    unknown = (only or set()) - set(SOURCE_NAMES) - {"watch"}
    if unknown:
        ap.error(f"unknown source(s): {', '.join(sorted(unknown))}")

    lock_path = Path(cfg["run"]["db_path"]).with_suffix(".lock")
    lock = lock_path.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another homefind run is in progress", file=sys.stderr)
        return 1

    if not notifier.configured and not (args.dry_run or args.no_push):
        log.warning("no notification channel configured; matches only go to the console and %s",
                    cfg["run"]["report_path"])
    if not args.loop:
        run_once(cfg, store, http, notifier, only=only, dry_run=args.dry_run)
        if not args.dry_run:
            log.info("report: %s", cfg["run"]["report_path"])
        return 0

    log.info("watching; each source is checked on its own schedule (sources.*.every_minutes). Ctrl+C stops.")
    log.info("report: %s", cfg["run"]["report_path"])
    while True:
        run_once(cfg, store, http, notifier, only=only, scheduled=True)
        time.sleep(seconds_until_due(cfg, store, only) * random.uniform(0.9, 1.15))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nstopped")
        sys.exit(130)
