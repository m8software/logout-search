"""Shared pieces of the search-quality tooling: where results live, the open sets, which engine
build answered, and the engine's query normalisation. Imported by sq-eval.py and sq-candidates.py; stdlib only.
DEV TOOLING — never imported by apps/search/serve."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
OPEN = Path(os.environ.get("SQ_OPEN") or TOOLS / "evalsets" / "open")      # SQ_OPEN: a test's fixture sets
SQ_DIR = Path(os.environ.get("SQ_DIR", Path.home() / ".local/share/logout-search/sq"))
# Answer keys: "accept" (the default) scores a row's expected answer AND the accepted alternatives in
# evalsets/open/accept/<set>.accept.tsv (every official site of the row's entity, tools/sq-accept-keys.py; a place's
# own website, tools/sq-accept-places.py); "primary" scores the expected answer alone, as every number before
# 2026-10-13 was. Each has its own frozen baseline: rows scored on one key set are never paired with rows scored on
# the other. baseline-rc8-keys2 was the accept baseline before the place keys: its place rows are scored without them.
# baseline-master-20261013 (dev AND test halves) replaced baseline-rc8-keys3 on 2026-10-13: keys3 was frozen on master
# 0ad83c68 and master's answers had moved since (evalsets/open/README.md, "Baselines"). A baseline is valid only for
# the serving code it was frozen on: re-freeze whenever a merge to master changes what the engine answers.
ACCEPT_DIR = "accept"
PAGE_DIR = "page"          # page-level gold sidecars (2026-11-10): page/<set>.page.tsv = query, entity, page; read by sq-eval's page@1
BASELINES = {"primary": "baseline-rc8", "accept": "baseline-master-20261013"}
KEYS = os.environ.get("SQ_KEYS", "accept")
if KEYS not in BASELINES:
    raise SystemExit(f"SQ_KEYS={KEYS!r}: accept or primary")
BASELINE = BASELINES[KEYS]
# every open set, in the order the table shows them; the hand/click sets are private and not here
DEFAULT_SETS = ("head-brand", "site-nav", "own-title", "maker-product", "wiki-redirect", "account",
                "typo-site", "typo-wiki", "place", "place-town", "typo-place")
# files in evalsets/open that are not scored open sets: the ambiguous-name lists, exclude.tsv, and the judged sets'
# files (judged-info.tsv, .verdicts.tsv, .exclude.tsv) — a judged set goes through --judged, not this loop
SKIP = ("ambiguous-", "exclude", "judged-")
_TOKEN = re.compile(r"[a-z0-9]+")


# Stack Exchange network hosts. Licence decision 2026-10-17 (docs/legal/SEARCH-DATA-SOURCES.md, condition 4): no
# Stack Exchange row is an input to any training set, whatever the label says.
_SE_HOSTS = frozenset({"stackexchange.com", "stackoverflow.com", "superuser.com", "askubuntu.com", "serverfault.com",
                       "mathoverflow.net", "stackapps.com"})


def is_stack_exchange_url(url: str) -> bool:
    host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host in _SE_HOSTS or host.endswith(".stackexchange.com") or host.endswith(".stackoverflow.com")


def norm_query(q: str) -> str:
    """lower-case, single-spaced, trimmed: the query as typed, made comparable."""
    return " ".join(q.lower().split())


def norm_key(q: str) -> str:
    """search_api.norm_key: letters and digits only, lower-case — the key the engine's exact tiers look up."""
    return "".join(_TOKEN.findall(q.lower()))


def load_tool(name: str, path: Path):
    """Import a hyphen-named tool (eval-queries.py, compare-evals.py) as a module."""
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def all_sets() -> list[str]:
    """Every scored set in evalsets/open (not the ambiguous-* lists), known order first."""
    here = {p.stem for p in OPEN.glob("*.tsv") if not p.name.startswith(SKIP)}
    return [s for s in DEFAULT_SETS if s in here] + sorted(here - set(DEFAULT_SETS))


