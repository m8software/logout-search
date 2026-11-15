"""The scoreboard ("when I typed in yankees or jaguars or devils and they had an
upcoming game it displayed when and where and how to watch, and if theres an ongoing game
the inning or quarter or whatever and score"). PERSONAL BOX ONLY: MLB's and the NHL's data
is licensed for individual, non-commercial use and ESPN's scoreboard is undocumented —
shipping this on customer boxes waits on the review in Gitea (see the issue on sports data).
Exception: the cleared public sources below run on the public site too.

Sources (approved 2026-09-25), one request per league at a time:
  MLB  statsapi.mlb.com  /api/v1/schedule (linescore, broadcasts, venue)
  NHL  api-web.nhle.com  /v1/schedule/now (the week) + /v1/score/now (live clock/period)
  NFL   nflverse-data games.csv (CC-BY-4.0)       public_ok, every 10 min Sep-Feb else 6 h
  epl, championship, ucl  DROPPED 2026-11-14 (owner): openfootball stopped updating on 2026-09-22 and has no
       Champions League file this season, so tables and results were two months old. The readers below stay for tests
       and a user's own source; nothing fetches them, and the rows already stored are removed at start.
  NBA   OFF — cdn.nba.com refuses non-browsers and ESPN (site.api.espn.com) returns Akamai
       403 to the box; neither is worked around (see BUILTIN)
  The openfootball files are weekly: matches are 'final' or 'pre', never 'live'. nflverse has
  no in-game scores: a started game with none is 'live' for 4 h, scores arrive when it ends.
  One request at a time per host; If-None-Match / If-Modified-Since on every file.

Each league is refreshed every LIVE_EVERY_S while one of its games is on (or about to start,
or just ended), else every IDLE_EVERY_S. Games from yesterday to a week ahead are kept in
the `news` database's `games` table; search-api reads it.

    python sports.py          # run forever
    python sports.py --once   # one pass over every league (tests, first run)

Personal sources (search.logout.cloud ⚙): a league whose data cannot ship (NFL, NBA) runs
when the USER gives its address — settings.json "sources": {"nfl": {"url": ".../scoreboard?dates={date}",
"format": "espn"}}. SPORTS_SETTINGS names that file; it is re-read every loop. {date} becomes
YYYYMMDD (one request per day, as ESPN wants); a URL without it is fetched once per pass.
The user's engine fetches from the user's own connection. A 401/403/429 is a refusal: that
source waits BLOCKED_WAIT_S and the status says so — it is never worked around.
SPORTS_BUILTIN=0 turns every built-in source off (customer engines). SPORTS_PUBLIC=1 (the public
site) runs ONLY the sources whose SOURCES entry says public_ok — never MLB/NHL — and ignores
user sources; SOURCES[league]["attribution"] is the credit each licence asks for. Status per league goes to SPORTS_STATUS (JSON) for ⚙.
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request

import newsdb  # noqa: E402  (Postgres or the NEWS_SQLITE file)

USER_AGENT = "logout-sports/1 (personal scoreboard; one household)"
TIMEOUT_S = 20
LIVE_EVERY_S = 60
IDLE_EVERY_S = 30 * 60
TICK_S = 15
SOON = dt.timedelta(minutes=20)          # about to start: refresh at the live pace
KEEP_DAYS_BACK = 8                      # a weekly team's last result stays on its card until the next game (2026-11-14)
WINDOW_DAYS = 7
STATIC_AHEAD_DAYS = 14                  # the published files: a team with a bye or a Thursday game still has its next one (2026-11-14: the Giants had none)

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    league      text NOT NULL,
    game_id     text NOT NULL,
    start_utc   timestamptz NOT NULL,
    state       text NOT NULL,          -- pre | live | final | postponed
    detail      text NOT NULL DEFAULT '',
    away_name   text NOT NULL, away_nick text NOT NULL, away_abbr text, away_score integer,
    home_name   text NOT NULL, home_nick text NOT NULL, home_abbr text, home_score integer,
    away_keys   text[] NOT NULL, home_keys text[] NOT NULL,
    venue       text,
    broadcasts  text[] NOT NULL DEFAULT '{}',
    url         text,
    updated     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (league, game_id)
);
CREATE INDEX IF NOT EXISTS games_away_keys ON games USING gin (away_keys);
CREATE INDEX IF NOT EXISTS games_home_keys ON games USING gin (home_keys);
CREATE INDEX IF NOT EXISTS games_start ON games (start_utc);
CREATE TABLE IF NOT EXISTS teams (
    league      text NOT NULL,
    name        text NOT NULL,
    nick        text NOT NULL,
    keys        text[] NOT NULL,
    updated     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (league, name)
);
CREATE INDEX IF NOT EXISTS teams_keys ON teams USING gin (keys);
"""
# Every team of a built-in league, so a search can say "no Mets game in the next 7 days" when the
# week holds none (2026-09-30: most MLB teams' seasons had ended and their searches showed junk).
# One request per league a day, to the same sources as the schedules.
TEAMS_EVERY_S = 86400
TEAM_URLS: dict = {}                     # filled by sports_personal when it is present


