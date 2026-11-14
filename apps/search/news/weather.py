"""Weather for the box's home town ("build it that way, just
focus on the weather for now"). US National Weather Service first — the official forecast
and the only source of official US warnings — and MET Norway when NWS is down or the home
is outside the US (free, commercial use allowed with attribution).

The home forecast is polled on a schedule, so a search never causes a request. The result is
one row in the `news` database (`weather`), which search-api reads for "weather",
"will it rain tomorrow", "temperature" ….

    python weather.py          # run forever
    python weather.py --once   # one refresh (tests, first run)

Needs SEARCH_HOME="lat,lon" (town level). Both APIs ask for an identifying User-Agent and for
clients to honour their caching; we poll well inside their guidance:
  forecast + hourly every 30 min, current conditions every 15 min, alerts every 5 min.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request

import newsdb  # noqa: E402  (Postgres or the NEWS_SQLITE file)

USER_AGENT = "logout-weather/1 (personal weather on a home server; logout.cloud)"
TIMEOUT_S = 20
TICK_S = 30
EVERY = {"forecast": 30 * 60, "obs": 15 * 60, "alerts": 5 * 60}
HOURS_KEPT = 24
NWS = "https://api.weather.gov"
MET = "https://api.met.no/weatherapi/locationforecast/2.0/compact"

SCHEMA = """
CREATE TABLE IF NOT EXISTS weather (
    place    text PRIMARY KEY,           -- 'home'
    payload  jsonb NOT NULL,
    source   text NOT NULL,              -- nws | met
    updated  timestamptz NOT NULL DEFAULT now()
);
"""


def log(msg: str) -> None:
    print(f"[weather] {dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def home() -> tuple[float, float] | None:
    raw = os.environ.get("SEARCH_HOME", "").strip().strip('"')
    try:
        lat, lon = (float(x) for x in raw.split(","))
        return lat, lon
    except ValueError:
        return None


def fetch_json(url: str, accept: str = "application/geo+json") -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept,
                                               "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        body = r.read(10 * 1024 * 1024)
        if r.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
    return json.loads(body)


# ---------------------------------------------------------------------------
# Units and wording
# ---------------------------------------------------------------------------
def c_to_f(c) -> int | None:
    return None if c is None else round(float(c) * 9 / 5 + 32)


def kmh_to_mph(v) -> int | None:
    return None if v is None else round(float(v) / 1.609344)


def ms_to_mph(v) -> int | None:
    return None if v is None else round(float(v) * 2.236936)


def _pop(p: dict) -> int | None:
    v = (p.get("probabilityOfPrecipitation") or {}).get("value")
    return None if v is None else int(v)


# MET Norway symbol codes -> words (the forecast shows them; day/night suffix dropped)
MET_WORDS = {
    "clearsky": "Clear", "fair": "Mostly clear", "partlycloudy": "Partly cloudy", "cloudy": "Cloudy",
    "fog": "Fog", "lightrain": "Light rain", "rain": "Rain", "heavyrain": "Heavy rain",
    "lightrainshowers": "Light showers", "rainshowers": "Showers", "heavyrainshowers": "Heavy showers",
    "lightsnow": "Light snow", "snow": "Snow", "heavysnow": "Heavy snow", "sleet": "Sleet",
    "lightsleet": "Light sleet", "snowshowers": "Snow showers", "lightsnowshowers": "Light snow showers",
    "rainandthunder": "Rain and thunder", "lightrainandthunder": "Light rain and thunder",
    "heavyrainandthunder": "Heavy rain and thunder", "rainshowersandthunder": "Thunderstorms",
}


def met_words(code: str | None) -> str:
    base = (code or "").split("_")[0]
    return MET_WORDS.get(base, base.replace("and", " and ").capitalize() if base else "")


# ---------------------------------------------------------------------------
# NWS -> the answer
# ---------------------------------------------------------------------------
def nws_now(obs: dict) -> dict:
    p = obs.get("properties") or {}
    temp = c_to_f((p.get("temperature") or {}).get("value"))
    feels = c_to_f((p.get("heatIndex") or {}).get("value")) or c_to_f((p.get("windChill") or {}).get("value"))
    hum = (p.get("relativeHumidity") or {}).get("value")
    return {"temp_f": temp, "feels_f": feels if feels is not None and feels != temp else None,
            "text": p.get("textDescription") or "", "wind_mph": kmh_to_mph((p.get("windSpeed") or {}).get("value")),
            "humidity": None if hum is None else round(float(hum)), "at": p.get("timestamp")}


def nws_days(forecast: dict) -> list[dict]:
    """12-hour periods -> days: {name, high_f, low_f, text, pop, detail}. A forecast that
    starts at night ('Tonight') opens with a day that has only a low."""
    periods = (forecast.get("properties") or {}).get("periods") or []
    days: list[dict] = []
    for p in periods:
        t = p.get("temperature")
        t = c_to_f(t) if p.get("temperatureUnit") == "C" else t
        if p.get("isDaytime") or not days:
            days.append({"name": p.get("name") or "", "date": (p.get("startTime") or "")[:10],
                         "high_f": t if p.get("isDaytime") else None, "low_f": None if p.get("isDaytime") else t,
                         "text": p.get("shortForecast") or "", "pop": _pop(p),
                         "detail": p.get("detailedForecast") or "", "wind": f"{p.get('windDirection', '')} {p.get('windSpeed', '')}".strip()})
        else:
            d = days[-1]
            d["low_f"] = t
            d["night_text"] = p.get("shortForecast") or ""
            n = _pop(p)
            if n is not None and (d["pop"] is None or n > d["pop"]):
                d["pop"] = n
    return days[:7]


def nws_hours(hourly: dict, now: dt.datetime) -> list[dict]:
    out = []
    for p in (hourly.get("properties") or {}).get("periods") or []:
        try:
            t = dt.datetime.fromisoformat(p["startTime"])
        except (KeyError, ValueError):
            continue
        if t < now - dt.timedelta(hours=1):
            continue
        temp = p.get("temperature")
        out.append({"time": p["startTime"], "temp_f": c_to_f(temp) if p.get("temperatureUnit") == "C" else temp,
                    "text": p.get("shortForecast") or "", "pop": _pop(p)})
        if len(out) >= HOURS_KEPT:
            break
    return out


SEVERITY_ORDER = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3, "Unknown": 4}


def nws_alerts(alerts: dict) -> list[dict]:
    out = []
    for f in alerts.get("features") or []:
        p = f.get("properties") or {}
        if p.get("status") not in (None, "Actual") or p.get("messageType") == "Cancel":
            continue
        out.append({"event": p.get("event") or "", "headline": p.get("headline") or "",
                    "severity": p.get("severity") or "Unknown", "ends": p.get("ends") or p.get("expires"),
                    "instruction": (p.get("instruction") or "")[:400]})
    seen, uniq = set(), []
    for a in sorted(out, key=lambda a: SEVERITY_ORDER.get(a["severity"], 9)):
        if a["event"] not in seen:
            seen.add(a["event"])
            uniq.append(a)
    return uniq


# ---------------------------------------------------------------------------
# MET Norway -> the same answer (fallback)
# ---------------------------------------------------------------------------
def met_answer(data: dict, tz: str | None, now: dt.datetime) -> dict:
    ts = (data.get("properties") or {}).get("timeseries") or []
    hours, by_day = [], {}
    zone = _zone(tz)
    for t in ts:
        try:
            when = dt.datetime.fromisoformat(t["time"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        det = ((t.get("data") or {}).get("instant") or {}).get("details") or {}
        nxt = (t.get("data") or {}).get("next_1_hours") or (t.get("data") or {}).get("next_6_hours") or {}
        temp = c_to_f(det.get("air_temperature"))
        text = met_words((nxt.get("summary") or {}).get("symbol_code"))
        if when >= now - dt.timedelta(hours=1) and len(hours) < HOURS_KEPT and (t.get("data") or {}).get("next_1_hours"):
            hours.append({"time": when.isoformat(), "temp_f": temp, "text": text, "pop": None})
        local = when.astimezone(zone).date() if zone else when.date()
        d = by_day.setdefault(local.isoformat(), {"temps": [], "texts": []})
        if temp is not None:
            d["temps"].append(temp)
        if text:
            d["texts"].append(text)
    first = ts[0] if ts else {}
    det = ((first.get("data") or {}).get("instant") or {}).get("details") or {}
    nxt = (first.get("data") or {}).get("next_1_hours") or {}
    days = []
    for i, (date, d) in enumerate(sorted(by_day.items())[:7]):
        if not d["temps"]:
            continue
        name = "Today" if i == 0 else dt.date.fromisoformat(date).strftime("%A")
        days.append({"name": name, "date": date, "high_f": max(d["temps"]), "low_f": min(d["temps"]),
                     "text": max(set(d["texts"]), key=d["texts"].count) if d["texts"] else "", "pop": None, "detail": ""})
    return {"now": {"temp_f": c_to_f(det.get("air_temperature")), "feels_f": None,
                    "text": met_words((nxt.get("summary") or {}).get("symbol_code")),
                    "wind_mph": ms_to_mph(det.get("wind_speed")),
                    "humidity": None if det.get("relative_humidity") is None else round(det["relative_humidity"]),
                    "at": first.get("time")},
            "days": days, "hours": hours, "alerts": []}


def _zone(tz: str | None):
    if not tz:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
class State:
    def __init__(self):
        self.point: dict | None = None        # NWS /points answer (grid URLs, place, zone)
        self.point_at = 0.0
        self.station: str | None = None
        self.parts: dict = {}                 # forecast, hourly, obs, alerts (raw NWS)
        self.due = {k: 0.0 for k in EVERY}
        self.nws_ok = True
        self.forecast_at = 0.0                # when the NWS forecast was last fetched


def refresh_nws(st: State, lat: float, lon: float) -> None:
    now = time.time()
    if st.point is None or now - st.point_at > 86400:
        st.point = fetch_json(f"{NWS}/points/{lat:.4f},{lon:.4f}")
        st.point_at = now
        stations = fetch_json(st.point["properties"]["observationStations"])
        st.station = stations["features"][0]["properties"]["stationIdentifier"]
    props = st.point["properties"]
    if now >= st.due["forecast"]:
        st.parts["forecast"] = fetch_json(props["forecast"])
        st.parts["hourly"] = fetch_json(props["forecastHourly"])
        st.due["forecast"] = now + EVERY["forecast"]
        st.forecast_at = now
    if now >= st.due["obs"]:
        st.parts["obs"] = fetch_json(f"{NWS}/stations/{st.station}/observations/latest")
        st.due["obs"] = now + EVERY["obs"]
    if now >= st.due["alerts"]:
        st.parts["alerts"] = fetch_json(f"{NWS}/alerts/active?point={lat:.4f},{lon:.4f}")
        st.due["alerts"] = now + EVERY["alerts"]


def met_fallback(st: State, lat: float, lon: float, now: dt.datetime) -> tuple[dict | None, str | None]:
    tz = ((st.point or {}).get("properties") or {}).get("timeZone")
    try:
        answer = met_answer(fetch_json(f"{MET}?lat={lat:.2f}&lon={lon:.2f}", "application/json"), tz, now)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as e:
        log(f"MET Norway failed too ({type(e).__name__}) — keeping the last forecast")
        return None, None
    answer["place"] = nws_answer(st, now)["place"] if st.point else ""
    answer["tz"] = tz
    answer["link"] = "https://www.yr.no/en"         # MET's licence (CC BY 4.0) asks for credit
    return answer, "met"


NWS_STALE_S = 2 * 3600


def nws_still_good(st: State, now_s: float) -> bool:
    """A failed NWS request with a forecast from the last NWS_STALE_S: keep NWS."""
    return bool(st.parts.get("forecast")) and now_s - st.forecast_at <= NWS_STALE_S


def nws_answer(st: State, now: dt.datetime) -> dict:
    loc = ((st.point or {}).get("properties") or {}).get("relativeLocation", {}).get("properties") or {}
    return {"now": nws_now(st.parts.get("obs") or {}), "days": nws_days(st.parts.get("forecast") or {}),
            "hours": nws_hours(st.parts.get("hourly") or {}, now), "alerts": nws_alerts(st.parts.get("alerts") or {}),
            "place": ", ".join(x for x in (loc.get("city"), loc.get("state")) if x),
            # the forecast office's page — the grid, never the coordinates, leaves the box
            "link": f"https://www.weather.gov/{cwa.lower()}" if (cwa := ((st.point or {}).get("properties") or {}).get("cwa")) else "https://www.weather.gov/",
            "tz": ((st.point or {}).get("properties") or {}).get("timeZone")}


def store_weather(conn, answer: dict, source: str, now) -> None:
    conn.execute("""INSERT INTO weather (place, payload, source, updated) VALUES ('home', %s, %s, %s)
                    ON CONFLICT (place) DO UPDATE SET payload = EXCLUDED.payload,
                        source = EXCLUDED.source, updated = EXCLUDED.updated""",
                 (json.dumps(answer), source, newsdb.ts(now)))


def run(once: bool = False) -> None:
    where = home()
    if where is None:
        log("SEARCH_HOME is not set (\"lat,lon\") — nothing to do")
        return
    lat, lon = where
    st = State()
    if newsdb.is_sqlite():
        newsdb.ensure_sqlite_schema()
    with newsdb.connect() as conn:
        if not newsdb.is_sqlite():
            conn.execute(SCHEMA)
        while True:
            now = dt.datetime.now(dt.timezone.utc)
            answer, source = None, None
            try:
                refresh_nws(st, lat, lon)
                answer, source = nws_answer(st, now), "nws"
                if not st.nws_ok:
                    log("NWS is answering again")
                st.nws_ok = True
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError) as e:
                for k in EVERY:
                    st.due[k] = time.time() + 5 * 60        # try NWS again in 5 minutes
                if nws_still_good(st, time.time()):
                    # one request failed but the forecast is recent: keep NWS, the rest as last fetched
                    answer, source = nws_answer(st, now), "nws"
                else:
                    # outside the US NWS answers 404 for /points; otherwise it is down: MET Norway
                    if st.nws_ok:
                        log(f"NWS failed ({type(e).__name__}: {str(e)[:100]}) — MET Norway until it is back")
                    st.nws_ok = False
                    answer, source = met_fallback(st, lat, lon, now)
            if answer and (answer["now"].get("temp_f") is not None or answer["days"]):
                answer["source"] = source
                answer["updated"] = now.isoformat()
                store_weather(conn, answer, source, now)
                if once:
                    log(f"{source}: {answer['now'].get('temp_f')}°F {answer['now'].get('text')}, "
                        f"{len(answer['days'])} days, {len(answer['hours'])} hours, {len(answer['alerts'])} alerts")
            if once:
                return
            time.sleep(TICK_S)


if __name__ == "__main__":
    run(once="--once" in sys.argv)
