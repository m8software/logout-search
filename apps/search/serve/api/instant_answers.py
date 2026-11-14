"""Instant answers computed on the box: arithmetic, unit conversion, the time somewhere, money.

    instant(q, now=None, rates=None) -> {"answer", "title", "detail", "question"} or None

The dict is the answer box /instant already carries for Wikidata facts (search_api.answer): "answer" is the big
line, "title" and "detail" the small one. No "url": there is nothing to link to. question is "calc", "unit",
"time" or "currency".

Pure local code, standard library only: nothing is fetched, nothing leaves the box. The expression parser is a
small recursive-descent parser over a fixed token set; nothing is ever eval()ed. Unit factors are the exact legal
definitions (international yard and pound 1959, US customary gallon). Time zones come from the IANA database
through zoneinfo; a place is a zone's own city name ("tokyo" from Asia/Tokyo) or one of the aliases below.

It answers only what it is sure is a calculation: "9/11", "24/7", "555-1234", "1986-87" and "2024-10-06" are searches,
not sums; "87-9" is a subtraction (owner 2026-11-06). To divide 9 by 11, ask for it: "9 / 11" (spaces), "9/11 =", "=9/11", "what is 9/11", "calc 9/11",
"9 divided by 11", "9 over 11" or "9÷11".

Currency ("100 usd to eur", owner decision 2026-10-06) needs rates, and this module fetches nothing: the caller
passes them in as rates = {"date": "2026-10-06", "checked": "2026-10-06T15:12:00Z", "per_eur": {"USD": 1.08, ...}}
— the ECB's daily euro reference rates, which the news poller (apps/search/news/fx.py) keeps in the news file,
and when it last fetched them successfully. No rates, rates more than FX_MAX_AGE_DAYS old, or a last check more
than FX_MAX_UNCHECKED_DAYS ago (the poller is broken) is no answer. The answer always names the ECB and says the
rate's date and how long ago it was checked.
"""
from __future__ import annotations

import datetime as dt
import math
import re

# ---------------------------------------------------------------------------------------------- numbers


def _sci(v: float, sig: int) -> str:
    m, e = f"{v:.{sig - 1}e}".split("e")
    return f"{m.rstrip('0').rstrip('.')} × 10^{int(e)}"


def fmt(v: float | int, sig: int = 10) -> str:
    """A number as a person writes it: thousands separators, no float noise, no exponent for ordinary sizes."""
    if isinstance(v, bool):
        raise TypeError("not a number")
    if isinstance(v, int):
        return f"{v:,}" if abs(v) < 10 ** 21 else _sci(float(v), sig)
    if math.isnan(v) or math.isinf(v):
        raise OverflowError("not a finite number")
    if v == int(v) and abs(v) < 1e15:
        return f"{int(v):,}"
    if abs(v) >= 1e15 or abs(v) < 1e-6:
        return _sci(v, sig)
    s = f"{v:,.{max(0, sig - 1 - int(math.floor(math.log10(abs(v)))))}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


# ---------------------------------------------------------------------------------------------- calculator

_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+"
_TOKEN = re.compile(r"\s*(?:(?P<num>%s)|(?P<op>\*\*|[-+*/^()%%×÷−–])|(?P<word>[a-z]+))" % _NUM)
_WORD_OPS = {"plus": "+", "minus": "-", "times": "*", "x": "*", "of": "of", "mod": "mod", "over": "/"}
_FUNCS = {"sqrt": math.sqrt, "abs": abs}
_CONSTS = {"pi": math.pi}
_PREFIX = re.compile(r"^(?:what\s+is|what's|whats|how\s+much\s+is|calculate|calc|compute|solve)\s+")
# a bare date, ratio, range or phone number: "9/11", "24/7", "50/50", "2024-10-06", "555-1234", "1-800-555-1234"
_NOT_A_SUM = re.compile(r"^\d+(?:[-/]\d+)+$")
_MINUS = re.compile(r"^(\d+)-(\d+)$")


def _looks_like_a_sum(t: str) -> bool:
    """One number minus another is a sum ("87-9", owner 2026-11-06), unless it reads as a phone number ("555-1234"),
    a year range ("1986-87", "2024-2025") or starts with 0 ("07-04"). Dates, ratios and longer runs stay searches."""
    m = _MINUS.match(t)
    if not m:
        return False
    a, b = m.groups()
    if a.startswith("0") or b.startswith("0") and len(b) > 1:
        return False
    if len(a) == 3 and len(b) == 4:
        return False
    if len(a) == 4 and 1000 <= int(a) <= 2199 and len(b) in (2, 4):
        return False
    return True
MAX_BITS = 4096                    # an integer power larger than this is given as a float, or refused


