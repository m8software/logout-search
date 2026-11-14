#!/usr/bin/env python3
"""Intent slots per site (2026-11-10): login, returns, support, pricing, careers ... -> the page, taken
from the links of the site's own home page. Nothing is fetched here: the input is site-nav.py's fetch output.

    site-slots.py vocab  FETCHED.jsonl [FETCHED2.jsonl ...] [--top 150]   the most common link texts, and the slot table
    site-slots.py build  OUT.jsonl FETCHED.jsonl [FETCHED2 ...] [--picked SITENAV.jsonl]
                                                                          site -> slot -> {text, url}, same registered domain only
    site-slots.py report SLOTS.jsonl [--targets TARGETS.txt]              coverage: sites, slots per site, top slots (markdown)
    site-slots.py evalset SLOTS.jsonl OUT.tsv [--per-entity 2]            "name + section words" searches (open eval set)
    site-slots.py search-table TEMPLATES.jsonl OUT.jsonl                  the engine's site-search table from site-nav.py search output

The vocabulary (slot ids, the words a person types, the link texts that fill each slot) is serve/api/site_slots.py: the
engine, this tool and the unit tests all read that one file. A slot is filled by a link of the home page whose whole
text is one of the slot's link texts; the URL must be https and on the site's own registered domain (a "login" link to
another company's domain is what phishing imitates, and a status page on statuspage.io is not the site's own).
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import importlib.util
import json
import os
import re
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("site_slots", os.path.join(HERE, "..", "serve", "api", "site_slots.py"))
S = importlib.util.module_from_spec(_spec)
sys.modules["site_slots"] = S
_spec.loader.exec_module(S)
OPEN = os.path.join(HERE, "evalsets", "open")
SEED = "575"
_TRACKING = re.compile(r"^(?:utm_|lfm_|gclid$|fbclid$|mc_|_ga$|ref$|cmp$|cid$)", re.I)


def records(paths):
    """First record with links per site over the files (the main fetch first, then Common Crawl fill-ins)."""
    seen = {}
    for fn in paths:
        with open(fn, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                if r.get("status") == "ok" and r.get("links") and r["site"] not in seen:
                    seen[r["site"]] = r
    return seen


def norm_text(t: str) -> str:
    return " ".join(t.lower().split()).strip("›»>|·-–— ")


def vocab(a):
    recs = records(a.fetched)
    c, cn = collections.Counter(), collections.Counter()
    for site, r in recs.items():
        final = r.get("final") or f"https://{site}/"
        reg = S.reg_domain(S.host_of(final))
        ts, tn = set(), set()
        for href, text, nav in r["links"]:
            t = norm_text(text)
            if not t or len(t) > 40 or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            try:
                u = urllib.parse.urljoin(final, href)
            except ValueError:
                continue
            if S.reg_domain(S.host_of(u)) != reg:
                continue
            ts.add(t)
            if nav:
                tn.add(t)
        c.update(ts)
        cn.update(tn)
    print(f"## Link-text vocabulary\n\n{len(recs)} home pages with links (same-site links only; a text counts once per site)\n")
    print("| rank | link text | sites | in a nav element | slot |\n|---|---|---|---|---|")
    for i, (t, n) in enumerate(c.most_common(a.top), 1):
        print(f"| {i} | {t} | {n} | {cn[t]} | {S.slot_of_text(t) or ''} |")
    per = collections.Counter()
    for t, n in c.items():
        sid = S.slot_of_text(t)
        if sid:
            per[sid] += 0
    # sites per slot (a site counts once per slot)
    sites_per = collections.Counter()
    for site, r in recs.items():
        final = r.get("final") or f"https://{site}/"
        reg = S.reg_domain(S.host_of(final))
        got = set()
        for href, text, nav in r["links"]:
            sid = S.slot_of_text(text)
            if sid and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                try:
                    if S.reg_domain(S.host_of(urllib.parse.urljoin(final, href))) == reg:
                        got.add(sid)
                except ValueError:
                    pass
        sites_per.update(got)
    print("\n## Slots (sites with a same-site link filling the slot)\n\n| slot | sites | asked as | link texts (first 8) |\n|---|---|---|---|")
    for s in sorted(S.SLOTS, key=lambda s: -sites_per[s.id]):
        print(f"| {s.id} | {sites_per[s.id]} | {', '.join(s.asks[:4]) or '(nav only)'} | {', '.join(s.texts[:8])} |")
    top = sum(n for t, n in c.items() if S.slot_of_text(t))
    print(f"\nlink-text occurrences covered by a slot: {top} of {sum(c.values())} ({100 * top / max(1, sum(c.values())):.1f} %); the rest are product aisles, languages, numbers and one-off labels.")


def pick_slots(rec: dict) -> dict:
    """slot -> {text, url, nav}: the best same-site https link per slot (nav element first, then the shorter path)."""
    final = rec.get("final") or f"https://{rec['site']}/"
    reg = S.reg_domain(rec["site"])                              # the site's own registered domain: the rule the engine's loader applies
    fsp = urllib.parse.urlsplit(final)
    best = {}
    for href, text, nav in rec.get("links") or []:
        sid = S.slot_of_text(text)
        if not sid or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        try:
            u = urllib.parse.urljoin(final, href)
            sp = urllib.parse.urlsplit(u)
        except ValueError:
            continue
        if sp.scheme != "https" or not sp.hostname or sp.username or S.reg_domain(sp.hostname) != reg:
            continue
        if (sp.path.rstrip("/") or "/") == (fsp.path.rstrip("/") or "/") and not sp.query and sp.hostname == fsp.hostname:
            continue                                            # the home page itself
        if sp.port:
            u = urllib.parse.urlunsplit(sp._replace(netloc=sp.hostname))
        if sp.query or sp.fragment:
            kept = [(k, v) for k, v in urllib.parse.parse_qsl(sp.query, keep_blank_values=True) if not _TRACKING.match(k)]
            u = urllib.parse.urlunsplit(sp._replace(query=urllib.parse.urlencode(kept), fragment=""))
        rank = (0 if nav else 1, len(sp.path), u)
        if sid not in best or rank < best[sid][0]:
            best[sid] = (rank, {"text": " ".join(text.split())[:40], "url": u})
    return {k: v[1] for k, v in best.items()}


def build(a):
    recs = records(a.fetched)
    n = filled = 0
    out = {}
    for site, r in recs.items():
        out[site] = pick_slots(r)
    if a.picked:                                                # sitemap-sourced sites (no raw links): their picked sections
        with open(a.picked, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                if r["site"] in recs or not r.get("links"):
                    continue
                reg = S.reg_domain(r["site"])
                got = {}
                for x in r["links"]:
                    sid = S.slot_of_text(x.get("text", ""))
                    u = x.get("url", "")
                    if sid and sid not in got and u.startswith("https://") and S.reg_domain(S.host_of(u)) == reg:
                        got[sid] = {"text": x["text"][:40], "url": u}
                if got:
                    out[r["site"]] = got
    with open(a.out, "w", encoding="utf-8") as f:
        for site, slots in out.items():
            n += 1
            if slots:
                filled += 1
                f.write(json.dumps({"site": site, "slots": slots}, ensure_ascii=False) + "\n")
    print(f"{n} sites with a tree, {filled} with at least one slot -> {a.out}", file=sys.stderr)


def report(a):
    rows = [{"site": k, "slots": p["slots"]} for k, p in S.load_slots(a.slots).rows.items()]       # as the engine loads them
    per_slot = collections.Counter(s for r in rows for s in r["slots"])
    k = collections.Counter(min(len(r["slots"]), 10) for r in rows)
    print(f"## Slot table coverage\n\n{len(rows)} sites with at least one slot; {sum(len(r['slots']) for r in rows)} slot rows; "
          f"{sum(len(r['slots']) for r in rows) / max(1, len(rows)):.2f} slots per site on average\n")
    if a.targets:
        t = len({S.host_of(next((c for c in l.split("\t") if c.startswith("http")), "")) for l in open(a.targets, encoding="utf-8") if l.strip()} - {""})
        print(f"of {t} official-site targets: {100 * len(rows) / t:.1f} % have a slot\n")
    print("slots filled per site: " + ", ".join(f"{i}{'+' if i == 10 else ''}: {k[i]}" for i in range(1, 11)) + "\n")
    print("| slot | sites | share of sites with a slot |\n|---|---|---|")
    for sid, n in per_slot.most_common():
        print(f"| {sid} | {n} | {100 * n / len(rows):.1f} % |")
    intent = {s.id for s in S.SLOTS if s.asks}
    q = sum(1 for r in rows if any(s in intent for s in r["slots"]))
    print(f"\nsites with at least one slot a person asks for (not nav-only): {q}")


# A shared host is not the entity's own site: "agnez mo register" -> instagram.com/accounts/emailsignup
PLATFORMS = frozenset(("instagram.com", "facebook.com", "youtube.com", "twitter.com", "x.com", "linkedin.com", "tiktok.com", "wikipedia.org",
                       "spotify.com", "soundcloud.com", "linktr.ee", "github.io", "blogspot.com", "wordpress.com",
                       "medium.com", "pinterest.com", "reddit.com", "twitch.tv", "bandcamp.com", "github.com", "vimeo.com",
                       "wixsite.com", "weebly.com", "tumblr.com", "myshopify.com", "square.site", "carrd.co", "notion.site"))


def hsh(*p) -> str:
    return hashlib.md5((SEED + ":" + ":".join(map(str, p))).encode()).hexdigest()


def norm_host(d: str) -> str:
    d = d.lower().strip()
    return d[4:] if d.startswith("www.") else d


def evalset(a):
    slots = {site: p["slots"] for site, p in S.load_slots(a.slots).rows.items()}      # what the engine loads, nothing else
    cand = []                                                   # (query name, domain, entity, split, source set)
    for name in ("site-nav", "head-brand", "own-title"):
        with open(os.path.join(OPEN, name + ".tsv"), encoding="utf-8") as f:
            next(f)
            for line in f:
                c = line.rstrip("\n").split("\t")
                q, exp, stratum, split, ent = c[0], c[2], c[4], c[5], c[6]
                if name == "site-nav" and "template:bare" not in stratum:
                    continue
                if "(" in q or len(q.split()) > 3:
                    continue
                for d in exp.split("|"):
                    if norm_host(d) in slots and S.reg_domain(norm_host(d)) not in PLATFORMS and S.reg_domain(norm_host(d)) == norm_host(d):
                        cand.append((q, norm_host(d), ent, split, name))
                        break
    seen, rows, used = set(), [], collections.Counter()
    sites_per = collections.Counter(sid for v in slots.values() for sid in v)
    for q, d, ent, split, src in sorted(cand, key=lambda x: hsh("e", x[0], x[1])):
        if (ent, d) in seen or q in {r[0] for r in rows}:
            continue
        seen.add((ent, d))
        intent = [(sid, v) for sid, v in sorted(slots[d].items()) if S.SLOT_BY_ID[sid].asks and "?" not in v["url"]]   # norm_url drops a query: such a key cannot match
        # rarer slots first (a table of privacy and contact pages would measure nothing), at most --cap rows per slot
        intent.sort(key=lambda x: (sites_per[x[0]] > 3000, used[x[0]] // 8, hsh("s", d, x[0])))
        for sid, v in [x for x in intent if used[x[0]] < a.cap][: a.per_entity]:
            used[sid] += 1
            asks = S.SLOT_BY_ID[sid].asks[:3]
            ask = asks[int(hsh("a", d, sid)[:4], 16) % len(asks)]
            first = int(hsh("t", d, sid)[:4], 16) % 100 < 80
            query = f"{q} {ask}" if first else f"{ask} {q}"
            u = urllib.parse.urlsplit(v["url"])
            exp = (S.host_of(v["url"]) + (u.path.rstrip("/") or "") + (("?" + u.query) if u.query else "")).lower()
            rows.append((query.lower(), "account", exp, "site-slot",
                         f"slot:{sid},template:{'name-first' if first else 'slot-first'},from:{src}", split, ent))
    # one query once; the split is the source set's own (an entity is test in every set where it appears)
    uniq = {}
    for r in rows:
        uniq.setdefault(r[0], r)
    rows = sorted(uniq.values(), key=lambda r: (r[5], r[0]))
    with open(a.out, "w", encoding="utf-8") as f:
        f.write("query\tcategory\texpected\tset\tstratum\tsplit\tentity\n")
        for r in rows:
            f.write("\t".join(r) + "\n")
    sp = collections.Counter(r[5] for r in rows)
    print(f"{len(rows)} searches ({sp['dev']} dev / {sp['test']} test), {len({r[6] for r in rows})} entities -> {a.out}", file=sys.stderr)
    print("by slot: " + ", ".join(f"{k} {v}" for k, v in collections.Counter(r[4].split(',')[0][5:] for r in rows).most_common()), file=sys.stderr)


def search_table(a):
    n = k = 0
    with open(a.out, "w", encoding="utf-8") as f:
        for line in open(a.templates, encoding="utf-8"):
            r = json.loads(line)
            n += 1
            if r.get("template") and S.template_ok(r["template"], r["site"]):
                k += 1
                f.write(json.dumps({"site": r["site"], "template": r["template"], "source": r.get("source")}, ensure_ascii=False) + "\n")
    print(f"{n} sites, {k} with a valid template -> {a.out}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("vocab"); p.add_argument("fetched", nargs="+"); p.add_argument("--top", type=int, default=150)
    p = sub.add_parser("build"); p.add_argument("out"); p.add_argument("fetched", nargs="+"); p.add_argument("--picked")
    p = sub.add_parser("report"); p.add_argument("slots"); p.add_argument("--targets")
    p = sub.add_parser("evalset"); p.add_argument("slots"); p.add_argument("out"); p.add_argument("--per-entity", type=int, default=2); p.add_argument("--cap", type=int, default=60)
    p = sub.add_parser("search-table"); p.add_argument("templates"); p.add_argument("out")
    a = ap.parse_args()
    {"vocab": vocab, "build": build, "report": report, "evalset": evalset, "search-table": search_table}[a.cmd](a)


if __name__ == "__main__":
    main()
