"""Polite HTTP: per-host delay with jitter, gzip, retries, bot-wall detection."""

from __future__ import annotations

import gzip
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0 Safari/537.36"
)


class Blocked(Exception):
    """The site answered with a bot wall or rate limit (401/403/429)."""


class Fetcher:
    def __init__(self, delay: float = 2.0, timeout: float = 25, retries: int = 2):
        self.delay, self.timeout, self.retries = delay, timeout, retries
        self._last: dict[str, float] = {}

    def _wait(self, host: str) -> None:
        gap = self.delay * random.uniform(0.8, 1.5)
        elapsed = time.monotonic() - self._last.get(host, 0.0)
        if elapsed < gap:
            time.sleep(gap - elapsed)
        self._last[host] = time.monotonic()

    def request(self, url, **kw) -> str:
        return self.fetch(url, **kw)[1]

    def fetch(self, url, *, method="GET", data=None, headers=None, ua=BROWSER_UA) -> tuple[str, str]:
        """(URL after redirects, body)."""
        host = urllib.parse.urlsplit(url).hostname or ""
        hdrs = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        }
        hdrs.update(headers or {})
        body = data if data is None or isinstance(data, bytes) else json.dumps(data).encode()
        if body is not None:
            hdrs.setdefault("Content-Type", "application/json")

        for attempt in range(self.retries + 1):
            self._wait(host)
            req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    encoding = resp.headers.get("Content-Encoding", "")
                    if encoding == "gzip":
                        raw = gzip.decompress(raw)
                    elif encoding == "deflate":
                        raw = zlib.decompress(raw)
                    return resp.url, raw.decode(resp.headers.get_content_charset() or "utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 429):
                    raise Blocked(f"{host} answered HTTP {e.code}") from None
                if e.code < 500 or attempt == self.retries:
                    raise
            except OSError:
                if attempt == self.retries:
                    raise
            time.sleep(3 * 2**attempt)
        raise AssertionError("unreachable")

    def get(self, url, **kw) -> str:
        return self.request(url, **kw)

    def json(self, url, **kw):
        return json.loads(self.request(url, **kw))