class _NoAnswer(Exception):
    pass


def _tokens(s: str) -> list[tuple[str, object]]:
    out, i = [], 0
    while i < len(s):
        m = _TOKEN.match(s, i)
        if not m:
            if s[i:].strip() == "":
                break
            raise _NoAnswer
        i = m.end()
        if m.group("num"):
            t = m.group("num").replace(",", "")
            out.append(("num", float(t) if "." in t else int(t)))
        elif m.group("op"):
            out.append(("op", {"×": "*", "÷": "/", "−": "-", "–": "-", "**": "^"}.get(m.group("op"), m.group("op"))))
        else:
            w = m.group("word")
            if w in _WORD_OPS:
                out.append(("op", _WORD_OPS[w]))
            elif w in _FUNCS or w in _CONSTS:
                out.append(("name", w))
            else:
                raise _NoAnswer                    # any other word: this is a search, not a sum
    # "divided by" is two words; "x" is "times" only between two operands
    return out


class _Parser:
    """expr := term (('+'|'-') term)*      term := unary (('*'|'/'|'mod'|'of') unary)*
    unary := ('-'|'+') unary | power       power := postfix ('^' unary)?
    postfix := primary '%'?                primary := number | '(' expr ')' | func '(' expr ')' | const"""

    def __init__(self, toks):
        self.t, self.i, self.ops = toks, 0, 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self, kind=None, val=None):
        k, v = self.peek()
        if k is None or (kind and k != kind) or (val is not None and v != val):
            raise _NoAnswer
        self.i += 1
        return v

    def expr(self):
        v = self.term()
        while self.peek() in (("op", "+"), ("op", "-")):
            op = self.take()
            r = self.term()
            v, self.ops = (v + r if op == "+" else v - r), self.ops + 1
        return v

    def term(self):
        v = self.unary()
        while True:
            k, op = self.peek()
            if k != "op" or op not in ("*", "/", "mod", "of", "%"):
                return v
            if op == "%":                          # "10 % 3" is modulo only when an operand follows
                nk, nv = self.t[self.i + 1] if self.i + 1 < len(self.t) else (None, None)
                if not (nk in ("num", "name") or (nk == "op" and nv == "(")):
                    return v
                op = "mod"
            self.take()
            r = self.unary()
            self.ops += 1
            if op in ("*", "of"):
                v = v * r
            elif op == "/":                        # r == 0 raises ZeroDivisionError: calculate() answers None
                v = v // r if isinstance(v, int) and isinstance(r, int) and v % r == 0 else v / r
            else:
                v = v % r

    def unary(self):
        if self.peek() in (("op", "-"), ("op", "+")):
            return -self.unary() if self.take() == "-" else self.unary()
        return self.power()

    def power(self):
        v = self.postfix()
        if self.peek() == ("op", "^"):
            self.take()
            e = self.unary()
            self.ops += 1
            if isinstance(v, int) and isinstance(e, int) and e >= 0:
                if v not in (0, 1, -1) and e * max(1, abs(v).bit_length()) > MAX_BITS:
                    raise _NoAnswer
                return v ** e
            try:
                r = float(v) ** float(e)
            except (OverflowError, ZeroDivisionError):
                raise _NoAnswer from None
            if isinstance(r, complex):
                raise _NoAnswer
            return r
        return v

    def postfix(self):
        v = self.primary()
        if self.peek() == ("op", "%"):
            nk, nv = self.t[self.i + 1] if self.i + 1 < len(self.t) else (None, None)
            if not (nk in ("num", "name") or (nk == "op" and nv == "(")):      # a percent sign, not modulo
                self.take()
                self.ops += 1
                return v / 100
        return v

    def primary(self):
        k, v = self.peek()
        if k == "num":
            return self.take()
        if k == "op" and v == "(":
            self.take()
            r = self.expr()
            self.take("op", ")")
            return r
        if k == "name":
            self.take()
            if v in _CONSTS:
                self.ops += 1
                return _CONSTS[v]
            self.take("op", "(")
            r = self.expr()
            self.take("op", ")")
            self.ops += 1
            try:
                return _FUNCS[v](r)
            except ValueError:
                raise _NoAnswer from None
        raise _NoAnswer


