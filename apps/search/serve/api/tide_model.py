"""Tide high/low predictions computed locally from NOAA harmonic constants.

The module is pure: no network access and no file reads except the data file handed to ``load``.

Method (the one measured to agree with NOAA CO-OPS' own published high/low predictions):

* Height above MLLW is  Z0 + sum over constituents of  f * A * cos(V + u - kappa)  where
    A      is the amplitude from NOAA's harmonic constants (feet),
    kappa  is NOAA's "phase_GMT" (degrees, referenced to UTC),
    V      is the equilibrium argument of the constituent at the time of prediction,
    f, u   are the node factor and nodal phase correction, computed for the MIDDLE OF THE CALENDAR YEAR of the
           prediction (NOAA's convention), so a window crossing New Year uses each year's own factors,
    Z0     is MSL - MLLW from the station datums (the constants are about mean sea level).
* V is built from the astronomical mean longitudes (moon s, sun h, lunar perigee p, lunar node N, solar perigee p1)
  and the mean solar hour angle T, with the integer multipliers of each constituent (Schureman 1958 / IHO tables).
* Highs and lows are found where the analytic time derivative changes sign, refined by bisection.
* Subordinate stations apply NOAA's time offsets (minutes) and height offsets (ratio "R" or additive feet "F") to
  the highs and lows of their reference harmonic station.

All times are UTC.  Heights are feet above MLLW.
"""

import gzip
import json
import math
from datetime import datetime, timedelta, timezone

UTC = timezone.utc

# Default for the near-flat wiggle merge.  Measured against NOAA over 40 harmonic stations and two 30-day windows:
#   0.00 ft: 0 missing, 67 extra events (all in diurnal/mixed tides, every one within 0.08 ft of a neighbour)
#   0.02 ft: 22 missing, 15 extra      0.05 ft: 82 missing, 13 extra      0.10 ft: 152 missing, 1 extra
# NOAA itself lists many tiny diurnal wiggles, so merging trades "extra" events for "missing" ones one for one.
# A card that should not show hundredth-of-a-foot wiggles uses 0.1; a strict NOAA comparison uses 0.0.
DEFAULT_MIN_SWING_FT = 0.1

EARTH_RADIUS_KM = 6371.0088
SAMPLE_STEP_HOURS = 0.1        # derivative is sampled every 6 minutes, then refined by bisection
BISECTION_STEPS = 24
SEARCH_PADDING = timedelta(days=1)          # extra raw events either side so merging/offsets see neighbours
OFFSET_PADDING = timedelta(days=2)          # subordinate offsets move events by hours; fetch reference events wider

# ---------------------------------------------------------------------------------------------------------------
# Astronomical arguments
# ---------------------------------------------------------------------------------------------------------------

J2000 = datetime(2000, 1, 1, 12, 0, tzinfo=UTC)
HOURS_PER_CENTURY = 36525 * 24

# Mean longitudes in degrees at J2000 and their rates in degrees per Julian century (Meeus, "Astronomical
# Algorithms", linear terms only; the dropped quadratic terms are under 0.01 degree over this century).
_MEAN_LONGITUDES = {
    "s": (218.3164477, 481267.88123421),    # mean longitude of the moon
    "h": (280.46646, 36000.76983),          # mean longitude of the sun
    "p": (83.3532465, 4069.0137287),        # longitude of the lunar perigee
    "N": (125.04452, -1934.136261),         # longitude of the moon's ascending node
    "p1": (282.93735, 1.71946),             # longitude of the solar perigee
}
# Mean solar hour angle T (Schureman): 15 degrees per hour, and 0 at 12:00 UT, i.e. 180 at 00:00 UT.
_HOUR_ANGLE_RATE = 15.0


def _hours_since_j2000(moment):
    return (moment - J2000).total_seconds() / 3600.0


def _as_utc(moment):
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _longitudes(moment):
    """Mean longitudes (degrees) at ``moment``: dict with s, h, p, N, p1."""
    centuries = _hours_since_j2000(moment) / HOURS_PER_CENTURY
    return {name: start + rate * centuries for name, (start, rate) in _MEAN_LONGITUDES.items()}


