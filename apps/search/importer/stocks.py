"""Stock and ticker searches (2026-09-30): "netflix stock", "nflx share price", "$nflx".

search-api answers them with links only — a live quote page, the company's own site and its SEC
filings — never a price we fetch or pass on (exchange data licences; see
the internal stock-queries note and ADR-061). These tables say which company a
query names:

  stock_tickers  ticker, exchange, name, cik, qid, label, site, sitelinks — one row per ticker
  stock_keys     key -> ticker: the SEC name and Wikidata's label/aliases, normalised, and the ticker

Sources: SEC's company/ticker/exchange list as republished daily by sec-cik-mapper (MIT; the data
is SEC's, US government) and Wikidata (CC0) joined on the SEC company id (P5531) for the name
people use ("Netflix", not "Netflix Inc"), aliases and the official website. Facts only.

    search_import.py stocks-load --mappings /import/stocks/mappings.csv [--ir /import/stocks/ir.tsv]
                                 [--funds /import/stocks/mf-mappings.csv] [--words /import/stocks/words.txt]
"""
from __future__ import annotations

import csv
import re
import time

from wikilinks import fetch as _fetch

EXCHANGES = {"Nasdaq", "NYSE", "CBOE", "OTC"}
PFX = """PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
"""
WD = {
    "label": "?item wdt:P5531 ?cik ; rdfs:label ?v FILTER(LANG(?v) = \"en\")",
    "alias": "?item wdt:P5531 ?cik ; skos:altLabel ?v FILTER(LANG(?v) = \"en\")",
    "site": "?item wdt:P5531 ?cik ; wdt:P856 ?v",
    "sitelinks": "?item wdt:P5531 ?cik ; wikibase:sitelinks ?v",
}
_SUFFIX = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|companies|ltd|limited|plc|llc|lp|l p|"
                     r"sa|nv|ag|se|class [a-c]|common stock|ordinary shares|the)\b")


# Names people use that neither the SEC name nor Wikidata's aliases give (2026-09-30 check of
# the most-searched companies): the brand is a different company name, or a product line.
BRANDS = {"google": "GOOGL", "youtube": "GOOGL", "facebook": "META", "instagram": "META", "whatsapp": "META",
          "disney": "DIS", "exxon": "XOM", "chase": "JPM", "jp morgan": "JPM", "coke": "KO",
          "bank of america": "BAC", "bofa": "BAC", "goldman": "GS", "pepsi": "PEP", "mcdonalds": "MCD",
          "home depot": "HD", "johnson and johnson": "JNJ", "j and j": "JNJ", "procter and gamble": "PG",
          "p and g": "PG", "ups": "UPS", "fedex": "FDX", "boeing": "BA", "ford": "F", "gm": "GM",
          "general motors": "GM", "general electric": "GE", "ibm": "IBM", "amd": "AMD", "paypal": "PYPL"}


def log(msg: str) -> None:
    print(f"[stocks] {msg}", flush=True)


def stock_key(text: str) -> str:
    """'Netflix, Inc.' / 'The Coca-Cola Company' / 'Amazon.com Inc' -> 'netflix' / 'coca cola' / 'amazon'.
    Must match search_api.stock_key()."""
    t = text.lower().replace("&", " and ").replace(".com", " ").replace("'", "").replace("\u2019", "")
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = _SUFFIX.sub(" ", t)
    w = t.split()
    while w and w[-1] == "and":                     # "JPMorgan Chase & Co." -> "jpmorgan chase"
        w.pop()
    while w and w[0] == "and":
        w.pop(0)
    return " ".join(w)


def stock_keys(text: str) -> set[str]:
    """A name's lookup keys: normalised, without "and" (At&T -> "at t"), and with no spaces
    ("att", "cocacola") — people type names every way."""
    k = stock_key(text)
    if not k:
        return set()
    bare = " ".join(x for x in k.split() if x != "and")
    return {x for x in (k, bare, k.replace(" ", ""), bare.replace(" ", "")) if len(x) >= 2}


def _lit(v: str) -> str:
    v = v.strip()
    if v.startswith('"'):
        v = v[1:v.rfind('"')] if v.rfind('"') > 0 else v[1:]
    return v.replace("\\t", " ").replace('\\"', '"')