def store_teams(conn, teams: list[dict]) -> None:
    for t in teams:
        if t["keys"]:
            conn.execute("""INSERT INTO teams (league, name, nick, keys, updated)
                            VALUES (%(league)s, %(name)s, %(nick)s, %(keys)s, %(upd)s)
                            ON CONFLICT (league, name) DO UPDATE SET nick = EXCLUDED.nick, keys = EXCLUDED.keys,
                            updated = EXCLUDED.updated""", dict(t, upd=newsdb.ts(newsdb.now())))


def log(msg: str) -> None:
    print(f"[sports] {dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def norm(s: str | None) -> str:
    return re.sub(r"[^0-9a-z]", "", (s or "").lower())


def team_keys(*names: str | None) -> list[str]:
    """How a team can be typed: "yankees", "newyorkyankees" — never an abbreviation alone
    ("nyy" collides with too much)."""
    return sorted({norm(n) for n in names if n and len(norm(n)) >= 3})


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _utc(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _uniq(xs) -> list[str]:
    out = []
    for x in xs:
        if x and x not in out:
            out.append(x)
    return out


# ---------------------------------------------------------------------------
# One normaliser per source -> the same game dict
# ---------------------------------------------------------------------------
def _game(league, gid, start, state, detail, side, venue, tv, url) -> dict | None:
    t = _utc(start)
    if not gid or not t or "away" not in side or "home" not in side or not side["away"]["name"] or not side["home"]["name"]:
        return None
    return {"league": league, "game_id": str(gid), "start_utc": t, "state": state, "detail": detail or "",
            "away": side["away"], "home": side["home"], "venue": venue, "broadcasts": tv[:5],
            "url": url if url and url.startswith("https://") else None}


# ---------------------------------------------------------------------------
# Fetching and the loop
# ---------------------------------------------------------------------------
def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                               "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        body = r.read(20 * 1024 * 1024)
        if r.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
    return json.loads(body)


def window(today: dt.date) -> tuple[dt.date, dt.date]:
    return today - dt.timedelta(days=1), today + dt.timedelta(days=WINDOW_DAYS)


# ---------------------------------------------------------------------------
# Cleared public sources: licences that allow the public site, polled into our own
# store like the rest — nothing is fetched at search time. docs/legal/SEARCH-DATA-SOURCES.md has
# the quoted terms. Each source says public_ok (may run on search.logout.cloud) and the
# attribution its licence asks for.
# ---------------------------------------------------------------------------
NFLVERSE_URL = "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"
FOOTBALL_JSON = "https://raw.githubusercontent.com/openfootball/football.json/master/{season}/{file}"
UCL_URL = "https://raw.githubusercontent.com/openfootball/champions-league/master/{season}/cl.txt"
PUBLIC_UA = "logout-search/1.0 (sports poller; contact in repo)"
LIVE_WINDOW = dt.timedelta(hours=4)      # nflverse has no in-game scores: a started game with none is "live" this long
STATIC_IDLE_S = 6 * 3600
NFLVERSE_SEASON_MONTHS = (9, 10, 11, 12, 1, 2)
NFLVERSE_EVERY_S = 600

# abbreviation -> (city, nickname): nflverse carries abbreviations only
NFL_TEAMS = {
    "ARI": ("Arizona", "Cardinals"), "ATL": ("Atlanta", "Falcons"), "BAL": ("Baltimore", "Ravens"),
    "BUF": ("Buffalo", "Bills"), "CAR": ("Carolina", "Panthers"), "CHI": ("Chicago", "Bears"),
    "CIN": ("Cincinnati", "Bengals"), "CLE": ("Cleveland", "Browns"), "DAL": ("Dallas", "Cowboys"),
    "DEN": ("Denver", "Broncos"), "DET": ("Detroit", "Lions"), "GB": ("Green Bay", "Packers"),
    "HOU": ("Houston", "Texans"), "IND": ("Indianapolis", "Colts"), "JAX": ("Jacksonville", "Jaguars"),
    "KC": ("Kansas City", "Chiefs"), "LA": ("Los Angeles", "Rams"), "LAC": ("Los Angeles", "Chargers"),
    "LV": ("Las Vegas", "Raiders"), "MIA": ("Miami", "Dolphins"), "MIN": ("Minnesota", "Vikings"),
    "NE": ("New England", "Patriots"), "NO": ("New Orleans", "Saints"), "NYG": ("New York", "Giants"),
    "NYJ": ("New York", "Jets"), "PHI": ("Philadelphia", "Eagles"), "PIT": ("Pittsburgh", "Steelers"),
    "SEA": ("Seattle", "Seahawks"), "SF": ("San Francisco", "49ers"), "TB": ("Tampa Bay", "Buccaneers"),
    "TEN": ("Tennessee", "Titans"), "WAS": ("Washington", "Commanders"),
}
# how a club is typed beyond its name (the full name and the name without FC/AFC are always keys)
CLUB_ALIASES = {
    "Manchester United": ["Man United", "Man Utd"], "Manchester City": ["Man City"],
    "Tottenham Hotspur": ["Spurs", "Tottenham"], "Wolverhampton Wanderers": ["Wolves"],
    "Newcastle United": ["Newcastle"], "Brighton & Hove Albion": ["Brighton"],
    "Nottingham Forest": ["Forest"], "West Ham United": ["West Ham"], "Leeds United": ["Leeds"],
    "Leicester City": ["Leicester"], "Sheffield United": ["Sheffield Utd"],
    "Sheffield Wednesday": ["Sheffield Wed"], "Queens Park Rangers": ["QPR"],
    "West Bromwich Albion": ["West Brom"], "Crystal Palace": ["Palace"],
    "Paris Saint-Germain": ["PSG"], "FC Bayern München": ["Bayern Munich", "Bayern"],
}

SOURCES = {
    "nfl": {"public_ok": True, "licence": "CC-BY-4.0",
            "attribution": "NFL schedules and scores: nflverse-data (https://github.com/nflverse/nflverse-data), CC BY 4.0"},
}
DROPPED_LEAGUES = ("epl", "championship", "ucl")          # openfootball went stale (see the top of this file)
STATIC_LEAGUES = ("nfl", "epl", "championship", "ucl")     # read from a published file, not a live API
PUBLIC_TEAMS: dict[str, list[dict]] = {}                   # league -> every team; the adapters set it, the loop stores it
HTTP_CACHE: dict = {}                                      # url -> (ETag, Last-Modified, body)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _nth_sunday(year: int, month: int, n: int) -> dt.date:
    """The n-th Sunday of the month; n = -1 is the last."""
    if n > 0:
        d = dt.date(year, month, 1)
        return d + dt.timedelta(days=(6 - d.weekday()) % 7 + 7 * (n - 1))
    d = dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)


def _local_to_utc(day: dt.date, hhmm: str | None, zone: str) -> dt.datetime | None:
    """A wall-clock time in US Eastern, the UK or Central Europe -> UTC. The slim image has no
    tzdata, so the daylight rules are written out: US from the 2nd Sunday of March (07:00Z) to the
    1st Sunday of November (06:00Z); Europe from the last Sunday of March to the last Sunday of
    October, both 01:00Z."""
    m = re.match(r"^(\d{1,2}):(\d\d)", hhmm or "")
    if not m or zone not in ("us-eastern", "uk", "cet"):
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    std = {"us-eastern": -5, "uk": 0, "cet": 1}[zone]
    utc_std = dt.datetime(day.year, day.month, day.day, h, mi, tzinfo=dt.timezone.utc) - dt.timedelta(hours=std)
    if zone == "us-eastern":
        start = dt.datetime.combine(_nth_sunday(day.year, 3, 2), dt.time(7), dt.timezone.utc)
        end = dt.datetime.combine(_nth_sunday(day.year, 11, 1), dt.time(6), dt.timezone.utc)
    else:
        start = dt.datetime.combine(_nth_sunday(day.year, 3, -1), dt.time(1), dt.timezone.utc)
        end = dt.datetime.combine(_nth_sunday(day.year, 10, -1), dt.time(1), dt.timezone.utc)
    return utc_std - dt.timedelta(hours=1) if start <= utc_std < end else utc_std


def fetch_text(url: str, cache: dict | None = None) -> str:
    """GET with If-None-Match / If-Modified-Since from the last answer. A 304 returns the body
    kept in memory (a game's state is still worked out against the clock). 403/429 raise
    HTTPError: the loop treats them as a refusal and waits."""
    cache = HTTP_CACHE if cache is None else cache
    hdr = {"User-Agent": PUBLIC_UA, "Accept-Encoding": "gzip"}
    old = cache.get(url)
    if old:
        if old[0]:
            hdr["If-None-Match"] = old[0]
        if old[1]:
            hdr["If-Modified-Since"] = old[1]
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=TIMEOUT_S) as r:
            body = r.read(20 * 1024 * 1024)
            if r.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            text = body.decode("utf-8", "replace")
            cache[url] = (r.headers.get("ETag"), r.headers.get("Last-Modified"), text)
            return text
    except urllib.error.HTTPError as e:
        if e.code == 304 and old:
            return old[2]
        raise


