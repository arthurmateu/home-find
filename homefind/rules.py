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
from .util import days_since, street_of, warm_from_text


@dataclass
class Verdict:
    ok: bool = True
    reasons: list[str] = field(default_factory=list)  # why it was rejected
    codes: list[str] = field(default_factory=list)    # machine-readable reasons, see REASONS
    flags: list[str] = field(default_factory=list)    # scam warnings (scored)
    notes: list[str] = field(default_factory=list)    # neutral info worth knowing
    score: int = 0
    warm: float | None = None

    def reject(self, reason: str, code: str) -> None:
        self.reasons.append(reason)
        if code not in self.codes:
            self.codes.append(code)

    def flag(self, points: int, message: str) -> None:
        self.score += points
        self.flags.append(message)

    def to_dict(self) -> dict:
        return asdict(self)


# Reason code -> label, in the order the web UI lists them.
REASONS = {
    "near_miss": "Near miss (cold rent in range)",
    "stretch": "On a street worth stretching for",
    "budget": "Over budget",
    "edge": "Edge of the area",
    "over_cap": "More than the search covers",
    "no_rent": "No rent stated",
    "size": "Too small",
    "rooms": "Too few rooms",
    "swap": "Swap offer",
    "wanted": "Wanted ad",
    "not_flat": "Not a flat",
    "wg": "WG room",
    "temporary": "Temporary / sublet",
    "furnished": "Furnished",
    "wbs": "WBS required",
    "members": "Co-op members only",
    "senior": "Senior housing",
    "scam": "Looks like a scam",
    "photos": "No photos",
    "district": "Title names another district",
    "area": "Location unclear",
    "avoid": "Outside the area",
}


def dropped(verdict: Verdict, cfg: dict) -> bool:
    """Rejected for one of search.drop_reasons: stored, so it isn't loaded again, but never shown."""
    return not set(cfg["search"]["drop_reasons"]).isdisjoint(verdict.codes)


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I)


SWAP_TITLE = _rx(r"tausch|swap")
SWAP_TEXT = _rx(r"tauschangebot|tauschwohnung|wohnungstausch|tauschpartner|nur (im |gegen |zum )?tausch|zum tausch\b"
               r"|im tausch gegen|(wohnung|zimmer\w*) (zu |zum )?tauschen|tausche (meine|unsere)"
               r"|\bswap|(flat|apartment|home|house) ?exchange|exchange (my|our) (flat|apartment)")
