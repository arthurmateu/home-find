"""Console output and push notifications (ntfy.sh and/or Telegram)."""

from __future__ import annotations

import html
import logging

from .models import Listing
from .rules import Verdict

log = logging.getLogger("homefind")


def headline(listing: Listing, verdict: Verdict | None = None) -> str:
    warm = verdict.warm if verdict and verdict.warm is not None else listing.warm_rent
    parts = []
    if warm:
        parts.append(f"{warm:.0f} € warm")
    if listing.size_sqm:
        parts.append(f"{listing.size_sqm:g} m²")
    if listing.rooms:
        parts.append(f"{listing.rooms:g} Zi")
    where = " ".join(x for x in (listing.zip_code, listing.district) if x)
    if where:
        parts.append(where)
    return " · ".join(parts) or listing.title


def print_match(listing: Listing, verdict: Verdict, score: int | None = None) -> None:
    lead = f"[{score}] " if score is not None else ""
    print(f"  ★ {lead}{headline(listing, verdict)}   [{listing.source}]")
    print(f"    {listing.title[:100]}")
    print(f"    {listing.url}")
    for f in verdict.flags:
        print(f"    ⚠ {f}")
    for n in verdict.notes:
        print(f"    · {n}")


def print_reject(listing: Listing, verdict: Verdict) -> None:
    print(f"  ✗ {headline(listing, verdict)}   [{listing.source}]  {listing.title[:60]}")
    print(f"      → {'; '.join(verdict.reasons)}")


class Notifier:
    def __init__(self, cfg: dict, http, enabled: bool = True):
        n = cfg["notify"]
        self.http, self.enabled = http, enabled
        self.ntfy_server = n["ntfy_server"].rstrip("/")
        self.ntfy_topic = str(n["ntfy_topic"]).strip()
        self.tg_token = str(n["telegram_bot_token"]).strip()
        self.tg_chat = str(n["telegram_chat_id"]).strip()

    @property
    def configured(self) -> bool:
        return bool(self.ntfy_topic or (self.tg_token and self.tg_chat))

    def listing(self, listing: Listing, verdict: Verdict, score: int | None = None) -> bool:
        body = [listing.title]
        body += [f"⚠ {f}" for f in verdict.flags]
        body += [f"· {n}" for n in verdict.notes]
        body.append(f"via {listing.source}")
        title = headline(listing, verdict) if score is None else f"{score}/100 · {headline(listing, verdict)}"
        return self.push(title, "\n".join(body), listing.url,
                         image=listing.image_url, warn=bool(verdict.flags))

    def page_changed(self, name: str, url: str, added: list[str]) -> bool:
        body = "\n".join(f"+ {line[:160]}" for line in added[:8])
        return self.push(f"New on {name}", body, url)

    def push(self, title: str, body: str, url: str, image: str | None = None, warn: bool = False) -> bool:
        if not self.enabled or not self.configured:
            return False
        sent = False
        if self.ntfy_topic:
            payload = {"topic": self.ntfy_topic, "title": title, "message": body, "click": url,
                       "tags": ["warning" if warn else "house"], "priority": 3 if warn else 4}
            if image and image.startswith("https://") and "%" not in image:
                payload["attach"] = image
            try:
                self.http.request(self.ntfy_server, method="POST", data=payload)
                sent = True
            except Exception as e:  # noqa: BLE001 - a failed push must not stop the run
                log.warning("ntfy push failed: %s", e)
        if self.tg_token and self.tg_chat:
            text = f"<b>{html.escape(title)}</b>\n{html.escape(body)}\n{html.escape(url)}"
            try:
                self.http.request(f"https://api.telegram.org/bot{self.tg_token}/sendMessage", method="POST",
                                  data={"chat_id": self.tg_chat, "text": text, "parse_mode": "HTML"})
                sent = True
            except Exception as e:  # noqa: BLE001
                log.warning("Telegram push failed: %s", e)
        return sent
