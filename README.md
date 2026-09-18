# homefind

Polls Berlin rental portals every few minutes for flats in **Charlottenburg,
Westend, Halensee and Wilmersdorf** **under 1000 € warm**, drops the junk
(swaps, sublets, WG rooms, furnished short-term lets, WBS-only, scams) and
pushes the rest to your phone. Python 3.11+, standard library only.

## Sources

| Source | Checked every | What it covers | How |
|---|---|---|---|
| `inberlinwohnen` | 2 min | All 6 municipal landlords (degewo, GESOBAU, Gewobag, HOWOGE, STADT UND LAND, WBM). Below-market rents, no scams. Many need a WBS. | Livewire JSON in the page |
| `immoscout` | 2 min | ImmoScout24, the largest portal | The mobile app's API (the website blocks bots) |
| `kleinanzeigen` | 3 min | Private landlords and Nachmieter ads; also the most scams | HTML + detail page per candidate |
| `wggesucht` | 5 min | Whole flats only, open-ended leases only | HTML + detail page per candidate |
| `immowelt` | 15 min | Partial: 30 listings per neighbourhood page, no paging (its search API is bot-protected) | LZ-compressed JSON in the page |
| `charlotte1907` | 60 min | Charlottenburger Baugenossenschaft, the big co-op in Charlottenburg. Weekly offers, mostly members-only. | HTML |
| `[[watch]]` | 60 min | 7 more co-ops with Charlottenburg-Wilmersdorf stock | Alerts when new text appears on their offers page |

Intervals are `every_minutes` in `config.toml`. That's about 2–3 requests a
minute in total, at most one a minute per site, with ≥ 2 s between requests
to the same site. A site that blocks a request is paused (2×, 4×, … its
interval, up to an hour) while the others carry on.

## Setup

1. Install the **ntfy** app ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / [iOS](https://apps.apple.com/app/ntfy/id1625396347), or <https://ntfy.sh/app> in a browser) and subscribe to the `ntfy_topic` in `config.toml`.
2. Send a test notification (see below). The listings already online when a source was first added are in the report but were not pushed; from then on, only new ones are.

## Running it from Windows (PowerShell)

Keep it running in a PowerShell window (Ctrl+C stops it; closing the window too):

```powershell
wsl -d Ubuntu --cd /home/mouse/projects/home-find -- python3 -m homefind --loop
```

Or run it hidden in the background, and stop it later:

```powershell
Start-Process wsl -ArgumentList '-d Ubuntu --cd /home/mouse/projects/home-find -- python3 -m homefind --loop' -WindowStyle Hidden
wsl -d Ubuntu -- pkill -f '^python3 -m homefind'
```

Test notification, and opening the report:

```powershell
wsl -d Ubuntu --cd /home/mouse/projects/home-find -- python3 -m homefind --test-notify
start \\wsl.localhost\Ubuntu\home\mouse\projects\home-find\out\report.html
```

Only one instance runs at a time (a second one exits with "another homefind
run is in progress"). While it runs, WSL stays up; if Windows restarts,
start it again.

## Commands

```
python3 -m homefind                  # check every source once
python3 -m homefind --loop           # keep going, each source on its own schedule
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
area (by postcode; the portal's neighbourhood label only when there is no
postcode), a title naming another district ("… Berlin Spandau"), under
`min_size_sqm`, swap offers ("Tauschwohnung", ~85% of cheap IS24 results),
wanted ads, cellars/parking/offices, WG rooms, temporary/sublet/holiday flats,
furnished, WBS-only (unless `have_wbs`), co-op members-only (unless
`coop_member`), senior housing, and no photos.

**Scam points** (rejected at `scam_threshold`, default 3; below it the match is
sent with ⚠ warnings): money before viewing, keys by post, "I'm abroad",
Airbnb as payment, viewing fees (3 or 2 points each); account < 30 days old,
suspiciously cheap (< 10 €/m² warm), implausible rooms/size (2 each);
WhatsApp/e-mail contact, only one photo, broker fee, "VB" rent, private
listing online > 180 days, private landlord based outside Berlin (1 each).
Municipal and co-op listings skip the scam checks.

Reposts, or the same flat on two portals (same warm rent, size and postcode
within 30 days), are recorded but not pushed a second time.

## Limitations

- Sites change their markup; a source that breaks logs an error and the others keep running. Most parsers are ~50 lines in `homefind/sources/`.
- The ImmoScout24 app API is unofficial. If it stops working, set up a saved search with e-mail alerts on immobilienscout24.de.
- Automated access is against most portals' terms; the request rate above is kept low, keep it to personal use.
- Many good ImmoScout listings are "Plus members only" for their first days; the push says so.

## Tips for the search itself

- **Speed matters most**: cheap flats in Charlottenburg get hundreds of messages and go offline within hours. Keep a short German template message ready.
- Have a PDF ready: SCHUFA, last 3 payslips, Mietschuldenfreiheitsbescheinigung (from your current landlord), ID.
- **WBS:** check if you qualify with the [income check](https://ssl.stadtentwicklung.berlin.de/wohnen/wbs/index.shtml) and [apply online](https://service.berlin.de/dienstleistung/120671/). With one, set `have_wbs = true`; that opens up about a quarter of the municipal listings.
- **Co-ops** are the cheapest long-term option but need membership (shares, often a few hundred to a few thousand €) and many have waiting lists or closed membership. Joining one early is worth it even if it takes a while.