WANTED_TITLE = _rx(r"^\W*(ich |wir )?(suche|suchen|gesucht|gesuch)\b")
# A single room offered in someone's flat, even when not called a WG: "Zimmer zu vermieten",
# "Helles Zimmer in ...", "vermiete ein Schlafzimmer", "Küche und Bad werden geteilt".
# "1-Zimmer-Wohnung", "Ein Zimmer Wohnung", "Zimmer, Küche, Bad" are flats and don't match.
_NOT_FLAT_AFTER = r"(?![- ]?(wohnung|whg|apartment|appartement|studio))"
WG_TITLE = _rx(
    r"\bwg\b|wg-?zimmer|mitbewohner|flat ?share|shared (flat|apartment)|\broom in (a|an|my|our)\b"
    r"|^\W*zimmer\b(?!\W*(wohnung|whg|apartment|appartement|studio|küche|kü\b))"
    rf"|\b\w+(es|ens) zimmer\b{_NOT_FLAT_AFTER}|studentenzimmer"
    r"|\b(single|private|furnished) room\b|\broom (for rent|to rent|available)\b"
)
WG_TEXT = _rx(
    r"wg-?zimmer|\b(in|für) (eine|unsere|meine)[nr]? \w{0,6} ?wg\b|mitbewohner(in)? gesucht|flat ?share"
    r"|\b(die|unsere|meine|der) wg\b(?!-)"
    r"|\b(küche|bad\w*|flur)\b[\w ,]{0,40} (werden|wird) (gemeinsam )?(geteilt|mitbenutzt|gemeinsam genutzt)"
    r"|küchenmitbenutzung|badmitbenutzung|shared (kitchen|bathroom)"
    rf"|\b(biete|vermiete)n? (hier )?(ein|mein|unser|das) (\w+ )?(schlaf)?zimmer\b{_NOT_FLAT_AFTER}"
)
TEMP_TITLE = _rx(
    r"zwischenmiete|untermiete|untervermiet|(?<!un)(?<!nicht )befristet|auf zeit\b|temporär|temporary|sublet"
    r"|short[- ]?term|kurzzeit|\bfür \d+ (monate|wochen)|\bbis (zum |ende )?\d{1,2}\.\d{1,2}\."
    r"|ferienwohnung|monteur|serviced|business[- ]?apartment|co-?living"
)
_DATE = r"\d{1,2}\.\d{1,2}\.(\d{4}|\d{2})?"
TEMP_TEXT = _rx(
    r"zwischenmiete|(?<!un)befristete[rn]? (mietvertrag|vermietung|mietverhältnis)"
    r"|(?<!un)(?<!nicht )befristet (bis|auf|für)"
    r"|\bfür (\d+|zwei|drei|vier|fünf|sechs|sieben|acht|neun|zehn|elf|zwölf) (monate|wochen)\b"
    r"|\btemporary\b|short[- ]?term|co-?living|mietdauer:? (max\.?|maximal|höchstens)"
    r"|(?<!not )time[- ]limited|fixed[- ]term|\blimited to \d+ (months|years)"
    # "Verfügbar vom 1.10.26 - 1.11.26", "Untermiete vom 01.11.2026 bis 27.06.2027"
    r"|(verfügbar|frei|zeitraum|miete|available)\W{1,3}(vom |von |ab |from )?" + _DATE
    + r" ?(-|–|bis|to|until) ?" + _DATE +
    r"|business[- ]?(apartment|suite)|serviced apartment|boardinghouse|€ ?/ ?nacht|pro nacht|per night"
)
# Subletting, in the description; see mentions_sublet.
SUBLET = _rx(r"\bunter(ver)?miet\w*|\bunterzuvermieten\b|\bsub-?(let|leas)\w*")
# Next to it, the landlord's terms: "keine Untervermietung", "Untervermietung nicht gestattet / nur mit Zustimmung".
SUBLET_TERMS = _rx(r"\b(nicht|kein\w*|no|not)\b|ausgeschlossen|untersagt|verboten|unzulässig|gestattet|erlaubt"
                   r"|möglich|zustimmung|genehmigung|absprache|allowed|permitted|prohibited")
FURNISHED = _rx(
    r"(?<!un)(?<!teil)(?<!nicht )(?<!nicht voll )möbliert|(?<!un)furnished|all[- ]?inclusive"
    r"|(voll|komplett|hochwertig|stilvoll|modern|elegant) eingerichtete[n]? (apartment|wohnung|studio)"
    r"|(elegante|stilvolle|hochwertige|komplette|moderne|gemütliche) (einrichtung|möblierung)"
    r"|furniture (will be |is |are )?(provided|included)"
)
PART_FURNISHED = _rx(r"teil(weise )?möbliert|part(ly|ially)[- ]furnished")
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
# Borough names that span wanted/avoided and other neighbourhoods: too broad to count.
BROAD_BOROUGHS = ["charlottenburg-wilmersdorf", "friedrichshain-kreuzberg", "steglitz-zehlendorf",
                  "tempelhof-schöneberg"]
