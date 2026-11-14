"""Which US state a query names, and which state a government site belongs to.

"nj firearms id card" is a New Jersey question: New Jersey's government pages go first in the page list and
another state's government pages go last ("arizona unemployment benefits apply" was answered by
unemployment.dc.gov, "ca drivers license renewal" by Louisiana's, 2026-10-12).

A site's state comes from gov-states.json in the release folder: {"domains": {"nj.gov": "NJ", ...}}, the .gov
registry's state, county, city and district domains (CISA current-full.csv, public domain; federal, tribal and
interstate rows left out, they carry the state of their office and not of whom they serve), and from the
address itself for state.<xx>.us. No file: no site has a state, and nothing here changes a result.

A state in a query:
  - its name anywhere ("new jersey", "west virginia" before "virginia"), not when the next word makes it another
    place ("kansas city", "delaware county", "washington dc", "new york avenue"); two different states: none.
  - its postal code as the first or the last word. Never the ten codes that are everyday words
    (in or me ok hi oh id la co de: "sign in", "real id", "la liga"). The other codes that are also words or
    abbreviations (ms ga mt al ...) need two more words beside them and count as `weak`: the caller acts on a weak
    state only for pages that hold every other word of the query.
"""
from __future__ import annotations

import json
import re

_STATE_US = re.compile(r"(^|\.)state\.([a-z]{2})\.us$")
_NOT_A_STATE_BEFORE = frozenset({"city", "county", "dc", "d.c", "avenue", "ave", "street", "st", "road", "rd", "blvd"})
NEVER_CODES = frozenset({"in", "or", "me", "ok", "hi", "oh", "id", "la", "co", "de"})
WEAK_CODES = frozenset({"ma", "pa", "al", "ar", "mo", "ne", "ut", "wa", "mi", "mt", "ms", "ga", "pr"})
# Codes that are a word first and no evidence can make a state: "me" is the object of "near me", the
# commonest local query ("dmv near me" is not Maine); "la" is Los Angeles before it is Louisiana; in a government
# query "id" is the document ("id card renewal"); "in" opens "in state tuition" and "in person voting".
NO_EVIDENCE_CODES = frozenset({"la"})
# 2026-11-06 (owner: "do b"): Maine, Idaho and Indiana scored far under their names ("id drivers license renewal"
# went to Washington's page). "me", "id" and "in" now count like the other never codes (evidence that the rest is a
# state task), unless the word beside the code makes it the everyday word: "dmv near me", "real id", "id card
# renewal", "in state tuition", "sign in". "la" stays out: Los Angeles first. "in" first also needs three more words
# ("in jury duty" is English; "in drivers license renewal" is Indiana).
_WORD_SENSE_BEFORE = {
    "me": frozenset({"near", "for", "to", "help", "with", "call", "text", "email", "tell", "show", "give", "let", "about",
                     "around", "like", "of", "on", "beside", "behind", "from", "find", "remind", "send", "pay"}),
    "id": frozenset({"real", "photo", "state", "voter", "tax", "student", "employee", "national", "apple", "user", "an",
                     "my", "new", "lost", "replacement", "get", "valid", "caller", "login", "device", "product", "game",
                     "discord", "face", "touch", "tracking", "app", "account", "military", "veteran", "medicaid",
                     "insurance", "school", "work", "ein", "federal", "for", "with", "your"}),
    "in": frozenset({"sign", "log", "logged", "signed", "check", "checked", "plug", "plugged", "opt", "drop", "walk",
                     "move", "moved", "built", "fill", "cash", "zoom", "breaking", "all", "born", "made", "live", "living",
                     "work", "working", "stay", "staying", "who's", "whos", "what's", "whats", "is", "be", "get", "go",
                     "come", "turn", "give", "hand", "fit", "kick", "pitch", "chip", "factor", "phase", "lock", "dial"}),
}
_WORD_SENSE_AFTER = {
    "me": frozenset({"too", "up", "out", "now", "please"}),
    "id": frozenset({"card", "cards", "number", "numbers", "badge", "theft", "verification", "verify", "photo", "me",
                     "check", "scanner", "printer", "holder", "lookup", "please"}),
    "in": frozenset({"state", "person", "network", "store", "stock", "home", "the", "a", "an", "my", "your", "our", "touch",
                     "case", "time", "love", "depth", "general", "line", "progress", "office", "house", "n", "out", "app"}),
}
_DC = ("district of columbia", "washington dc", "washington d.c")
_SPLIT = re.compile(r"[^\w.']+")


