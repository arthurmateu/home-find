from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields


@dataclass
class Listing:
    source: str
    id: str
    url: str
    title: str = ""
    warm_rent: float | None = None   # € incl. Nebenkosten + Heizung
    cold_rent: float | None = None
    size_sqm: float | None = None
    rooms: float | None = None
    zip_code: str | None = None
    district: str | None = None
    address: str | None = None
    photos: int | None = None        # None = unknown
    private: bool | None = None      # private landlord vs. company
    wbs_required: bool | None = None
    available_from: str | None = None
    published: str | None = None
    landlord: str | None = None
    image_url: str | None = None
    description: str = ""
    trusted: bool = False            # municipal / co-op landlord: skip scam heuristics
    signals: dict = field(default_factory=dict)  # source-specific facts rules.py understands

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Listing":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})
