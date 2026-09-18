"""Loads config.toml on top of built-in defaults."""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path

DEFAULTS: dict = {
    "search": {
        "max_warm_rent": 1000,
        "min_size_sqm": 25,
        "min_rooms": 1,
        "have_wbs": False,
        "coop_member": False,
        "reject_furnished": True,
        "include_senior_housing": False,
        "extra_costs_per_sqm": 2.5,
        "suspicious_below_eur_sqm": 8.0,
        "scam_threshold": 3,
    },
    "area": {
        "zip_codes": [
            "10585", "10587", "10589", "10623", "10625", "10627", "10629",  # Charlottenburg
            "14057", "14059",                                                # around Lietzensee / Klausenerplatz
            "14050", "14052", "14053", "14055",                              # Westend
            "13627",                                                         # Charlottenburg-Nord
        ],
        "names": ["Charlottenburg", "Westend", "Charlottenburg-Nord"],
    },
    "run": {
        "interval_minutes": 10,
        "request_delay_seconds": 2.0,
        "db_path": "data/homefind.db",
        "report_path": "out/report.html",
    },
    "notify": {
        "ntfy_server": "https://ntfy.sh",
        "ntfy_topic": "",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
    },
    "sources": {
        "inberlinwohnen": {"enabled": True, "max_pages": 45},
        "immoscout": {"enabled": True, "geocodes": ["1276003001011"], "max_pages": 3},
        "kleinanzeigen": {
            "enabled": True,
            "max_pages": 2,
            "locations": [{"slug": "charlottenburg", "id": 3332}, {"slug": "westend", "id": 25905}],
        },
        "wggesucht": {"enabled": True, "districts": [126]},
        "immowelt": {
            "enabled": True,
            "urls": [
                "https://www.immowelt.de/suche/mieten/wohnung/preis--900/zimmer-1/berlin-10115/charlottenburg-13627/nbh2de91302007",
                "https://www.immowelt.de/suche/mieten/wohnung/berlin-10115/charlottenburg-13627/nbh2de91302007",
            ],
        },
        "charlotte1907": {"enabled": True},
    },
    "watch": [],
}


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load(path: str | Path) -> dict:
    path = Path(path)
    cfg = copy.deepcopy(DEFAULTS)
    if path.exists():
        with path.open("rb") as f:
            _merge(cfg, tomllib.load(f))
    root = path.resolve().parent
    for key in ("db_path", "report_path"):
        p = Path(cfg["run"][key]).expanduser()
        cfg["run"][key] = str(p if p.is_absolute() else root / p)
    return cfg