def _argument_rates_per_hour():
    """Degrees per hour of the five longitudes and of T, in the order (T, s, h, p, p1)."""
    rates = [_HOUR_ANGLE_RATE]
    for name in ("s", "h", "p", "N", "p1"):
        if name != "N":
            rates.append(_MEAN_LONGITUDES[name][1] / HOURS_PER_CENTURY)
    return tuple(rates)


_RATES = _argument_rates_per_hour()                       # (T, s, h, p, p1)
_AT_J2000 = (0.0,) + tuple(_MEAN_LONGITUDES[name][0] for name in ("s", "h", "p", "p1"))


# ---------------------------------------------------------------------------------------------------------------
# Node factors (f) and nodal phase corrections (u), Schureman (1958) formulas, evaluated mid-year
# ---------------------------------------------------------------------------------------------------------------

OBLIQUITY_DEG = 23.452     # obliquity of the ecliptic (Schureman)
LUNAR_ORBIT_INCLINATION_DEG = 5.145


def _wrap_pi(angle):
    return (angle + math.pi) % (2 * math.pi) - math.pi


def node_factors(moment):
    """Return {family: (f, u_degrees)} for the constituent families, evaluated at ``moment``.

    Families are named after their base constituent (M2, O1, K1, K2, J1, OO1, Mf, Mm, L2, M1); compound
    constituents combine them (see ``CONSTITUENTS``).
    """
    longitudes = _longitudes(moment)
    node = math.radians(longitudes["N"] % 360.0)
    perigee = math.radians(longitudes["p"])
    obliquity, inclination = math.radians(OBLIQUITY_DEG), math.radians(LUNAR_ORBIT_INCLINATION_DEG)

    # Inclination I of the lunar orbit to the equator (Schureman eq. for cos I).
    cos_i = (math.cos(obliquity) * math.cos(inclination)
             - math.sin(obliquity) * math.sin(inclination) * math.cos(node))
    big_i = math.acos(cos_i)
    # nu and xi: longitude corrections between lunar orbit and equator nodes (half-angle tangent relations).
    ratio_a = math.cos((obliquity - inclination) / 2) / math.cos((obliquity + inclination) / 2)
    ratio_b = math.sin((obliquity - inclination) / 2) / math.sin((obliquity + inclination) / 2)
    angle_a = 2 * math.atan2(ratio_a * math.sin(node / 2), math.cos(node / 2))    # = N - xi + nu
    angle_b = 2 * math.atan2(ratio_b * math.sin(node / 2), math.cos(node / 2))    # = N - xi - nu
    nu = _wrap_pi((angle_a - angle_b) / 2)
    xi = _wrap_pi(node - (angle_a + angle_b) / 2)
    # nu' and 2nu'' for the K1 and K2 families.
    sin_2i = math.sin(2 * big_i)
    nu_prime = math.atan2(sin_2i * math.sin(nu), sin_2i * math.cos(nu) + 0.3347)
    nu_double_prime = 0.5 * math.atan2(math.sin(big_i) ** 2 * math.sin(2 * nu),
                                       math.sin(big_i) ** 2 * math.cos(2 * nu) + 0.0727)

    sin_i = math.sin(big_i)
    half_i_cos_sq = math.cos(big_i / 2) ** 2
    factor_m2 = math.cos(big_i / 2) ** 4 / 0.9154
    factor_o1 = sin_i * half_i_cos_sq / 0.3800

    # L2: Schureman's R and 1/Ra (the f ratio is sqrt(...), verified against NOAA; the inverse is wrong).
    lunar_perigee_in_orbit = perigee - xi                                          # "P" = p - xi
    tan_half_i = math.tan(big_i / 2)
    l2_ratio = math.sqrt(1 - 12 * tan_half_i ** 2 * math.cos(2 * lunar_perigee_in_orbit) + 36 * tan_half_i ** 4)
    l2_r = math.atan2(math.sin(2 * lunar_perigee_in_orbit),
                      (1 / 6) / tan_half_i ** 2 - math.cos(2 * lunar_perigee_in_orbit))

    # M1: amplitude factor is f(O1) times sqrt(2.310 + 1.435 cos 2P) (Schureman).  The phase uses a tan-form Q
    # PLUS AN EMPIRICAL +25 DEGREE TERM.  That term is a fit, not derived: it minimised the rms difference between
    # this model and NOAA's own 6-minute predictions at Seattle (9447130), San Francisco (9414290) and Honolulu
    # (1612340) for the 30-day windows starting 2026-12-01 and 2027-06-01 (P = 86 and 130 degrees).  With it the
    # M1 residual is about 0.013 ft rms at Seattle in both epochs; without it, 0.03 to 0.1 ft.  M1 amplitude is
    # at most about 0.1 ft, so the term only matters at the hundredth-of-a-foot level.
    m1_factor = factor_o1 * math.sqrt(2.310 + 1.435 * math.cos(2 * lunar_perigee_in_orbit))
    m1_q = math.atan2((5 * cos_i - 1) * math.sin(lunar_perigee_in_orbit),
                      (7 * cos_i + 1) * math.cos(lunar_perigee_in_orbit)) + math.radians(25.0)

    return {
        "M2": (factor_m2, math.degrees(2 * xi - 2 * nu)),
        "O1": (factor_o1, math.degrees(2 * xi - nu)),
        "K1": (math.sqrt(0.8965 * sin_2i ** 2 + 0.6001 * sin_2i * math.cos(nu) + 0.1006), math.degrees(-nu_prime)),
        "K2": (math.sqrt(19.0444 * sin_i ** 4 + 2.7702 * sin_i ** 2 * math.cos(2 * nu) + 0.0981),
               math.degrees(-2 * nu_double_prime)),
        "J1": (sin_2i / 0.7214, math.degrees(-nu)),
        "OO1": (sin_i * math.sin(big_i / 2) ** 2 / 0.0164, math.degrees(-2 * xi - nu)),
        "Mf": (sin_i ** 2 / 0.1578, math.degrees(-2 * xi)),
        "Mm": ((2 / 3 - sin_i ** 2) / 0.5021, 0.0),
        "L2": (factor_m2 * l2_ratio, math.degrees(2 * xi - 2 * nu - l2_r)),
        "M1": (m1_factor, math.degrees(xi - nu + m1_q)),
    }


