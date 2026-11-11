# homefind

Polls Berlin rental portals every few minutes for flats with **2+ rooms** in
**west-central Berlin** (Charlottenburg, Wilmersdorf, Moabit, Tiergarten,
Wedding, and around them Mitte, Schöneberg, Kreuzberg, Tegel, ...) **under 982 € warm**, drops the junk
(swaps, sublets, WG rooms, furnished short-term lets, WBS-only, scams) and
pushes the rest to your phone, and can fill in (or send) the contact form for
you (ImmoScout24 and Immowelt). Python 3.11+, standard library only (applying uses Tampermonkey in Firefox).

## Sources

| Source | Checked every | What it covers | How |
|---|---|---|---|
| `inberlinwohnen` | 60 min | All 6 municipal landlords (degewo, GESOBAU, Gewobag, HOWOGE, STADT UND LAND, WBM). Below-market rents, no scams. Many need a WBS. | Livewire JSON in the page; for candidates, the landlord's own ad for every photo and the text (inberlinwohnen has one photo and no text; HOWOGE and GESOBAU ads keep just that) |
| `immoscout` | 15 min | ImmoScout24, the largest portal and where most matches come from; all of Berlin | The mobile app's API (the website blocks bots) |
| `kleinanzeigen` | 60 min | Private landlords and Nachmieter ads; also the most scams; all of Berlin | HTML + detail page per candidate |
| `wggesucht` | 60 min | Whole flats only, open-ended leases only; all of Berlin | HTML + detail page per candidate |
| `immowelt` | 60 min | Partial: Charlottenburg, Westend, Halensee and Wilmersdorf up to 900 € cold, 30 listings per neighbourhood page, no paging (its search API is bot-protected, and other filters have no page). Its ad pages are bot-protected too, so ads taken offline are only noticed when a lookup gets through | LZ-compressed JSON in the page |
| `charlotte1907` | 60 min | Charlottenburger Baugenossenschaft, the big co-op in Charlottenburg. Weekly offers, mostly members-only. | HTML |
| `[[watch]]` | 60 min | 7 more co-ops with Charlottenburg-Wilmersdorf stock | Alerts when new text appears on their offers page |

Intervals are `every_minutes` in `config.toml` (hourly by default, to stay well
clear of rate limits; ImmoScout every 15 minutes, since that's where most
matches come from and each check is one request when nothing is new): roughly
23 requests an hour in total, spread over 6 sites, with ≥ 2 s between
requests to the same site, plus up to 20 an hour to find ads that were taken
offline (see below). A site that blocks a request is paused (2×, 4×, …
its interval, up to 6 hours) while the others carry on.

## Setup

1. Install the **ntfy** app ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / [iOS](https://apps.apple.com/app/ntfy/id1625396347), or <https://ntfy.sh/app> in a browser) and subscribe to a topic with a name nobody could guess: whoever knows it can read your pushes and send you their own. Set it as `ntfy_topic` under `[notify]` in `config.local.toml` (see 3), not in `config.toml`.
2. Send a test notification (see below). Listings that were already online when
   a source was first added were stored without a push; from then on new
   Matches and Good flats are pushed (see the tabs, below).
3. Settings you'd rather keep off GitHub (the ntfy topic, the places in
   `[bonus] daily`) go in `config.local.toml`, next to `config.toml`: it's in
   `.gitignore`, and what it sets goes over `config.toml`'s.

## Running it

From the Ubuntu terminal:

```bash
cd ~/projects/home-find
python3 -m homefind --loop --open
```

Ctrl+C stops it. After that, just open **<http://localhost:8765>** whenever
you like (bookmark it). The page is always current when opened, and while it
stays open it keeps itself current: new listings, ads taken offline, and an
Applied or Hide made in another tab (straight away) all show up without the page
jumping under you; if new Matches or Good flats arrive while you're scrolled down or on
another tab of the page, a "new match" banner offers them. If one comes in while you're in
another tab or window, the page's tab shows it: "(1) New flat · Flat hunt"
and a red dot on its icon, until you switch back. No sound, no pop-up.

Test notification: `python3 -m homefind --test-notify`

Only one instance runs at a time; after a reboot, start it again.

