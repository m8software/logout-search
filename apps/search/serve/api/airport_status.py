"""Airport status questions: "delays at ewr", "is jfk closed", "newark airport delays".

The poller (news/wxall.py) keeps the FAA's whole-country airport status and the airport list in the news
store; this module decides whether a search asks about an airport, which one, and builds the card from rows
the engine read. Nothing here opens a file or a socket, so a search never causes a request.

What counts as the question (precision first: a missed card costs nothing, a wrong one hijacks a search):
  - every word of the search must be the place, a status word or a small filler word;
  - a status word is required ("delays", "delayed", "ground stop", "closed", "open", "status", "on time"),
    except "<place> airport" alone, which gets the card only while that airport has an active delay;
  - a bare 3-letter code needs no "airport"/"flight" word only for a large airport, and a code that is also
    an everyday word ("sea", "pit", "den") then needs a delay word, not just "open"/"status";
  - a town ("newark", "chicago") or a one-word name is read as an airport only beside "airport"/"flight".

A flight number ("ua 123", "united flight 123") gets its record from the government's monthly on-time file: the
usual times and how often it was on time last month. It is a record, not live status, and the card says so.
  - the whole search must be the airline, the number and at most "flight", "status", "on time", "delayed";
  - an airline's code ("ua 123", "dl88"), or its name with "flight", a status word, "airlines" or a number of three or
    more digits that is not a year, is surely a flight: its card leads;
  - an airline's name with a short number or a year ("delta 88" is also a car, "united 93" a film) may be one: the
    record is shown too, under whatever else the search found (owner 2026-11-22: "can we just show two cards");
  - no card unless that airline flew that number in the month on record.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import unicodedata

STALE_S = 30 * 60                 # older than this the delay lines are hidden and the card says how old it is
MAX_AIRPORTS = 3                  # "chicago airport delays": O'Hare and Midway
LINK = "https://nasstatus.faa.gov/"
DELAY_KINDS = ("ground_stop", "ground_delay", "departure", "arrival", "closure")      # "notice" is not a delay
KINDS = DELAY_KINDS + ("notice",)

_DELAY_WORDS = frozenset(("delay", "delays", "delayed"))
_WEAK_WORDS = frozenset(("closed", "closure", "closures", "open", "status"))
_AIR_WORDS = frozenset(("airport", "airports", "flight", "flights"))
_FILLER = frozenset("at in for the is are there any current currently today now right faa a of".split())
_GENERIC = frozenset("international intl airport regional municipal field".split())      # dropped from a name
# large airports whose code is an everyday word: "sea status" and "pit closed" are not about flying
WORDY_CODES = frozenset("den fat pie pit sat oak sea gum san las cos ind bur roc sav alb ric ont mem".split())
# names people type that are not the airport's listed name or town
NICKNAMES = {"ohare": "ORD", "o hare": "ORD", "laguardia": "LGA", "dulles": "IAD", "midway": "MDW", "hartsfield": "ATL",
             "sky harbor": "PHX", "seatac": "SEA", "sea tac": "SEA", "reagan national": "DCA", "love field": "DAL",
             "kennedy airport": "JFK", "logan airport": "BOS"}
# "southwest flight delays" is the airline, not Southwest Florida International
AIRLINES = frozenset(("american", "delta", "united", "southwest", "alaska", "hawaiian", "spirit", "frontier", "jetblue",
                      "allegiant", "breeze", "avelo", "sun country", "american airlines", "delta airlines", "united airlines",
                      "southwest airlines", "alaska airlines", "hawaiian airlines", "spirit airlines", "frontier airlines"))
_SIZE = {"large_airport": 0, "medium_airport": 1, "small_airport": 2}


def norm(text: str) -> str:
    """Lower case, no accents, apostrophes and periods closed up, everything else a space."""
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = re.sub(r"['’.]", "", s)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def maybe(q: str) -> bool:
    """A cheap first look, before any table is read: could this be an airport question at all?"""
    words = set(norm(q).split())
    return bool(words & (_DELAY_WORDS | _WEAK_WORDS | {"airport", "stop", "stops", "time"}))


class Index:
    """The airport list, by everything a search may call an airport."""

    def __init__(self, rows):
        self.by_code: dict[str, dict] = {}
        self.by_name: dict[str, dict] = {}
        self.places: dict[str, list] = {}                # a town, or the words a name starts with -> airports
        self.short: dict[str, list] = {}                 # the name without its town: "ohare", "dulles"
        tokens: dict[str, list] = {}
        for code, icao, name, city, state, kind, sched in rows:
            if not code or not name:
                continue
            a = {"code": code.upper(), "icao": (icao or "").upper(), "name": name, "city": (city or "").split(",")[0].strip(),
                 "state": state or "", "kind": kind or "", "sched": bool(sched)}
            self.by_code[a["code"].lower()] = a
            full = [w for w in norm(name).split() if w not in _GENERIC]
            ckey = norm(a["city"])
            a["_name_is_town"] = " ".join(full) == ckey
            if full:
                self.by_name.setdefault(" ".join(full), a)
            if ckey:
                self.places.setdefault(ckey, []).append(a)
            for n in range(1, len(full)):                # "dallas" finds Dallas Fort Worth as well as Dallas Love Field
                lead = " ".join(full[:n])
                if lead != ckey:
                    self.places.setdefault(lead, []).append(a)
            if ckey and full[:len(ckey.split())] == ckey.split() and len(full) > len(ckey.split()):
                self.short.setdefault(" ".join(full[len(ckey.split()):]), []).append(a)
            for w in set(full):
                tokens.setdefault(w, []).append(a)
        self.unique = {w: v[0] for w, v in tokens.items() if len(v) == 1 and len(w) >= 5}
        for v in self.places.values():
            v.sort(key=_rank)

    def __len__(self) -> int:
        return len(self.by_code)


def _rank(a: dict):
    return (_SIZE.get(a["kind"], 3), not a["sched"], a["code"])


def parse(q: str):
    """-> (place words, said airport/flight, delay word, bare) or None. bare: "<place> airport" with no status word."""
    words = norm(q).split()
    if not 2 <= len(words) <= 9:
        return None
    text = " " + " ".join(words) + " "
    strong = bool(set(words) & _DELAY_WORDS)
    for phrase in (" ground stop ", " ground stops ", " on time "):
        if phrase in text:
            text, strong = text.replace(phrase, " "), True
    words = text.split()
    weak = bool(set(words) & _WEAK_WORDS)
    air = bool(set(words) & _AIR_WORDS)
    # "kennedy airport" and "logan airport" are nicknames as written; the word "airport" still counts as said
    place = [w for w in words if w not in _DELAY_WORDS and w not in _WEAK_WORDS and w not in _AIR_WORDS and w not in _FILLER
             and w not in ("international", "intl")]
    if not place or len(place) > 5:
        return None
    if not strong and not weak:
        if words[-1] != "airport" or any(w in _FILLER or w in ("flight", "flights") for w in words):
            return None
        return place, True, False, True
    return place, air, strong, False


def resolve(index: Index, q: str):
    """-> ([airport, ...], bare) for an airport status question, else None."""
    got = parse(q)
    if got is None:
        return None
    place, air, strong, bare = got
    key = " ".join(place)
    if key in AIRLINES:
        return None
    a = index.by_code.get(key) if len(place) == 1 and len(key) == 3 else None
    if a is not None:
        if air:
            return [a], bare
        if a["kind"] != "large_airport" or (key in WORDY_CODES and not strong):
            return None
        return [a], bare
    nick = NICKNAMES.get(key) or (NICKNAMES.get(key + " airport") if air else None)
    if nick and nick.lower() in index.by_code:
        return [index.by_code[nick.lower()]], bare
    a = index.by_name.get(key)
    if a is not None and (air or (len(place) >= 2 and not a["_name_is_town"])):
        return [a], bare
    if not air:
        return None
    if key in index.places:                              # a town: its biggest airports, not every strip that shares the name
        best = index.places[key]
        return [x for x in best if _SIZE.get(x["kind"], 3) == _SIZE.get(best[0]["kind"], 3)][:MAX_AIRPORTS], bare
    if key in index.short and len(index.short[key]) == 1:
        return index.short[key][:1], bare
    if len(place) == 1 and key in index.unique:
        return [index.unique[key]], bare
    return None


def _minutes(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 100000 else None


def _item(raw) -> dict | None:
    try:
        it = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    if not isinstance(it, dict) or it.get("kind") not in KINDS:
        return None
    text = lambda k, n: it[k][:n] if isinstance(it.get(k), str) else ""
    return {"kind": it["kind"], "reason": text("reason", 80), "min": _minutes(it.get("min")), "max": _minutes(it.get("max")),
            "avg": _minutes(it.get("avg")), "trend": text("trend", 12), "until": text("until", 40),
            "start": text("start", 40), "reopen": text("reopen", 40), "text": text("text", 300)}


def card(airports: list, status_rows, feed, obs_rows, now: float, bare: bool = False) -> dict | None:
    """The card for these airports. status_rows: (code, item JSON); feed: (the FAA's update time, our fetch
    time) as Unix seconds, None when the status was never fetched; obs_rows: (station, reading)."""
    if not airports or not feed or feed[1] is None:
        return None
    updated, fetched = feed
    at = min(float(fetched), float(updated)) if updated is not None else float(fetched)
    age = max(0.0, now - at)
    stale = age > STALE_S
    items: dict[str, list] = {}
    for code, raw in status_rows or []:
        it = _item(raw)
        if it:
            items.setdefault(code, []).append(it)
    obs = {}
    for sid, o in obs_rows or []:
        if isinstance(o, str):
            try:
                o = json.loads(o)
            except ValueError:
                continue
        if isinstance(o, dict):
            obs[sid] = o
    out = []
    for a in airports[:MAX_AIRPORTS]:
        mine = [] if stale else sorted(items.get(a["code"], []), key=lambda it: KINDS.index(it["kind"]))
        o = obs.get(a["icao"])
        out.append({"code": a["code"], "name": a["name"], "city": a["city"], "state": a["state"], "items": mine,
                    "wx": {"temp_f": o.get("temp_f"), "text": o.get("text") or "", "wind_mph": o.get("wind_mph")} if o else None})
    if bare and not any(it["kind"] in DELAY_KINDS for a in out for it in a["items"]):
        return None
    return {"airports": out, "updated": dt.datetime.fromtimestamp(at, dt.timezone.utc).isoformat(timespec="seconds"),
            "age_min": int(age // 60), "stale": stale, "source": "faa", "link": LINK}


# ---------------------------------------------------------------------------------------------------------------
# A flight number's on-time record
# ---------------------------------------------------------------------------------------------------------------
ONTIME_LINK = "https://www.transtats.bts.gov/"
ONTIME_ROUTES = 3
ONTIME_MIN_DONE = 5               # fewer completed flights than this: no percentage
AIRLINE_NAMES = {"AA": "American Airlines", "DL": "Delta Air Lines", "UA": "United Airlines", "WN": "Southwest Airlines",
                 "AS": "Alaska Airlines", "B6": "JetBlue", "F9": "Frontier Airlines", "G4": "Allegiant Air", "NK": "Spirit Airlines",
                 "HA": "Hawaiian Airlines", "SY": "Sun Country Airlines"}
_AIRLINE_WORDS = {"american": "AA", "american airlines": "AA", "delta": "DL", "delta airlines": "DL", "delta air lines": "DL",
                  "united": "UA", "united airlines": "UA", "southwest": "WN", "southwest airlines": "WN", "alaska": "AS",
                  "alaska airlines": "AS", "jetblue": "B6", "jet blue": "B6", "jetblue airways": "B6", "frontier": "F9",
                  "frontier airlines": "F9", "allegiant": "G4", "allegiant air": "G4", "spirit": "NK", "spirit airlines": "NK",
                  "hawaiian": "HA", "hawaiian airlines": "HA", "sun country": "SY", "sun country airlines": "SY"}
_FLIGHT_FILLER = frozenset(("flight", "status", "delayed", "delay", "delays", "number", "today", "is"))
_CODE_NUM = re.compile(r"^([a-z][a-z0-9]|[0-9][a-z])([1-9][0-9]{0,3})$")


def flight_maybe(q: str) -> bool:
    """A cheap first look: a number in it, and not long."""
    return len(q) <= 60 and any(ch.isdigit() for ch in q)


def flight_query(q: str):
    """-> (airline code, flight number) when the search is or may be a flight number, else None."""
    got = flight_asked(q)
    return got[:2] if got else None


def flight_asked(q: str):
    """-> (airline code, flight number, sure) or None. sure: nothing else is meant ("ua 123", "delta flight 88");
    not sure: an airline's name and a short number or a year, which is often something else ("delta 88")."""
    text = " " + norm(q) + " "
    said = " on time " in text
    words = text.replace(" on time ", " ").split()
    said = said or bool(set(words) & _FLIGHT_FILLER)
    words = [w for w in words if w not in _FLIGHT_FILLER]
    if not 1 <= len(words) <= 4:
        return None
    m = _CODE_NUM.match(words[0]) if len(words) == 1 else None
    if m:                                               # "ua123"
        code, num = m.group(1).upper(), int(m.group(2))
        return (code, num, True) if code in AIRLINE_NAMES else None
    if len(words) < 2 or not words[-1].isdigit() or words[-1].startswith("0") or len(words[-1]) > 4:
        return None
    name, num = " ".join(words[:-1]), int(words[-1])
    if len(words) == 2 and name.upper() in AIRLINE_NAMES and len(name) == 2:      # "ua 123"
        return name.upper(), num, True
    code = _AIRLINE_WORDS.get(name)
    if code is None:
        return None
    sure = said or "air" in name.split()[-1] or (num >= 100 and not 1900 <= num <= 2100)
    return code, num, bool(sure)