# ---------------------------------------------------------------------------------------------------------------
# The 37 NOAA constituents
# ---------------------------------------------------------------------------------------------------------------
# name: ((multiplier of T, s, h, p, p1), constant degrees, ((coefficient, node-factor family), ...))
# V = n_T*T + n_s*s + n_h*h + n_p*p + n_p1*p1 + constant.   Doodson numbers with lunar time tau = T + h - s, the
# constants are the usual +-90 / 180 phase conventions of Schureman's table.  Compound constituents combine the
# factors of their parts: f = product of f_family ** |coefficient|, u = sum of coefficient * u_family.
CONSTITUENTS = {
    "M2": ((2, -2, 2, 0, 0), 0, ((1, "M2"),)),
    "S2": ((2, 0, 0, 0, 0), 0, ()),
    "N2": ((2, -3, 2, 1, 0), 0, ((1, "M2"),)),
    "K1": ((1, 0, 1, 0, 0), -90, ((1, "K1"),)),
    "M4": ((4, -4, 4, 0, 0), 0, ((2, "M2"),)),
    "O1": ((1, -2, 1, 0, 0), 90, ((1, "O1"),)),
    "M6": ((6, -6, 6, 0, 0), 0, ((3, "M2"),)),
    "MK3": ((3, -2, 3, 0, 0), -90, ((1, "M2"), (1, "K1"))),
    "S4": ((4, 0, 0, 0, 0), 0, ()),
    "MN4": ((4, -5, 4, 1, 0), 0, ((2, "M2"),)),
    "NU2": ((2, -3, 4, -1, 0), 0, ((1, "M2"),)),
    "S6": ((6, 0, 0, 0, 0), 0, ()),
    "MU2": ((2, -4, 4, 0, 0), 0, ((1, "M2"),)),
    "2N2": ((2, -4, 2, 2, 0), 0, ((1, "M2"),)),
    "OO1": ((1, 2, 1, 0, 0), -90, ((1, "OO1"),)),
    "LAM2": ((2, -1, 0, 1, 0), 180, ((1, "M2"),)),
    "S1": ((1, 0, 0, 0, 0), 0, ()),
    "M1": ((1, -1, 1, 0, 0), -90, ((1, "M1"),)),
    "J1": ((1, 1, 1, -1, 0), -90, ((1, "J1"),)),
    "MM": ((0, 1, 0, -1, 0), 0, ((1, "Mm"),)),
    "SSA": ((0, 0, 2, 0, 0), 0, ()),
    "SA": ((0, 0, 1, 0, 0), 0, ()),
    "MSF": ((0, 2, -2, 0, 0), 0, ((-1, "M2"),)),
    "MF": ((0, 2, 0, 0, 0), 0, ((1, "Mf"),)),
    "RHO": ((1, -3, 3, -1, 0), 90, ((1, "O1"),)),
    "Q1": ((1, -3, 1, 1, 0), 90, ((1, "O1"),)),
    "T2": ((2, 0, -1, 0, 1), 0, ()),
    "R2": ((2, 0, 1, 0, -1), 180, ()),
    "2Q1": ((1, -4, 1, 2, 0), 90, ((1, "O1"),)),
    "P1": ((1, 0, -1, 0, 0), 90, ()),
    "2SM2": ((2, 2, -2, 0, 0), 0, ((-1, "M2"),)),
    "M3": ((3, -3, 3, 0, 0), 0, ((1.5, "M2"),)),
    "L2": ((2, -1, 2, -1, 0), 180, ((1, "L2"),)),
    "2MK3": ((3, -4, 3, 0, 0), 90, ((2, "M2"), (-1, "K1"))),
    "K2": ((2, 0, 2, 0, 0), 0, ((1, "K2"),)),
    "M8": ((8, -8, 8, 0, 0), 0, ((4, "M2"),)),
    "MS4": ((4, -2, 2, 0, 0), 0, ((1, "M2"),)),
}
CONSTITUENT_NAMES = tuple(CONSTITUENTS)        # the order of the amplitude/phase pairs in the data file


