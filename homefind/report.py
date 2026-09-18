"""Writes a static HTML page with every match so far, plus recent rejects."""

from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path

from .notify import headline

CSS = """
:root { --bg:#f7f7f5; --card:#fff; --ink:#1d1d1b; --muted:#6b6b66; --line:#e4e4df;
        --accent:#1f6f5c; --warn:#a2560c; --warn-bg:#fdf1e3; --new:#1f6f5c; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#161615; --card:#1f1f1d; --ink:#ecece8; --muted:#9a9a93; --line:#33332f;
          --accent:#6fc2a9; --warn:#f0b36b; --warn-bg:#3a2a17; --new:#6fc2a9; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
       font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width:1100px; margin:0 auto; padding:24px 16px 64px; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:17px; margin:36px 0 12px; }
.sub { color:var(--muted); margin:0 0 20px; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:14px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden;
        display:flex; flex-direction:column; }
.card img { width:100%; height:170px; object-fit:cover; background:var(--line); display:block; }
.body { padding:12px 14px 14px; display:flex; flex-direction:column; gap:6px; }
.price { font-weight:650; font-size:16px; }
.title { color:var(--ink); text-decoration:none; } .title:hover { text-decoration:underline; }
.meta { color:var(--muted); font-size:13px; }
.chips { display:flex; flex-wrap:wrap; gap:4px; }
.chip { font-size:12px; padding:2px 7px; border-radius:99px; border:1px solid var(--line); color:var(--muted); }
.chip.warn { background:var(--warn-bg); color:var(--warn); border-color:transparent; }
.chip.new { color:var(--new); border-color:var(--new); }
table { width:100%; border-collapse:collapse; font-size:13px; }
td { padding:6px 8px; border-top:1px solid var(--line); vertical-align:top; }
td a { color:var(--accent); } details summary { cursor:pointer; color:var(--muted); }
.wrap { overflow-x:auto; }
"""


def write_report(store, path: str, new_keys: set[str] | None = None) -> None:
    new_keys = new_keys or set()
    cards = []
    for listing, verdict, first_seen, last_seen, _ in store.matches():
        chips = []
        if listing.key in new_keys:
            chips.append('<span class="chip new">new</span>')
        chips += [f'<span class="chip warn">⚠ {escape(f)}</span>' for f in verdict.flags]
        chips += [f'<span class="chip">{escape(n)}</span>' for n in verdict.notes]
        img = (f'<img src="{escape(listing.image_url)}" alt="" loading="lazy">'
               if listing.image_url and "%" not in listing.image_url else "")
        who = " · ".join(x for x in (listing.source, listing.landlord) if x)
        cards.append(f"""
<article class="card">{img}<div class="body">
  <div class="price">{escape(headline(listing, verdict))}</div>
  <a class="title" href="{escape(listing.url)}" target="_blank" rel="noopener">{escape(listing.title or listing.url)}</a>
  <div class="meta">{escape(who)} · first seen {first_seen[:16].replace("T", " ")} · last seen {last_seen[:16].replace("T", " ")}</div>
  <div class="chips">{"".join(chips)}</div>
</div></article>""")

    rows = []
    for listing, verdict, first_seen, _, _ in store.rejected(200):
        rows.append(
            f'<tr><td>{first_seen[:16].replace("T", " ")}</td><td>{escape(listing.source)}</td>'
            f'<td><a href="{escape(listing.url)}" target="_blank" rel="noopener">{escape(listing.title[:70] or listing.id)}</a>'
            f'<br>{escape(headline(listing, verdict))}</td><td>{escape("; ".join(verdict.reasons))}</td></tr>')

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Flat matches</title>
<style>{CSS}</style></head><body><main>
<h1>Flat matches</h1>
<p class="sub">{len(cards)} matches · updated {datetime.now():%Y-%m-%d %H:%M}</p>
<div class="grid">{"".join(cards) or "<p>No matches yet.</p>"}</div>
<h2>Recently rejected</h2>
<details><summary>{len(rows)} most recent rejects and why (check these if you think the filters are too strict)</summary>
<div class="wrap"><table>{"".join(rows)}</table></div></details>
</main></body></html>"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(page, encoding="utf-8")
