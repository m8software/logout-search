"""stage_place: code moved out of search_api.py. Byte-identical to what it was there; the names it reads come from
search_api (imported at the end of that file's definitions) and the stdlib."""
from __future__ import annotations

from stage_parts import part_run
import re
from search_api import (
    _conn,
    cached,
    home_point,
    norm_key,
    org_named,
    parts_for,
    place_exact,
    tables,
)


def place_build(q: str, card: dict | None, want: bool):
    """The place stage (parts stage "place"): a place searched by name, and what the card says of it. Returns
    (card, place, failed): failed names the exception when the step could not finish (then nothing is kept)."""
    place = None
    try:
        pl = cached(("place", q.lower(), home_point()), lambda: [place_exact(q)], static=True)[0] if want and len(q) >= 3 else None
        person = False
        if pl and card is not None:
            with _conn() as c:
                person = "wiki_facts" in tables(c) and c.execute(PERSON_SQL, {"t": card["title"]}).fetchone() is not None
        org = org_named(q) if (pl or card) else False
        if org and card is not None:
            # branches go by the organisation's full name too: "cvs" -> CVS Pharmacy
            oname = re.sub(r"\s*\(.*\)$", "", card["title"])
            alt = cached(("place", oname.lower(), home_point()), lambda: [place_exact(oname)], static=True)[0] if oname.lower() != q.lower() else None
            if alt and (not pl or (alt.get("miles") or 1e9) < (pl.get("miles") or 1e9)):
                pl = alt
        card, place = place_answer(q, card, pl, person, org)
    except Exception as e:  # noqa: BLE001
        return card, None, type(e).__name__
    return card, place, None

def place_stage(q: str, card: dict | None, want: bool, log):
    import parts as _parts
    if parts_for("place") is None:
        card2, place, failed = place_build(q, card, want)
    else:
        card2, place, failed = part_run("place", [q, _parts.canon(card).decode(), want], lambda: list(place_build(q, card, want)),
                                        keep=lambda v: not v[2])
    if failed:
        log("place failed: %s", failed)
    return card2, place

def place_in_card(card: dict | None, pl: dict | None) -> bool:
    """The card is about the place ("Parx Casino and Racing" / PARX Casino), not a namesake
    ("trump" is not a business called Trump)."""
    if not card or not pl:
        return False
    strip = lambda t: re.sub(r"^the\s+", "", t.strip(), flags=re.I)        # "The Home Depot"
    a, b = norm_key(strip(card["title"])), norm_key(strip(pl["name"]))
    return len(b) >= 3 and (a.startswith(b) or b.startswith(a))

PERSON_SQL = "SELECT 1 FROM wiki_facts WHERE title = %(t)s AND fact = 'born' LIMIT 1"

LONE_PLACE_MI = 10           # a one-word name with no card answers with a place this close

ORG_PLACE_MI = 25            # an organisation's lone location farther than this is not "its" place (SoFi, Delaware)

CARD_PLACE_MI = 30           # a card's chain branch farther than this is no help ("apple": a store 66 mi off)

# A practice named after a person ("james bond" got a doctor's office in
# Oklahoma): it joins a card only when the card is about an organisation.
PLACE_PRACTICE = re.compile(r"^(amenity=(doctors|dentist|clinic)|healthcare=|office=|craft=|shop=hairdresser"
                            r"|schema=(Physician|Dentist|Attorney|LegalService|InsuranceAgency|RealEstateAgent"
                            r"|AccountingService|MedicalBusiness|MedicalClinic|FinancialService))")

def place_answer(q: str, card: dict | None, pl: dict | None, person: bool,
                 org: bool = False) -> tuple[dict | None, dict | None]:
    """(card, place) for the page. A person's card never takes a place (a law office called
    "John Smith"); a card about the place takes it; with no card the place is its own card;
    a place named exactly by several words ("parx casino shippensburg") replaces a card
    about something else (the Bensalem casino's article)."""
    if not pl or person or (org and not pl.get("chain") and (pl.get("miles") or 0) > ORG_PLACE_MI):
        return card, None
    if card is not None and ((not org and PLACE_PRACTICE.match(pl.get("kind") or ""))
                             or (pl.get("chain") and (pl.get("miles") or 0) > CARD_PLACE_MI)):
        return card, None
    if card is None:
        # a place as the whole answer: named by several words ("parx casino shippensburg"),
        # or a chain, or close by ("wawa") — not a business called Sun 85 miles off for "sun"
        if len(q.split()) >= 2 or pl.get("chain") or (pl.get("miles") or 0) <= LONE_PLACE_MI:
            return None, pl
        return None, None
    if place_in_card(card, pl) or org:            # an organisation's card keeps its nearest branch
        card["place"] = pl
        if pl["own_site"] and not any(l["title"] == "Official site" for l in card["links"]):
            card["links"].insert(0, {"title": "Official site", "url": pl["own_site"]})
        return card, None
    if len(q.split()) >= 2:
        return None, pl
    return card, None