def constituent_speed(name):
    """Angular speed in degrees per hour, from the argument multipliers (matches NOAA's published speeds)."""
    multipliers = CONSTITUENTS[name][0]
    return sum(multiplier * rate for multiplier, rate in zip(multipliers, _RATES))


def constituent_phase_at_j2000(name):
    """V of the constituent at the J2000 epoch, degrees (the linear V(t) = this + speed * hours since J2000)."""
    multipliers, constant, _ = CONSTITUENTS[name]
    return sum(multiplier * start for multiplier, start in zip(multipliers, _AT_J2000)) + constant


class HarmonicYearModel:
    """Water level model for one station and one calendar year (node factors fixed at mid-year)."""

    def __init__(self, constants, z0_ft, year):
        """``constants``: list of (name, amplitude_ft, phase_gmt_degrees); ``z0_ft`` = MSL - MLLW."""
        factors = node_factors(datetime(year, 7, 2, tzinfo=UTC))
        self.z0_ft = z0_ft
        self.terms = []     # (amplitude * f, speed deg/h, phase at J2000 deg, speed in rad/h)
        for name, amplitude, phase_gmt in constants:
            if amplitude == 0:
                continue
            _, _, factor_terms = CONSTITUENTS[name]
            factor, nodal_phase = 1.0, 0.0
            for coefficient, family in factor_terms:
                family_factor, family_phase = factors[family]
                factor *= family_factor ** abs(coefficient)
                nodal_phase += coefficient * family_phase
            speed = constituent_speed(name)
            phase = constituent_phase_at_j2000(name) + nodal_phase - phase_gmt
            self.terms.append((amplitude * factor, speed, phase))

    def height_at_hours(self, hours):
        """Height above MLLW at ``hours`` since J2000."""
        return self.z0_ft + sum(amp * math.cos(math.radians(phase + speed * hours))
                                for amp, speed, phase in self.terms)

    def slope_at_hours(self, hours):
        """Time derivative of the height, feet per hour (zero at highs and lows)."""
        return -sum(amp * math.radians(speed) * math.sin(math.radians(phase + speed * hours))
                    for amp, speed, phase in self.terms)

    def extrema(self, start, end):
        """Highs and lows in [start, end): list of (datetime, "H"|"L", height_ft)."""
        first, last = _hours_since_j2000(start), _hours_since_j2000(end)
        found = []
        hours, slope = first, self.slope_at_hours(first)
        while hours < last:
            next_hours = min(hours + SAMPLE_STEP_HOURS, last)
            next_slope = self.slope_at_hours(next_hours)
            if (slope > 0) != (next_slope > 0) and slope != 0:
                low, high = hours, next_hours
                for _ in range(BISECTION_STEPS):
                    middle = (low + high) / 2
                    if (self.slope_at_hours(middle) > 0) == (slope > 0):
                        low = middle
                    else:
                        high = middle
                peak_hours = (low + high) / 2
                found.append((J2000 + timedelta(hours=peak_hours), "H" if slope > 0 else "L",
                              self.height_at_hours(peak_hours)))
            hours, slope = next_hours, next_slope
        return found