def nfl_games(text: str, now: dt.datetime | None = None) -> list[dict]:
    """nflverse games.csv (kickoff gameday + gametime, US Eastern). Scores appear only when a game
    is over (seen 2026-10-04: the 20:20 ET game had none at 21:30 ET), so: scores -> final; none and
    kickoff ahead -> pre; none and started less than LIVE_WINDOW ago -> live (no score, no clock);
    none and long past -> dropped. There is no TV column."""
    now = now or _now()
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        try:
            day = dt.date.fromisoformat(r.get("gameday") or "")
        except ValueError:
            continue
        start = _local_to_utc(day, r.get("gametime"), "us-eastern")
        a, h = NFL_TEAMS.get(r.get("away_team") or ""), NFL_TEAMS.get(r.get("home_team") or "")
        if not start or not a or not h:
            continue
        a_sc, h_sc = _int(r.get("away_score")), _int(r.get("home_score"))
        if a_sc is not None and h_sc is not None:
            state, detail = "final", "Final" + ("/OT" if r.get("overtime") == "1" else "")
        elif now < start:
            state, detail, a_sc, h_sc = "pre", "", None, None
        elif now < start + LIVE_WINDOW:
            state, detail, a_sc, h_sc = "live", "In progress", None, None
        else:
            continue
        side = {}
        for k, abbr, (city, nick), sc in (("away", r["away_team"], a, a_sc), ("home", r["home_team"], h, h_sc)):
            full = f"{city} {nick}"
            side[k] = {"name": full, "nick": nick, "abbr": abbr, "score": sc, "keys": team_keys(full, nick)}
        out.append(_game("nfl", r.get("game_id"), start.isoformat(), state, detail, side, r.get("stadium") or None, [], None))
    PUBLIC_TEAMS["nfl"] = [{"league": "nfl", "name": f"{c} {n}", "nick": n, "keys": team_keys(f"{c} {n}", n)}
                           for c, n in NFL_TEAMS.values()]
    return [x for x in out if x]


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _club(name: str) -> str:
    """"Manchester United FC" -> "Manchester United"; "AFC Bournemouth" -> "Bournemouth"; "(ESP)" dropped."""
    name = re.sub(r"\s*\([A-Z]{3}\)\s*$", "", name.strip())
    name = re.sub(r"\s+A?FC$", "", name)
    return re.sub(r"^AFC\s+", "", name).strip()