# Every neighbourhood (Ortsteil) in Berlin, a few as portals also spell them. A listing without a
# postcode that names one of these, and none in the area or on its edge, is elsewhere in Berlin.
BERLIN_PLACES = [
    "Mitte", "Moabit", "Hansaviertel", "Tiergarten", "Wedding", "Gesundbrunnen", "Friedrichshain", "Kreuzberg",
    "Prenzlauer Berg", "Pankow", "Weißensee", "Weissensee", "Blankenburg", "Heinersdorf", "Karow", "Buch",
    "Französisch Buchholz", "Blankenfelde", "Niederschönhausen", "Rosenthal", "Wilhelmsruh",
    "Charlottenburg", "Charlottenburg-Nord", "Wilmersdorf", "Schmargendorf", "Grunewald", "Westend", "Halensee",
    "Spandau", "Haselhorst", "Siemensstadt", "Wilhelmstadt", "Staaken", "Gatow", "Kladow", "Hakenfelde",
    "Falkenhagener Feld", "Steglitz", "Lichterfelde", "Lankwitz", "Zehlendorf", "Dahlem", "Nikolassee", "Wannsee",
    "Schöneberg", "Friedenau", "Tempelhof", "Mariendorf", "Marienfelde", "Lichtenrade",
    "Neukölln", "Neukoelln", "Britz", "Buckow", "Gropiusstadt", "Rudow",
    "Treptow", "Plänterwald", "Baumschulenweg", "Johannisthal", "Oberschöneweide", "Niederschöneweide",
    "Schöneweide", "Adlershof", "Altglienicke", "Bohnsdorf", "Köpenick", "Friedrichshagen", "Rahnsdorf", "Grünau",
    "Müggelheim", "Schmöckwitz", "Marzahn", "Biesdorf", "Kaulsdorf", "Mahlsdorf", "Hellersdorf",
    "Lichtenberg", "Friedrichsfelde", "Karlshorst", "Rummelsburg", "Fennpfuhl", "Hohenschönhausen", "Falkenberg",
    "Malchow", "Wartenberg", "Reinickendorf", "Tegel", "Wittenau", "Märkisches Viertel", "Lübars", "Waidmannslust",
    "Hermsdorf", "Frohnau", "Heiligensee", "Konradshöhe", "Borsigwalde",
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
        before, after = s[max(0, m.start() - 30): m.start()].lower(), s[m.end(): m.end() + 30].lower()
        # "ohne WBS", "keine WBS-Wohnung", "WBS nicht erforderlich"; not "WBS erforderlich. Keine Haustiere"
        if (re.search(r"\b(ohne|kein\w*)\b[^.!?\n]*$", before)
                or re.search(r"^[^.!?\n]*nicht (erforderlich|notwendig|nötig|benötigt)", after)):
            continue
        ctx = before + s[m.start(): m.end()].lower() + after
        if re.search(r"erforderlich|notwendig|nötig|benötigt|vorausgesetzt|zwingend|pflicht|nur mit|mit (einem )?wbs"
                     r"|wbs[- ]?\d{2,3}|wbs bis (zu )?\d|wbs-?berechtig|wbs-?wohnung", ctx):
            return True
    return False


def mentions_sublet(s: str) -> bool:
    """The flat is offered as a sublet ("möchte meine Wohnung untervermieten", "zur
    Untermiete"), not just the landlord's terms ("Untervermietung nicht gestattet")."""
    for m in SUBLET.finditer(s):
        before = re.split(r"[.!?\n,;:]", s[max(0, m.start() - 40): m.start()])[-1]
        after = re.split(r"[.!?\n,;:]", s[m.end(): m.end() + 40])[0]
        if not SUBLET_TERMS.search(f"{before} {after}"):
            return True
    return False


def mentions_tenant_fee(s: str) -> bool:
    for m in re.finditer(r"provision|courtage|maklergebühr", s, re.I):
        ctx = s[max(0, m.start() - 30): m.end() + 20].lower()
        if not re.search(r"frei|kein|ohne|nicht|vermieter (zahlt|trägt)|bestellerprinzip", ctx):
            return True
    return False


def _place_in_title(title: str, places: list[str]) -> str | None:
    """The first of `places` named as a location in the title: "Berlin-Spandau",
    "in Spandau", or leading the title; not "ab Mitte Oktober" or "nahe Grunewald"."""
    t = title.lower()
    return next((p for p in places if re.search(rf"(berlin[- ]|\bin |^\W*){re.escape(p.lower())}\b", t)), None)


def _area_names(cfg: dict) -> list[str]:
    """Neighbourhoods in the area or on its edge."""
    return [*cfg["area"]["names"], *cfg["area"].get("edge_names", [])]


def _other_district_in_title(title: str, cfg: dict) -> str | None:
    """A Berlin district outside the area and its edge named in the title, unless
    the title also names one of those ("Wilmersdorf/Schöneberg border" is fine)."""
    t = title.lower()
    for excluded in cfg["area"].get("exclude_names", []):
        if re.search(rf"\b{re.escape(excluded.lower())}\b", t):
            return excluded
    if _named(t, _area_names(cfg)):
        return None
    wanted = {n.lower() for n in _area_names(cfg)}
    return _place_in_title(title, [d for d in BERLIN_DISTRICTS if d.lower() not in wanted])


def _label(listing: Listing, remove: list[str]) -> str:
    """The portal's neighbourhood label and address, minus names too broad to count."""
    label = f"{listing.district or ''} {listing.address or ''}".lower()
    for name in [*BROAD_BOROUGHS, *(n.lower() for n in remove)]:
        label = label.replace(name, "")
    return label


def _named(text: str, names: list[str]) -> str | None:
    """The first of `names` in `text` (lower case), as a word of its own."""
    return next((n for n in names if re.search(rf"\b{re.escape(n.lower())}\b", text)), None)


def placement(listing: Listing, cfg: dict) -> tuple[str, str | None]:
    """Where the listing is: "in" the area, on its "edge", "out"side both, or
    "unknown"; and the postcode or place that says so. By postcode; without one,
    by the portal's neighbourhood label, else a Berlin place named in the title."""
    area = cfg["area"]
    z = listing.zip_code
    if z:
        return ("in" if z in area["zip_codes"] else "edge" if z in area.get("edge_zip_codes", []) else "out"), z
    label = _label(listing, area.get("exclude_names", []))
    for kind, names in (("in", area["names"]), ("edge", area.get("edge_names", []))):
        name = _named(label, names)
        if name:
            return kind, name
    known = {n.lower() for n in _area_names(cfg)}
    elsewhere = [p for p in BERLIN_PLACES if p.lower() not in known]
    name = _named(_label(listing, []), elsewhere) or _place_in_title(listing.title or "", elsewhere)
    return ("out", name) if name else ("unknown", None)


STREET_ALIASES = {"kudamm": "kurfuerstendamm"}


def street_key(s: str) -> str:
    """Spelling-proof street name: "Kantstr." / "Kantstrasse" -> "kantstrasse", "Ku’damm" -> "kurfuerstendamm"."""
    s = s.lower().replace("ß", "ss").replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    s = re.sub(r"['‘’`´]", "", s)
    s = re.sub(r"str(asse\b|\.|\b)", "strasse", s)
    s = " ".join(re.sub(r"[-\s]+", " ", s).split())
    return re.sub(r"\b\w+\b", lambda m: STREET_ALIASES.get(m.group(0), m.group(0)), s)


def on_street(listing: Listing, streets) -> str | None:
    """Which of `streets` the listing is on. The street is taken from the address;
    without one, from the title, when it says the flat is there ("Wohnung am
    Ku'damm", not "nahe Kudamm"). "Neue Kantstraße" is not Kantstraße."""
    street = street_of(listing.address)
    where = street_key(street) if street else street_key(listing.title or "")
    for name in streets:
        n = re.escape(street_key(name))
        if re.search(rf"^{n}\b" if street else rf"\b(am|an der|in der|in|auf dem|on|at) {n}\b", where):
            return name
    return None


def street_budget(listing: Listing, cfg: dict) -> tuple[float, str | None]:
    """The warm-rent budget for the listing, and the street it's on if that's one
    of `street_max_warm_rent`."""
    s = cfg["search"]
    budgets = s.get("street_max_warm_rent") or {}
    street = on_street(listing, budgets)
    return (budgets[street], street) if street else (s["max_warm_rent"], None)


def street_stretch(listing: Listing, cfg: dict) -> tuple[float, str | None]:
    """On a street of `street_stretch_warm_rent`: the warm rent up to which a flat
    there still shows under Rejected, and the street. (0, None) elsewhere."""
    stretch = cfg["search"].get("street_stretch_warm_rent") or {}
    street = on_street(listing, stretch)
    return (stretch[street], street) if street else (0, None)


def evaluate(listing: Listing, cfg: dict, final: bool = True) -> Verdict:
    """`final=False` is the cheap pre-check on search-result data, before
    fetching the detail page; unknown fields pass. A listing it rejects never
    gets its detail page loaded, so if the search showed a rent (ImmoScout the
    cold, WG-Gesucht the warm), that rent is then judged as it would be on the
    detail page: a furnished flat at 997 € cold is over budget too (warm
    estimated from the cold rent). Kleinanzeigen's listed price can be either,
    so it isn't."""
    v = _evaluate(listing, cfg, final)
    if (not final and not v.ok and "over_cap" not in v.codes
            and (listing.warm_rent is not None or listing.cold_rent is not None)):
        v = _evaluate(listing, cfg, final=True)
    return v


def _evaluate(listing: Listing, cfg: dict, final: bool) -> Verdict:
    s, sig = cfg["search"], listing.signals
    v = Verdict()
    title, desc = listing.title or "", listing.description or ""
    blob = f"{title}\n{desc}"

    kind, place = placement(listing, cfg)
    where = f"{place} {listing.district or ''}".strip() if listing.zip_code else place
    if kind == "out":
        v.reject(f"outside the area ({where})", "avoid")
    elif kind == "edge" and final:  # fine, just not a match: loaded in full, to judge under Rejected
        v.reject(f"on the edge of the area ({where})", "edge")
    elif kind == "unknown":
        v.reject(f"location unclear ({listing.district or 'none given'})", "area")

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
    # Over budget, flats are still fetched and fully loaded, so they can be browsed
    # under "Rejected": up to stretch_warm_rent warm, or a street's own stretch
    # (street_stretch_warm_rent); pricier ones are dropped ("over_cap"). Without
    # stretch_warm_rent, near misses (cold rent within near_miss_cold_rent) are kept
    # whatever their warm rent.
    cap, street = street_budget(listing, cfg)
    if street:
        v.notes.append(f"on {street}: budget {cap:g} € warm")
    stretch, stretch_street = street_stretch(listing, cfg)
    near = s.get("near_miss_cold_rent") or 0
    shown = s.get("stretch_warm_rent") or 0
    ceiling = max(cap, shown, stretch) if shown else None  # the most a flat here can cost and still show
    # The sources search up to the highest of all these, for every street at once
    # (Source.fetch_cap); what that brings in over this listing's own limit isn't
    # loaded ("over_cap", in drop_reasons). Without a ceiling, a warm rent gets the
    # leeway WG-Gesucht's search has (1.3x), for near misses.
    limit = max(cap, near, stretch, shown)
    cold = listing.cold_rent
    listed = cold if cold is not None else sig.get("list_price")
    if not final and (warm is not None and warm > (ceiling or limit * 1.3) or warm is None and (listed or 0) > limit):
        v.reject(f"{warm or listed:.0f} € is more than the search covers here ({ceiling or limit:g} €)", "over_cap")
    elif warm is not None and warm > cap:
        if final:
            if warm <= stretch:
                v.reject(f"{warm:.0f} € warm > {cap} €, but on {stretch_street} (≤ {stretch:g} €)", "stretch")
            elif ceiling and warm > ceiling:
                v.reject(f"{warm:.0f} € warm > {ceiling:g} €, the most a flat here can cost", "over_cap")
            elif near and cold is not None and cold <= near:
                v.reject(f"{warm:.0f} € warm > {cap} € (cold {cold:.0f} €)", "near_miss")
            else:
                v.reject(f"{warm:.0f} € warm > {cap} €", "budget")
    elif warm is None and final:
        v.reject("no rent stated", "no_rent")

    if listing.size_sqm and listing.size_sqm < s["min_size_sqm"]:
        v.reject(f"{listing.size_sqm:g} m² < {s['min_size_sqm']} m²", "size")
    if listing.rooms and listing.rooms < s["min_rooms"]:
        v.reject(f"{listing.rooms:g} rooms < {s['min_rooms']}", "rooms")
    elif listing.rooms and listing.rooms < s.get("wanted_rooms", 0):
        v.notes.append(f"heads-up: only {listing.rooms:g} room{'s' * (listing.rooms != 1)}")

    # --- not a normal long-term rental
    if sig.get("swap_only") or SWAP_TITLE.search(title) or SWAP_TEXT.search(desc):
        v.reject("swap offer (Wohnungstausch)", "swap")
    if NOT_A_FLAT.search(title):
        v.reject("not a flat (cellar / parking / commercial)", "not_flat")
    elsewhere = _other_district_in_title(title, cfg)
    if elsewhere:
        v.reject(f"title says it's in {elsewhere}", "district")
    if WANTED_TITLE.search(title):
        v.reject("wanted ad, not an offer", "wanted")
    if WG_TITLE.search(title) or WG_TEXT.search(desc):
        v.reject("room in a shared flat (WG)", "wg")
    if sig.get("temporary") or TEMP_TITLE.search(title) or TEMP_TEXT.search(desc):
        v.reject("temporary / sublet", "temporary")
    elif mentions_sublet(desc):
        v.reject("sublet: the lease would be with the tenant, not the landlord", "temporary")
    if SENIOR.search(blob) and not s["include_senior_housing"]:
        v.reject("senior housing (age-restricted)", "senior")
    if sig.get("furnished") or FURNISHED.search(blob):
        if s["reject_furnished"]:
            v.reject("furnished (in Berlin nearly always short-term and overpriced)", "furnished")
        else:
            v.notes.append("furnished")
    elif sig.get("part_furnished") or PART_FURNISHED.search(blob):
        v.notes.append("partly furnished")

    # Either counts: inberlinwohnen's field says "nicht erforderlich" on flats titled "WBS 220 erforderlich".
    if listing.wbs_required or mentions_wbs_required(blob):
        if s["have_wbs"]:
            v.notes.append("WBS required")
        else:
            v.reject("WBS required", "wbs")
    if sig.get("members_only"):
        if s["coop_member"]:
            v.notes.append("co-op members only")
        else:
            v.reject("co-op members only", "members")

    # --- sketchiness
    if not listing.trusted:
        _scam_checks(listing, v, s, final)
        if v.score >= s["scam_threshold"]:
            v.reject("looks like a scam: " + "; ".join(v.flags), "scam")

    # --- neutral notes
    if NACHMIETER.search(blob):
        v.notes.append("Nachmieter ad: the landlord still has to accept you")
    if ABLOESE.search(blob) or sig.get("abloese"):
        amount = f" of {sig['abloese']:.0f} €" if sig.get("abloese") else ""
        v.notes.append(f"asks for Ablöse{amount} (paying the old tenant for furniture/kitchen)")
    if sig.get("is24_plus_until"):
        v.notes.append(f"ImmoScout Plus members only until {sig['is24_plus_until']}")
    if sig.get("verified_landlord"):
        v.notes.append("identity-verified landlord")
    if listing.private:
        v.notes.append("private landlord")

    # A near miss, or a stretch, is a flat that would match if it were a bit cheaper.
    for code in ("near_miss", "stretch"):
        if code in v.codes and len(v.codes) > 1:
            v.codes[v.codes.index(code)] = "budget"

    v.ok = not v.reasons
    return v


def _scam_checks(listing: Listing, v: Verdict, s: dict, final: bool) -> None:
    sig = listing.signals
    blob = f"{listing.title}\n{listing.description}"

    if final and listing.photos is not None:
        if listing.photos == 0:
            v.reject("no photos", "photos")
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
