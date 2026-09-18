"""Loads config.toml on top of built-in defaults."""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path

DEFAULTS: dict = {
    "search": {
        "max_warm_rent": 1000,
        "near_miss_cold_rent": 1000,
        "min_size_sqm": 25,
        "min_rooms": 1,
        "have_wbs": False,
        "coop_member": False,
        "reject_furnished": True,
        "include_senior_housing": False,
        "drop_reasons": ["wg", "senior"],
        "extra_costs_per_sqm": 2.5,
        "suspicious_below_eur_sqm": 10.0,
        "scam_threshold": 3,
    },
    "area": {
        # Postcodes per Wikipedia's Ortsteil infoboxes. Postcodes don't follow
        # district borders exactly (e.g. 14197, 10777 are partly Friedenau / Schöneberg).
        "zip_codes": [
            "10585", "10587", "10589", "10623", "10625", "10627", "10629",  # Charlottenburg
            "14050", "14052", "14053", "14055", "14057", "14059",           # Westend (+ west Charlottenburg)
            "10709", "10711",                                                # Halensee
            "10707", "10713", "10715", "10717", "10719", "10777", "14197",  # Wilmersdorf
        ],
        # Only used when a listing has no postcode: the portal's neighbourhood label.
        "names": ["Charlottenburg", "Westend", "Halensee", "Wilmersdorf"],
        "exclude_names": ["Charlottenburg-Nord"],
    },
    "run": {
        "request_delay_seconds": 2.0,
        "db_path": "data/homefind.db",
        "web_port": 8765,
    },
    "notify": {
        "ntfy_server": "https://ntfy.sh",
        "ntfy_topic": "",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
    },
    # every_minutes: how often --loop checks each source.
    "sources": {
        "inberlinwohnen": {"enabled": True, "every_minutes": 60, "max_pages": 45},
        "immoscout": {"enabled": True, "every_minutes": 60, "max_pages": 3,
                      "geocodes": ["1276003001011", "1276003001076"]},  # Charlottenburg (+Westend), Wilmersdorf (+Halensee)
        "kleinanzeigen": {
            "enabled": True,
            "every_minutes": 60,
            "max_pages": 2,
            "locations": [{"slug": "charlottenburg", "id": 3332}, {"slug": "westend", "id": 25905},
                          {"slug": "wilmersdorf", "id": 3532}],
        },
        "wggesucht": {"enabled": True, "every_minutes": 60, "districts": [126, 192, 85083]},
        "immowelt": {
            "enabled": True,
            "every_minutes": 60,
            "urls": [
                f"https://www.immowelt.de/suche/mieten/wohnung/preis--900/zimmer-1/berlin-10115/{n}"
                for n in ("charlottenburg-13627/nbh2de91302007", "westend-14055/nbh2de91302127",
                          "halensee-10709/nbh2de91302034", "wilmersdorf-14197/nbh2de91302130")
            ],
        },
        "charlotte1907": {"enabled": True, "every_minutes": 60},
        "watch": {"every_minutes": 60},
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
    p = Path(cfg["run"]["db_path"]).expanduser()
    cfg["run"]["db_path"] = str(p if p.is_absolute() else root / p)
    return cfg