def load(path: str) -> dict[str, str]:
    """domain -> state code from gov-states.json; {} when the file is not there."""
    try:
        with open(path, encoding="utf-8") as f:
            return {d.lower(): s.upper() for d, s in json.load(f)["domains"].items()}
    except FileNotFoundError:
        return {}


def state_token(code: str) -> str:
    """The word the release builder adds to a government page's bag of words so the pages index can be asked
    for one state's pages (release_v1.py web-input): "NJ" -> "govstatenj". Letters only, so every analyzer
    keeps it whole; the engine runs it through the index's own analyzer before asking, as the builder's did."""
    return "govstate" + code.lower()


def site_token(domain: str) -> str:
    """The word the release builder adds to a government page's bag of words for its site (release_v1.py web-input,
   ), so the pages index can be asked for one town's or county's pages: "govsite" + 12 letters of the
    domain's sha1. Letters only and 19 long: every analyzer keeps it whole (a domain spelled out could pass 40)."""
    import hashlib
    return "govsite" + "".join(chr(97 + int(c, 16)) for c in hashlib.sha1(domain.lower().encode()).hexdigest()[:12])


def host_domain(host: str, domains: dict[str, str]) -> str | None:
    """The registry's domain a host belongs to: dmv.ca.gov -> ca.gov, www.abseconnj.gov -> abseconnj.gov."""
    parts = (host or "").lower().rstrip(".").split(".")
    for i in range(len(parts) - 1):
        d = ".".join(parts[i:])
        if d in domains:
            return d
    return None


def load_local(path: str) -> dict[str, dict[str, list[str]]]:
    """gov-states.json's "local": {"city": {"NJ:absecon": [domains]}, "county": {"NJ:atlantic": [domains]}}; empty
    maps for a file without it (a release before) or no file."""
    try:
        with open(path, encoding="utf-8") as f:
            loc = json.load(f).get("local") or {}
    except FileNotFoundError:
        loc = {}
    return {"city": dict(loc.get("city") or {}), "county": dict(loc.get("county") or {})}


def local_key(state: str, name: str) -> str:
    """"NJ", "Twin Falls" -> "NJ:twinfalls": the key the local map and the engine's towns share."""
    return state.upper() + ":" + re.sub(r"[^a-z0-9]", "", name.lower())


def host_state(host: str, domains: dict[str, str]) -> str | None:
    """dmv.ca.gov -> CA (ca.gov is California's), www.dot.state.mn.us -> MN, usa.gov -> None."""
    host = (host or "").lower().rstrip(".")
    m = _STATE_US.search(host)
    if m:
        return m.group(2).upper()
    parts = host.split(".")
    for i in range(len(parts) - 1):
        s = domains.get(".".join(parts[i:]))
        if s:
            return s
    return None