def _club_side(raw: str, score: int | None) -> dict:
    short = _club(raw)
    names = [raw, short] + CLUB_ALIASES.get(short, [])
    return {"name": short, "nick": short, "abbr": None, "score": score, "keys": team_keys(*[_fold(n) for n in names])}


def _football_game(league: str, day: dt.date, hhmm: str | None, zone: str, t1: str, t2: str,
                   s1: int | None, s2: int | None, detail: str) -> dict | None:
    """Both scores -> final; else pre. Never live: the files are updated weekly. t1 is the home side."""
    start = _local_to_utc(day, hhmm or "12:00", zone)      # a match filed without a time is put at noon
    played = s1 is not None and s2 is not None
    side = {"home": _club_side(t1, s1 if played else None), "away": _club_side(t2, s2 if played else None)}
    return _game(league, f"{day}-{norm(_fold(_club(t1)))}-{norm(_fold(_club(t2)))}", start.isoformat() if start else None,
                 "final" if played else "pre", detail if played else "", side, None, [], None)


def _ft(score) -> tuple[int | None, int | None, str]:
    """openfootball scores: {"ht": [..], "ft": [h, a], "et": [h, a], "p": [h, a]} or a bare [h, a]."""
    if isinstance(score, list) and len(score) == 2:
        return _int(score[0]), _int(score[1]), "FT"
    if isinstance(score, dict):
        for key, label in (("et", "AET"), ("ft", "FT")):
            v = score.get(key)
            if isinstance(v, list) and len(v) == 2:
                p = score.get("p")
                if key == "et" and isinstance(p, list) and len(p) == 2:
                    label = f"pens {p[0]}-{p[1]}"
                return _int(v[0]), _int(v[1]), label
    return None, None, ""


def football_json_games(league: str, data: dict) -> list[dict]:
    """football.json (openfootball): {"matches": [{date, time (UK local), team1 = home, team2 = away, score}]}."""
    out = []
    teams: dict[str, dict] = {}
    for m in data.get("matches") or []:
        try:
            day = dt.date.fromisoformat(m.get("date") or "")
        except ValueError:
            continue
        if not m.get("team1") or not m.get("team2"):
            continue
        s1, s2, label = _ft(m.get("score"))
        out.append(_football_game(league, day, m.get("time"), "uk", m["team1"], m["team2"], s1, s2, label))
        for raw in (m["team1"], m["team2"]):
            side = _club_side(raw, None)
            teams[side["name"]] = {"league": league, "name": side["name"], "nick": side["nick"], "keys": side["keys"]}
    PUBLIC_TEAMS[league] = list(teams.values())
    return [x for x in out if x]


_MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
_UCL_DAY = re.compile(r"^\s{1,4}(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+([A-Z][a-z]{2})\s+(\d{1,2})(?:\s+(\d{4}))?\s*$")
_UCL_TIME = re.compile(r"^\s+(\d{1,2}:\d\d)\s+(\S.*)$")


def ucl_games(text: str) -> list[dict]:
    """champions-league cl.txt (Football.TXT): "= UEFA Champions League 2025/26", day lines
    "  Tue Sep 16 2025" (the year only on the first), match lines "    18:45  A (ESP)  v B (ENG)  0-2 (0-0)"
    where the next matches of that day inherit the time. Kickoffs are read as Central European time."""
    head = re.search(r"^=.*?(\d{4})/\d\d", text, re.M)
    year = int(head.group(1)) if head else None
    day, hhmm, last_month = None, None, 0
    out = []
    teams: dict[str, dict] = {}
    for line in text.splitlines():
        d = _UCL_DAY.match(line)
        if d:
            mon = _MONTHS.get(d.group(1))
            if not mon:
                continue
            if d.group(3):
                year = int(d.group(3))
            elif year is not None and last_month and mon < last_month:
                year += 1
            last_month = mon
            try:
                day = dt.date(year, mon, int(d.group(2))) if year else None
            except ValueError:
                day = None
            hhmm = None
            continue
        if day is None or " v " not in line:
            continue
        body = line
        t = _UCL_TIME.match(line)
        if t:
            hhmm, body = t.group(1), t.group(2)
        left, right = re.split(r"\s+v\s+", body.strip(), maxsplit=1)
        sc = re.match(r"^(.*?)\s{2,}(\d+)-(\d+)\b(.*)$", right)
        if sc:
            team2, s1, s2, rest = sc.group(1), int(sc.group(2)), int(sc.group(3)), sc.group(4)
            label = "FT"
            aet = re.search(r"(\d+)-(\d+)\s+a\.e\.t\.", rest)
            if "pen." in rest and aet:                 # "4-3 pen. 1-1 a.e.t.": the match ended 1-1 after extra time
                label = f"pens {s1}-{s2}"
                s1, s2 = int(aet.group(1)), int(aet.group(2))
            elif "a.e.t." in rest:
                label = "AET"
        else:
            team2, s1, s2, label = right.strip(), None, None, ""
        g = _football_game("ucl", day, hhmm, "cet", left, team2, s1, s2, label)
        if g:
            out.append(g)
        for raw in (left, team2):
            side = _club_side(raw, None)
            teams[side["name"]] = {"league": "ucl", "name": side["name"], "nick": side["nick"], "keys": side["keys"]}
    PUBLIC_TEAMS["ucl"] = list(teams.values())
    return out


