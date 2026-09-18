"""Small parsing helpers shared by the sources."""

from __future__ import annotations

import html
import re
from datetime import date


def num(value) -> float | None:
    """Parse '1.102,69 €', '59 m²', '2,5', 1243.1 into a float."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"\d[\d.,]*", str(value))
    if not m:
        return None
    s = m.group(0).rstrip(".,")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


_DROP = re.compile(r"<(script|style|svg|noscript|template)\b.*?</\1>", re.S | re.I)
_BLOCK_END = re.compile(r"<br\s*/?>|</(p|div|li|tr|h\d|td|th|dd|dt|section|article|ul|table)>", re.I)


def text(fragment: str) -> str:
    """HTML -> plain text, one line per block element."""
    s = re.sub(r"\s+", " ", _DROP.sub(" ", fragment))
    s = _BLOCK_END.sub("\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    lines = (re.sub(r"[ \t\r\f\v\xa0]+", " ", line).strip() for line in s.split("\n"))
    return "\n".join(line for line in lines if line)


def berlin_zip(s: str | None) -> str | None:
    m = re.search(r"\b(1[0-4]\d{3})\b", s or "")
    return m.group(1) if m else None


def to_date(value: str | None) -> date | None:
    """'2026-09-18T16:21:08Z' or '18.09.2026' -> date."""
    if not value:
        return None
    value = value.strip()
    try:
        if re.match(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value[:10])
        m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", value)
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        pass
    return None


def days_since(value: str | None) -> int | None:
    d = to_date(value)
    return (date.today() - d).days if d else None


_WARM_RX = [
    re.compile(r"warm(?:miete)?\s*[:=]?\s*(?:ca\.?|circa)?\s*(\d[\d.,]*)\s*(?:€|eur|euro)", re.I),
    re.compile(r"(\d[\d.,]*)\s*(?:€|eur|euro)?\s*(?:warm\b|inkl\.?\s*(?:aller\s*)?(?:neben|betriebs)kosten|all[- ]?in\b)", re.I),
]


def warm_from_text(s: str | None) -> float | None:
    """Find a warm rent stated in free text ('ca. 540 € warm', 'Warmmiete: 850 €')."""
    for rx in _WARM_RX:
        for m in rx.finditer(s or ""):
            v = num(m.group(1))
            if v and 150 <= v <= 5000:
                return v
    return None