def merge_flat_pairs(events, min_swing_ft):
    """Remove adjacent high/low pairs whose heights differ by less than ``min_swing_ft``.

    Repeatedly removes the closest-in-height adjacent pair while it is under the limit, so the list keeps
    alternating H, L, H, L.  ``min_swing_ft <= 0`` returns the events unchanged.  Trade-off (see
    DEFAULT_MIN_SWING_FT): NOAA lists some hundredth-of-a-foot wiggles on diurnal tides; merging removes the
    wiggles we compute but also some NOAA lists.
    """
    kept = list(events)
    if min_swing_ft <= 0:
        return kept
    while len(kept) >= 2:
        swings = [abs(kept[index + 1][2] - kept[index][2]) for index in range(len(kept) - 1)]
        smallest = min(range(len(swings)), key=swings.__getitem__)
        if swings[smallest] >= min_swing_ft:
            break
        del kept[smallest:smallest + 2]
    return kept


def great_circle_km(lat_a, lon_a, lat_b, lon_b):
    """Haversine distance in kilometres."""
    phi_a, phi_b = math.radians(lat_a), math.radians(lat_b)
    delta_phi, delta_lambda = phi_b - phi_a, math.radians(lon_b - lon_a)
    chord = math.sin(delta_phi / 2) ** 2 + math.cos(phi_a) * math.cos(phi_b) * math.sin(delta_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(chord))


