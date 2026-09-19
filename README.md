# homefind

Polls Berlin rental portals every few minutes for flats in **Charlottenburg,
Westend, Halensee and Wilmersdorf** **under 1000 € warm**, drops the junk
(swaps, sublets, WG rooms, furnished short-term lets, WBS-only, scams) and
pushes the rest to your phone. Python 3.11+, standard library only.

## Sources

| Source | Checked every | What it covers | How |
|---|---|---|---|
| `inberlinwohnen` | 60 min | All 6 municipal landlords (degewo, GESOBAU, Gewobag, HOWOGE, STADT UND LAND, WBM). Below-market rents, no scams. Many need a WBS. | Livewire JSON in the page |
| `immoscout` | 60 min | ImmoScout24, the largest portal | The mobile app's API (the website blocks bots) |
| `kleinanzeigen` | 60 min | Private landlords and Nachmieter ads; also the most scams | HTML + detail page per candidate |
| `wggesucht` | 60 min | Whole flats only, open-ended leases only | HTML + detail page per candidate |
| `immowelt` | 60 min | Partial: 30 listings per neighbourhood page, no paging (its search API is bot-protected) | LZ-compressed JSON in the page |
| `charlotte1907` | 60 min | Charlottenburger Baugenossenschaft, the big co-op in Charlottenburg. Weekly offers, mostly members-only. | HTML |
| `[[watch]]` | 60 min | 7 more co-ops with Charlottenburg-Wilmersdorf stock | Alerts when new text appears on their offers page |

Intervals are `every_minutes` in `config.toml` (hourly by default, to stay well
clear of rate limits): roughly 20 requests an hour in total, spread over 6 sites, with ≥ 2 s between
requests to the same site. A site that blocks a request is paused (2×, 4×, …
its interval, up to 6 hours) while the others carry on.

## Setup

1. Install the **ntfy** app ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / [iOS](https://apps.apple.com/app/ntfy/id1625396347), or <https://ntfy.sh/app> in a browser) and subscribe to the `ntfy_topic` in `config.toml`.
2. Send a test notification (see below). Listings that were already online when
   a source was first added were stored without a push; from then on only new
   matches are pushed.

## Running it from Windows (PowerShell)

Start it hidden in the background and open the page:

```powershell
Start-Process wsl -ArgumentList '-d Ubuntu --cd /home/mouse/projects/home-find -- python3 -m homefind --loop --open' -WindowStyle Hidden
```

After that, just open **<http://localhost:8765>** whenever you like (bookmark
it). The page is always current when opened, and while it stays open new
listings appear by themselves (or as a "new match" banner if you've scrolled
down). If a new match comes in while you're in another tab or window, the
page's tab shows it: "(1) New match · Flat hunt" and a red dot on its icon,
until you switch back. No sound, no pop-up. Stop it with:

```powershell
wsl -d Ubuntu -- pkill -f '^python3 -m homefind'
```

Or keep it in a visible window instead (Ctrl+C or closing the window stops it):

```powershell
wsl -d Ubuntu --cd /home/mouse/projects/home-find -- python3 -m homefind --loop --open
```

Test notification: `wsl -d Ubuntu --cd /home/mouse/projects/home-find -- python3 -m homefind --test-notify`

Only one instance runs at a time. It keeps WSL up while running; after a
Windows restart, start it again.

## The web page

- **Matches / Saved / Rejected / Hidden** tabs (bookmarkable: `/#saved`).
- **Photos:** arrows on each card flip through all photos; click a photo for
  the full-screen viewer (arrow keys, swipe, thumbnail strip, Esc to close).
- **Descriptions:** per card, or "Expand descriptions" for all.
- **☆ Save / Hide** on every listing, stored in the database (both have Undo).
  Saved listings move to the Saved tab, so they don't take space under Matches.
- **Score (0–100)** on every listing, rejected ones included, sorted best
  first (or newest / cheapest / largest). "Why 74?" shows the points:
  value for money (warm €/m²) 35 · budget fit 15 (negative when over) ·
  size 20 + rooms 5 · freshness 10 (the first hours after posting count most) ·
  photos / verified or municipal landlord / description 10 · balcony,
  kitchen, lift, Altbau, garden up to 8; minus scam signs, Ablöse, an
  estimated warm rent, ImmoScout Plus-only, semi-basement. Tune it in
  `homefind/rating.py`. Push notifications start with the score.
- **Rejected** has a chip per reason. **Near miss** = everything fine except
  the warm rent is over budget while the cold rent is within
  `near_miss_cold_rent` (1000 € by default); that's why the sources search up
  to 1000 € *cold*. Sorted by score, the near misses worth a look come first.
- WG rooms, senior housing, swap offers and anything in former East Berlin or
  Spandau don't show up at all (`drop_reasons` in `config.toml`).

## Commands

```
python3 -m homefind                  # check every source once
python3 -m homefind --loop           # keep going, each source on its own schedule, + web UI
python3 -m homefind --loop --open    # ... and open the web UI in your browser
python3 -m homefind --serve          # web UI only, no polling
python3 -m homefind --dry-run        # evaluate what's online now, print every verdict, store nothing
python3 -m homefind --only immoscout,kleinanzeigen
python3 -m homefind --rejected 50    # what got filtered out, and why
python3 -m homefind --recheck        # re-apply the rules after editing config.toml
python3 -m homefind --no-push        # store + print, no notifications
```

State (listings, verdicts, saved/hidden) lives in `data/homefind.db`; delete
it to start over.

## Filtering

All in `homefind/rules.py`. **Hard rejects:** over budget (warm rent; estimated
from cold rent at `extra_costs_per_sqm` if that's all there is), outside the
area (by postcode; the portal's neighbourhood label only when there is no
postcode; East Berlin and Spandau, `avoid_zip_codes`, are dropped outright), a title naming another district ("… Berlin Spandau"), under
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
