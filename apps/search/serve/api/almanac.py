"""Sun, moon, season and tide answers computed on the box: sunrise and sunset, day length, the moon's
phase, moonrise and moonset, the next four phases, the first day of each season, the day's high and low tides
(the wording here; the prediction is tide_model.py, the nearest NOAA station is the caller's), and from tables shipped
beside it (phase 3): solar and lunar eclipses, meteor showers, and the dates of the last and first freeze.

    parse(q)  -> {"kind": "sun"|"moon"|"season"|"tide"|"eclipse"|"meteor"|"frost", "what", "day", "place", "year"} or None
    answer(p, now, lat=None, lon=None, tz=None, label=None) -> {"answer", "title", "detail", "question"} or None

Pure local code, standard library only: nothing is fetched, nothing leaves the box. The caller (search_api) turns the
place text into a point, a time zone and a label with the weather card's town table; with no point there is no rise or
set answer, and the moon's phase and the seasons are answered in `tz` (the caller's local zone when it knows none).

Method. Sun: the low-precision solar position of Meeus, Astronomical Algorithms ch. 25 (0.01 degree). Moon position:
the Astronomical Almanac's low-precision series (0.3 degree; rise and set within about two minutes). Rise and set are
where the altitude crosses the standard limit (-0.833 degree for the sun, -6 for civil twilight, 0.7275 x parallax -
0.5667 for the moon), found by sampling the local day and bisecting. Phases: Meeus ch. 49 with its periodic terms.
Seasons: the U.S. Naval Observatory's published equinox and solstice times 2025-2045 (a table: the formula would be a
few minutes off). tests/unit/test-search-almanac.sh holds Naval Observatory values for all of it.

Triggers are strict: "sunrise", "sunset in denver", "moon phase", "next full moon", "first day of spring", "winter
solstice 2027". A place needs "in/at/for/near": "sunrise florida" (a city), "sunset boulevard", "full moon", "moon" and
"blue moon" stay searches.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import math
import os
import re

UTC = dt.timezone.utc
_R, _D = math.radians, math.degrees
DELTA_T_S = 70.0            # TT - UTC in these years (69 s in 2025, drifting slowly): the phase formula is in TT


def _jd(t: dt.datetime) -> float:
    return t.timestamp() / 86400.0 + 2440587.5


def _from_jd(jd: float) -> dt.datetime:
    return dt.datetime.fromtimestamp((jd - 2440587.5) * 86400.0, UTC)


# ---------------------------------------------------------------------------------------------- positions


def sun_pos(jd: float) -> tuple[float, float, float]:
    """(right ascension, declination, apparent ecliptic longitude) in degrees."""
    T = (jd - 2451545.0) / 36525.0
    L0 = 280.46646 + 36000.76983 * T + 0.0003032 * T * T
    M = _R(357.52911 + 35999.05029 * T - 0.0001537 * T * T)
    C = ((1.914602 - 0.004817 * T - 0.000014 * T * T) * math.sin(M) + (0.019993 - 0.000101 * T) * math.sin(2 * M)
         + 0.000289 * math.sin(3 * M))
    om = _R(125.04 - 1934.136 * T)
    lam = L0 + C - 0.00569 - 0.00478 * math.sin(om)
    eps = _R(23.439291 - 0.0130042 * T + 0.00256 * math.cos(om))
    lam_r = _R(lam)
    ra = _D(math.atan2(math.cos(eps) * math.sin(lam_r), math.cos(lam_r)))
    return ra % 360.0, _D(math.asin(math.sin(eps) * math.sin(lam_r))), lam % 360.0


def moon_pos(jd: float) -> tuple[float, float, float, float, float]:
    """(right ascension, declination, ecliptic longitude, ecliptic latitude, horizontal parallax) in degrees."""
    T = (jd - 2451545.0) / 36525.0
    s = lambda a, b: math.sin(_R(a + b * T))
    c = lambda a, b: math.cos(_R(a + b * T))
    lam = (218.32 + 481267.881 * T + 6.29 * s(135.0, 477198.87) - 1.27 * s(259.3, -413335.36) + 0.66 * s(235.7, 890534.22)
           + 0.21 * s(269.9, 954397.74) - 0.19 * s(357.5, 35999.05) - 0.11 * s(186.5, 966404.03))
    beta = 5.13 * s(93.3, 483202.02) + 0.28 * s(228.2, 960400.89) - 0.28 * s(318.3, 6003.15) - 0.17 * s(217.6, -407332.21)
    par = 0.9508 + 0.0518 * c(135.0, 477198.87) + 0.0095 * c(259.3, -413335.36) + 0.0078 * c(235.7, 890534.22) + 0.0028 * c(269.9, 954397.74)
    eps = _R(23.439291 - 0.0130042 * T)
    lam_r, br = _R(lam), _R(beta)
    ra = _D(math.atan2(math.sin(lam_r) * math.cos(eps) - math.tan(br) * math.sin(eps), math.cos(lam_r)))
    dec = _D(math.asin(math.sin(br) * math.cos(eps) + math.cos(br) * math.sin(eps) * math.sin(lam_r)))
    return ra % 360.0, dec, lam % 360.0, beta, par


def _altitude(jd: float, lat: float, lon: float, ra: float, dec: float) -> float:
    T = (jd - 2451545.0) / 36525.0
    gmst = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * T * T
    H = _R(gmst + lon - ra)
    return _D(math.asin(math.sin(_R(lat)) * math.sin(_R(dec)) + math.cos(_R(lat)) * math.cos(_R(dec)) * math.cos(H)))


def _sun_f(limit: float):
    def f(jd, lat, lon):
        ra, dec, _ = sun_pos(jd)
        return _altitude(jd, lat, lon, ra, dec) - limit
    return f


def _moon_f(jd, lat, lon):
    ra, dec, _l, _b, par = moon_pos(jd)
    return _altitude(jd, lat, lon, ra, dec) - (0.7275 * par - 0.5667)


def crossings(f, lat: float, lon: float, start: dt.datetime, end: dt.datetime) -> tuple[list, list, bool]:
    """(rise times, set times, above the limit at the start) of f between two instants."""
    a, b = _jd(start), _jd(end)
    n = max(2, int(round((b - a) * 288)))                # every five minutes
    rises, sets = [], []
    prev_t, prev_v = a, f(a, lat, lon)
    up0 = prev_v > 0
    for i in range(1, n + 1):
        t = a + (b - a) * i / n
        v = f(t, lat, lon)
        if (prev_v > 0) != (v > 0):
            lo, hi, vlo = prev_t, t, prev_v
            for _ in range(24):
                mid = (lo + hi) / 2
                vm = f(mid, lat, lon)
                if (vm > 0) == (vlo > 0):
                    lo, vlo = mid, vm
                else:
                    hi = mid
            (rises if v > 0 else sets).append(_from_jd((lo + hi) / 2))
        prev_t, prev_v = t, v
    return rises, sets, up0


# ---------------------------------------------------------------------------------------------- moon phases

_PHASE_NAMES = ("New moon", "First quarter", "Full moon", "Last quarter")
# Meeus table 49.A: (coefficient for new, for full, power of E, multiples of M, M', F, omega)
_NEW_FULL = (
    (-0.40720, -0.40614, 0, 0, 1, 0, 0), (0.17241, 0.17302, 1, 1, 0, 0, 0), (0.01608, 0.01614, 0, 0, 2, 0, 0),
    (0.01039, 0.01043, 0, 0, 0, 2, 0), (0.00739, 0.00734, 1, -1, 1, 0, 0), (-0.00514, -0.00515, 1, 1, 1, 0, 0),
    (0.00208, 0.00209, 2, 2, 0, 0, 0), (-0.00111, -0.00111, 0, 0, 1, -2, 0), (-0.00057, -0.00057, 0, 0, 1, 2, 0),
    (0.00056, 0.00056, 1, 1, 2, 0, 0), (-0.00042, -0.00042, 0, 0, 3, 0, 0), (0.00042, 0.00042, 1, 1, 0, 2, 0),
    (0.00038, 0.00038, 1, 1, 0, -2, 0), (-0.00024, -0.00024, 1, -1, 2, 0, 0), (-0.00017, -0.00017, 0, 0, 0, 0, 1),
    (-0.00007, -0.00007, 0, 2, 1, 0, 0), (0.00004, 0.00004, 0, 0, 2, -2, 0), (0.00004, 0.00004, 0, 3, 0, 0, 0),
    (0.00003, 0.00003, 0, 1, 1, -2, 0), (0.00003, 0.00003, 0, 0, 2, 2, 0), (-0.00003, -0.00003, 0, 1, 1, 2, 0),
    (0.00003, 0.00003, 0, -1, 1, 2, 0), (-0.00002, -0.00002, 0, -1, 1, -2, 0), (-0.00002, -0.00002, 0, 1, 3, 0, 0),
    (0.00002, 0.00002, 0, 0, 4, 0, 0))
_QUARTER = (
    (-0.62801, 0, 0, 1, 0, 0), (0.17172, 1, 1, 0, 0, 0), (-0.01183, 1, 1, 1, 0, 0), (0.00862, 0, 0, 2, 0, 0),
    (0.00804, 0, 0, 0, 2, 0), (0.00454, 1, -1, 1, 0, 0), (0.00204, 2, 2, 0, 0, 0), (-0.00180, 0, 0, 1, -2, 0),
    (-0.00070, 0, 0, 1, 2, 0), (-0.00040, 0, 0, 3, 0, 0), (-0.00034, 1, -1, 2, 0, 0), (0.00032, 1, 1, 0, 2, 0),
    (0.00032, 1, 1, 0, -2, 0), (-0.00028, 2, 2, 1, 0, 0), (0.00027, 1, 1, 2, 0, 0), (-0.00017, 0, 0, 0, 0, 1),
    (-0.00005, 0, -1, 1, -2, 0), (0.00004, 0, 0, 2, 2, 0), (-0.00004, 0, 1, 1, 2, 0), (0.00004, 0, -2, 1, 0, 0),
    (0.00003, 0, 1, 1, -2, 0), (0.00003, 0, 3, 0, 0, 0), (0.00002, 0, 0, 2, -2, 0), (0.00002, 0, -1, 1, 2, 0),
    (-0.00002, 0, 1, 3, 0, 0))
_PLANET = ((299.77, 0.107408, 325), (251.88, 0.016321, 165), (251.83, 26.651886, 164), (349.42, 36.412478, 126),
           (84.66, 18.206239, 110), (141.74, 53.303771, 62), (207.14, 2.453732, 60), (154.84, 7.306860, 56),
           (34.52, 27.261239, 47), (207.19, 0.121824, 42), (291.34, 1.844379, 40), (161.72, 24.198154, 37),
           (239.56, 25.513099, 35), (331.55, 3.592518, 23))


def phase_time(k: float) -> dt.datetime:
    """The instant (UTC) of lunation k: whole = new moon, +0.25 first quarter, +0.5 full, +0.75 last quarter."""
    T = k / 1236.85
    jde = 2451550.09766 + 29.530588861 * k + 0.00015437 * T * T - 0.000000150 * T ** 3 + 0.00000000073 * T ** 4
    E = 1 - 0.002516 * T - 0.0000074 * T * T
    M = _R(2.5534 + 29.10535670 * k - 0.0000014 * T * T)
    Mp = _R(201.5643 + 385.81693528 * k + 0.0107582 * T * T)
    F = _R(160.7108 + 390.67050284 * k - 0.0016118 * T * T)
    om = _R(124.7746 - 1.56375588 * k + 0.0020672 * T * T)
    q = round((k % 1) * 4) % 4
    if q in (0, 2):
        for cn, cf, e, m, mp, f, o in _NEW_FULL:
            jde += (cn if q == 0 else cf) * E ** e * math.sin(m * M + mp * Mp + f * F + o * om)
    else:
        for cq, e, m, mp, f, o in _QUARTER:
            jde += cq * E ** e * math.sin(m * M + mp * Mp + f * F + o * om)
        W = (0.00306 - 0.00038 * E * math.cos(M) + 0.00026 * math.cos(Mp) - 0.00002 * math.cos(Mp - M)
             + 0.00002 * math.cos(Mp + M) + 0.00002 * math.cos(2 * F))
        jde += W if q == 1 else -W
    for i, (a0, a1, c) in enumerate(_PLANET):
        jde += c * 1e-6 * math.sin(_R(a0 + a1 * k - (0.009173 * T * T if i == 0 else 0)))
    return _from_jd(jde) - dt.timedelta(seconds=DELTA_T_S)


def phases_after(now: dt.datetime, n: int = 4) -> list[tuple[str, dt.datetime]]:
    """The next n principal phases after now, oldest first."""
    k = math.floor((now.year + (now.timetuple().tm_yday - 1) / 365.25 - 2000.0) * 12.3685 * 4) / 4 - 0.5
    out = []
    while len(out) < n:
        t = phase_time(k)
        if t > now:
            out.append((_PHASE_NAMES[round((k % 1) * 4) % 4], t))
        k += 0.25
    return out


def moon_now(now: dt.datetime) -> tuple[float, float]:
    """(elongation east of the sun 0-360, lit fraction 0-1)."""
    jd = _jd(now)
    _ra, _dec, ls = sun_pos(jd)
    _ra, _dec, lm, bm, _p = moon_pos(jd)
    elong = (lm - ls) % 360.0
    psi = math.acos(math.cos(_R(bm)) * math.cos(_R(lm - ls)))
    return elong, (1 - math.cos(psi)) / 2


# ---------------------------------------------------------------------------------------------- seasons

# U.S. Naval Observatory, Earth's Seasons (UTC): March equinox, June solstice, September equinox, December solstice
_SEASONS = {
    2025: ("03-20 09:01", "06-21 02:42", "09-22 18:19", "12-21 15:03"),
    2026: ("03-20 14:46", "06-21 08:24", "09-23 00:05", "12-21 20:50"),
    2027: ("03-20 20:25", "06-21 14:11", "09-23 06:02", "12-22 02:42"),
    2028: ("03-20 02:17", "06-20 20:02", "09-22 11:45", "12-21 08:19"),
    2029: ("03-20 08:02", "06-21 01:48", "09-22 17:38", "12-21 14:14"),
    2030: ("03-20 13:52", "06-21 07:31", "09-22 23:27", "12-21 20:09"),
    2031: ("03-20 19:41", "06-21 13:17", "09-23 05:15", "12-22 01:55"),
    2032: ("03-20 01:22", "06-20 19:09", "09-22 11:11", "12-21 07:56"),
    2033: ("03-20 07:22", "06-21 01:01", "09-22 16:51", "12-21 13:46"),
    2034: ("03-20 13:17", "06-21 06:44", "09-22 22:39", "12-21 19:34"),
    2035: ("03-20 19:02", "06-21 12:33", "09-23 04:39", "12-22 01:31"),
    2036: ("03-20 01:03", "06-20 18:32", "09-22 10:23", "12-21 07:13"),
    2037: ("03-20 06:50", "06-21 00:22", "09-22 16:13", "12-21 13:07"),
    2038: ("03-20 12:40", "06-21 06:09", "09-22 22:02", "12-21 19:02"),
    2039: ("03-20 18:32", "06-21 11:57", "09-23 03:49", "12-22 00:40"),
    2040: ("03-20 00:11", "06-20 17:46", "09-22 09:45", "12-21 06:33"),
    2041: ("03-20 06:07", "06-20 23:36", "09-22 15:26", "12-21 12:18"),
    2042: ("03-20 11:53", "06-21 05:16", "09-22 21:11", "12-21 18:04"),
    2043: ("03-20 17:27", "06-21 10:58", "09-23 03:07", "12-22 00:01"),
    2044: ("03-19 23:20", "06-20 16:51", "09-22 08:48", "12-21 05:43"),
    2045: ("03-20 05:07", "06-20 22:34", "09-22 14:33", "12-21 11:35"),
}
_SEASON_NAMES = (("spring", "March equinox"), ("summer", "June solstice"), ("fall", "September equinox"), ("winter", "December solstice"))


def season_time(i: int, year: int) -> dt.datetime | None:
    row = _SEASONS.get(year)
    if not row:
        return None
    return dt.datetime.strptime(f"{year}-{row[i]}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)


# ---------------------------------------------------------------------------------------------- which searches

_PLACE = r"(?:\s+(?:in|at|for|near)\s+(?P<place>[a-z][a-z .'-]{1,40}?))?"
_DAY = r"(?:\s+(?P<day>today|tonight|tomorrow|this morning|this evening))?"
_ASK = r"(?:(?:what|which)\s+time\s+(?:is|does|will)\s+(?:the\s+)?|when\s+(?:is|does|will)\s+(?:the\s+)?|what\s+is\s+(?:the\s+)?|what's\s+(?:the\s+)?|time\s+of\s+)?"
_SUN = re.compile(r"^" + _ASK + r"(?:sun\s+)?(?P<what>sunrise|sunset|sun\s?rise|sun\s?set|rise|set)(?:\s+times?)?(?:\s+be)?" + _DAY + _PLACE + _DAY.replace("day>", "day2>") + r"$")
_SUN_VERB = re.compile(r"^(?:what\s+time|when)\s+(?:does|will)\s+(?:the\s+)?sun\s+(?P<what>rise|set|come\s+up|go\s+down)" + _DAY + _PLACE + _DAY.replace("day>", "day2>") + r"$")
_DAYLEN = re.compile(r"^(?:how\s+long\s+is\s+(?:the\s+)?(?:day|daylight)|(?:how\s+many\s+)?hours\s+of\s+daylight|(?:what\s+is\s+(?:the\s+)?)?(?:day\s?length|length\s+of\s+(?:the\s+)?day|daylight\s+hours))" + _DAY + _PLACE + _DAY.replace("day>", "day2>") + r"$")
_MOON_RS = re.compile(r"^" + _ASK + r"(?P<what>moonrise|moonset|moon\s+rise|moon\s+set)(?:\s+times?)?" + _DAY + _PLACE + _DAY.replace("day>", "day2>") + r"$")
_MOON_PHASE = re.compile(r"^(?:(?:what(?:'s|\s+is)\s+(?:the\s+)?)?(?:current\s+)?(?:moon\s+phase|phase\s+of\s+the\s+moon|lunar\s+phase|moon\s+phases)(?:\s+(?:today|tonight|now))?"
                         r"|what\s+phase\s+is\s+the\s+moon(?:\s+in)?(?:\s+(?:today|tonight|now))?"
                         r"|is\s+(?:it|there)\s+a\s+(?:full|new)\s+moon(?:\s+(?:today|tonight))?|is\s+the\s+moon\s+full(?:\s+(?:today|tonight))?)$")
_MOON_NEXT = re.compile(r"^(?:when\s+is\s+(?:the\s+)?)?next\s+(?P<which>full|new)\s+moon$|^when\s+is\s+the\s+(?P<which2>full|new)\s+moon$")
_SEASON = re.compile(r"^(?:when\s+(?:is|does)\s+(?:the\s+)?)?(?:"
                     r"first\s+day\s+of\s+(?P<s1>spring|summer|fall|autumn|winter)"
                     r"|(?P<s2>spring|summer|fall|autumn|winter)\s+(?:start|starts|begin|begins)"
                     r"|(?P<e>spring|vernal|march|fall|autumn|autumnal|september)\s+equinox"
                     r"|(?P<so>summer|june|winter|december)\s+solstice)"
                     r"(?:\s+(?:in\s+)?(?P<year>20\d\d))?$")
# Tides (phase 2). Bare "tide", "tides" and "high tide" are names of things (a detergent, films, songs): the
# question needs a second word that only the question has ("times", "today", "in <place>", "when is ...").
_TIDE_KIND = r"(?P<what>high\s+tides?|low\s+tides?|tides?)"
_TIDE = re.compile(r"^(?:" + _ASK + r"(?:the\s+)?(?:next\s+)?" + _TIDE_KIND + r"(?:\s+(?P<word>times?|table|tables|chart|charts|schedule|predictions?|forecast))?"
                   + _DAY + _PLACE + _DAY.replace("day>", "day2>") + r")$")
# "high tide atlantic city", "tides cape may nj": no "in". Too loose to be a question by its words alone ("tide pods",
# "high tide inn"), so parse() does not take it: the caller answers only when the rest names a US town it knows and
# a station is in reach (search_api.almanac_answer).
_TIDE_BARE = re.compile(r"^" + _TIDE_KIND + r"(?:\s+(?:times?|table|tables|chart|charts|schedule))?\s+(?P<place>[a-z][a-z .'-]{2,40})$")


def parse_bare_tide(q: str) -> dict | None:
    t = " ".join(q.lower().strip().rstrip("?!. ").replace(",", " ").split())
    m = _TIDE_BARE.match(t)
    if not m or parse(q):
        return None
    what = m.group("what")
    return {"kind": "tide", "what": "high" if what.startswith("high") else "low" if what.startswith("low") else "all",
            "day": 0, "place": m.group("place").strip(), "year": None, "bare": True}


_SEASON_IDX = {"spring": 0, "vernal": 0, "march": 0, "summer": 1, "june": 1, "fall": 2, "autumn": 2, "autumnal": 2, "september": 2,
               "winter": 3, "december": 3}
_DAY_OFF = {"tomorrow": 1}
_HERE = re.compile(r"\s+(?:here|near\s+me|in\s+my\s+(?:area|location|town|city)|at\s+my\s+location|where\s+i\s+am)$")


# A whole search that is only the thing's name (owner 2026-11-23: "a search for moon should show the cards we have for
# moon, high tide, tide or low tide, tide state etc should give tide info for closest using location"). Exact matches
# only: "tide pods" or "moon river" stay ordinary searches, and the answer sits above the usual list. Tide, sun and
# frost need a point (the searcher's location or the home point); without one the caller gives no answer.
def _b(kind, what, **kw):
    return dict({"kind": kind, "what": what, "day": 0, "place": None, "year": None, "bare": True}, **kw)


_BARE = {}
for _w in ("moon", "the moon", "moon today", "moon tonight", "moon now", "moon phase today", "lunar phase", "moon cycle"):
    _BARE[_w] = _b("moon", "phase")
for _w in ("full moon", "next full moon", "full moon date"):
    _BARE[_w] = _b("moon", "nextfull")
for _w in ("new moon", "next new moon"):
    _BARE[_w] = _b("moon", "nextnew")
for _w in ("tide", "tides", "the tide", "tide now", "tide state", "tide status", "current tide", "tide today", "tides now",
           "is the tide in", "is the tide out", "is the tide coming in", "is the tide going out", "is it high tide",
           "is it low tide", "tide level", "tide chart", "tide table"):
    _BARE[_w] = _b("tide", "all")
for _w in ("high tide", "high tides", "next high tide", "high tide today"):
    _BARE[_w] = _b("tide", "high")
for _w in ("low tide", "low tides", "next low tide", "low tide today"):
    _BARE[_w] = _b("tide", "low")
for _w in ("sun", "the sun", "sun today", "daylight", "sun times"):
    _BARE[_w] = _b("sun", "sunrise")
for _w in ("eclipse", "eclipses", "an eclipse"):
    _BARE[_w] = _b("eclipse", "any", type=None, span=None)
for _w in ("solar eclipse", "solar eclipses", "sun eclipse"):
    _BARE[_w] = _b("eclipse", "solar", type=None, span=None)
for _w in ("lunar eclipse", "lunar eclipses", "moon eclipse", "blood moon"):
    _BARE[_w] = _b("eclipse", "lunar", type=None, span=None)
for _w in ("meteor shower", "meteor showers", "meteors", "shooting stars"):
    _BARE[_w] = _b("meteor", "next", code=None)
for _w in ("frost", "frost date", "last frost", "first frost", "freeze date", "frost dates near me"):
    _BARE[_w] = _b("frost", "last" if _w.startswith("last") else "first" if _w.startswith("first") else "both")


def parse(q: str) -> dict | None:
    if not q or len(q) > 80:
        return None
    t = " ".join(q.lower().strip().rstrip("?!. ").replace(",", " ").split())
    t = _HERE.sub("", t)                                  # "sunset near me" is the bare question
    if t in _BARE:
        return dict(_BARE[t])
    sky = _parse_sky(t)
    if sky:
        return sky
    for rx, kind in ((_SUN, "sun"), (_SUN_VERB, "sun"), (_DAYLEN, "sun"), (_MOON_RS, "moon")):
        m = rx.match(t)
        if not m:
            continue
        g = m.groupdict()
        what = (g.get("what") or "daylength").replace(" ", "")
        if rx is _SUN and what in ("rise", "set") and not t.startswith(("sun rise", "sun set")) and "sun " not in t:
            return None                                   # "rise", "set in stone": not the sun
        what = {"rise": "sunrise", "comeup": "sunrise", "set": "sunset", "godown": "sunset"}.get(what, what)
        day = _DAY_OFF.get(g.get("day") or g.get("day2") or "", 0)
        return {"kind": kind, "what": what, "day": day, "place": (g.get("place") or "").strip() or None, "year": None}
    m = _TIDE.match(t)
    if m:
        g = m.groupdict()
        asked = t.startswith(("what time ", "which time ", "when ", "time of ")) or " next " in f" {t} "   # not "what is high tide"
        day = g.get("day") or g.get("day2")
        if not (asked or g.get("word") or day or g.get("place") or t != q.lower().strip().rstrip("?!. ") and _HERE.search(q.lower().strip().rstrip("?!. "))):
            return None                                   # "tide", "tides", "high tide", "low tide": searches
        what = "high" if g["what"].startswith("high") else "low" if g["what"].startswith("low") else "all"
        return {"kind": "tide", "what": what, "day": _DAY_OFF.get(day or "", 0), "place": (g.get("place") or "").strip() or None, "year": None}
    if _MOON_PHASE.match(t):
        return {"kind": "moon", "what": "phase", "day": 0, "place": None, "year": None}
    m = _MOON_NEXT.match(t)
    if m:
        return {"kind": "moon", "what": "next" + (m.group("which") or m.group("which2")), "day": 0, "place": None, "year": None}
    m = _SEASON.match(t)
    if m:
        g = m.groupdict()
        if g["s2"] and not t.startswith("when "):         # "summer start", "fall begins": names of things, not the question
            return None
        name = g["s1"] or g["s2"] or g["e"] or g["so"]
        return {"kind": "season", "what": _SEASON_IDX[name], "day": 0, "place": None, "year": int(g["year"]) if g["year"] else None}
    return None


# ---------------------------------------------------------------------------------------------- wording


def _clock(t: dt.datetime) -> str:
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'am' if t.hour < 12 else 'pm'}"


def _rounded(t: dt.datetime, tz) -> dt.datetime:
    return (t + dt.timedelta(seconds=30)).astimezone(tz).replace(second=0, microsecond=0)


def _rounded5(t: dt.datetime, tz) -> dt.datetime:
    """To the nearest five minutes: an approximate time is not shown to the minute."""
    lt = (t + dt.timedelta(seconds=150)).astimezone(tz).replace(second=0, microsecond=0)
    return lt.replace(minute=lt.minute - lt.minute % 5)


def _date(t: dt.datetime, year: bool = False) -> str:
    return f"{t.strftime('%A')}, {t.strftime('%B')} {t.day}" + (f", {t.year}" if year else "")


def _when(t: dt.datetime, tz, year: bool = False) -> str:
    lt = _rounded(t, tz)
    abbr = lt.tzname() or ""
    return f"{_date(lt, year)}, {_clock(lt)}" + (f" {abbr}" if abbr and not abbr.startswith(("+", "-")) else "")


def _short(t: dt.datetime) -> str:
    return f"{t.strftime('%a')} {t.strftime('%b')} {t.day}"


def _zone(t: dt.datetime) -> str:
    abbr = t.tzname() or ""
    return f" {abbr}" if abbr and not abbr.startswith(("+", "-")) else ""


def _span(seconds: float) -> str:
    m = int(round(seconds / 60))
    return f"{m // 60} h {m % 60:02d} min"


def _local_day(now: dt.datetime, tz, off: int) -> tuple[dt.datetime, dt.datetime]:
    d = now.astimezone(tz).date() + dt.timedelta(days=off)
    start = dt.datetime(d.year, d.month, d.day, tzinfo=tz)
    n = d + dt.timedelta(days=1)
    return start, dt.datetime(n.year, n.month, n.day, tzinfo=tz)


def _mins(t: dt.datetime, start: dt.datetime) -> float:
    """Minutes since the local day began, as elapsed (a 23- or 25-hour day keeps its true length)."""
    return round((t - start).total_seconds() / 60, 1)


_PHASE_DISC = {"New moon": (0.0, True), "First quarter": (0.5, True), "Full moon": (1.0, True), "Last quarter": (0.5, False)}


def _moon_draw(now: dt.datetime, lat, tz, nxt: list, facts: list, mark: str | None, at: dt.datetime | None = None) -> dict:
    """What a page draws for the moon: the disc (lit part 0-1, growing or not; south: the lit side is mirrored below
    the equator), the labelled times, and the coming phases as small discs."""
    elong, lit = moon_now(at or now)
    return {"kind": "moon", "disc": {"lit": round(lit, 3), "waxing": elong < 180.0, "south": lat is not None and lat < 0},
            "facts": facts, "mark": mark,
            "phases": [[n.replace(" moon", ""), _short(_rounded(t, tz)), _PHASE_DISC[n][0], _PHASE_DISC[n][1]] for n, t in nxt if n in _PHASE_DISC],
            "note": "Calculated on this device."}


def _phase_name(elong: float, today_event: str | None) -> str:
    if today_event:
        return today_event
    return ("Waxing crescent", "Waxing gibbous", "Waning gibbous", "Waning crescent")[int(elong // 90) % 4]


def sun_answer(p: dict, now: dt.datetime, lat: float, lon: float, tz, label: str | None) -> dict | None:
    start, end = _local_day(now, tz, p["day"])
    rises, sets, up0 = crossings(_sun_f(-0.833), lat, lon, start, end)
    dawn, dusk, _ = crossings(_sun_f(-6.0), lat, lon, start, end)
    day = _date(start) if not p["day"] else "Tomorrow, " + _date(start)
    title = f"{label} · {day}" if label else day
    c = lambda ts: _clock(_rounded(ts[0], tz)) if ts else None
    rise, sset = c(rises), c(sets)
    parts = []
    if not rises and not sets:
        ans = "The sun is up all day" if up0 else "The sun does not rise"
        length = 86400.0 if up0 else 0.0
    else:
        r0 = _rounded(rises[0], tz) if rises else start          # from the minutes shown, so the box adds up
        s0 = _rounded(sets[0], tz) if sets else end
        length = (s0 - r0).total_seconds()
        if sets and rises and sets[0] < rises[0]:                # high latitudes: it sets after midnight, then rises again
            length = (s0 - start).total_seconds() + (end - r0).total_seconds()
        ans = {"sunrise": f"Sunrise {rise}" if rise else "No sunrise: the sun is already up",
               "sunset": f"Sunset {sset}" if sset else "No sunset: the sun stays up",
               "daylength": f"{_span(length)} of daylight"}[p["what"]]
        if rise:
            parts.append(f"Sunrise {rise}")
        if sset:
            parts.append(f"sunset {sset}" if parts else f"Sunset {sset}")
    if p["what"] != "daylength" and 0 < length < 86400:
        parts.append(f"{_span(length)} of daylight")
    if dawn:
        parts.append(f"first light {c(dawn)}")
    if dusk:
        parts.append(f"last light {c(dusk)}")
    detail = " · ".join(parts)
    facts = ([["First light", c(dawn)]] if dawn else []) + ([["Sunrise", rise]] if rise else []) + ([["Sunset", sset]] if sset else []) \
        + ([["Last light", c(dusk)]] if dusk else []) + ([["Daylight", _span(length)]] if 0 < length < 86400 else [])
    path = None
    if len(rises) == 1 and len(sets) == 1 and rises[0] < sets[0]:      # the plain day; anything else is shown as facts only
        path = {"day": _mins(end, start), "rise": _mins(rises[0], start), "set": _mins(sets[0], start),
                "first": _mins(dawn[0], start) if dawn and dawn[0] < rises[0] else None,
                "last": _mins(dusk[0], start) if dusk and dusk[0] > sets[0] else None,
                "now": _mins(now, start) if start <= now < end else None,
                "rise_label": rise, "set_label": sset, "span": f"{_span(length)} of daylight"}
    draw = {"kind": "sun", "icon": p["what"] if p["what"] in ("sunrise", "sunset") else "sun", "path": path, "facts": facts,
            "mark": {"sunrise": "Sunrise", "sunset": "Sunset", "daylength": "Daylight"}[p["what"]], "note": "Calculated on this device."}
    return {"answer": ans, "title": title, "detail": (detail + ". " if detail else "") + "Calculated on this device.", "question": "sun",
            "draw": draw}


def moon_answer(p: dict, now: dt.datetime, lat, lon, tz, label: str | None) -> dict | None:
    nxt = phases_after(now, 4)
    if p["what"] in ("nextfull", "nextnew"):
        name = "Full moon" if p["what"] == "nextfull" else "New moon"
        t = next(t for n, t in phases_after(now, 8) if n == name)
        days = (_rounded(t, tz).date() - now.astimezone(tz).date()).days
        ago = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
        return {"answer": _when(t, tz, year=t.astimezone(tz).year != now.astimezone(tz).year), "title": f"Next {name.lower()} ({ago})",
                "detail": "Then: " + " · ".join(f"{n.lower()} {_short(_rounded(x, tz))}" for n, x in nxt if n != name) + ". Calculated on this device.",
                "question": "moon", "draw": _moon_draw(now, lat, tz, nxt, [], None)}
    start, end = _local_day(now, tz, p["day"])
    rs = ""
    mfacts = []
    if lat is not None and lon is not None:
        rises, sets, _up = crossings(_moon_f, lat, lon, start, end)
        mfacts = ([["Moonrise", _clock(_rounded(rises[0], tz))]] if rises else []) + ([["Moonset", _clock(_rounded(sets[0], tz))]] if sets else [])
        bits = ([f"moonrise {_clock(_rounded(rises[0], tz))}"] if rises else []) + ([f"moonset {_clock(_rounded(sets[0], tz))}"] if sets else [])
        if p["what"] in ("moonrise", "moonset"):
            got = rises if p["what"] == "moonrise" else sets
            word = "Moonrise" if p["what"] == "moonrise" else "Moonset"
            day = _date(start) if not p["day"] else "Tomorrow, " + _date(start)
            elong, lit = moon_now(start + (end - start) / 2)
            return {"answer": f"{word} {_clock(_rounded(got[0], tz))}" if got else f"No {word.lower()} on this day",
                    "title": f"{label} · {day}" if label else day,
                    "detail": " · ".join(b[0].upper() + b[1:] if i == 0 else b for i, b in enumerate(bits)) + (". " if bits else "")
                              + f"{_phase_name(elong, None)}, {round(lit * 100)}% lit. Calculated on this device.",
                    "question": "moon",
                    "draw": _moon_draw(now, lat, tz, nxt, ([["Moonrise", _clock(_rounded(rises[0], tz))]] if rises else [])
                                       + ([["Moonset", _clock(_rounded(sets[0], tz))]] if sets else []), word, at=start + (end - start) / 2)}
        rs = (" " + " · ".join(bits).capitalize() + ".") if bits else ""
    elif p["what"] in ("moonrise", "moonset"):
        return None
    elong, lit = moon_now(now)
    today = next((n for n, t in _around(now) if start <= t < end), None)
    first = _rounded(nxt[0][1], tz)
    return {"answer": f"{_phase_name(elong, today)}, {round(lit * 100)}% lit", "title": "Moon phase · " + _date(now.astimezone(tz)),
            "detail": f"{nxt[0][0]} {_short(first)}, {_clock(first)}{_zone(first)} · "
                      + " · ".join(f"{n.lower()} {_short(_rounded(t, tz))}" for n, t in nxt[1:]) + "." + rs,
            "question": "moon", "draw": _moon_draw(now, lat, tz, nxt, mfacts, None)}


def _around(now: dt.datetime) -> list[tuple[str, dt.datetime]]:
    """The principal phases from two days before now on (a phase earlier today is still today's phase)."""
    return phases_after(now - dt.timedelta(days=2), 2)


def _feet(v: float) -> str:
    r = round(v, 1)
    return f"{'−' if r < 0 else ''}{abs(r):.1f} ft"


def tide_answer(p: dict, now: dt.datetime, events: list, tz, label: str | None, station: dict, km: float) -> dict | None:
    """events: (utc time, "H"|"L", feet above the low-water datum), in time order, covering the day asked and the next."""
    start, end = _local_day(now, tz, p["day"])
    day = [(t, k, h) for t, k, h in events if start <= t < end]
    if not day:
        return None
    want = {"high": "H", "low": "L"}.get(p["what"])
    after = start if p["day"] else now
    nxt = next(((t, k, h) for t, k, h in events if t >= after and (want is None or k == want)), None)
    if nxt is None:
        return None
    if not p["day"] and not any(t >= now for t, _k, _h in day):  # late evening, today's tides are over: show the day
        start, end = start + dt.timedelta(days=1), end + dt.timedelta(days=1)      # the next one falls on
        if nxt[0] >= end:
            return None
        day = [(t, k, h) for t, k, h in events if start <= t < end]
    approx = bool(station.get("approximate"))                    # times 6 to 13 minutes from NOAA's own: "about"
    clock = (lambda t: _clock(_rounded5(t, tz))) if approx else (lambda t: _clock(_rounded(t, tz)))
    lt = _rounded(nxt[0], tz)
    word = "High tide" if nxt[1] == "H" else "Low tide"
    other = "" if lt.date() == start.date() else f" {lt.strftime('%a')}"
    miles = km / 1.609344
    where = f"{station['name']} ({miles:.0f} mi)" if miles >= 1 else station["name"]
    title = f"{label} · " if label else ""
    title += ("Tomorrow, " if p["day"] else "") + f"{start.strftime('%a')} {start.strftime('%b')} {start.day} · NOAA station {where}"
    row = " · ".join(f"{'High' if k == 'H' else 'Low'} {clock(t)} {_feet(h)}" for t, k, h in day[:5])
    tail = ". Calculated here from NOAA constants; not for navigation."
    if approx:
        tail = ". Times are approximate; not for navigation."
    if len(row) + len(tail) > 160:                               # five events in a day: the page shows 160 characters
        tail = ". Approximate; not for navigation." if approx else ". Not for navigation."
    rt = (lambda t: _rounded5(t, tz)) if approx else (lambda t: _rounded(t, tz))    # the curve uses the times shown
    before = [e for e in events if e[0] < start][-1:]
    after = [e for e in events if e[0] >= end][:1]
    curve = None
    if before and after and len(day) >= 2:                       # a drawing needs the tide on both sides of the day
        ticks = []
        for h, name in ((0, "midnight"), (6, "6 am"), (12, "noon"), (18, "6 pm")):
            ticks.append([_mins(start.replace(hour=h), start), name])
        curve = {"day": _mins(end, start), "ticks": ticks + [[_mins(end, start), "midnight"]],
                 "events": [[_mins(rt(t), start), round(h, 2), k] for t, k, h in before + day + after],
                 "now": _mins(now, start) if start <= now < end else None}
    last = [e for e in events if e[0] <= now][-1:]
    draw = {"kind": "tide", "curve": curve, "approx": approx,
            "trend": (None if not (start <= now < end) or not last else "rising" if last[0][1] == "L" else "falling"),
            "facts": [["High" if k == "H" else "Low", clock(t), _feet(h)] for t, k, h in day[:5]],
            "mark": next((i for i, e in enumerate(day[:5]) if e[0] == nxt[0]), None),
            "note": f"NOAA station {where}" + tail}
    return {"draw": draw, "answer": f"{word} {'about ' if approx else ''}{clock(nxt[0])}{other}, {_feet(nxt[2])}", "title": title, "detail": row + tail, "question": "tide"}


def season_answer(p: dict, now: dt.datetime, tz) -> dict | None:
    i = p["what"]
    year = p["year"]
    if year is None:                                             # the next one, this year's or the following
        year = now.year
        t = season_time(i, year)
        if t is not None and t.astimezone(tz).date() < now.astimezone(tz).date():
            year += 1
    t = season_time(i, year)
    if t is None:
        return None
    name, event = _SEASON_NAMES[i]
    today = now.astimezone(tz).date()
    coming = sorted((x, j) for y in (now.year, now.year + 1) for j in range(4) for x in [season_time(j, y)]
                    if x is not None and x.astimezone(tz).date() >= today)[:4]
    if (t, i) not in coming:                                     # a year named, or far ahead: that year's four
        coming = sorted((x, j) for j in range(4) for x in [season_time(j, year)] if x is not None)
    draw = {"kind": "season", "seasons": [[_SEASON_NAMES[j][0].capitalize(), f"{x.astimezone(tz).strftime('%b')} {x.astimezone(tz).day}", x == t]
                                          for x, j in coming],
            "note": "Northern Hemisphere. Times from the U.S. Naval Observatory."}
    return {"answer": _when(t, tz, year=True), "title": f"First day of {name} {year} ({event})",
            "detail": "Northern Hemisphere. Times from the U.S. Naval Observatory.", "question": "season", "draw": draw}


# ---------------------------------------------------------------------------------------------- eclipses, showers, frost
# Phase 3. Two tables ship beside this file, each from one authority and each compared with a second copy of it in
# tests/unit/test-search-almanac-sky.sh (fixtures under tests/unit/fixtures/):
#   almanac_sky.json.gz    eclipses 2024-2050: NASA GSFC Five Millennium Catalogs of Solar and Lunar Eclipses (Espenak and
#                          Meeus); meteor showers: the International Meteor Organization's calendars, Table 5 (Working List
#                          of Visual Meteor Showers), one table per year, its dates accurate only for that year
#   frost_normals.tsv.gz   NOAA NCEI 1991-2020 U.S. Climate Normals, freeze probability dates per weather station
# What is not in them is not answered: no eclipse outside 2024-2050, no shower date in a year with no IMO table, no frost date
# for a town with no NOAA station in reach, and no "is the eclipse visible in <place>" (NASA's regions are shown as NASA
# writes them; a place's own circumstances are not calculated).
SKY_DATA = os.environ.get("SEARCH_ALMANAC_SKY", os.path.join(os.path.dirname(os.path.abspath(__file__)), "almanac_sky.json.gz"))
FROST_DATA = os.environ.get("SEARCH_FROST_DATA", os.path.join(os.path.dirname(os.path.abspath(__file__)), "frost_normals.tsv.gz"))
FROST_MAX_KM = float(os.environ.get("SEARCH_FROST_MAX_KM", "40"))
_cache: dict = {}


def sky_data() -> dict | None:
    if "sky" not in _cache:
        try:
            with gzip.open(SKY_DATA, "rt", encoding="utf-8") as f:
                _cache["sky"] = json.load(f)
        except (OSError, ValueError):                            # an older image, or no file: no such answers
            _cache["sky"] = None
    return _cache["sky"]


def _km(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    pa, pb = _R(lat_a), _R(lat_b)
    h = math.sin((pb - pa) / 2) ** 2 + math.cos(pa) * math.cos(pb) * math.sin(_R(lon_b - lon_a) / 2) ** 2
    return 12742.0 * math.asin(math.sqrt(h))


_FROST_COLS = ("l10", "l50", "l90", "f10", "f50", "f90", "l28", "f28", "gsl")


def frost_stations() -> list | None:
    """[{id, name, state, lat, lon, elev, l10 ... gsl}] from frost_normals.tsv.gz, or None without the file."""
    if "frost" not in _cache:
        try:
            rows = []
            with gzip.open(FROST_DATA, "rt", encoding="utf-8") as f:
                for line in f:
                    c = line.rstrip("\n").split("\t")
                    if len(c) != 6 + len(_FROST_COLS):
                        continue
                    rows.append({"id": c[0], "name": c[1], "state": c[2], "lat": float(c[3]), "lon": float(c[4]), "elev": int(c[5]),
                                 **{k: c[6 + i] or None for i, k in enumerate(_FROST_COLS)}})
            _cache["frost"] = rows or None
        except (OSError, ValueError):
            _cache["frost"] = None
    return _cache["frost"]


def nearest_frost_station(lat: float, lon: float, max_km: float | None = None) -> tuple[dict, float] | None:
    """The closest NOAA station that has its 32 F dates, and how far away it is; None beyond max_km."""
    best, best_km = None, None
    for s in frost_stations() or []:
        if abs(s["lat"] - lat) > 1.0:                           # a degree of latitude is 111 km: past any cap
            continue
        d = _km(lat, lon, s["lat"], s["lon"])
        if best_km is None or d < best_km:
            best, best_km = s, d
    limit = FROST_MAX_KM if max_km is None else max_km
    return (best, best_km) if best is not None and best_km <= limit else None


_ECL_T = r"(?:total|annular|partial|hybrid|penumbral)"
_ECL = re.compile(r"^(?:(?P<q>(?:when\s+(?:is|will|does)|what\s+(?:day|date)\s+is)\s+(?:the\s+)?)|(?P<there>(?:is\s+there|will\s+there\s+be)\s+an?\s+))?"
                  r"(?P<next>(?:next|upcoming)\s+)?(?:(?P<t1>" + _ECL_T + r")\s+)?(?:(?P<k1>solar|lunar)\s+)?(?:(?P<t2>" + _ECL_T + r")\s+)?eclipses?"
                  r"(?:\s+(?:in\s+|for\s+)?(?P<when>20\d\d|this\s+year|today|tonight))?$")
_ECL_YEAR_FIRST = re.compile(r"^(?P<when>20\d\d)\s+(?:(?P<t1>" + _ECL_T + r")\s+)?(?P<k1>solar|lunar)\s+eclipses?$")
_SHOWERS = {"perseid": "PER", "geminid": "GEM", "quadrantid": "QUA", "lyrid": "LYR", "april lyrid": "LYR", "orionid": "ORI", "leonid": "LEO",
            "draconid": "DRA", "ursid": "URS", "eta aquarid": "ETA", "eta aquariid": "ETA", "southern delta aquarid": "SDA",
            "southern delta aquariid": "SDA"}
_METEOR = re.compile(r"^(?P<q>(?:when\s+(?:is|are|do|does|will)\s+)?(?:the\s+)?)(?P<name>" + "|".join(sorted(_SHOWERS, key=len, reverse=True)) + r")s?"
                     r"(?P<ms>\s+meteor\s+showers?)?(?P<pk>\s+peaks?)?(?:\s+(?:in\s+)?(?P<when>20\d\d|this\s+year))?$")
_METEOR_ANY = re.compile(r"^(?P<q>is\s+there\s+(?:a\s+)?|when\s+(?:is|are)\s+(?:the\s+)?)?(?P<next>next\s+)?meteor\s+showers?"
                         r"(?:\s+(?P<when>tonight|today|this\s+year|20\d\d|calendar(?:\s+20\d\d)?))?$")
_FROST = re.compile(r"^(?P<q>(?:what|when)\s+(?:is|are|was)\s+(?:the\s+)?)?(?:(?:average|avg|typical|expected|usual)\s+)?"
                    r"(?:(?P<which>last|first)\s+(?:(?P<season>spring|fall|autumn)\s+)?(?:killing\s+)?(?:frost|freeze)s?(?P<date>\s+dates?)?|(?P<both>(?:frost|freeze)\s+dates?))"
                    r"(?:\s+(?:in|at|for|near)\s+(?P<place>[a-z][a-z .'-]{1,40}?))?$")


def _parse_sky(t: str) -> dict | None:
    """The eclipse, meteor-shower and frost questions (t is the question lower-cased and trimmed)."""
    m = _ECL.match(t) or _ECL_YEAR_FIRST.match(t)
    if m:
        g = m.groupdict()
        t1, t2 = g.get("t1"), g.get("t2")
        asked = bool(g.get("q") or g.get("there") or g.get("next") or g.get("when"))
        if not asked or (t1 and t2):                            # "eclipse", "total eclipse", "solar eclipse": names of things
            return None
        when = g.get("when")
        return {"kind": "eclipse", "what": g.get("k1") or "any", "type": t1 or t2, "day": 0, "place": None,
                "year": int(when) if when and when.isdigit() else None,
                "span": when if when in ("today", "tonight", "this year") else None}
    m = _METEOR.match(t)
    if m and ("when" in m.group("q") or m.group("ms") or m.group("pk") or m.group("when")):    # "the perseids": a name
        w = m.group("when")
        return {"kind": "meteor", "what": "shower", "code": _SHOWERS[m.group("name")], "day": 0, "place": None,
                "year": int(w) if w and w.isdigit() else None}
    m = _METEOR_ANY.match(t)
    if m and (m.group("q") or m.group("next") or m.group("when")):
        w = m.group("when") or ""
        year = re.search(r"20\d\d", w)
        what = "year" if year or "calendar" in w or w == "this year" else "next" if m.group("next") or not w else "tonight"
        return {"kind": "meteor", "what": what, "code": None, "day": 0, "place": None, "year": int(year.group()) if year else None}
    m = _FROST.match(t)
    if m:
        g = m.groupdict()
        if not (g["q"] or g["date"] or g["both"] or g["place"]):
            return None                                         # "last frost", "first frost": a band, a game, a poem
        which = g["which"] or "both"
        if (which == "last" and g["season"] in ("fall", "autumn")) or (which == "first" and g["season"] == "spring"):
            return None
        return {"kind": "frost", "what": which, "day": 0, "place": (g["place"] or "").strip() or None, "year": None}
    return None


# ---- eclipses
_ECL_KIND = {"S": "solar", "L": "lunar"}
_ECL_TYPE = {"T": "Total", "A": "Annular", "H": "Hybrid", "P": "Partial", "N": "Penumbral"}
_ECL_NOTE = "Eclipse Predictions by Fred Espenak (NASA's GSFC). Regions are as NASA names them; what a town sees is not calculated here."


def eclipse_time(row: dict) -> dt.datetime:
    """Greatest eclipse in UT: the catalog's time is TD, and each row carries its own delta T."""
    y, m, d = map(int, row["d"].split("-"))
    h, mi, s = map(int, row["td"].split(":"))
    return dt.datetime(y, m, d, tzinfo=UTC) + dt.timedelta(hours=h, minutes=mi, seconds=s - row["dt"])


def _ecl_name(r: dict) -> str:
    return f"{_ECL_TYPE[r['t']].lower()} {_ECL_KIND[r['k']]} eclipse"


def _hm(minutes: float) -> str:
    m = int(round(minutes))
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def _fit(parts: list[str], limit: int = 160) -> str:
    """Whole sentences, dropped from the end until the line fits the 160 characters the page shows."""
    parts = [x for x in parts if x]
    while parts and len(" ".join(parts)) > limit:
        parts = parts[:-1]
    return " ".join(parts)


def _fit_list(items: list[str], limit: int = 140) -> str:
    """As many of the items as fit, joined by semicolons."""
    out = ""
    for x in items:
        if len(out) + len(x) + 2 > limit:
            break
        out += ("; " if out else "") + x
    return out


def _ecl_facts(r: dict, t: dt.datetime, tz) -> list:
    lt = _rounded(t, tz)
    f = [["Type", _ECL_TYPE[r["t"]]], ["Greatest", _clock(lt) + _zone(lt)]]
    if r["k"] == "S":
        f.append(["Magnitude", f"{r['mag']:.3f}"])
        if r.get("dur"):
            f.append(["Longest", f"{int(r['dur'][:2])} min {int(r['dur'][3:5])} s"])
        if r.get("width"):
            f.append(["Path width", f"{r['width']} km"])
    else:
        f.append(["Penumbral mag.", f"{r['pen']:.3f}"] if r["t"] == "N" else ["Umbral mag.", f"{r['mag']:.3f}"])
        if r.get("par"):
            f.append(["Partial phase", _hm(r["par"])])
        if r.get("tot"):
            f.append(["Totality", _hm(r["tot"])])
    return f[:6]


def _ecl_card(r: dict, t: dt.datetime, tz, after: list) -> tuple[dict, str]:
    lt = _rounded(t, tz)
    seen = f"Seen from {r['region']}." if r.get("region") else ""
    path = ""
    if r["k"] == "S" and r["t"] != "P" and r.get("path"):
        path = f"{_ECL_TYPE[r['t']]} along {r['path']}." if r["t"] != "H" else f"Central along {r['path']}."
    detail = _fit([f"Greatest eclipse {_clock(lt)}{_zone(lt)}.", seen, path, "NASA."])
    draw = {"kind": "eclipse", "icon": _ECL_KIND[r["k"]], "facts": _ecl_facts(r, t, tz), "region": r.get("region", ""), "path": r.get("path", ""),
            "next": [[_short(_rounded(x, tz)), f"{_ECL_TYPE[y['t']]} {_ECL_KIND[y['k']]}"] for x, y in after], "note": _ECL_NOTE}
    return draw, detail


def _in_days(days: int) -> str:
    return ("today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days" if days < 100
            else f"in {round(days / 30.4)} months" if days < 700 else f"in {days / 365.25:.1f} years")


def eclipse_answer(p: dict, now: dt.datetime, tz) -> dict | None:
    data = sky_data()
    if not data:
        return None
    rows = sorted(((eclipse_time(r), r) for r in data["eclipses"]), key=lambda x: x[0])
    first, last = rows[0][0].year, rows[-1][0].year
    want = lambda r: (p["what"] == "any" or _ECL_KIND[r["k"]] == p["what"]) and (not p["type"] or _ECL_TYPE[r["t"]].lower() == p["type"])
    today = now.astimezone(tz).date()
    label = " ".join(x for x in (p["type"], None if p["what"] == "any" else p["what"], "eclipse") if x)
    after = lambda t: [(x, y) for x, y in rows if x > t and want(y)][:4]

    def one(t, r, head):
        draw, detail = _ecl_card(r, t, tz, after(t))
        lt = _rounded(t, tz)
        return {"answer": _when(t, tz, year=True), "title": f"{head} {_ecl_name(r)}" + ("" if head in ("Today:", "Tonight:") else f" ({_in_days((lt.date() - today).days)})"),
                "detail": detail, "question": "eclipse", "draw": draw}

    year = p["year"] or (today.year if p["span"] == "this year" else None)
    if p["span"] in ("today", "tonight"):
        if p["span"] == "today":
            hit = [(t, r) for t, r in rows if want(r) and t.astimezone(tz).date() == today]
        else:                                                    # tonight: from 6 pm to 6 am, so one at 12:12 am is tonight's
            local = now.astimezone(tz)
            begin = local.replace(hour=18, minute=0, second=0, microsecond=0) - dt.timedelta(days=1 if local.hour < 6 else 0)
            hit = [(t, r) for t, r in rows if want(r) and begin <= t < begin + dt.timedelta(hours=12)]
        if hit:
            out = one(*hit[0], "Today:" if p["span"] == "today" else "Tonight:")
            out["answer"] = f"Yes: {_ecl_name(hit[0][1])} {p['span']}, greatest {_clock(_rounded(hit[0][0], tz))}{_zone(_rounded(hit[0][0], tz))}"
            return out
        nxt = next(((t, r) for t, r in rows if t > now and want(r)), None)
        if nxt is None:
            return None
        out = one(*nxt, "Next:")
        out["answer"] = f"No {label} {p['span']}"
        out["detail"] = _fit([f"Next: {_when(nxt[0], tz, year=True)}.", out["detail"]])
        return out
    if year is not None:
        if not first <= year <= last:
            return None
        hit = [(t, r) for t, r in rows if want(r) and t.astimezone(tz).year == year]
        if not hit:
            return {"answer": f"No {label} in {year}", "title": f"{label.capitalize()}s in {year}", "question": "eclipse",
                    "detail": "NASA's Five Millennium Catalog lists none.", "draw": {"kind": "eclipse", "icon": "lunar" if p["what"] == "lunar" else "solar",
                                                                                     "facts": [], "next": [], "note": _ECL_NOTE}}
        names = [f"{_short(_rounded(t, tz))} {_ECL_TYPE[r['t']].lower()}" + (f" {_ECL_KIND[r['k']]}" if p["what"] == "any" else "") for t, r in hit]
        draw, detail = _ecl_card(hit[0][1], hit[0][0], tz, [])
        draw["facts"] = []
        draw["next"] = [[_short(_rounded(t, tz)), f"{_ECL_TYPE[r['t']]} {_ECL_KIND[r['k']]}"] for t, r in hit][:6]
        draw["region"] = draw["path"] = ""
        head = f"{len(hit)} {label}{'s' if len(hit) != 1 else ''} in {year}: "
        text = head + _fit_list(names, 160 - len(head))
        return {"answer": text, "title": f"{label.capitalize()}s in {year}",
                "detail": f"First: {_fit([_when(hit[0][0], tz)])}. Times and regions from NASA.", "question": "eclipse", "draw": draw}
    nxt = next(((t, r) for t, r in rows if t > now and want(r)), None)
    return one(*nxt, "Next") if nxt else None


# ---- meteor showers
def _md(s: str, year: int = 2001) -> dt.date:
    return dt.date(year, int(s[:2]), int(s[3:5]))


def _major(s: dict) -> bool:
    z = s["zhr"].rstrip("+")
    return z.isdigit() and int(z) >= 10 and not s["name"].startswith("Daytime") and not s.get("ref_only")


def _active(s: dict, day: dt.date) -> bool:
    a, b = _md(s["from"], day.year), _md(s["to"], day.year)
    return a <= day <= b if a <= b else day >= a or day <= b      # December into January


def _zhr(s: dict) -> str:
    return s["zhr"] if s["zhr"].rstrip("+").isdigit() else "varies"


def _md_short(s: str) -> str:
    d = _md(s)
    return f"{d.strftime('%b')} {d.day}"


_METEOR_NOTE = ("Dates are UT, from the International Meteor Organization's calendar. ZHR is the most an observer sees under perfectly dark "
                "skies with the radiant overhead.")


def _shower_card(s: dict, peak: dt.date, upcoming: list) -> tuple[dict, int]:
    # the peak night begins the evening before its UT date: the moon as at noon UT the day before (the American Meteor
    # Society's figure for the same nights is within 3 points; tests/unit/test-search-almanac-sky.sh)
    lit = round(moon_now(dt.datetime(peak.year, peak.month, peak.day, tzinfo=UTC) - dt.timedelta(hours=12))[1] * 100)
    facts = [["Peak", _md_short(s["max"])], ["ZHR", _zhr(s)], ["Active", f"{_md_short(s['from'])} – {_md_short(s['to'])}"],
             ["Moon", f"about {lit}% lit"], ["Speed", f"{s['v']} km/s"]]
    return {"kind": "meteor", "facts": facts, "mark": "Peak", "next": upcoming, "note": _METEOR_NOTE}, lit


def _upcoming(tables: dict, since: dt.date, n: int = 4, skip: str | None = None) -> list:
    out = sorted(((_md(s["max"], int(y)), s) for y, tbl in tables.items() for s in tbl if _major(s) and s["code"] != skip), key=lambda x: x[0])
    return [[s["name"], _md_short(s["max"]), _zhr(s)] for d, s in out if d >= since][:n]


def _shower(s: dict, y: int, today: dt.date, tables: dict, head: str | None = None) -> dict:
    peak = _md(s["max"], y)
    card, lit = _shower_card(s, peak, _upcoming(tables, peak + dt.timedelta(days=1), 4, s["code"]))
    days = (peak - today).days
    ago = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days" if days > 1 else f"{-days} days ago"
    return {"answer": f"{peak.strftime('%A')}, {peak.strftime('%B')} {peak.day}, {peak.year} (UT)", "title": f"{s['name']} meteor shower peak ({ago})",
            "detail": _fit([f"Active {_md_short(s['from'])} – {_md_short(s['to'])}.", f"Moon about {lit}% lit around the peak.",
                            f"ZHR {_zhr(s)}: the most an observer sees under perfect skies.", "IMO date, UT."]),
            "question": "meteor", "draw": card}


def meteor_answer(p: dict, now: dt.datetime, tz) -> dict | None:
    data = sky_data()
    if not data or not data.get("showers"):
        return None
    tables = data["showers"]
    today = now.astimezone(tz).date()
    if p["what"] == "shower":
        for y in ([p["year"]] if p["year"] else [today.year, today.year + 1]):
            s = next((s for s in tables.get(str(y), []) if s["code"] == p["code"]), None)
            if s is not None and (p["year"] or _md(s["max"], y) >= today):
                return _shower(s, y, today, tables)
        return None
    if p["what"] == "year":
        y = p["year"] or today.year
        tbl = tables.get(str(y))
        if not tbl:
            return None
        row = [[s["name"], _md_short(s["max"]), _zhr(s)] for s in sorted((s for s in tbl if _major(s)), key=lambda s: _md(s["max"], y))]
        return {"answer": f"{len(row)} major meteor showers in {y}", "title": f"Meteor showers {y}: peak dates (UT) and ZHR",
                "detail": _fit_list([f"{n} {d}" for n, d, _z in row]) + " (UT, IMO).", "question": "meteor",
                "draw": {"kind": "meteor", "facts": [], "next": row[:12], "mark": None,
                         "note": "Major showers: ZHR 10 or more, not daytime. " + _METEOR_NOTE}}
    peaks = sorted(((_md(s["max"], int(y)), s, int(y)) for y, tbl in tables.items() for s in tbl if _major(s)), key=lambda x: x[0])
    nxt = next(((d, s, y) for d, s, y in peaks if d >= today), None)
    if p["what"] == "next":
        return _shower(nxt[1], nxt[2], today, tables) if nxt else None
    tbl = tables.get(str(today.year))
    if not tbl or nxt is None:
        return None                                              # no calendar for this year: no "tonight"
    # Table 5 gives a date in UT and no hour (an hour is in some showers' own paragraphs, not in the table): a peak date
    # within a day of the local date is "around the peak", and nothing finer is claimed
    near = next(((d, s, y) for d, s, y in peaks if abs((d - today).days) <= 1), None)
    if near:
        d, s, y = near
        out = _shower(s, y, today, tables)
        out["answer"] = f"{s['name']}: peak {_md_short(s['max'])} (UT), within a day of tonight"
        out["title"] = f"{s['name']} meteor shower · ZHR {_zhr(s)}"
        return out
    act = [s for s in tbl if _major(s) and _active(s, today)]
    d, s, y = nxt
    words = (", ".join(f"{a['name']} (peak {_md_short(a['max'])})" for a in act) + " active.") if act else ""
    return {"answer": "No major meteor shower peaks tonight", "title": f"Next peak: {s['name']}, {_MONTHS[d.month - 1]} {d.day}",
            "detail": _fit([words, f"Next peak: {s['name']} {_md_short(s['max'])} (UT), ZHR {_zhr(s)}."]), "question": "meteor",
            "draw": {"kind": "meteor", "facts": [["Tonight", "no major peak"]], "mark": None, "next": _upcoming(tables, today, 4),
                     "note": "Major showers: ZHR 10 or more, not daytime. " + _METEOR_NOTE}}


# ---- frost
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")


def _fd(s: str) -> str:
    return f"{_MONTHS[int(s[:2]) - 1]} {int(s[3:5])}"


def _fs(s: str) -> str:
    return f"{_MONTHS[int(s[:2]) - 1][:3]} {int(s[3:5])}"


def _doy(s: str | None) -> int | None:
    return dt.date(2001, int(s[:2]), int(s[3:5])).timetuple().tm_yday if s else None


def frost_answer(p: dict, station: dict, km: float, label: str | None) -> dict | None:
    """NOAA's meaning of the dates: l10 is the date on or after which there is a 10% chance of the last spring freeze (32 F
    or colder); f10 the date on or before which there is a 10% chance of the first fall freeze."""
    s = station
    if not (s.get("l50") and s.get("f50")):
        return None
    miles = km / 1.609344
    where = f"{s['name']}, {s['state']} ({miles:.0f} mi)" if miles >= 1 else f"{s['name']}, {s['state']}"
    title = (f"{label} · " if label else "") + f"32°F freeze · NOAA station {where}"
    last = f"In half of years there is still a freeze on or after {_fd(s['l50'])}" + (f", in 1 year in 10 on or after {_fs(s['l10'])}" if s.get("l10") else "") + "."
    first = f"In half of years the first freeze comes on or before {_fd(s['f50'])}" + (f", in 1 year in 10 on or before {_fs(s['f10'])}" if s.get("f10") else "") + "."
    src = "NOAA 1991-2020 normals."
    if p["what"] == "last":
        ans, detail = f"Last spring freeze about {_fd(s['l50'])}", _fit([last, src])
    elif p["what"] == "first":
        ans, detail = f"First fall freeze about {_fd(s['f50'])}", _fit([first, src])
    else:
        calendar = _doy(s["l50"]) < _doy(s["f50"])               # a warm station's first freeze can come before its last
        ans = (f"Last freeze about {_fs(s['l50'])}, first about {_fs(s['f50'])}" if calendar
               else f"First freeze about {_fs(s['f50'])}, last about {_fs(s['l50'])}")
        detail = _fit([last, first, src] if calendar else [first, last, src])
    facts = [["Last spring", _fs(s["l50"])], ["First fall", _fs(s["f50"])]]
    if s.get("gsl"):
        facts.append(["Freeze-free", f"{s['gsl']} days"])
    if s.get("l28") and s.get("f28"):
        facts += [["Last at 28°F", _fs(s["l28"])], ["First at 28°F", _fs(s["f28"])]]
    def bar(a, b, c):                                            # None when the dates run over New Year (a warm station)
        days = [_doy(a), _doy(b), _doy(c)]
        ok = all(x is not None for x in days) and days == sorted(days)
        return {"days": days, "labels": [_fs(x) if x else "" for x in (a, b, c)]} if ok else None
    s_bars = bool(bar(s.get("l90"), s["l50"], s.get("l10")) and bar(s.get("f10"), s["f50"], s.get("f90")))
    draw = {"kind": "frost", "mark": "Last spring" if p["what"] == "last" else "First fall" if p["what"] == "first" else None, "facts": facts,
            "last": bar(s.get("l90"), s["l50"], s.get("l10")), "first": bar(s.get("f10"), s["f50"], s.get("f90")),
            "note": f"NOAA station {where}. 1991-2020 U.S. Climate Normals: the 50% date" + (", with the 10% and 90% dates as the bars' ends" if s_bars else "") + ". Your own yard can differ."}
    return {"answer": ans, "title": title, "detail": detail, "question": "frost", "draw": draw}


def answer(p: dict, now: dt.datetime | None = None, lat: float | None = None, lon: float | None = None, tz=None,
           label: str | None = None) -> dict | None:
    """The answer box for a parsed question. tz: a tzinfo (None = this computer's zone)."""
    now = now or dt.datetime.now(UTC)
    tz = tz or now.astimezone().tzinfo
    try:
        if p["kind"] == "season":
            return season_answer(p, now, tz)
        if p["kind"] == "eclipse":
            return eclipse_answer(p, now, tz)
        if p["kind"] == "meteor":
            return meteor_answer(p, now, tz)
        if p["kind"] == "frost":
            return None                                          # needs a station: search_api picks it (frost_answer)
        if p["kind"] == "moon":
            return moon_answer(p, now, lat, lon, tz, label)
        if lat is None or lon is None:
            return None
        return sun_answer(p, now, lat, lon, tz, label)
    except Exception:                                            # an answer is extra: never fail the search over it
        return None