def calculate(q: str) -> dict | None:
    s = q.strip().lower().rstrip("?").strip()
    # an "=" before or after, or "what is" / "calc": the person asked for a sum, so "9/11 =" is a division
    asked = bool(_PREFIX.match(s)) or s.startswith("=") or s.endswith("=")
    s = _PREFIX.sub("", s).strip("=").strip().replace("divided by", "/").replace("to the power of", "^")
    if not s or not any(c.isdigit() for c in s) or (not asked and _NOT_A_SUM.match(s.replace(" ", "")) and " " not in s and not _looks_like_a_sum(s)):
        return None
    if re.search(r"\Sx|x\S", s):                  # "2x4" is lumber and "4x4" a truck: x multiplies only between spaces
        return None
    try:
        p = _Parser(_tokens(s))
        v = p.expr()
        if p.i != len(p.t) or p.ops == 0:          # trailing tokens, or a bare number: not a sum
            return None
        if isinstance(v, float) and round(v) != 0 and abs(v - round(v)) < 1e-9 * abs(v) and abs(v) < 1e15:
            v = int(round(v))                      # float noise next to a whole number (2.9999999999 is 3); never to 0
        ans = fmt(v)
    except (_NoAnswer, OverflowError, RecursionError, ZeroDivisionError):
        return None
    shown = re.sub(r"\s+", " ", s.replace("**", "^").replace("*", " × ").replace("/", " ÷ ")).strip()
    return {"answer": ans, "title": shown.replace(" x ", " × "), "detail": "", "question": "calc"}


# ---------------------------------------------------------------------------------------------- units

# dimension -> {canonical unit: (factor to the base unit, singular, plural)}; factors are exact definitions
_U = {
    "length": {"mm": (0.001, "mm", "mm"), "cm": (0.01, "cm", "cm"), "m": (1.0, "m", "m"), "km": (1000.0, "km", "km"),
               "in": (0.0254, "inch", "inches"), "ft": (0.3048, "foot", "feet"), "yd": (0.9144, "yard", "yards"),
               "mi": (1609.344, "mile", "miles"), "nmi": (1852.0, "nautical mile", "nautical miles")},
    "mass": {"mg": (1e-6, "mg", "mg"), "g": (0.001, "g", "g"), "kg": (1.0, "kg", "kg"), "tonne": (1000.0, "tonne", "tonnes"),
             "oz": (0.028349523125, "oz", "oz"), "lb": (0.45359237, "lb", "lb"), "st": (6.35029318, "stone", "stone"),
             "ton": (907.18474, "US ton", "US tons")},
    "volume": {"ml": (0.001, "mL", "mL"), "l": (1.0, "L", "L"), "tsp": (0.00492892159375, "US teaspoon", "US teaspoons"),
               "tbsp": (0.01478676478125, "US tablespoon", "US tablespoons"), "floz": (0.0295735295625, "US fl oz", "US fl oz"),
               "cup": (0.2365882365, "US cup", "US cups"), "pt": (0.473176473, "US pint", "US pints"),
               "qt": (0.946352946, "US quart", "US quarts"), "gal": (3.785411784, "US gallon", "US gallons")},
    "area": {"sqm": (1.0, "m²", "m²"), "sqkm": (1e6, "km²", "km²"), "sqft": (0.09290304, "sq ft", "sq ft"),
             "sqmi": (2589988.110336, "sq mi", "sq mi"), "acre": (4046.8564224, "acre", "acres"), "ha": (10000.0, "hectare", "hectares")},
    "speed": {"mps": (1.0, "m/s", "m/s"), "kmh": (1 / 3.6, "km/h", "km/h"), "mph": (0.44704, "mph", "mph"), "kn": (1852 / 3600, "knot", "knots")},
    "time": {"s": (1.0, "second", "seconds"), "min": (60.0, "minute", "minutes"), "h": (3600.0, "hour", "hours"),
             "day": (86400.0, "day", "days"), "week": (604800.0, "week", "weeks")},
}
_TEMP = {"c": "°C", "f": "°F", "k": "K"}
_ALIASES = {
    "mm": "millimeter millimeters millimetre millimetres", "cm": "centimeter centimeters centimetre centimetres",
    "m": "meter meters metre metres", "km": "kilometer kilometers kilometre kilometres kms",
    "in": "inch inches", "ft": "foot feet", "yd": "yard yards yds", "mi": "mile miles", "nmi": "nautical mile|nautical miles",
    "mg": "milligram milligrams", "g": "gram grams", "kg": "kilogram kilograms kilo kilos kgs", "tonne": "tonnes|metric ton|metric tons",
    "oz": "ounce ounces", "lb": "lbs pound pounds", "st": "stone stones", "ton": "tons|short ton|short tons",
    "ml": "milliliter milliliters millilitre millilitres", "l": "liter liters litre litres",
    "tsp": "teaspoon teaspoons", "tbsp": "tablespoon tablespoons", "floz": "fl oz|fluid ounce|fluid ounces",
    "cup": "cups", "pt": "pint pints", "qt": "quart quarts", "gal": "gallon gallons",
    "sqm": "m2|sq m|square meter|square meters|square metre|square metres", "sqkm": "km2|sq km|square kilometer|square kilometers|square kilometre|square kilometres",
    "sqft": "ft2|sq ft|square foot|square feet", "sqmi": "mi2|sq mi|square mile|square miles", "acre": "acres", "ha": "hectare hectares",
    "mps": "m/s|meters per second|metres per second", "kmh": "km/h|kph|kilometers per hour|kilometres per hour",
    "mph": "miles per hour|", "kn": "knot knots",
    "s": "sec secs second seconds", "min": "mins minute minutes", "h": "hr hrs hour hours", "day": "days", "week": "weeks",
}
_TEMP_ALIASES = {"c": "celsius centigrade", "f": "fahrenheit", "k": "kelvin kelvins"}


