"""Loads config.toml on top of built-in defaults."""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path

DEFAULTS: dict = {
    "search": {
        "max_warm_rent": 1000,
        "stretch_warm_rent": 0,  # over budget, shown under Rejected up to this warm rent; 0 = see near_miss_cold_rent
        "street_max_warm_rent": {},  # street -> its own budget, e.g. {"Kantstraße": 1200}
        "street_stretch_warm_rent": {},  # street -> shown under Rejected up to this, e.g. {"Kurfürstendamm": 1500}
        "near_miss_cold_rent": 1000,
        "min_size_sqm": 25,
        "min_rooms": 1,
        "wanted_rooms": 0,  # fewer rooms still match, with a heads-up note
        "have_wbs": False,
        "coop_member": False,
        "reject_furnished": True,
        "include_senior_housing": False,
        "drop_reasons": ["wg", "senior", "swap", "avoid", "wbs", "over_cap"],
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
        # Shown under Rejected, as "Edge of the area"; every other postcode is outside the area
        # (reason "avoid": never shown, while it's in drop_reasons).
        "edge_zip_codes": [],
        # Only used when a listing has no postcode: the portal's neighbourhood label.
        "names": ["Charlottenburg", "Westend", "Halensee", "Wilmersdorf"],
        "edge_names": [],
        "exclude_names": ["Charlottenburg-Nord"],
    },
    # Extra points in the score (rating.py) for where a flat is.
    "bonus": {
        "old_area": [],  # postcodes
        "streets": [],   # matched like street_max_warm_rent's
        "daily": [],     # [{name, lat, lon}]: places you go every day (in config.local.toml)
    },
    "run": {
        "request_delay_seconds": 2.0,
        "db_path": "data/homefind.db",
        "web_port": 8765,
        "highlight_sources": [],  # the web UI makes these sources' cards stand out
    },
    "notify": {
        "ntfy_server": "https://ntfy.sh",
        "ntfy_topic": "",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
    },
    # every_minutes: how often --loop checks each source. check_per_run: how many
    # older ads it also looks up per check, to find the ones taken offline.
    # sweep_every_hours: how often inberlinwohnen goes through all its pages, for the same reason.
    "sources": {
        # max_pages: room for growth (523 ads, 53 pages, in Nov 2026); a sweep that can't see them all finds nothing gone
        "inberlinwohnen": {"enabled": True, "every_minutes": 60, "max_pages": 100, "sweep_every_hours": 12},
        "immoscout": {"enabled": True, "every_minutes": 60, "max_pages": 3, "check_per_run": 5,
                      "geocodes": ["1276003001011", "1276003001076"]},  # Charlottenburg (+Westend), Wilmersdorf (+Halensee)
        "kleinanzeigen": {
            "enabled": True,
            "every_minutes": 60,
            "max_pages": 2,
            "check_per_run": 5,
            "locations": [{"slug": "charlottenburg", "id": 3332}, {"slug": "westend", "id": 25905},
                          {"slug": "wilmersdorf", "id": 3532}],
        },
        "wggesucht": {"enabled": True, "every_minutes": 60, "check_per_run": 2, "districts": [126, 192, 85083]},
        "immowelt": {
            "enabled": True,
            "every_minutes": 60,
            "check_per_run": 5,
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
    # Applying from the web UI (see apply.py). What goes in the form is in `profile`,
    # a file of its own so it stays off GitHub (it's in .gitignore).
    "apply": {
        "profile": "apply.toml",
        "min_score": 75,    # auto-apply: new matches scoring at least this
        "max_per_day": 10,  # auto-apply "send": at most this many sent a day
        "firefox": "",      # Firefox to open ads in (auto-apply); empty = Windows' one under WSL, else any
    },
    # The read-only family page (see casa.py); off until host, remote_dir and url are all set.
    "casa": {
        "host": "",           # ssh host to upload to
        "remote_dir": "",     # folder nginx serves there
        "url": "",            # where that folder is on the web
        "every_minutes": 15,  # how often it's rebuilt and uploaded while homefind runs
        "max_photos": 6,      # per flat
    },
}


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def local_path(path: str | Path) -> Path:
    """config.local.toml next to config.toml: settings that stay on this machine (it's in
    .gitignore), such as the places in [bonus] daily. What it sets goes over config.toml's."""
    path = Path(path).resolve()
    return path.with_name(f"{path.stem}.local{path.suffix}")


def load(path: str | Path) -> dict:
    path = Path(path)
    cfg = copy.deepcopy(DEFAULTS)
    for p in (path, local_path(path)):
        if p.exists():
            with p.open("rb") as f:
                _merge(cfg, tomllib.load(f))
    cfg["path"] = str(path.resolve())  # read once, at startup: the web UI says when it changes
    root = path.resolve().parent
    for section, k in (("run", "db_path"), ("apply", "profile")):
        p = Path(cfg[section][k]).expanduser()
        cfg[section][k] = str(p if p.is_absolute() else root / p)
    return cfg