**Updates need no restart.** When homefind's Python code, `config.toml` or
`config.local.toml` change (they're only read at startup), it restarts by
itself, in the same terminal and with the same options: once the files have
stayed the same for 30 seconds (`SETTLE_SECONDS` in `homefind/update.py`),
between two checks, and not while an application is under way (opening,
filling in, sending, or waiting for you to send it, for up to half an hour) or
the family page is uploading. It first makes sure the new version starts; if
it doesn't (a mistake in the code or in a config file), the one running keeps
going, and the terminal and the page say what's wrong, until the files change
again.

Open pages reload themselves into a new version of the page (`ui.html`, read
on every page load, so that needs no restart), once it has stayed the same for
30 seconds and homefind runs the update too: straight away when the page is out
of sight (another tab, a minimized window, the phone's browser in the
background), else once you haven't touched it for 5 minutes (`AWAY_MS` in
`ui.html`). Until then, a banner offers a Reload. It comes back as you left it:
the same tab, search, filters, cards on screen, open descriptions, photos, the
photo viewer, and the new-flat count in the tab's title. A page that doesn't
start (saved halfway through an edit, say) tries again every half minute.

## The web page

- **Under the title:** when the last check was and when the next one is
  (hover for each site), or which site it's checking right now; a site that
  blocked us shows up there as paused until when. **↻ Check now** checks
  every site straight away (except paused ones, to let the block wear off)
  and says what it found; only with `--loop`.