def _unit_names() -> dict[str, list[tuple[str, str]]]:
    names: dict[str, list[tuple[str, str]]] = {}
    for dim, table in _U.items():
        for u in table:
            al = _ALIASES.get(u, "")              # "a b c" = three one-word names; with a "|" = names that may hold spaces
            for n in [u] + [a.strip() for a in (al.split("|") if "|" in al else al.split()) if a.strip()]:
                names.setdefault(n, []).append((dim, u))
    for u, al in _TEMP_ALIASES.items():
        for n in [u] + al.split():
            names.setdefault(n, []).append(("temp", u))
    return names


_NAMES = _unit_names()
_UNIT_RE = "|".join(sorted((re.escape(n) for n in _NAMES), key=len, reverse=True))
_DEG = r"(?:°\s*|degrees?\s+|deg\s+)?"
_CONVERT = re.compile(r"^(?:convert\s+)?(-?(?:%s))\s*%s(%s)\s+(?:in|to|into|as|=)\s+%s(%s)$" % (_NUM, _DEG, _UNIT_RE, _DEG, _UNIT_RE))
_HOW_MANY = re.compile(r"^(?:how\s+many\s+)?(%s)\s+(?:are\s+)?(?:in|per|to)\s+(?:(a|an|one|%s)\s+)?(%s)$" % (_UNIT_RE, _NUM, _UNIT_RE))


def _pair(a: str, b: str) -> tuple[str, str, str] | None:
    """(dimension, unit a, unit b) for two unit names that measure the same thing. "oz" is weight, unless the other
    side is a volume ("8 oz in ml": fluid ounces)."""
    for da, ua in _NAMES.get(a, []):
        for db, ub in _NAMES.get(b, []):
            if da == db:
                return da, ua, ub
    if a == "oz" or a.startswith("ounce"):
        return next((("volume", "floz", ub) for db, ub in _NAMES.get(b, []) if db == "volume"), None)
    if b == "oz" or b.startswith("ounce"):
        return next((("volume", ua, "floz") for da, ua in _NAMES.get(a, []) if da == "volume"), None)
    return None


def _temp(v: float, a: str, b: str) -> float:
    k = v + 273.15 if a == "c" else (v - 32) * 5 / 9 + 273.15 if a == "f" else v
    return k - 273.15 if b == "c" else (k - 273.15) * 9 / 5 + 32 if b == "f" else k


def _label(dim: str, u: str, v: float) -> str:
    _f, one, many = _U[dim][u]
    return one if abs(v) == 1 else many