def _rows(name: str, where: str) -> list[list[str]]:
    rows = []
    with _fetch(name, PFX + f"SELECT DISTINCT ?item ?cik ?v WHERE {{ {where} }}") as resp:
        for line in resp:
            line = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
            if not line.startswith("?"):
                rows.append(line.rstrip("\n").split("\t"))
    time.sleep(2)
    with _fetch(name, PFX + f"SELECT (COUNT(*) AS ?n) WHERE {{ SELECT DISTINCT ?item ?cik ?v WHERE {{ {where} }} }}") as resp:
        want = int(_lit(resp.read().decode().splitlines()[1]))
    if abs(len(rows) - want) > max(5, want // 100_000):
        raise RuntimeError(f"{name}: QLever returned {len(rows)} of {want} rows — truncated, refusing")
    log(f"wikidata {name}: {len(rows)} rows")
    time.sleep(2)
    return rows


def _ir_pages(path: str | None) -> dict[str, str]:
    """irlinks.py's ir.tsv (domain, ir_url, link text) -> {company domain: IR page}."""
    out = {}
    if path:
        for line in open(path, encoding="utf-8"):
            p = line.rstrip("\n").split("\t")
            if len(p) >= 2 and p[1].startswith(("http://", "https://")):
                out[p[0].lower()] = p[1]
    return out


def _site_domain(site: str | None) -> str:
    host = re.sub(r"^https?://", "", site or "").split("/")[0].lower()
    return host[4:] if host.startswith("www.") else host


def _funds(path: str | None) -> list[tuple[str, int]]:
    """sec-cik-mapper's mutual_funds/mappings.csv (ETFs and mutual funds: VTI, VOO, VFIAX) ->
    (ticker, cik). No name and no exchange in that list."""
    out = []
    if path:
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("Ticker") and r.get("CIK"):
                    out.append((r["Ticker"].upper(), int(r["CIK"])))
    return out


def _words(path: str | None) -> set[str]:
    """Lowercase entries of a dictionary (SCOWL /usr/share/dict/american-english): a ticker that is
    an everyday word is never taken from a one-word query ("cat", "low", "spy"). Only the
    per-ticker yes/no is stored, not the list."""
    if not path:
        return set()
    return {w.strip() for w in open(path, encoding="utf-8", errors="replace") if w.strip().isalpha() and w.strip().islower()}


def cmd_stocks_load(conn, mappings: str, ir: str | None = None, funds: str | None = None,
                    words: str | None = None) -> None:
    t0 = time.time()
    tickers = []
    with open(mappings, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("Exchange") in EXCHANGES and r.get("Ticker") and r.get("CIK"):
                tickers.append((r["Ticker"].upper(), r["Exchange"], r["Name"], int(r["CIK"])))
    log(f"SEC list: {len(tickers)} tickers on {sorted(EXCHANGES)}")
    wd: dict[int, dict] = {}
    for name, where in WD.items():
        for p in _rows(name, where):
            if len(p) < 3:
                continue
            try:
                cik = int(re.sub(r"\D", "", _lit(p[1])) or "0")
            except ValueError:
                continue
            d = wd.setdefault(cik, {"qid": p[0].strip("<>").rsplit("/", 1)[-1], "alias": []})
            v = _lit(p[2]).strip("<>")
            if name == "alias":
                d["alias"].append(v)
            elif name == "sitelinks":
                d["sitelinks"] = int(v) if v.isdigit() else 0
            else:
                d.setdefault(name, v)
    conn.execute("DROP TABLE IF EXISTS stock_tickers_new, stock_keys_new")
    conn.execute("CREATE TABLE stock_tickers_new (ticker text PRIMARY KEY, exchange text, name text, cik integer, "
                 "qid text, label text, site text, sitelinks integer, ir text)")
    irs = _ir_pages(ir)
    conn.execute("CREATE TABLE stock_keys_new (key text, ticker text)")
    keys = set()
    with conn.cursor().copy("COPY stock_tickers_new FROM STDIN") as cp:
        seen = set()
        for tk, ex, nm, cik in tickers:
            if tk in seen:
                continue
            seen.add(tk)
            d = wd.get(cik, {})
            cp.write_row((tk, ex, nm, cik, d.get("qid"), d.get("label"), d.get("site"), d.get("sitelinks", 0),
                          irs.get(_site_domain(d.get("site")))))
            for name in [nm, d.get("label") or ""] + d.get("alias", [])[:8]:
                keys.update((k, tk) for k in stock_keys(name))
            keys.add((tk.lower(), tk))
            keys.add((tk.lower().replace("-", "."), tk))
            keys.add((tk.lower().replace("-", " "), tk))
    with conn.cursor().copy("COPY stock_tickers_new (ticker, exchange, name, cik) FROM STDIN") as cp:
        nf = 0
        for tk, cik in _funds(funds):
            if tk not in seen:                     # a listed share class wins over the fund list
                seen.add(tk)
                cp.write_row((tk, "FUND", tk, cik))
                keys.add((tk.lower(), tk))
                nf += 1
    log(f"funds: {nf} fund tickers (ETFs, mutual funds)")
    known = {tk for tk, *_ in tickers}
    keys.update((k, tk) for k, tk in BRANDS.items() if tk in known)
    with conn.cursor().copy("COPY stock_keys_new FROM STDIN") as cp:
        for k in sorted(keys):
            cp.write_row(k)
    conn.execute("CREATE INDEX stock_keys_new_key ON stock_keys_new (key)")
    dictionary = _words(words)
    conn.execute("ALTER TABLE stock_tickers_new ADD COLUMN word boolean NOT NULL DEFAULT false")
    if dictionary:
        conn.execute("UPDATE stock_tickers_new SET word = true WHERE lower(ticker) = ANY(%s)",
                     ([w for w in dictionary if 2 <= len(w) <= 5],))
    conn.execute("DROP TABLE IF EXISTS stock_tickers, stock_keys")
    conn.execute("ALTER TABLE stock_tickers_new RENAME TO stock_tickers")
    conn.execute("ALTER TABLE stock_keys_new RENAME TO stock_keys")
    conn.execute("ALTER INDEX stock_keys_new_key RENAME TO stock_keys_key")
    n, w, s, i = conn.execute("SELECT count(*), count(qid), count(site), count(ir) FROM stock_tickers").fetchone()
    log(f"stock_tickers: {n} tickers, {w} matched to Wikidata, {s} with an official site, {i} with an IR page; "
        f"{len(keys)} keys in {time.time() - t0:.0f} s")