class Stations:
    """All stations from a data file; answers nearest-station and high/low queries."""

    def __init__(self, data):
        self.source = data.get("source")
        self.fetched = data.get("fetched")
        self.counts = data.get("counts", {})
        self.excluded = data.get("excluded", [])
        self._harmonic = {}       # id -> dict(info=..., z0=..., constants=[(name, amp, phase)])
        self._subordinate = {}    # id -> dict(info=..., reference=..., offsets=...)
        self._reference_only = {}  # harmonic stations kept only because subordinate stations are computed from them
        reference_only = set(data.get("reference_only", []))
        names = data["constituents"]
        for row in data["harmonic"]:
            station_id, name, state, lat, lon, z0, flat = row
            constants = [(names[index], flat[2 * index], flat[2 * index + 1]) for index in range(len(names))]
            info = {"id": station_id, "name": name, "state": state, "lat": lat, "lon": lon, "kind": "harmonic"}
            target = self._reference_only if station_id in reference_only else self._harmonic
            target[station_id] = {"info": info, "z0": z0, "constants": constants, "models": {}}
        for row in data["subordinate"]:
            station_id, name, state, lat, lon, reference, high_min, low_min, high_height, low_height, kind = row
            info = {"id": station_id, "name": name, "state": state, "lat": lat, "lon": lon, "kind": "subordinate"}
            self._subordinate[station_id] = {"info": info, "reference": reference, "high_minutes": high_min,
                                             "low_minutes": low_min, "high_height": high_height,
                                             "low_height": low_height, "ratio": kind == "R"}
        for station_id in data.get("approximate", []):      # times 6 to 13 minutes from NOAA's: worded as "about"
            entry = self._harmonic.get(station_id) or self._subordinate.get(station_id)
            if entry:
                entry["info"]["approximate"] = True
        self._all = [entry["info"] for entry in self._harmonic.values()]
        self._all += [entry["info"] for entry in self._subordinate.values()]

    def station(self, station_id):
        """The station dict, or None for an unknown (or excluded) id."""
        entry = self._harmonic.get(station_id) or self._subordinate.get(station_id)
        return dict(entry["info"]) if entry else None

    def nearest(self, lat, lon, max_km):
        """Nearest station within ``max_km`` great-circle kilometres, or None."""
        best, best_km = None, None
        for info in self._all:
            distance = great_circle_km(lat, lon, info["lat"], info["lon"])
            if best_km is None or distance < best_km:
                best, best_km = info, distance
        return dict(best) if best is not None and best_km <= max_km else None

    def _year_model(self, entry, year):
        models = entry["models"]
        if year not in models:
            models[year] = HarmonicYearModel(entry["constants"], entry["z0"], year)
        return models[year]

    def _harmonic_raw_events(self, entry, start, end):
        """Unmerged events in [start, end); each calendar year uses its own mid-year node factors."""
        events = []
        for year in range(start.year, end.year + 1):
            year_start = datetime(year, 1, 1, tzinfo=UTC)
            year_end = datetime(year + 1, 1, 1, tzinfo=UTC)
            segment_start, segment_end = max(start, year_start), min(end, year_end)
            if segment_start >= segment_end:
                continue
            model = self._year_model(entry, year)
            scan = model.extrema(segment_start - timedelta(hours=1), segment_end + timedelta(hours=1))
            events += [event for event in scan if segment_start <= event[0] < segment_end]
        return events

    def events(self, station_id, start_utc, end_utc, min_swing_ft=DEFAULT_MIN_SWING_FT):
        """Highs and lows in [start_utc, end_utc): list of (datetime_utc, "H"|"L", height_ft_mllw).

        Returns None for an unknown station id (including stations left out of the data file) and an empty list
        for a window with no events.  ``min_swing_ft`` merges near-flat high/low pairs (0 disables).
        """
        start, end = _as_utc(start_utc), _as_utc(end_utc)
        if station_id in self._harmonic:
            entry = self._harmonic[station_id]
            raw = self._harmonic_raw_events(entry, start - SEARCH_PADDING, end + SEARCH_PADDING)
        elif station_id in self._subordinate:
            sub = self._subordinate[station_id]
            reference = self._harmonic.get(sub["reference"]) or self._reference_only[sub["reference"]]
            raw = []
            for when, kind, height in self._harmonic_raw_events(reference, start - OFFSET_PADDING,
                                                                end + OFFSET_PADDING):
                minutes = sub["high_minutes"] if kind == "H" else sub["low_minutes"]
                offset = sub["high_height"] if kind == "H" else sub["low_height"]
                height = height * offset if sub["ratio"] else height + offset
                raw.append((when + timedelta(minutes=minutes), kind, height))
            raw.sort(key=lambda event: event[0])
        else:
            return None
        merged = merge_flat_pairs(raw, min_swing_ft)
        return [event for event in merged if start <= event[0] < end]


def load(path):
    """Read a ``tides.json.gz`` data file written by build_tide_data.py."""
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return Stations(json.load(handle))