def convert(q: str) -> dict | None:
    s = re.sub(r"\s+", " ", q.strip().lower().rstrip("?").strip())
    m = _CONVERT.match(s)
    if m:
        v = float(m.group(1).replace(",", ""))
        a, b = m.group(2), m.group(3)
    else:
        m = _HOW_MANY.match(s)
        if not m:
            return None
        n = m.group(2)
        v = 1.0 if n in (None, "a", "an", "one") else float(n.replace(",", ""))
        a, b = m.group(3), m.group(1)              # "cups in a liter": 1 liter -> cups
    got = _pair(a, b)
    if not got or (got[0] == "temp" and m.re is _HOW_MANY):     # "c to f" names two scales, it asks nothing
        return None
    dim, ua, ub = got
    if ua == ub:
        return None
    try:
        if dim == "temp":
            r = _temp(v, ua, ub)
            if _temp(v, ua, "k") < 0:
                return None                        # below absolute zero
            return {"answer": f"{fmt(round(r, 2), 6)} {_TEMP[ub]}", "title": f"{fmt(v)} {_TEMP[ua]}", "detail": "", "question": "unit"}
        r = v * _U[dim][ua][0] / _U[dim][ub][0]
        one = _U[dim][ua][0] / _U[dim][ub][0]
        return {"answer": f"{fmt(r, 6)} {_label(dim, ub, r)}", "title": f"{fmt(v)} {_label(dim, ua, v)}",
                "detail": "" if v == 1 else f"1 {_label(dim, ua, 1)} = {fmt(one, 6)} {_label(dim, ub, one)}", "question": "unit"}
    except (OverflowError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------- time

# What people call a zone -> an IANA zone. An abbreviation means the region's clock on the day asked, so "3pm est"
# in July is New York's wall time (EDT), which is what the person meant.
_ZONE_ALIASES = {
    "utc": "UTC", "gmt": "UTC", "zulu": "UTC",
    "est": "America/New_York", "edt": "America/New_York", "et": "America/New_York", "eastern": "America/New_York",
    "cst": "America/Chicago", "cdt": "America/Chicago", "ct": "America/Chicago", "central": "America/Chicago",
    "mst": "America/Denver", "mdt": "America/Denver", "mt": "America/Denver", "mountain": "America/Denver",
    "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles", "pt": "America/Los_Angeles", "pacific": "America/Los_Angeles",
    "akst": "America/Anchorage", "akdt": "America/Anchorage", "hst": "Pacific/Honolulu", "hawaii": "Pacific/Honolulu",
    "bst": "Europe/London", "wet": "Europe/Lisbon", "cet": "Europe/Berlin", "cest": "Europe/Berlin", "eet": "Europe/Athens",
    "msk": "Europe/Moscow", "ist": "Asia/Kolkata", "jst": "Asia/Tokyo", "kst": "Asia/Seoul", "hkt": "Asia/Hong_Kong",
    "sgt": "Asia/Singapore", "aest": "Australia/Sydney", "aedt": "Australia/Sydney", "awst": "Australia/Perth",
    "nzst": "Pacific/Auckland", "nzdt": "Pacific/Auckland",
    # large cities that are not a zone's own name, and countries with one clock
    "nyc": "America/New_York", "new york city": "America/New_York", "boston": "America/New_York", "miami": "America/New_York",
    "atlanta": "America/New_York", "philadelphia": "America/New_York", "washington dc": "America/New_York",
    "houston": "America/Chicago", "dallas": "America/Chicago", "austin": "America/Chicago", "minneapolis": "America/Chicago",
    "new orleans": "America/Chicago", "salt lake city": "America/Denver", "la": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles", "seattle": "America/Los_Angeles", "san diego": "America/Los_Angeles",
    "las vegas": "America/Los_Angeles", "montreal": "America/Toronto", "ottawa": "America/Toronto", "calgary": "America/Edmonton",
    "rio de janeiro": "America/Sao_Paulo", "rio": "America/Sao_Paulo", "buenos aires": "America/Argentina/Buenos_Aires",
    "edinburgh": "Europe/London", "glasgow": "Europe/London", "manchester": "Europe/London", "uk": "Europe/London",
    "england": "Europe/London", "britain": "Europe/London", "munich": "Europe/Berlin", "frankfurt": "Europe/Berlin",
    "hamburg": "Europe/Berlin", "germany": "Europe/Berlin", "france": "Europe/Paris", "milan": "Europe/Rome", "italy": "Europe/Rome",
    "barcelona": "Europe/Madrid", "spain": "Europe/Madrid", "geneva": "Europe/Zurich", "switzerland": "Europe/Zurich",
    "netherlands": "Europe/Amsterdam", "ireland": "Europe/Dublin", "sweden": "Europe/Stockholm", "norway": "Europe/Oslo",
    "poland": "Europe/Warsaw", "greece": "Europe/Athens", "turkey": "Europe/Istanbul", "israel": "Asia/Jerusalem",
    "tel aviv": "Asia/Jerusalem", "abu dhabi": "Asia/Dubai", "uae": "Asia/Dubai", "mumbai": "Asia/Kolkata", "delhi": "Asia/Kolkata",
    "new delhi": "Asia/Kolkata", "bangalore": "Asia/Kolkata", "india": "Asia/Kolkata", "beijing": "Asia/Shanghai",
    "shenzhen": "Asia/Shanghai", "china": "Asia/Shanghai", "osaka": "Asia/Tokyo", "japan": "Asia/Tokyo", "korea": "Asia/Seoul",
    "south korea": "Asia/Seoul", "hanoi": "Asia/Ho_Chi_Minh", "vietnam": "Asia/Ho_Chi_Minh", "thailand": "Asia/Bangkok",
    "philippines": "Asia/Manila", "melbourne": "Australia/Melbourne", "cape town": "Africa/Johannesburg",
    "south africa": "Africa/Johannesburg", "egypt": "Africa/Cairo", "nigeria": "Africa/Lagos", "kenya": "Africa/Nairobi",
    "new zealand": "Pacific/Auckland",
}
_zones: dict[str, str] | None = None


def zones() -> dict[str, str]:
    """place or abbreviation -> IANA zone: every zone's own city name, then the aliases (which win)."""
    global _zones
    if _zones is None:
        from zoneinfo import available_timezones
        z = {}
        for name in sorted(available_timezones()):
            head, _, city = name.rpartition("/")
            if not head or head.split("/")[0] in ("Etc", "SystemV", "posix", "right", "US", "Brazil", "Canada", "Chile", "Mexico"):
                continue
            z.setdefault(city.replace("_", " ").lower(), name)
        z.update(_ZONE_ALIASES)
        _zones = z
    return _zones


def _zone(place: str):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    key = re.sub(r"\s+", " ", re.sub(r"[.,]", "", place.strip().lower()))
    key = re.sub(r"^(?:the )?(?:city of )?", "", key)
    key = re.sub(r" (?:time|standard time|daylight time)$", "", key)
    name = zones().get(key)
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _clock(t: dt.datetime) -> str:
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def _offset(t: dt.datetime) -> str:
    mins = int(t.utcoffset().total_seconds() // 60)
    sign, mins = ("+" if mins >= 0 else "−"), abs(mins)
    return "UTC" + ("" if mins == 0 else f"{sign}{mins // 60}" + (f":{mins % 60:02d}" if mins % 60 else ""))


def _abbr(t: dt.datetime) -> str:
    a = t.tzname() or ""
    return a if a.isalpha() else _offset(t)        # zoneinfo gives "+03" for zones with no letters


def _day(t: dt.datetime) -> str:
    return f"{t.strftime('%A')}, {t.strftime('%B')} {t.day}"


_TIME_IN = re.compile(r"^(?:what(?:'s| is)? (?:the )?)?(?:current |local )?time(?: is it)?(?: right now| now)? in (.+)$")
_PLACE_TIME = re.compile(r"^(?:current |local )?(.+?) time(?: right now| now)?$")
_TIME_CONV = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s+(?:in\s+)?(.+?)\s+(?:to|in|into)\s+(.+?)(?: time)?$")


def time_answer(q: str, now: dt.datetime | None = None) -> dict | None:
    s = re.sub(r"\s+", " ", q.strip().lower().rstrip("?").strip())
    now = now or dt.datetime.now(dt.timezone.utc)
    m = _TIME_CONV.match(s)
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(".", "")
        za, zb = _zone(m.group(4)), _zone(m.group(5))
        if za and zb and mi < 60 and ((ap and 1 <= h <= 12) or (not ap and h < 24 and m.group(2) is not None)):
            if ap:
                h = h % 12 + (12 if ap == "pm" else 0)
            src = now.astimezone(za).replace(hour=h, minute=mi, second=0, microsecond=0)
            dst = src.astimezone(zb)
            days = (dst.date() - src.date()).days
            when = "the same day" if days == 0 else "the next day" if days == 1 else "the day before"
            return {"answer": f"{_clock(dst)} {_abbr(dst)}", "title": f"{_clock(src)} {_abbr(src)}",
                    "detail": f"{when} · {_offset(dst)}", "question": "time"}
    m = _TIME_IN.match(s) or _PLACE_TIME.match(s)
    if not m:
        return None
    place = m.group(1).strip()
    z = _zone(place)
    if not z:
        return None
    t = now.astimezone(z)
    return {"answer": _clock(t), "title": place.title() if len(place) > 4 else place.upper(),
            "detail": f"{_day(t)} · {_abbr(t)} ({_offset(t)})", "question": "time"}


# ---------------------------------------------------------------------------------------------- currency

# Names people use -> ISO code. A bare "dollar" is the US one, "peso" Mexico's, "franc" Switzerland's, "pound"
# sterling. A three-letter code is also accepted as itself when the rates carry it (every code in the ECB file).
_CUR_NAMES = {
    "USD": "dollar|dollars|us dollar|us dollars|american dollar|american dollars|buck|bucks",
    "EUR": "euro|euros",
    "GBP": "pound|pounds|pound sterling|pounds sterling|sterling|british pound|british pounds|quid",
    "JPY": "yen|japanese yen", "CNY": "yuan|chinese yuan|renminbi|rmb",
    "INR": "rupee|rupees|indian rupee|indian rupees",
    "MXN": "peso|pesos|mexican peso|mexican pesos", "PHP": "philippine peso|philippine pesos",
    "CHF": "franc|francs|swiss franc|swiss francs",
    "CAD": "canadian dollar|canadian dollars", "AUD": "australian dollar|australian dollars|aussie dollar|aussie dollars",
    "NZD": "new zealand dollar|new zealand dollars", "HKD": "hong kong dollar|hong kong dollars",
    "SGD": "singapore dollar|singapore dollars",
    "SEK": "swedish krona|swedish kronor", "NOK": "norwegian krone|norwegian kroner", "DKK": "danish krone|danish kroner",
    "ISK": "icelandic krona", "PLN": "zloty|zlotys|polish zloty", "CZK": "koruna|czech koruna", "HUF": "forint|forints",
    "RON": "romanian leu", "TRY": "lira|turkish lira", "BRL": "reais|brazilian real", "ILS": "shekel|shekels",
    "KRW": "won|korean won", "ZAR": "rand|south african rand", "THB": "baht|thai baht", "IDR": "rupiah", "MYR": "ringgit",
}
_CUR_BY_NAME = {n: code for code, names in _CUR_NAMES.items() for n in names.split("|")}
_CUR_SYM = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR"}
# "$100 cad" is Canadian: a symbol next to a name must be able to mean it
_CUR_SYM_OK = {"$": {"USD", "CAD", "AUD", "NZD", "HKD", "SGD", "MXN"}, "€": {"EUR"}, "£": {"GBP"}, "¥": {"JPY", "CNY"}, "₹": {"INR"}}
_CUR_WHOLE = {"JPY", "KRW", "ISK", "IDR", "HUF"}      # written without decimals once the amount is large
FX_MAX_AGE_DAYS = 7                                   # older rates than this are not an answer
FX_MAX_UNCHECKED_DAYS = 3                             # nor are rates nobody has verified for this long (a dead poller)
_SYM = "[%s]" % "".join(_CUR_SYM)
_CUR_PREFIX = re.compile(r"^(?:convert|what\s+is|what's|whats|how\s+much\s+is|how\s+much\s+are)\s+")
_CUR_SPLIT = re.compile(r"^(.+?)\s+(?:to|in|into|as|=)\s+(.+)$")
_CUR_LEFT = re.compile(r"^(?:(?P<sym>%s)\s*)?(?:(?P<num>%s)\s*(?P<sym2>%s)?|(?P<one>a|an|one)(?=\s))?\s*(?P<name>[a-z][a-z ]*)?$" % (_SYM, _NUM, _SYM))
_CUR_HOW_MANY = re.compile(r"^how\s+many\s+(?P<to>[a-z][a-z ]*?)\s+(?:is|are|in|per|to|for|=)\s+(?P<from>.+)$")
_CUR_ONE = re.compile(r"^(?:a|an|one)\s+(?P<name>[a-z][a-z ]*)$")


def _cur(name: str | None, per: dict | None) -> str | None:
    """A currency name or code -> its ISO code, when the rates can price it. per None: any code-shaped word."""
    if not name:
        return None
    code = _CUR_BY_NAME.get(name) or (name.upper() if len(name) == 3 and name.isalpha() and name.isascii() else None)
    if code and per is not None and code != "EUR" and code not in per:
        return None
    return code


def _money_question(q: str, per: dict | None) -> tuple[float | int, str, str] | None:
    """(amount, from, to) when q asks to change one currency into another, else None. Both sides must be money:
    "5 pounds in kg" is a weight and "dollar tree" is a shop."""
    s = re.sub(r"\s+", " ", q.strip().lower().rstrip("?").strip()).replace("u.s.", "us")
    s = _CUR_PREFIX.sub("", s)
    m = _CUR_HOW_MANY.match(s)
    if m:                                              # "how many euros is 100 dollars", "how many pesos in a dollar"
        left, right = m.group("from"), m.group("to")
    else:
        m = _CUR_SPLIT.match(s)
        if not m:
            return None
        left, right = m.group(1), m.group(2)
        one = _CUR_ONE.match(right)
        if one and _cur(left, per):                    # "pesos in a dollar": one dollar, in pesos
            left, right = right, left
    lm = _CUR_LEFT.match(left.strip())
    if not lm or (lm.group("sym") and lm.group("sym2")):
        return None
    sym, name = lm.group("sym") or lm.group("sym2"), (lm.group("name") or "").strip()
    src = _cur(name, per) if name else _CUR_SYM.get(sym or "")
    if not src or (sym and name and src not in _CUR_SYM_OK[sym]):
        return None
    if sym and per is not None and src != "EUR" and src not in per:
        return None
    right = right.strip()
    dst = _CUR_SYM.get(right) or _cur(right, per)
    if not dst or dst == src or (per is not None and dst != "EUR" and dst not in per):
        return None
    n = lm.group("num")
    if n is None and name == "try":                    # "try to usd" is a sentence; "100 try to usd" and "lira to usd" are money
        return None
    amount = 1 if n is None else (float(n.replace(",", "")) if "." in n else int(n.replace(",", "")))
    if not 0 < amount <= 1e15:
        return None
    return amount, src, dst


def wants_rates(q: str) -> bool:
    """True when q is shaped like a currency question, so the caller reads the rates only then."""
    try:
        return bool(q) and len(q) <= 120 and _money_question(q, None) is not None
    except Exception:
        return False


def _digits4(v: float) -> str:
    """A value under 1 to four significant digits, at least two decimals: 0.9259, 0.006312, 0.50."""
    d = min(12, 3 - int(math.floor(math.log10(abs(v)))))
    s = f"{v:.{d}f}"
    whole, _, frac = s.partition(".")
    return whole + "." + frac.rstrip("0").ljust(2, "0")


def _money(v: float, code: str) -> str:
    """An amount of money: two decimals; none for yen-like currencies from 100 up; four digits under 1."""
    if abs(v) < 1:
        return _digits4(v)
    return f"{v:,.0f}" if code in _CUR_WHOLE and abs(v) >= 100 else f"{v:,.2f}"


def _rate(v: float) -> str:
    """What one unit buys: 0.9259, 1.1269, 158.09, 20,104.80."""
    if abs(v) < 1:
        return _digits4(v)
    if abs(v) >= 100:
        return f"{v:,.2f}"
    whole, _, frac = f"{v:.4f}".partition(".")
    return whole + "." + frac.rstrip("0").ljust(2, "0")


def _checked_ago(checked, now: dt.datetime) -> str | None:
    """"2026-10-06T15:12:00Z" -> "just now" (under 5 minutes), "12 min ago", "2 h ago", "yesterday", "3 days ago".
    None when it is not a time, is over FX_MAX_UNCHECKED_DAYS old, or is more than a day ahead of the clock."""
    if not isinstance(checked, str):
        return None
    try:
        t = dt.datetime.fromisoformat(checked.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    age = (now - t).total_seconds()
    if age > FX_MAX_UNCHECKED_DAYS * 86400 or age < -86400:
        return None
    if age < 300:
        return "just now"
    if age < 3600:
        return f"{int(age // 60)} min ago"
    if age < 86400:
        return f"{int(age // 3600)} h ago"
    return "yesterday" if age < 2 * 86400 else f"{int(age // 86400)} days ago"


def currency(q: str, rates: dict | None, now: dt.datetime | None = None) -> dict | None:
    """"100 usd to eur" from rates = {"date": "YYYY-MM-DD", "checked": ISO 8601 UTC, "per_eur": {code: units per
    euro}}. A pair without the euro goes through it (a cross rate), and the small line says it was calculated.
    "checked" is the last successful fetch; a table without it (None) is answered with the rate's date alone."""
    if not isinstance(rates, dict) or not isinstance(rates.get("per_eur"), dict) or not isinstance(rates.get("date"), str):
        return None
    try:
        day = dt.date.fromisoformat(rates["date"])
    except ValueError:
        return None
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    if not -1 <= (now.date() - day).days <= FX_MAX_AGE_DAYS:
        return None
    ago = None
    if rates.get("checked") is not None:
        ago = _checked_ago(rates["checked"], now)
        if ago is None:                               # not checked for days, or not a time: nobody has verified it
            return None
    per = {c: float(r) for c, r in rates["per_eur"].items()
           if isinstance(c, str) and isinstance(r, (int, float)) and not isinstance(r, bool) and math.isfinite(r) and r > 0}
    per["EUR"] = 1.0
    got = _money_question(q, per)
    if not got:
        return None
    amount, src, dst = got
    one = per[dst] / per[src]
    v = amount * one
    if not math.isfinite(v) or abs(v) < 1e-9 or abs(v) >= 1e18:
        return None
    # owner, 2026-10-07: the short "ECB" with the words "reference rate" in the answer; the bank's full name and its
    # "for information purposes only" sentence are in NOTICE. Paid boxes:.
    how = "ECB reference rate" if "EUR" in (src, dst) else "calculated from ECB reference rates"
    detail = f"1 {src} = {_rate(one)} {dst} · {how} of {day.strftime('%b')} {day.day}" + (f" · checked {ago}" if ago else "")
    return {"answer": f"{_money(v, dst)} {dst}", "title": f"{fmt(amount)} {src}", "detail": detail, "question": "currency"}


# ---------------------------------------------------------------------------------------------- entry


def instant(q: str, now: dt.datetime | None = None, rates: dict | None = None) -> dict | None:
    """The first of: a unit conversion, a time somewhere, a currency conversion, a calculation. None for everything
    else. rates: see currency(); None means no currency answers."""
    if not q or len(q) > 120:
        return None
    for f in (convert, lambda s: time_answer(s, now), lambda s: currency(s, rates, now), calculate):
        try:
            a = f(q)
        except Exception:                           # an answer is extra: never fail the search over it
            a = None
        if a:
            return a
    return None
