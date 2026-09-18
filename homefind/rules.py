"""Decides whether a listing is a match, and if not, why.

Hard rules reject outright (price, area, swaps, sublets, WG rooms, ...). Scam
heuristics add points: at `scam_threshold` the listing is rejected, below it the
match is still sent, with the warnings attached, so you can judge for yourself.
Municipal and co-op listings (`trusted`) skip the scam heuristics.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .models import Listing
from .util import days_since, warm_from_text


@dataclass
class Verdict:
    ok: bool = True
    reasons: list[str] = field(default_factory=list)  # why it was rejected
    flags: list[str] = field(default_factory=list)    # scam warnings (scored)
    notes: list[str] = field(default_factory=list)    # neutral info worth knowing
    score: int = 0
    warm: float | None = None

    def reject(self, reason: str) -> None:
        self.reasons.append(reason)

    def flag(self, points: int, message: str) -> None:
        self.score += points
        self.flags.append(message)

    def to_dict(self) -> dict:
        return asdict(self)


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I)


SWAP_TITLE = _rx(r"tausch|swap")
SWAP_TEXT = _rx(r"tauschangebot|tauschwohnung|wohnungstausch|nur (im |gegen |zum )?tausch|zum tausch\b|im tausch gegen")
WANTED_TITLE = _rx(r"^\W*(ich |wir )?(suche|suchen|gesucht|gesuch)\b")
WG_TITLE = _rx(r"\bwg\b|wg-?zimmer|mitbewohner|flat ?share|shared (flat|apartment)|\broom in (a|an|my|our)\b")
WG_TEXT = _rx(r"wg-?zimmer|\b(in|für) (eine|unsere|meine)[nr]? \w{0,6} ?wg\b|mitbewohner(in)? gesucht|flat ?share")
TEMP_TITLE = _rx(
    r"zwischenmiete|untermiete|untervermiet|(?<!un)(?<!nicht )befristet|auf zeit\b|temporär|temporary|sublet"
    r"|short[- ]?term|kurzzeit|\bfür \d+ (monate|wochen)|\bbis (zum |ende )?\d{1,2}\.\d{1,2}\."
    r"|ferienwohnung|monteur|serviced|business[- ]?apartment|co-?living"
)
TEMP_TEXT = _rx(
    r"zwischenmiete|zur untermiete|(?<!un)befristete[rn]? (mietvertrag|vermietung|mietverhältnis)"
    r"|(?<!un)(?<!nicht )befristet (bis|auf|für)"
    r"|\bfür (\d+|zwei|drei|vier|fünf|sechs|sieben|acht|neun|zehn|elf|zwölf) (monate|wochen)\b"
    r"|\btemporary\b|\bsublet|short[- ]?term|co-?living|mietdauer:? (max\.?|maximal|höchstens)"
    r"|business[- ]?(apartment|suite)|serviced apartment|boardinghouse|€ ?/ ?nacht|pro nacht|per night"
)
FURNISHED = _rx(
    r"(?<!un)(?<!teil)(?<!nicht )(?<!nicht voll )möbliert|(?<!un)furnished|all[- ]?inclusive"
    r"|(voll|komplett|hochwertig|stilvoll|modern|elegant) eingerichtete[n]? (apartment|wohnung|studio)"
    r"|(elegante|stilvolle|hochwertige|komplette|moderne|gemütliche) (einrichtung|möblierung)"
)
# Title is about something other than a flat ("Großer Keller zu vermieten", "Stellplatz ...").
NOT_A_FLAT = _rx(r"^\W*(\w+ ){0,2}(keller(raum)?|lager(raum|fläche)?|abstellraum|stellplatz|tiefgarage\w*|garage"
                 r"|parkplatz|büro(raum|fläche)?|gewerbe\w*|praxis\w*|ladenfläche)\b")
BERLIN_DISTRICTS = [
    "Mitte", "Moabit", "Wedding", "Gesundbrunnen", "Tiergarten", "Kreuzberg", "Friedrichshain", "Neukölln",
    "Pankow", "Prenzlauer Berg", "Weißensee", "Lichtenberg", "Hohenschönhausen", "Marzahn", "Hellersdorf",
    "Treptow", "Köpenick", "Adlershof", "Spandau", "Siemensstadt", "Reinickendorf", "Tegel", "Steglitz",
    "Zehlendorf", "Lichterfelde", "Lankwitz", "Dahlem", "Tempelhof", "Schöneberg", "Friedenau",
    "Mariendorf", "Britz", "Rudow", "Buckow", "Grunewald", "Schmargendorf",
]
SENIOR = _rx(r"\b(wohnen )?ab (50|55|60|65|70) jahren?\b|senioren(wohnung|wohnen|wohnanlage|residenz)|betreutes wohnen"
             r"|\bfür senior(en|innen)\b")
NACHMIETER = _rx(r"nachmieter")
ABLOESE = _rx(r"ablöse|abstandszahlung|\babstand\b|\d{3,5} ?(€|euro) für (die |das |den )?\(?"
              r"(möbel|küche|einbauküche|couch|coach|sofa|bett|einrichtung)")
EMAIL = _rx(r"[\w.+-]+@[\w-]+\.[a-z]{2,}")

SCAM_PATTERNS = [
    (3, "asks for money up front", _rx(
        r"western union|moneygram|paysafe|bitcoin|krypto|crypto"
        r"|kaution (vorab|im voraus|vor (der )?besichtigung|vor (der )?schlüsselübergabe)"
        r"|deposit (first|before|in advance|up ?front)|vorab (überweisen|zahlen)|anzahlung")),
    (3, "keys by post / no viewing", _rx(
        r"schlüssel\w* (per|mit der|via) (post|dhl|kurier)|keys? (by|via|through) (post|mail|courier|dhl)"
        r"|keine besichtigung|no viewing|without (a )?viewing|besichtigung (ist )?(leider )?nicht möglich")),
    (3, "charges a viewing/reservation fee", _rx(
        r"besichtigungsgebühr|reservierungsgebühr|bearbeitungsgebühr|viewing fee|reservation fee")),
    (2, "landlord says they are abroad", _rx(
        r"(bin|lebe|wohne|arbeite|befinde mich) (\w+ )?(derzeit |zur ?zeit |momentan |gerade )?im ausland"
        r"|\babroad\b|\bout of (the )?country\b|missionar|missionary")),
    (2, "mentions Airbnb/Booking (common payment scam)", _rx(r"airbnb|booking\.com")),
    (1, "wants to move to WhatsApp/Telegram", _rx(r"whatsapp|telegram")),
]

WBS_RX = _rx(r"\bwbs\b|wohnberechtigungsschein")


def mentions_wbs_required(s: str) -> bool:
    for m in WBS_RX.finditer(s):
        ctx = s[max(0, m.start() - 30): m.end() + 30].lower()
        if re.search(r"ohne|kein|nicht (erforderlich|notwendig|nötig|benötigt)", ctx):
            continue
        if re.search(r"erforderlich|notwendig|nötig|benötigt|vorausgesetzt|zwingend|pflicht|nur mit|mit wbs"
                     r"|wbs[- ]?\d{2,3}|wbs-?berechtig", ctx):
            return True
    return False


def mentions_tenant_fee(s: str) -> bool:
    for m in re.finditer(r"provision|courtage|maklergebühr", s, re.I):
        ctx = s[max(0, m.start() - 30): m.end() + 20].lower()
        if not re.search(r"frei|kein|ohne|nicht|vermieter (zahlt|trägt)|bestellerprinzip", ctx):
            return True
    return False


def _other_district_in_title(title: str, cfg: dict) -> str | None:
    """A Berlin district outside the search area named in the title, unless the
    title also names the area ("Wilmersdorf/Schöneberg border" is fine)."""
    t = title.lower()
    for excluded in cfg["area"].get("exclude_names", []):
        if re.search(rf"\b{re.escape(excluded.lower())}\b", t):
            return excluded
    if any(re.search(rf"\b{re.escape(n.lower())}\b", t) for n in cfg["area"]["names"]):
        return None
    wanted = {n.lower() for n in cfg["area"]["names"]}
    for d in BERLIN_DISTRICTS:
        if d.lower() not in wanted and re.search(rf"\b{re.escape(d.lower())}\b", t):
            return d
    return None


def in_area(listing: Listing, cfg: dict) -> bool:
    area = cfg["area"]
    if listing.zip_code:
        return listing.zip_code in area["zip_codes"]
    # No postcode: fall back to the portal's neighbourhood label. The borough
    # name "Charlottenburg-Wilmersdorf" is too broad to count.
    label = f"{listing.district or ''} {listing.address or ''}".lower()
    for excluded in ["charlottenburg-wilmersdorf", *(n.lower() for n in area.get("exclude_names", []))]:
        label = label.replace(excluded, "")
    return any(re.search(rf"\b{re.escape(n.lower())}\b", label) for n in area["names"])


def evaluate(listing: Listing, cfg: dict, final: bool = True) -> Verdict:
    """`final=False` is the cheap pre-check on search-result data, before
    fetching the detail page; unknown fields pass."""
    s, sig = cfg["search"], listing.signals
    v = Verdict()
    title, desc = listing.title or "", listing.description or ""
    blob = f"{title}\n{desc}"

    if not in_area(listing, cfg):
        v.reject(f"outside area ({listing.zip_code or listing.district or 'no location'})")

    # --- rent
    warm = listing.warm_rent
    if warm is None and final:
        stated = warm_from_text(desc)
        if stated and stated >= (listing.cold_rent or 0):
            warm = stated
            v.notes.append("warm rent taken from the description")
    if warm is None and final and listing.cold_rent:
        warm = round(listing.cold_rent + (listing.size_sqm or 50) * s["extra_costs_per_sqm"])
        v.notes.append(f"warm rent estimated from cold rent (~{warm:.0f} €)")
    v.warm = warm
    cap = s["max_warm_rent"]
    if warm is not None:
        if warm > cap:
            v.reject(f"{warm:.0f} € warm > {cap} €")
    elif sig.get("list_price") and sig["list_price"] > cap:
        v.reject(f"listed at {sig['list_price']:.0f} € > {cap} €")
    elif final:
        v.reject("no rent stated")

    if listing.size_sqm and listing.size_sqm < s["min_size_sqm"]:
        v.reject(f"{listing.size_sqm:g} m² < {s['min_size_sqm']} m²")
    if listing.rooms and listing.rooms < s["min_rooms"]:
        v.reject(f"{listing.rooms:g} rooms < {s['min_rooms']}")

    # --- not a normal long-term rental
    if sig.get("swap_only") or SWAP_TITLE.search(title) or SWAP_TEXT.search(desc):
        v.reject("swap offer (Wohnungstausch)")
    if NOT_A_FLAT.search(title):
        v.reject("not a flat (cellar / parking / commercial)")
    elsewhere = _other_district_in_title(title, cfg)
    if elsewhere:
        v.reject(f"title says it's in {elsewhere}")
    if WANTED_TITLE.search(title):
        v.reject("wanted ad, not an offer")
    if WG_TITLE.search(title) or WG_TEXT.search(desc):
        v.reject("room in a shared flat (WG)")
    if sig.get("temporary") or TEMP_TITLE.search(title) or TEMP_TEXT.search(desc):
        v.reject("temporary / sublet")
    if SENIOR.search(blob) and not s["include_senior_housing"]:
        v.reject("senior housing (age-restricted)")
    if sig.get("furnished") or FURNISHED.search(blob):
        if s["reject_furnished"]:
            v.reject("furnished (in Berlin nearly always short-term and overpriced)")
        else:
            v.notes.append("furnished")

    wbs = listing.wbs_required if listing.wbs_required is not None else mentions_wbs_required(blob)
    if wbs:
        if s["have_wbs"]:
            v.notes.append("WBS required")
        else:
            v.reject("WBS required")
    if sig.get("members_only"):
        if s["coop_member"]:
            v.notes.append("co-op members only")
        else:
            v.reject("co-op members only")

    # --- sketchiness
    if not listing.trusted:
        _scam_checks(listing, v, s, final)
        if v.score >= s["scam_threshold"]:
            v.reject("looks like a scam: " + "; ".join(v.flags))

    # --- neutral notes
    if NACHMIETER.search(blob):
        v.notes.append("Nachmieter ad: the landlord still has to accept you")
    if ABLOESE.search(blob):
        v.notes.append("asks for Ablöse (paying the old tenant for furniture/kitchen)")
    if sig.get("is24_plus_until"):
        v.notes.append(f"ImmoScout Plus members only until {sig['is24_plus_until']}")
    if sig.get("verified_landlord"):
        v.notes.append("identity-verified landlord")
    if listing.private:
        v.notes.append("private landlord")

    v.ok = not v.reasons
    return v


def _scam_checks(listing: Listing, v: Verdict, s: dict, final: bool) -> None:
    sig = listing.signals
    blob = f"{listing.title}\n{listing.description}"

    if final and listing.photos is not None:
        if listing.photos == 0:
            v.reject("no photos")
        elif listing.photos == 1:
            v.flag(1, "only one photo")
    for points, message, pattern in SCAM_PATTERNS:
        if pattern.search(blob):
            v.flag(points, message)
    if listing.private and EMAIL.search(listing.description or ""):
        v.flag(1, "e-mail address in the text (wants to move off-platform)")
    if mentions_tenant_fee(blob) or sig.get("broker_fee"):
        v.flag(1, "mentions a broker fee (tenants rarely owe one since 2015)")
    if v.warm and listing.size_sqm:
        per_sqm = v.warm / listing.size_sqm
        if per_sqm < s["suspicious_below_eur_sqm"]:
            v.flag(2, f"unusually cheap: {per_sqm:.1f} €/m² warm")
    if listing.rooms and listing.size_sqm and listing.size_sqm / listing.rooms < 8:
        v.flag(2, f"implausible: {listing.rooms:g} rooms in {listing.size_sqm:g} m²")
    if sig.get("negotiable"):
        v.flag(1, "rent marked as negotiable (VB)")
    if listing.private:
        age = days_since(sig.get("account_since"))
        if age is not None and age < 30:
            v.flag(2, f"seller account is {age} days old")
        age = days_since(sig.get("created"))
        if age is not None and age > 180:
            v.flag(1, f"online for {age} days")
        city = (sig.get("provider_city") or "").strip()
        if city and city.lower() not in ("berlin", "potsdam"):
            v.flag(1, f"private landlord based in {city}")