# ---------------------------------------------------------------------------
# League tables (standings), computed from the full-season results the adapters already fetched
# (before in_window cuts them to a week). SQLite only (owner direction 2026-10-04: nothing new on
# Postgres); the engine reads the `standings` table, nothing is fetched at search time.
# ---------------------------------------------------------------------------
NFL_DIVISIONS = {
    "AFC East": ("BUF", "MIA", "NE", "NYJ"), "AFC North": ("BAL", "CIN", "CLE", "PIT"),
    "AFC South": ("HOU", "IND", "JAX", "TEN"), "AFC West": ("DEN", "KC", "LV", "LAC"),
    "NFC East": ("DAL", "NYG", "PHI", "WAS"), "NFC North": ("CHI", "DET", "GB", "MIN"),
    "NFC South": ("ATL", "CAR", "NO", "TB"), "NFC West": ("ARI", "LA", "SF", "SEA"),
}
STANDINGS_LEAGUES = ("nfl", "epl", "championship")
PUBLIC_STANDINGS: dict[str, list[dict]] = {}     # league -> table rows; the adapters set it, the loop stores it


def _srow(league, grp, team, nick, w, d, l, pf, pa, points, extra) -> dict:
    return {"league": league, "grp": grp, "team": team, "nick": nick, "played": w + d + l, "won": w, "drawn": d,
            "lost": l, "for_": pf, "against": pa, "points": points, "extra": extra}


def nfl_standings(text: str) -> tuple[list[dict], str | None]:
    """nflverse games.csv -> (rows, as_of). The newest season that has a finished regular-season
    game (the file holds every season since 1999); a finished game has both scores; playoffs
    (game_type != REG) are not counted. W-L-T, points for/against, win% (ties count half) stored
    x1000 in `points`; sorted by win%, then point differential, then name. Groups = divisions."""
    rec: dict[str, list[int]] = {}               # abbr -> [w, l, t, pf, pa]
    season, as_of, counted = None, None, []
    for r in csv.DictReader(io.StringIO(text)):
        if (r.get("game_type") or "") != "REG":
            continue
        a_sc, h_sc, yr = _int(r.get("away_score")), _int(r.get("home_score")), _int(r.get("season"))
        if a_sc is None or h_sc is None or yr is None or r.get("away_team") not in NFL_TEAMS or r.get("home_team") not in NFL_TEAMS:
            continue
        try:
            day = dt.date.fromisoformat(r.get("gameday") or "")
        except ValueError:
            continue
        counted.append((yr, day, r["away_team"], a_sc, r["home_team"], h_sc))
        season = yr if season is None or yr > season else season
    for yr, day, away, a_sc, home, h_sc in counted:
        if yr != season:
            continue
        as_of = day if as_of is None or day > as_of else as_of
        for team, pf, pa in ((away, a_sc, h_sc), (home, h_sc, a_sc)):
            t = rec.setdefault(team, [0, 0, 0, 0, 0])
            t[0 if pf > pa else 1 if pf < pa else 2] += 1
            t[3] += pf
            t[4] += pa
    if not rec:
        return [], None
    rows = []
    for grp, abbrs in NFL_DIVISIONS.items():
        teams = []
        for ab in abbrs:
            w, l, t, pf, pa = rec.get(ab, [0, 0, 0, 0, 0])
            n = w + l + t
            pct = (w + t / 2) / n if n else 0.0
            city, nick = NFL_TEAMS[ab]
            teams.append((-pct, -(pf - pa), f"{city} {nick}", _srow("nfl", grp, f"{city} {nick}", nick, w, t, l, pf, pa,
                                                                    round(pct * 1000), {"pct": round(pct, 3), "diff": pf - pa})))
        for rank, (*_k, row) in enumerate(sorted(teams, key=lambda x: x[:3]), 1):
            rows.append(dict(row, rank=rank))
    return rows, as_of.isoformat()


def football_standings(league: str, games: list[dict]) -> tuple[list[dict], str | None]:
    """Normalised football games (all of the season) -> (rows, as_of): 3 points a win, 1 a draw;
    sorted by points, goal difference, goals for, then name. One group, 'league'. Every club that
    appears in the fixtures is listed; only finished games with both scores count."""
    rec: dict[str, list[int]] = {}               # club -> [w, d, l, gf, ga]
    as_of = None
    for g in games:
        for side in (g["home"], g["away"]):
            rec.setdefault(side["name"], [0, 0, 0, 0, 0])
        if g["state"] != "final" or g["home"]["score"] is None or g["away"]["score"] is None:
            continue
        day = g["start_utc"].date()
        as_of = day if as_of is None or day > as_of else as_of
        for me, you in ((g["home"], g["away"]), (g["away"], g["home"])):
            t = rec[me["name"]]
            t[0 if me["score"] > you["score"] else 2 if me["score"] < you["score"] else 1] += 1
            t[3] += me["score"]
            t[4] += you["score"]
    if as_of is None:
        return [], None
    table = sorted(rec.items(), key=lambda kv: (-(3 * kv[1][0] + kv[1][1]), -(kv[1][3] - kv[1][4]), -kv[1][3], kv[0]))
    return [dict(_srow(league, "league", name, name, w, d, l, gf, ga, 3 * w + d, {"gd": gf - ga}), rank=i)
            for i, (name, (w, d, l, gf, ga)) in enumerate(table, 1)], as_of.isoformat()