def _clock(hhmm) -> str:
    """1740 -> "5:40 pm" (the airport's own time)."""
    if not isinstance(hhmm, int) or isinstance(hhmm, bool) or not 0 <= hhmm <= 2400 or hhmm % 100 >= 60:
        return ""
    h, m = (hhmm // 100) % 24, hhmm % 100
    return f"{h % 12 or 12}:{m:02d} {'am' if h < 12 else 'pm'}"


def flight_card(code: str, num: int, rows, feed, index: Index | None = None, sure: bool = True) -> dict | None:
    """The on-time record of one flight number. rows: (origin, dest, scheduled, completed, on time, cancelled, usual
    departure, usual arrival), most flown first; feed: (year, month) of the file they came from."""
    if not rows or not feed or not feed[0] or not feed[1] or code not in AIRLINE_NAMES:
        return None
    city = lambda c: (index.by_code.get(c.lower()) or {}).get("city", "") if index else ""
    routes = []
    for origin, dest, n, done, ontime, cancelled, dep, arr in sorted(rows, key=lambda r: (-r[2], r[6] or 0))[:ONTIME_ROUTES]:
        routes.append({"from": origin, "to": dest, "from_city": city(origin), "to_city": city(dest), "dep": _clock(dep), "arr": _clock(arr),
                       "flights": int(n), "ontime_pct": round(100 * ontime / done) if done >= ONTIME_MIN_DONE else None,
                       "cancelled": int(cancelled)})
    return {"carrier": code, "airline": AIRLINE_NAMES[code], "number": int(num), "routes": routes,
            "month": f"{int(feed[0]):04d}-{int(feed[1]):02d}", "source": "bts", "link": ONTIME_LINK,
            "lead": bool(sure)}                    # False: shown under the search's other card, not above it