def set_keys(keys: str) -> str:
    """Choose the answer keys for this process (sq-eval.py --keys). -> the baseline arm scored on them."""
    global KEYS, BASELINE
    if keys not in BASELINES:
        raise SystemExit(f"keys {keys!r}: accept or primary")
    KEYS, BASELINE = keys, BASELINES[keys]
    return BASELINE


def load_accept(name: str) -> dict:
    """{(query, entity): [token]} from evalsets/open/accept/<name>.accept.tsv; {} when the set has no sidecar."""
    path = OPEN / ACCEPT_DIR / f"{name}.accept.tsv"
    out: dict = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        cols = f.readline().rstrip("\n").split("\t")
        if cols[:2] != ["query", "entity"] or "accept" not in cols:
            raise SystemExit(f"{path}: not an accept sidecar (header {cols})")
        for line in f:
            if line.strip():
                r = dict(zip(cols, line.rstrip("\n").split("\t")))
                out[(r["query"], r["entity"])] = [t for t in r["accept"].split("|") if t]
    return out


def row_hit(eq, r: dict, result: dict) -> bool:
    """Is this result a right answer for this set row: its expected answer (eval-queries.hit) or, under the accept
    keys, one of the row's accepted alternatives (eval-queries.accept_hit). eq = the loaded eval-queries module."""
    exp = [] if r["expected"] == "-" else r["expected"].split("|")
    return eq.hit(r["category"], exp, result) or bool(r.get("accept")) and eq.accept_hit(r["category"], r["accept"], result)


def load_pages(name: str) -> dict:
    """{(query, entity): "host/path|host/path"} from evalsets/open/page/<name>.page.tsv; {} when the set has none."""
    path = OPEN / PAGE_DIR / f"{name}.page.tsv"
    out: dict = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            cols = f.readline().rstrip("\n").split("\t")
            if cols[:3] != ["query", "entity", "page"]:
                raise SystemExit(f"{path}: not a page sidecar (header {cols})")
            for line in f:
                if line.strip():
                    r = dict(zip(cols, line.rstrip("\n").split("\t")))
                    out[(r["query"], r["entity"])] = r["page"]
    return out


def load_set(name: str, split: str | None = None, limit: int = 0, keys: str | None = None) -> list[dict]:
    """Rows of one open set (query, category, expected, set, stratum, split, entity); one split or both. Under the
    accept keys (the default; keys= overrides KEYS) a row with accepted alternatives carries them as r["accept"]."""
    path = OPEN / f"{name}.tsv"
    rows = []
    accept = load_accept(name) if (keys or KEYS) == "accept" else {}
    pages = load_pages(name)
    with open(path, encoding="utf-8") as f:
        cols = f.readline().rstrip("\n").split("\t")
        if cols[:4] != ["query", "category", "expected", "set"]:
            raise SystemExit(f"{path}: not an open set (header {cols[:4]})")
        for line in f:
            if not line.strip():
                continue
            r = dict(zip(cols, line.rstrip("\n").split("\t")))
            if split and r.get("split") != split:
                continue
            alt = accept.get((r["query"], r.get("entity", "")))
            if alt:
                r["accept"] = alt
            pg = pages.get((r["query"], r.get("entity", "")))
            if pg:
                r["page"] = pg
            rows.append(r)
            if limit and len(rows) >= limit:
                break
    return rows


def engine_sidecar(url: str) -> dict | None:
    """What sq-engine.sh recorded for the engine at url (by port), or None."""
    port = urllib.parse.urlsplit(url).port
    p = SQ_DIR / "engines" / f"{port}.json"
    if port and p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return None
    return None


def engine_build(url: str, override: str | None) -> str | None:
    """The build hash results are cached under: --build, else the sidecar's."""
    if override:
        return override
    side = engine_sidecar(url)
    return side["build"] if side else None