- **Matches / Good / Sufficient / Applied / Hidden** tabs (bookmarkable: `/#applied`).
  A flat that passes the rules goes on one of the first three, by where it is
  and how many rooms it has (`tier` in `homefind/rules.py`): **Matches** have 2+
  rooms (`wanted_rooms`) in the red area (`zip_codes`); **Good** have fewer
  there (1 or 1.5, or the ad doesn't say), or 2+ in the orange area round it
  (`edge_zip_codes`, "in the orange area" under Cons); **Sufficient** have
  fewer in the orange. New Matches and Good flats are pushed to your phone
  (`PUSHED_TIERS` in `homefind/app.py`); Sufficient ones aren't. A flat that
  fails a rule (over budget, outside the area, furnished, no rent stated, …,
  see [Filtering](#filtering)) isn't shown at all, except one you applied to:
  it stays on Applied, greyed out, with the rule it fails now on the card.
- **Photos:** arrows on each card flip through all photos; click a photo for
  the full-screen viewer (arrow keys, swipe, thumbnail strip, Esc to close).
  Every photo of a flat you've applied to is kept in `data/photos/` (a folder
  per ad), fetched right after you mark it applied or the Firefox helper sends
  your application, so they're still there when the ad is deleted, as ads
  often are within hours. Its card shows those copies. A photo the site no
  longer had gets the family page's smaller copy, if it has one
  (`homefind/keep.py`). Floor plans that ImmoScout24 shows as photos are among
  them; the PDFs an ImmoScout24 ad links to ("Weitere Dokumente": often a floor
  plan of the whole flat, the energy certificate) are kept in the same folder,
  named after their label, and linked on every card under 📄 (the site's copy
  until it's kept). Ads stored before PDFs were read get theirs when they're
  next looked up to see if they're still online.
- **Mini-map**: hover the 📍 neighbourhood line on a card (tap it on a phone)
  and a map pops up, with a pin at the address, or a dashed circle when only
  the postcode is known; click the map for Google Maps. Addresses are looked up
  once on OpenStreetMap's Nominatim (about one a second, as its usage policy
  asks) and kept in the database; while the UI runs, new listings are placed
  within a couple of minutes (`--geocode` does all of them at once). Map tiles
  are Stadia Maps' (light or dark, following the page), free without an API
  key for pages on localhost.
- **Search** box under the tabs, the same on every tab: each word has to be
  somewhere on the card (title, address, description, landlord, reasons, …),
  ignoring case and accents ("strasse" finds "Straße"). While it's in use, the
  tab counts show how many hits each tab has. Esc or "Clear" empties it.
- **Filters**, a dropdown each under the tabs: **Locality** on every tab
  (Ortsteil, from the address, else the portal's label; pick one or several),
  and on Hidden also **Show** (hidden by you, or taken offline). Each count
  in a menu is what you'd get by picking it, given the other filters; "Clear"
  resets the tab.
- **ImmoScout24 cards stand out** with a coloured outline and badge
  (`highlight_sources` in `config.toml`).
- **The same flat on several sites** gets one card, with its ImmoScout24 ad
  (or, once that's offline, one that's still online): the title and
  **Open ↗** go there, and **▾** next to Open lists its ad on each site. The
  line under the title names them all ("immoscout + immowelt"), as do the
  search. Mark applied and Hide go for every one of
  them, and the card only counts as offline once all of them are. Same flat
  = same warm rent, size and postcode, first seen within 30 days of each
  other (`SAME_FLAT_DAYS` in `homefind/store.py`), with no cold rent, street
  or house number that differs; when one
  site has two ads that could be it (identical flats in one building), they
  all keep their own cards. `FIRST_CHOICE` in `homefind/web.py` is the site
  that's opened first.
- **Descriptions:** per card, or "Expand descriptions" for all.
- **✉ Apply** on ImmoScout24 and Immowelt cards, and the **Full auto**,
  **Autofill** and **Autofill + send** switches in the header: see
  [Applying](#applying) below.
- **How far it got:** a dropdown next to ✓ Applied, on the Applied tab and on
  ones hidden after applying: no answer yet, Replied, Viewing, Turned down or
  Got it. It goes for the flat on every site it's on.
- **Mark applied / Hide** on every listing, stored in the database (both have
  Undo). Flats you applied for move to the Applied tab, so they don't take space
  under Matches, with the day you marked them on their badge (hover for the
  time). Hiding one keeps that date, and Unhide puts it back on Applied.
  Ones marked before dates were kept (when it was still called Saved) show no
  date.
- **Ads taken offline** are tagged **Deactivated** (switched off by the
  poster, may come back; mostly WG-Gesucht) or **Deleted**, with a grey photo
  and when it was noticed, and filed away by themselves: ones you applied for
  stay on Applied for 2 days (`APPLIED_OFFLINE_DAYS` in `homefind/store.py`),
  then move to Hidden; Matches, Good and Sufficient ones move to Hidden right away; ones you hid stay
  there; ones that fail the rules are deleted for good. The Hidden tab gets a chip per
  tag. If one comes back online, it moves back to where it was (a deleted one
  shows up as new).
  How it's found: each hourly check also looks up a few older ads
  (`check_per_run`: 5, WG-Gesucht 2; applied first, then the three tiers, then
  ones you hid; skipping any seen in the last 6 hours).
  inberlinwohnen goes through all its ~50 pages every 12 hours
  (`sweep_every_hours`) and charlotte1907's one page lists every offer, so
  anything missing there is gone. In between, inberlinwohnen's ads are looked
  up on the landlord's own page (not HOWOGE's or GESOBAU's): Gewobag's says
  when applications have closed, often within hours, and the lookup also
  brings in the photos and text of ads stored before those were loaded.
  `--check-offline` does all of it at once (inberlinwohnen by its sweep).
- **Score** (0–100, and bonuses can take it past 100) on every card, sorted best
  first (or newest / date applied / cheapest / largest). Hover the score (tap
  it on a phone) for its points:
  value for money (warm €/m²) 35 · budget fit 15 (negative when over) ·
  size 20 (25 m² none, 70 m² and up all) + rooms 10 (2 or more; 1.5
  rooms 4, 1 room none) · freshness 10 (the first hours after posting count most) ·
  photos / verified or municipal landlord / description 10 · balcony 5,
  kitchen, lift, garden up to 10 · not on the ground floor 3 (the
  floor the portal states, else the one the ad's text names; the points
  say which) · on the quiet side, says the text (Hinterhaus, Seitenflügel,
  Gartenhaus, Quergebäude, or rooms facing the courtyard) 2 · where it is,
  from `[bonus]` in `config.toml`: in the old area (Charlottenburg, Westend, Halensee, Wilmersdorf, the matches until 7 Nov
  2026) 5, on one of the streets you like (Ku'damm, Kantstraße,
  Otto-Suhr-Allee, Savignyplatz, Schloßstraße, ...) 4 · a U-Bahn or S-Bahn
  station (or its entrance) within 5 minutes on foot 4, a bus or tram stop
  within 2 minutes 2 · a supermarket within 5 minutes 10 · a park, woods, a
  lake, the Spree or the Landwehrkanal within 5 minutes 3 · near a place you
  go every day (`[bonus] daily`, in `config.local.toml`) its own `points` (9 if it doesn't set them, none with
  `score = false`): all of them up to 15 minutes door to door, on foot or by public transport, whichever is
  quicker (as on the card), sliding to none from 40 minutes, so one place can count for more than another;
  until BVG's planner has answered for a flat, its walk counts; minus scam signs, Ablöse, an estimated warm rent, semi-basement. ImmoScout Plus-only listings aren't
  marked down (you're a member). Tune it in `homefind/rating.py`. Push
  notifications start with the score.
  What's within walking distance needs the flat's house: ImmoScout24's pin
  when the ad shows the house number, else the address as looked up for the
  mini-map (so for other sites, it shows up a few minutes after the push).
  Minutes on foot are the straight line × 1.3 at 4.8 km/h. Stations,
  stops, supermarkets, parks and places to eat or drink come from OpenStreetMap (Overpass API), for all of Berlin, into
  `data/nearby.json`: fetched by the mini-map's background thread (or
  `--geocode`) and again once a month.
- **Notes** on a card, in three rows (hover a chip for the full text):
  **Pros**, what the score gives points for besides price and size (balcony,
  kitchen, lift, garden, the floor, the quiet side, a park nearby, a verified or
  municipal landlord, and work, a supermarket or a station close enough for
  its points) and ImmoScout Plus-only for now (fewer can apply); **Cons**, in red, the
  scam signs marked ⚠ (see Filtering), then what to know before going for it
  (fewer rooms than wanted, in the orange area, Nachmieter, Ablöse, an estimated warm rent,
  furnished, semi-basement, too residential, Altbau, ...); **Info**, how long
  each `[bonus] daily` place (work) takes, on foot or by public transport,
  whichever is quicker, and how far ("20 min by transport (5.4 km)"; in the
  place's own colour, its `pill`, and under Pros within 20 minutes when it's worth points), and the
  nearest supermarket, U-Bahn, S-Bahn and bus stop (up to 30 minutes on foot;
  hover for the name and metres), unless they're under Pros, then the rest
  (private landlord, a street's own budget, on another site first, ...).
  The walks need the house, like the score's; with only the street or
  postcode known, work is "~" kilometres and minutes by public transport from there.
- **Public transport to work** (on the card's work chip) comes from BVG's journey planner,
  through transport.rest (`v6.bvg.transport.rest`, free, no key): door to door,
  leaving at 08:30 on the next weekday, the fastest of the first few
  connections, by the timetable. It gets the two ends' coordinates (the flat's
  and your `[bonus] daily` place's), nothing else. Asked once per flat and
  kept in the database (table `trips`): the map's background thread asks about
  new ones after placing them, 30 at a time; `--geocode` does all of them.
- **Too residential** (under Cons, worth no points) = 5 or fewer
  restaurants, takeaways, bars and the like within 300 m of the house, so a
  courier at 1am is one the neighbours notice. When only the postcode is
  known it's **too residential?**, from how many there are per km² across the
  postcode (fewer than 18, the same as 5 within 300 m), which is a good guess
  but doesn't know your street. Pushes say so too, when it's known by then.
- **Rooms:** fewer than `min_rooms` (1) are dropped (the searches don't
  filter on rooms). Flats with fewer than `wanted_rooms` (2), so 1
  or 1.5 rooms, go a tier down (Good in the red area, Sufficient in the
  orange), with "only 1 room" under Cons on the card ("heads-up: only 1 room"
  in the push); the score gives them no points for rooms (2+ get 10).

## Applying

homefind can fill in the contact form on ImmoScout24 and Immowelt for you, in
Firefox, with the message and details in `apply.toml`. A small Tampermonkey
script, the **Firefox helper**, does the filling in on the site's own page,
where you're logged in.

Cards from the other sites have no ✉ Apply: apply to those on the site itself
(and Mark applied).

- **✉ Apply** on an ImmoScout24 or Immowelt card opens the ad in a new tab,
  fills in the form and stops at its send button: check it (change anything you
  like) and press it. The card says what it's up to, and moves to Applied once
  the site says it's sent. Two switches in the header say how far it goes:
  - **Autofill** (on to start with): it fills in the form. Off, ✉ Apply only
    opens the ad.
  - **Autofill + send**: it presses the send button too (once ImmoScout24's
    "Send in … s" has run down) and closes the tab; the button says
    "✉ Apply + send". Not on an ad the site says you've been in touch about.
- On any ImmoScout24 or Immowelt ad, **✉ Fill in** in the corner does the same.
  And whenever you send the form on an ad (filled in by hand or not), the helper
  tells homefind, which moves it to Applied: no Mark applied needed.
- homefind's links to ImmoScout24 ads (Open ↗, notifications) go to its English
  pages, and the helper fills those in as they are. `apply.toml` names the
  fields the way ImmoScout24's German form does; the helper knows what they're
  called in English and on Immowelt (`SAME` in `homefind/apply.user.js`).
- **Full auto** in the header (with `--loop`) opens new flats in Firefox by
  itself: ImmoScout24 ones only (where you apply, see below), and not reposts
  (a flat that was on another site first counts as new once it's on
  ImmoScout24). By tier:
  - a **Match** gets your message sent: the helper sends it (once "Send in …
    s" has run down), closes the tab and your phone gets "✓ Applied"; its
    badge says "Auto-applied". At most `max_per_day` (10) a day, counting
    ✉ Apply + send's; one with ⚠ warning signs, or past that, only gets
    filled in.
  - a **Good** one gets its form filled in, for you to send.
  - **Sufficient** ones are left alone.

  A form that asks for something `apply.toml` doesn't cover, or finds you logged
  out, isn't sent: it's left open in Firefox, with the field named on the page,
  on the card and (with Full auto) on your phone. `max_per_day` is under
  `[apply]` in `config.toml`.

Setting it up, once:

1. Copy `apply.example.toml` to `apply.toml` and fill it in: the message, and
   each field's label on the form with what to put there. `apply.toml` is in
   `.gitignore`, so your details stay on this machine. It's read each time a
   form is filled in, so changes need no restart.
2. **Install Firefox helper** in the header (while homefind runs), then Install
   in Tampermonkey. The link goes away once the helper has been on an ad; it's
   back as **Update Firefox helper** when homefind has a newer one (Tampermonkey
   also updates it by itself, once a day).
3. Be logged in to ImmoScout24 and Immowelt in Firefox.

Full auto opens ads with Windows' Firefox (`firefox` under `[apply]` names
another). If a field doesn't get filled in, `data/apply-last.json` lists every
field the last form had, with its label as the helper read it: that's the label
to use in `apply.toml`.

## The family page

A read-only page in Brazilian Portuguese, made for a phone, to follow the flats
you've applied to. Opened from a private link, it can be added to the home screen.
It's written for someone who knows how German flats work, so it keeps terms like
Zimmer and Warmmiete, and only explains what's particular to Berlin.

- **On it:** how many applications, per week; a map of roughly where the flats
  are; and a card per flat with photos, neighbourhood, rooms, size, warm rent, the
  day you applied, how far it got (the dropdown next to ✓ Applied), whether the ad
  is still up, its score with the points behind it, and its notes (heads-ups and
  extras). Those are translated by the table `SAID` in `homefind/casa.py`: a note
  that isn't in it stays off the page (and is logged once). It has every flat you've marked applied, also the ones no longer on
  your Applied tab; the ones from before dates were kept sit behind a button.
- **Not on it:** addresses, street and station names, titles, ad text and links,
  landlords, flats you didn't apply to, or anything from
  `apply.toml`. `_entry` in `homefind/casa.py`
  is the whole list of what it gets.
- **How it stays up to date:** homefind builds it as plain files in `data/casa/`
  and uploads them with rsync, while `--loop` or `--serve` runs: every 15 minutes
  (`every_minutes`) and about 20 seconds after you change something on your page.
  The server only hands out files, so the page keeps working while your PC is off
  (it says when it was last updated), and nothing on the internet reaches homefind.
- **Setting up the server**, once (`host`, `remote_dir` and `url` are under `[casa]`
  in `config.toml`):

  ```
  scp casa-setup.sh alt: && ssh -t alt 'sudo bash casa-setup.sh'
  ```

- **The link:** `--casa` uploads now and prints it. Anyone who has it can see the
  page; `--casa-new-link` replaces it, and the old one stops working.

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
python3 -m homefind --check-offline  # look up every stored ad now and file away the offline ones
python3 -m homefind --geocode        # place every listing on the map now (and fetch data/nearby.json, and the trips to work)
python3 -m homefind --no-push        # store + print, no notifications
python3 -m homefind --casa           # upload the family page now and print its private link
python3 -m homefind --casa-new-link  # replace that link (the old one stops working) and upload
```

State (listings, verdicts, applied/hidden) lives in `data/homefind.db`; delete
it to start over. The photos and PDFs of flats you've applied to are in `data/photos/`.

## Filtering

All in `homefind/rules.py`. A flat that fails one of these is stored, so it
isn't loaded again, but never shown (`--rejected` lists them, with why).
**Hard rejects:** over budget (over `max_warm_rent`, 982 € warm; the warm rent is
estimated from the cold rent at `extra_costs_per_sqm` if that's all there is), outside the
area (by postcode: `zip_codes` is the red area, `edge_zip_codes` the orange one round it,
every other postcode is dropped outright; the portal's neighbourhood label,
`names` / `edge_names`, only when there is no postcode), a title naming
another district ("… Berlin Neukölln"), under
`min_size_sqm`, swap offers ("Tauschwohnung", ~85% of cheap IS24 results),
wanted ads, cellars/parking/offices, WG rooms, temporary/sublet/holiday flats,
furnished, WBS-only (unless `have_wbs`), co-op members-only (unless
`coop_member`), senior housing, and no photos.

Detail pages are only loaded for ads that pass on what the search shows, so
an ad rejected there (say "voll möbliert" in the title) keeps the search's
data. Its rent is still checked: ImmoScout shows only the cold rent, so the
warm rent is estimated from it ("warm ≈ … (estimated)" on the card).

**Streets worth paying more for** (`street_max_warm_rent`, none now): their own
budget, and the score goes as much easier on their price. The street comes from
the address, or from the title when there's none ("Wohnung am Ku'damm" counts,
"nahe Ku'damm" doesn't). The searches go up to the highest budget, and the rules
reject what that brings in elsewhere.

**Scam points** (rejected at `scam_threshold`, default 3; below it the match is
sent with ⚠ warnings): money before viewing, keys by post, "I'm abroad",
Airbnb as payment, viewing fees (3 or 2 points each); account < 30 days old,
suspiciously cheap (< 10 €/m² warm), implausible rooms/size (2 each);
WhatsApp/e-mail contact, only one photo, broker fee, "VB" rent, private
listing online > 180 days, private landlord based outside Berlin (1 each).
Municipal and co-op listings skip the scam checks.

Reposts, or the same flat on two portals (same warm rent, size and postcode
within 30 days), are recorded but not pushed a second time. Except on
ImmoScout24, where you apply: a flat's first ad there is pushed (and taken
by Full auto) even when another site had it first, saying so, unless you
applied to it or hid it on that site. On the web page, the same flat on two
portals shares one card (see above), which is flagged as new when its
ImmoScout24 ad turns up.

## Limitations

- Sites change their markup; a source that breaks logs an error and the others keep running. Most parsers are ~50 lines in `homefind/sources/`.
- The ImmoScout24 app API is unofficial. If it stops working, set up a saved search with e-mail alerts on immobilienscout24.de.
- Automated access is against most portals' terms; the request rate above is kept low, keep it to personal use.
- Full auto and Autofill + send send messages from your ImmoScout24 account under your name. ImmoScout may not like automated contact requests: keep `max_per_day` low.
- The contact form is filled in by its labels, as they are on screen; if a site renames one, update `apply.toml` (or `SAME` in `homefind/apply.user.js`, for an English or Immowelt name). If the helper stops filling in or noticing sends altogether, the site changed the page: `homefind/apply.user.js` needs a look.
- Many good ImmoScout listings are "Plus members only" for their first days; the push says so.

## Tips for the search itself

- **Speed matters most**: cheap flats in Charlottenburg get hundreds of messages and go offline within hours. Keep a short German template message ready.
- Have a PDF ready: SCHUFA, last 3 payslips, Mietschuldenfreiheitsbescheinigung (from your current landlord), ID.
- **WBS:** check if you qualify with the [income check](https://ssl.stadtentwicklung.berlin.de/wohnen/wbs/index.shtml) and [apply online](https://service.berlin.de/dienstleistung/120671/). With one, set `have_wbs = true`; that opens up about a quarter of the municipal listings.
- **Co-ops** are the cheapest long-term option but need membership (shares, often a few hundred to a few thousand €) and many have waiting lists or closed membership. Joining one early is worth it even if it takes a while.