def query_state(q: str, states: dict[str, str], evidence=None) -> tuple[str, str, bool] | None:
    """"nj firearms id card" -> ("NJ", "firearms id card", False); "renew car registration new jersey" ->
    ("NJ", "renew car registration", False); "ms word download" -> ("MS", "word download", True);
    "kansas city royals", "sign in", "real id", "new york to new jersey" -> None.
    `states` is search_api.US_STATES (name or lower-case code -> code).

    A code that is also a word (NEVER_CODES, WEAK_CODES) is judged by what the rest of the query is, when an
    `evidence(state, rest) -> bool` is given: the rest is something residents ask of their state ("co jury
    duty", "pay traffic ticket al") or it is not ("ms word download", "oh my god", "al gore"). Evidence makes a weak
    code a full state and lets a never code count at all, with at least two other words; a code joined by a hyphen
    to the next word is a prefix ("co-parenting", "de-escalation"), and NO_EVIDENCE_CODES never count. Without
    `evidence` the codes behave as before."""
    words = [w.strip(".'") for w in _SPLIT.split(q.lower()) if w.strip(".'")]
    if len(words) < 2:
        return None
    names = sorted((n.split() for n in list(states) + list(_DC) if len(n) > 2), key=len, reverse=True)
    found, taken = set(), set()
    for name in names:
        n = len(name)
        for i in range(len(words) - n + 1):
            if words[i:i + n] != name or taken & set(range(i, i + n)):
                continue
            taken |= set(range(i, i + n))                  # "west virginia" keeps "virginia" from matching again
            if i + n < len(words) and words[i + n] in _NOT_A_STATE_BEFORE:
                continue
            found.add((states.get(" ".join(name)) or "DC", tuple(range(i, i + n))))
    if len({s for s, _ in found}) > 1:
        return None
    if found:
        state = next(iter(found))[0]
        gone = {i for _s, idx in found for i in idx}
        rest = [w for i, w in enumerate(words) if i not in gone]
        return (state, " ".join(rest), False) if rest else None
    if taken:                                              # "kansas city ks": a place was named, not asked about
        return None
    ends = [i for i in (0, len(words) - 1) if len(words[i]) == 2 and words[i] in states and words[i] not in NEVER_CODES]
    if len(ends) == 1:
        code = words[ends[0]]
        rest = " ".join(w for i, w in enumerate(words) if i != ends[0])
        if code not in WEAK_CODES:
            return states[code], rest, False
        if evidence is not None and not _hyphened(q, code) and evidence(states[code], rest):
            return states[code], rest, False
        return (states[code], rest, True) if len(words) >= 3 else None
    if ends or evidence is None or len(words) < 3:
        return None
    ends = [i for i in (0, len(words) - 1) if words[i] in NEVER_CODES and words[i] not in NO_EVIDENCE_CODES and words[i] in states]
    if len(ends) != 1 or _hyphened(q, words[ends[0]]):
        return None
    code = words[ends[0]]
    if _word_sense(code, words, ends[0]):
        return None
    rest = " ".join(w for i, w in enumerate(words) if i != ends[0])
    return (states[code], rest, False) if evidence(states[code], rest) else None


def _word_sense(code: str, words: list[str], i: int) -> bool:
    """The code at words[i] reads as the everyday word by its neighbour ("near me", "real id", "id card", "in state",
    "sign in"), or "in" opens a query of fewer than four words."""
    if code not in _WORD_SENSE_BEFORE:
        return False
    if code == "in" and i == 0 and len(words) < 4:
        return True
    rest = " ".join(w for j, w in enumerate(words) if j != i)
    if code == "id" and re.search(r"\b(real id|id card|photo id|state id|voter id)\b", rest):
        return True
    if i == 0:
        return len(words) > 1 and words[1] in _WORD_SENSE_AFTER[code]
    return words[i - 1] in _WORD_SENSE_BEFORE[code]


def _hyphened(q: str, code: str) -> bool:
    """The code is a prefix joined to a word by a hyphen ("co-parenting classes", "mi-fi hotspot")."""
    return bool(re.search(rf"(^|[^\w]){re.escape(code)}-\w", q.lower()))


def first_and_last(state: str, rows: list, state_of) -> list:
    """rows with another state's government sites moved to the end, order otherwise kept.
    state_of(row) -> the row's state code or None."""
    kept, later = [], []
    for r in rows:
        s = state_of(r)
        (later if s and s != state else kept).append(r)
    return kept + later