def store_standings(conn, league: str, rows: list[dict], as_of: str | None) -> bool:
    """Replace a league's table in one transaction (idempotent). Nothing on Postgres. An empty
    table (a new season with no result yet) clears the old season's rather than showing it."""
    if not newsdb.is_sqlite():
        return False
    upd = newsdb.ts(newsdb.now())
    with conn.transaction():
        conn.execute("DELETE FROM standings WHERE league = %s", (league,))
        conn.executemany("""INSERT INTO standings (league, grp, rank, team, nick, played, won, drawn, lost, for_, against,
                                points, extra, updated, as_of) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                         [(league, r["grp"], r["rank"], r["team"], r["nick"], r["played"], r["won"], r["drawn"], r["lost"],
                           r["for_"], r["against"], r["points"], json.dumps(r["extra"]), upd, as_of) for r in rows])
    return True


def season_dirs(today: dt.date) -> list[str]:
    """["2026-27", "2025-26"]: this season, then the one before (a new season's file may not exist yet)."""
    y = today.year if today.month >= 7 else today.year - 1
    return [f"{y}-{(y + 1) % 100:02d}", f"{y - 1}-{y % 100:02d}"]


def fetch_season_file(template: str, today: dt.date, **kw) -> str | None:
    """The newest season's file that exists. A 404 is a missing file, not a failure; anything
    else (403, 429, 5xx) propagates."""
    for season in season_dirs(today):
        url = template.format(season=season, **kw)
        try:
            return fetch_text(url)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            log(f"{url}: 404, no file for season {season}")
    return None


def in_window(games: list[dict], today: dt.date) -> list[dict]:
    """What prune_games would keep: a whole season must not be inserted just to be deleted again."""
    lo = dt.datetime.combine(today - dt.timedelta(days=KEEP_DAYS_BACK), dt.time(0), dt.timezone.utc)
    hi = dt.datetime.combine(today + dt.timedelta(days=STATIC_AHEAD_DAYS + 1), dt.time(0), dt.timezone.utc)
    return [g for g in games if lo <= g["start_utc"] < hi]


def fetch_public(league: str, today: dt.date) -> list[dict]:
    PUBLIC_STANDINGS.pop(league, None)           # a table is stored only from THIS fetch's season file
    if league == "nfl":
        text = fetch_text(NFLVERSE_URL)
        PUBLIC_STANDINGS["nfl"] = nfl_standings(text)
        return in_window(nfl_games(text, _now()), today)
    if league == "ucl":
        text = fetch_season_file(UCL_URL, today)
        return in_window(ucl_games(text), today) if text else []
    text = fetch_season_file(FOOTBALL_JSON, today, file={"epl": "en.1.json", "championship": "en.2.json"}[league])
    if not text:
        return []
    games = football_json_games(league, json.loads(text))
    PUBLIC_STANDINGS[league] = football_standings(league, games)
    return in_window(games, today)


def public_every(league: str, now: dt.datetime) -> int:
    """nflverse every 10 minutes in the season (September-February), else every 6 hours; openfootball every 6 hours."""
    if league == "nfl" and now.month in NFLVERSE_SEASON_MONTHS:
        return NFLVERSE_EVERY_S
    return STATIC_IDLE_S


# The readers for sources whose terms allow personal use only (MLB's and the NHL's own feeds, ESPN's scoreboard) are a
# separate file, sports_personal.py, which is not part of the public copy of this code (2026-11-13). Without it this
# program reads only the sources whose licences allow anyone to use them (SOURCES below).
PERSONAL_FORMATS: dict = {}
try:
    import sports_personal as _personal
except ImportError:
    _personal = None
if _personal is not None:
    _personal.use(globals())
    SOURCES = {**_personal.SOURCES, **SOURCES}
    TEAM_URLS = _personal.TEAM_URLS
    PERSONAL_FORMATS = _personal.PERSONAL_FORMATS
    for _n in _personal.EXPORTS:
        globals()[_n] = getattr(_personal, _n)


def builtin_leagues(env=None) -> list[str]:
    """SPORTS_PUBLIC=1 (search.logout.cloud): only sources whose licence allows the public site.
    SPORTS_BUILTIN=0: none at all (customer engines). Otherwise every built-in source."""
    env = os.environ if env is None else env
    if env.get("SPORTS_BUILTIN", "1") == "0":
        return []
    if env.get("SPORTS_PUBLIC", "0") == "1":
        return [lg for lg, s in SOURCES.items() if s["public_ok"]]
    return list(SOURCES)


# Formats a USER can point their own engine at (settings.json "sources"). "mlb" is statsapi.mlb.com's schedule JSON
# and "nhl" api-web.nhle.com's /v1/schedule/now: their terms allow individual, non-commercial use, so each
# household fetches its own (the built-in MLB/NHL sources stay off on customer boxes and on the public site).
BLOCKED_WAIT_S = 24 * 3600


def personal_sources(path: str | None = None) -> dict:
    """settings.json "sources" -> {league: (url template, format)}; invalid entries skipped."""
    path = path if path is not None else os.environ.get("SPORTS_SETTINGS", "")
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            src = json.load(f).get("sources") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    out = {}
    for lg, v in src.items() if isinstance(src, dict) else []:
        url = str((v or {}).get("url") or "") if isinstance(v, dict) else ""
        fmt = (v or {}).get("format", "espn") if isinstance(v, dict) else ""
        if re.match(r"^[a-z]{2,12}$", str(lg)) and fmt in PERSONAL_FORMATS and re.match(r"^https://[^\s/]+/\S*$", url):
            out[lg] = (url, fmt)
    return out


def fetch_personal(league: str, url: str, fmt: str, today: dt.date, live_only: bool = False) -> list[dict]:
    if "{start}" in url or "{end}" in url:           # a date range in one request (MLB): yesterday .. a week ahead
        a, b = window(today)
        return PERSONAL_FORMATS[fmt](league, fetch_json(url.replace("{start}", f"{a}").replace("{end}", f"{b}")))
    if "{date}" not in url:
        return PERSONAL_FORMATS[fmt](league, fetch_json(url))
    a, b = window(today)
    days = [today - dt.timedelta(days=1), today] if live_only else [a + dt.timedelta(days=i) for i in range((b - a).days + 1)]
    games: list[dict] = []
    for i, d in enumerate(days):
        if i:
            time.sleep(1)
        games += PERSONAL_FORMATS[fmt](league, fetch_json(url.replace("{date}", f"{d:%Y%m%d}")))
    return games


def fetch_league(league: str, today: dt.date, live_only: bool = False) -> list[dict]:
    """live_only: a game is on — ESPN is asked for yesterday and today only (it takes one day
    per request; the full week is fetched on the idle pace)."""
    personal = {} if os.environ.get("SPORTS_PUBLIC") == "1" else personal_sources()    # the public site has no user sources
    if league in personal:
        return fetch_personal(league, *personal[league], today, live_only)
    if league in STATIC_LEAGUES:
        return fetch_public(league, today)
    a, b = window(today)
    if _personal is None:                # this copy has no reader for that league's source
        return []
    return _personal.fetch_live(league, today, live_only, a, b)


# NFL comes from nflverse (see SOURCES). NBA stays OFF, and so does ESPN for the NFL: ESPN's
# scoreboard answered the box with Akamai "Access Denied" (403, 2026-09-25) though a workstation
# request minutes earlier worked, and cdn.nba.com refuses non-browsers. Getting past either means
# disguising the client — not done (respect blocks, never evade). The ESPN code stays for a
# user's own source.
BUILTIN = builtin_leagues()
LEAGUES = BUILTIN


def leagues() -> list[str]:
    """Built-in leagues plus every league the user gave a source for (re-read each loop). On the
    public site (SPORTS_PUBLIC=1) only the sources whose licence allows it."""
    builtin = builtin_leagues()
    if os.environ.get("SPORTS_PUBLIC") == "1":
        return builtin
    return builtin + [lg for lg in personal_sources() if lg not in builtin]


def write_status(status: dict) -> None:
    path = os.environ.get("SPORTS_STATUS", "")
    if not path:
        return
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(status, f)
        os.replace(tmp, path)
    except OSError:
        pass


def store(conn, games: list[dict]) -> None:
    upd = newsdb.ts(newsdb.now())
    for g in games:
        a, h = g["away"], g["home"]
        conn.execute("""INSERT INTO games (league, game_id, start_utc, state, detail,
                            away_name, away_nick, away_abbr, away_score, home_name, home_nick, home_abbr, home_score,
                            away_keys, home_keys, venue, broadcasts, url, updated)
                        VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s,%s, %s)
                        ON CONFLICT (league, game_id) DO UPDATE SET start_utc = EXCLUDED.start_utc,
                            state = EXCLUDED.state, detail = EXCLUDED.detail, away_score = EXCLUDED.away_score,
                            home_score = EXCLUDED.home_score, venue = EXCLUDED.venue,
                            broadcasts = EXCLUDED.broadcasts, url = EXCLUDED.url, updated = EXCLUDED.updated""",
                     (g["league"], g["game_id"], g["start_utc"], g["state"], g["detail"],
                      a["name"], a["nick"], a["abbr"], a["score"], h["name"], h["nick"], h["abbr"], h["score"],
                      a["keys"], h["keys"], g["venue"], g["broadcasts"], g["url"], upd))


def busy(conn, league: str) -> bool:
    """A game on, about to start, or just ended: refresh at the live pace."""
    now, ts = newsdb.now(), newsdb.ts
    return bool(conn.execute("""SELECT 1 FROM games WHERE league = %s AND (state = 'live'
                                OR (state = 'pre' AND start_utc BETWEEN %s AND %s)
                                OR (state = 'final' AND updated > %s AND start_utc > %s)) LIMIT 1""",
                             (league, ts(now - dt.timedelta(hours=3)), ts(now + SOON),
                              ts(now - dt.timedelta(minutes=10)), ts(now - dt.timedelta(hours=6)))).fetchone())


def prune_games(conn) -> None:
    conn.execute("DELETE FROM games WHERE start_utc < %s", (newsdb.ts(newsdb.now() - dt.timedelta(days=KEEP_DAYS_BACK)),))


def next_interval(is_busy: bool, fails: int) -> float:
    base = LIVE_EVERY_S if is_busy else IDLE_EVERY_S
    return min(6 * 3600, base * (2 ** fails)) if fails else base


def static_league(lg: str) -> bool:
    """A league read from a published file (nflverse, openfootball) — unless the user gave their own source."""
    return lg in STATIC_LEAGUES and (os.environ.get("SPORTS_PUBLIC") == "1" or lg not in personal_sources())


def next_static(lg: str, fails: int) -> float:
    """The file's own cadence, never busy()'s minute: a kickoff passed without a result must not make us hammer a file host."""
    base = public_every(lg, _now())
    return min(6 * 3600, base * (2 ** fails)) if fails else base


def ensure_database() -> None:
    """A user's engine starts from a snapshot with no news database: create it (PGDATABASE).
    With NEWS_SQLITE: the file and its tables."""
    if newsdb.is_sqlite():
        newsdb.ensure_sqlite_schema()
        return
    import psycopg
    try:
        psycopg.connect(autocommit=True, connect_timeout=10).close()
    except psycopg.OperationalError as e:
        if "does not exist" not in str(e):
            raise
        try:                                 # news + sports start together: one of them wins
            with psycopg.connect(dbname="postgres", autocommit=True) as c:
                c.execute(f'CREATE DATABASE "{os.environ.get("PGDATABASE", "news")}"')
        except (psycopg.errors.DuplicateDatabase, psycopg.errors.UniqueViolation):
            pass


def run(once: bool = False) -> None:
    ensure_database()
    with newsdb.connect() as conn:
        if not newsdb.is_sqlite():
            conn.execute(SCHEMA)
        due: dict = {}
        fails: dict = {}
        full_due: dict = {}                        # the whole week, at the idle pace
        teams_due: dict = {}                       # each built-in league's team list, daily
        status: dict = {}
        for lg in DROPPED_LEAGUES:                 # stale soccer rows from before 2026-11-14, unless a user's own source has it
            if lg not in leagues():
                for t in ("games", "standings", "teams"):
                    conn.execute(f"DELETE FROM {t} WHERE league = %s", (lg,))
        while True:
            active = leagues()
            for lg in list(status):
                if lg not in active:               # the user removed the source
                    status.pop(lg)
                    conn.execute("DELETE FROM games WHERE league = %s", (lg,))
            for lg in active:
                if lg in TEAM_URLS and time.time() >= teams_due.get(lg, 0.0):
                    try:
                        data = fetch_json(TEAM_URLS[lg])
                        tm = mlb_teams(data) if lg == "mlb" else nhl_teams(data)
                        store_teams(conn, tm)
                        log(f"{lg}: {len(tm)} teams")
                        teams_due[lg] = time.time() + TEAMS_EVERY_S
                    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as e:
                        log(f"{lg} teams: {type(e).__name__}: {str(e)[:120]}")
                        teams_due[lg] = time.time() + 3600          # try again in an hour, never harder
            for lg in active:
                due.setdefault(lg, 0.0)
                fails.setdefault(lg, 0)
                full_due.setdefault(lg, 0.0)
                if time.time() < due[lg]:
                    continue
                static = static_league(lg)
                try:
                    games = fetch_league(lg, _now().date(),
                                         live_only=not static and busy(conn, lg) and full_due[lg] > time.time())
                    if not (busy(conn, lg) and full_due[lg] > time.time()):
                        full_due[lg] = time.time() + IDLE_EVERY_S
                    store(conn, games)
                    if static:
                        store_teams(conn, PUBLIC_TEAMS.get(lg, []))
                        if lg in PUBLIC_STANDINGS:                      # computed from this fetch's full season
                            store_standings(conn, lg, *PUBLIC_STANDINGS.pop(lg))
                    fails[lg] = 0
                    live = sum(1 for g in games if g["state"] == "live")
                    status[lg] = {"ok": True, "at": int(time.time()), "games": len(games)}
                    if live or once:
                        log(f"{lg}: {len(games)} games, {live} live")
                except urllib.error.HTTPError as e:
                    fails[lg] += 1
                    refused = e.code in (401, 403, 429)
                    status[lg] = {"ok": False, "at": int(time.time()), "error": f"HTTP {e.code}" + (" (refused)" if refused else "")}
                    log(f"{lg}: HTTP {e.code} (failure {fails[lg]})" + ("; source refused — waiting a day, not retrying harder" if refused else ""))
                    if refused:
                        due[lg] = time.time() + BLOCKED_WAIT_S
                        continue
                except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, csv.Error) as e:
                    fails[lg] += 1
                    status[lg] = {"ok": False, "at": int(time.time()), "error": type(e).__name__}
                    log(f"{lg}: {type(e).__name__}: {str(e)[:120]} (failure {fails[lg]})")
                due[lg] = time.time() + (next_static(lg, fails[lg]) if static else next_interval(busy(conn, lg), fails[lg]))
            write_status(status)
            prune_games(conn)
            if once:
                return
            time.sleep(TICK_S)


if __name__ == "__main__":
    run(once="--once" in sys.argv)
