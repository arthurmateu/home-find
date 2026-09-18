# homefind

Polls Berlin rental portals every few minutes for flats in **Charlottenburg**
(incl. Westend and Charlottenburg-Nord) **under 1000 € warm**, drops the junk
(swaps, sublets, WG rooms, furnished short-term lets, WBS-only, scams) and
pushes the rest to your phone. Python 3.11+, standard library only.

## Sources

| Source | What it covers | How |
|---|---|---|
| `inberlinwohnen` | All 6 municipal landlords (degewo, GESOBAU, Gewobag, HOWOGE, STADT UND LAND, WBM). Below-market rents, no scams. Many need a WBS. | Livewire JSON in the page |
| `charlotte1907` | Charlottenburger Baugenossenschaft, the big co-op in Charlottenburg (~6,500 flats). Weekly offers, mostly members-only. | HTML |
| `immoscout` | ImmoScout24, the largest portal | The mobile app's API (the website blocks bots) |
| `kleinanzeigen` | Private landlords and Nachmieter ads; also the most scams | HTML + detail page per candidate |
| `wggesucht` | Whole flats only, open-ended leases only | HTML + detail page per candidate |
| `immowelt` | Partial: 30 listings per URL, no paging (its search API is bot-protected) | LZ-compressed JSON in the page |
| `[[watch]]` | 7 more co-ops with Charlottenburg-Wilmersdorf stock | Alerts when new text appears on their offers page |

## Setup

1. Install the **ntfy** app ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / [iOS](https://apps.apple.com/app/ntfy/id1625396347), or <https://ntfy.sh/app> in a browser) and subscribe to the `ntfy_topic` in `config.toml`.
2. `python3 -m homefind --test-notify`
3. `python3 -m homefind`: the first run stores what is online now **without** pushing it (look at `out/report.html`); later runs push only new matches.

## Running it automatically

systemd timer (every 10 min):

```bash
mkdir -p ~/.config/systemd/user && cp systemd/homefind.* ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now homefind.timer
journalctl --user -u homefind -f        # watch the logs
```

**WSL caveat:** the timer only fires while WSL is running, and Windows shuts
WSL down shortly after the last terminal closes. Either keep a WSL terminal
open, or skip the timer and use **Windows Task Scheduler**: trigger "every 10
minutes", action `wsl.exe` with arguments
`-d Ubuntu --cd /home/mouse/projects/home-find -- python3 -m homefind`.

Or simply leave `python3 -m homefind --loop` running in a terminal.

## Commands

```
python3 -m homefind                  # one run
python3 -m homefind --loop           # run every run.interval_minutes
python3 -m homefind --dry-run        # evaluate what's online now, print every verdict, store nothing
python3 -m homefind --only immoscout,kleinanzeigen
python3 -m homefind --rejected 50    # what got filtered out, and why
python3 -m homefind --recheck        # re-apply the rules after editing config.toml
python3 -m homefind --no-push        # store + print, no notifications
```

State lives in `data/homefind.db` (delete it to start over); `out/report.html`
lists every match so far plus the recent rejects and why.

## Filtering

All in `homefind/rules.py`. **Hard rejects:** over budget (warm rent; estimated
from cold rent at `extra_costs_per_sqm` if that's all there is), outside the
area, under `min_size_sqm`, swap offers ("Tauschwohnung", ~85% of cheap IS24
results), wanted ads, WG rooms, temporary/sublet/holiday flats, furnished,
WBS-only (unless `have_wbs`), co-op members-only (unless `coop_member`),
senior housing, and no photos.

**Scam points** (rejected at `scam_threshold`, default 3; below it the match is
sent with ⚠ warnings): money before viewing, keys by post, "I'm abroad",
Airbnb as payment, viewing fees (3 or 2 points each); account < 30 days old,
suspiciously cheap (< 8 €/m² warm), implausible rooms/size (2 each);
WhatsApp/e-mail contact, only one photo, broker fee, "VB" rent, private
listing online > 180 days, private landlord based outside Berlin (1 each).
Municipal and co-op listings skip the scam checks.

Reposts, or the same flat on two portals (same warm rent, size and postcode
within 30 days), are recorded but not pushed a second time.

## Limitations

- Sites change their markup; a source that breaks logs an error and the others keep running. Most parsers are ~50 lines in `homefind/sources/`.
- The ImmoScout24 app API is unofficial. If it stops working, set up a saved search with e-mail alerts on immobilienscout24.de.
- Request rates are deliberately low (≥ 2 s between requests per site, ~15 requests per run once warmed up). Automated access is against most portals' terms; keep it to personal use.
- Many good ImmoScout listings are "Plus members only" for their first days; the push says so.

## Tips for the search itself

- **Speed matters most**: cheap flats in Charlottenburg get hundreds of messages and go offline within hours. Keep a short German template message ready.
- Have a PDF ready: SCHUFA, last 3 payslips, Mietschuldenfreiheitsbescheinigung (from your current landlord), ID.
- **WBS:** check if you qualify with the [income check](https://ssl.stadtentwicklung.berlin.de/wohnen/wbs/index.shtml) and [apply online](https://service.berlin.de/dienstleistung/120671/). With one, set `have_wbs = true`; that opens up about a quarter of the municipal listings.
- **Co-ops** are the cheapest long-term option but need membership (shares, often a few hundred to a few thousand €) and many have waiting lists or closed membership. Joining one early is worth it even if it takes a while.
