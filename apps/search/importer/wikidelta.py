#!/usr/bin/env python3
"""Wikipedia change sets: what changed since the articles we ship, without re-reading everything (owner 2026-11-15:
catch-up to the newest full dump, then a DAILY incremental refresh; docs/internal/wikipedia-freshness-plan.md).

The shipped Wikipedia rows are the ones wikiparse.py writes: (page_id, rev_id, title, headings, lead, infobox) plus
`aliases` (the titles of the redirects that point at the article). This tool keeps a small state file (one row per
article: rev id, title, a hash of what the index ships, its aliases; plus the redirect table) and, given newer data,
writes only the rows that really differ:

    wikidelta.py state   --articles articles.tsv.gz --redirects redirects.tsv.gz --out STATE.sqlite --dump enwiki-20260901
        the baseline: wikiparse.py's split-mode output of the dump the release holds (indexhost: wikipedia/out/)
    wikidelta.py catchup --dump enwiki-DATE-pages-articles-multistream.xml.bz2 --state STATE.sqlite --out DIR
                         --base-articles articles.tsv.gz [--procs N] [--dump-name enwiki-DATE] [--limit N]
        one pass over a newer full dump. A page whose rev id is the stored one is not parsed. Everything else is
        parsed with wikiparse.parse_article and kept only if its shipped fields differ. A page missing from the dump
        (or now a redirect or disambiguation page) is a deletion; a redirect change moves aliases.
    wikidelta.py incr    --state STATE.sqlite --out DIR --pages PAGES-INCR.xml.bz2 --day YYYYMMDD
                         [--hold-hours 8] [--now ISO] [--no-api]
        one day of Wikimedia's adds-changes dump (other/incr/enwiki/DAY/). Only a revision that has stood HOLD hours
        is taken (the newest such revision of a page), so vandalism that was reverted within the hold never ships.
        Deleted pages come from the delete log (the dumps carry no deletions).

Output DIR: changed.tsv.gz (op, page_id, rev_id, title, headings, lead, infobox, aliases; COPY text format),
deleted.tsv (page_id, title), summary.json, DONE. The state is updated in place only after DONE is written
(--no-commit leaves it untouched; the output holds the new state as STATE.next when --no-commit is used on a copy).
Every number in summary.json is a count of rows, so the run can be checked against the dump's own page counts.

Text of Wikipedia is CC BY-SA 4.0; nothing here is fetched except Wikimedia's own dumps and API (descriptive User-Agent,
one request at a time).
"""
from __future__ import annotations

import argparse
import bz2
import gzip
import hashlib
import json
import multiprocessing
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import wikiparse as W  # noqa: E402
except ModuleNotFoundError as _e:        # `state` reads only the baseline files and needs no parser
    W = None
    _WERR = _e

NS = "{http://www.mediawiki.org/xml/export-0.11/}"
UA = "logout-search-freshness/1 (https://search.logout.cloud)"
API = "https://en.wikipedia.org/w/api.php"
SEP = " | "

_ESC = re.compile(r"\\(.)")
_MAP = {"t": "\t", "n": "\n", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "\\": "\\"}


def log(msg: str) -> None:
    print(f"[wikidelta] {msg}", flush=True)


def open_binary(path: str):
    """A path, or `cmd:<shell command>` whose stdout is the file (`cmd:ssh indexhost sudo cat /mnt/.../dump.bz2`): the dump
    and the baseline can stay on the machine that holds them, so a work box with a small disk never stores 26 GB."""
    if path.startswith("cmd:"):
        import subprocess
        return subprocess.Popen(path[4:], shell=True, stdout=subprocess.PIPE, bufsize=1 << 20).stdout
    return open(path, "rb")


def pages(path: str):
    """wikiparse._pages for a path or a cmd: stream: (kind, page_id, rev_id, title, text_or_target) of namespace 0."""
    f = bz2.open(open_binary(path), "rb")
    try:
        for _ev, el in ET.iterparse(f, events=("end",)):
            if el.tag != NS + "page":
                continue
            if el.findtext(NS + "ns") == "0":
                title = el.findtext(NS + "title") or ""
                pid = int(el.findtext(NS + "id") or 0)
                rev = el.find(NS + "revision")
                rid = int(rev.findtext(NS + "id") or 0) if rev is not None else 0
                red = el.find(NS + "redirect")
                if red is not None:
                    yield ("redirect", pid, rid, title, red.get("title") or "")
                else:
                    yield ("article", pid, rid, title, (rev.findtext(NS + "text") if rev is not None else "") or "")
            el.clear()
    except (EOFError, ET.ParseError) as e:
        log(f"stopped at end of readable data ({type(e).__name__}) - expected only for a partial file")


def unescape(field: str) -> str | None:
    """Reverse of wikiparse._copy_field (PostgreSQL COPY text format)."""
    if field == "\\N":
        return None
    return _ESC.sub(lambda m: _MAP.get(m.group(1), m.group(1)), field) if "\\" in field else field


def core_hash(title: str, headings: str, lead: str, infobox: str) -> int:
    """63-bit hash of the fields the index ships (aliases are kept apart: they change with redirects, not edits)."""
    h = hashlib.blake2b(digest_size=8)
    for f in (title, headings, lead, infobox):
        h.update((f or "").encode("utf-8", "replace"))
        h.update(b"\x1f")
    return int.from_bytes(h.digest(), "big") >> 1


def alias_text(titles) -> str:
    return SEP.join(sorted(set(titles)))


def read_rows(path: str):
    """articles.tsv.gz of wikiparse.py -> (page_id, rev_id, title, headings, lead, infobox)."""
    with gzip.open(open_binary(path), "rt", encoding="utf-8", newline="") as f:
        for line in f:
            p = line.rstrip("\r\n").split("\t")      # buildbox (Windows) wrote the September files with CRLF line ends; a real \r is escaped
            yield (int(p[0]), int(p[1])) + tuple(unescape(x) or "" for x in p[2:6])


def read_redirects(path: str):
    with gzip.open(open_binary(path), "rt", encoding="utf-8", newline="") as f:
        for line in f:
            t, _, tgt = line.rstrip("\r\n").partition("\t")
            yield unescape(t) or "", unescape(tgt) or ""


# ---------------------------------------------------------------- state
SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (page_id INTEGER PRIMARY KEY, rev_id INTEGER NOT NULL, title TEXT NOT NULL,
                                 core INTEGER NOT NULL, aliases TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS docs_title ON docs (title);
CREATE TABLE IF NOT EXISTS redirects (title TEXT PRIMARY KEY, target TEXT NOT NULL, page_id INTEGER);
CREATE INDEX IF NOT EXISTS redirects_target ON redirects (target);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
"""


def open_state(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def aliases_by_target(db: sqlite3.Connection) -> dict[str, str]:
    cur = db.execute("SELECT target, group_concat(title, char(31)) FROM redirects GROUP BY target")
    return {t: alias_text(g.split("\x1f")) for t, g in cur}


def cmd_state(a) -> None:
    t0 = time.time()
    if os.path.exists(a.out):
        sys.exit(f"refusing: {a.out} exists")
    db = open_state(a.out)
    red: dict[str, list[str]] = {}
    n = 0
    with db:
        for t, tgt in read_redirects(a.redirects):
            db.execute("INSERT OR REPLACE INTO redirects (title, target) VALUES (?, ?)", (t, tgt))
            red.setdefault(tgt, []).append(t)
        batch = []
        for pid, rev, title, hd, lead, ib in read_rows(a.articles):
            batch.append((pid, rev, title, core_hash(title, hd, lead, ib), alias_text(red.get(title, ()))))
            n += 1
            if len(batch) >= 50_000:
                db.executemany("INSERT OR REPLACE INTO docs VALUES (?,?,?,?,?)", batch)
                batch.clear()
        db.executemany("INSERT OR REPLACE INTO docs VALUES (?,?,?,?,?)", batch)
        db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                       [("dump", a.dump), ("incr_day", ""), ("built", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))])
    log(f"state {a.out}: {n:,} articles, {db.execute('SELECT count(*) FROM redirects').fetchone()[0]:,} redirects, "
        f"{time.time() - t0:.0f} s")


# ---------------------------------------------------------------- text sources for rows we did not parse
def rows_from_base(path: str, ids: set[int]) -> dict[int, tuple]:
    """(headings, lead, infobox) of the baseline rows with these page ids (one pass over articles.tsv.gz)."""
    out: dict[int, tuple] = {}
    if not ids:
        return out
    for pid, _rev, _t, hd, lead, ib in read_rows(path):
        if pid in ids:
            out[pid] = (hd, lead, ib)
            if len(out) == len(ids):
                break
    return out


def rows_from_api(db: sqlite3.Connection, ids: set[int], fetch=None) -> dict[int, tuple]:
    """Re-parse the stored revision of each page through the API (the daily job has no baseline file)."""
    fetch = fetch or api_revisions
    want = {pid: db.execute("SELECT rev_id, title FROM docs WHERE page_id = ?", (pid,)).fetchone() for pid in ids}
    want = {p: v for p, v in want.items() if v}
    out: dict[int, tuple] = {}
    revs = {v[0]: p for p, v in want.items()}
    for rid, text in fetch(sorted(revs)).items():
        pid = revs[rid]
        row = W.parse_article((pid, rid, want[pid][1], text))
        if row:
            out[pid] = (row[3], row[4], row[5])
    return out


def api_revisions(revids, batch: int = 50, gap: float = 1.0) -> dict[int, str]:
    out: dict[int, str] = {}
    for i in range(0, len(revids), batch):
        q = urllib.parse.urlencode({"action": "query", "prop": "revisions", "rvprop": "ids|content", "rvslots": "main",
                                    "revids": "|".join(map(str, revids[i:i + batch])), "format": "json", "formatversion": "2"})
        with urllib.request.urlopen(urllib.request.Request(f"{API}?{q}", headers={"User-Agent": UA}), timeout=60) as r:
            j = json.load(r)
        for pg in j.get("query", {}).get("pages", []):
            for rv in pg.get("revisions", []):
                text = rv.get("slots", {}).get("main", {}).get("content")
                if text is not None:                 # a revision an admin hid has no content: its page is left as it is
                    out[rv["revid"]] = text
        time.sleep(gap)
    return out


def api_deletes(since_iso: str, until_iso: str):
    """(page_id, title) of pages deleted in the window (the dumps carry no deletions)."""
    cont, out = {}, []
    while True:
        q = {"action": "query", "list": "logevents", "letype": "delete", "leaction": "delete/delete", "lenamespace": "0",
             "lestart": until_iso, "leend": since_iso, "ledir": "older", "lelimit": "500", "format": "json", "formatversion": "2"}
        q.update(cont)
        with urllib.request.urlopen(urllib.request.Request(f"{API}?{urllib.parse.urlencode(q)}", headers={"User-Agent": UA}), timeout=60) as r:
            j = json.load(r)
        for e in j.get("query", {}).get("logevents", []):
            if e.get("logpage"):
                out.append((int(e["logpage"]), e.get("title", "")))
        cont = j.get("continue") or {}
        if not cont:
            return out
        time.sleep(1.0)


# ---------------------------------------------------------------- the common finish
class Out:
    def __init__(self, d: str):
        os.makedirs(d, exist_ok=True)
        self.d = d
        self.changed = gzip.open(os.path.join(d, "changed.tsv.gz"), "wt", encoding="utf-8", compresslevel=5)
        self.deleted = open(os.path.join(d, "deleted.tsv"), "w", encoding="utf-8")
        self.n = {"new": 0, "change": 0, "alias_only": 0, "deleted": 0, "same_hash": 0, "rev_only": 0}

    def row(self, op, pid, rev, title, hd, lead, ib, aliases):
        self.changed.write("\t".join([op, str(pid), str(rev)] + [W._copy_field(v) for v in (title, hd, lead, ib, aliases)]) + "\n")
        self.n[op] += 1

    def delete(self, pid, title):
        self.deleted.write(f"{pid}\t{W._copy_field(title)}\n")
        self.n["deleted"] += 1

    def finish(self, summary: dict) -> dict:
        self.changed.close()
        self.deleted.close()
        summary.update(self.n)
        with open(os.path.join(self.d, "summary.json"), "w") as f:
            json.dump(summary, f, indent=1, sort_keys=True)
        with open(os.path.join(self.d, "DONE"), "w") as f:
            f.write(json.dumps(summary, sort_keys=True) + "\n")
        return summary


def alias_only_rows(db, alias_map_new: dict[str, str], candidates, out: Out, source, skip: set[int]) -> int:
    """Documents whose rev and shipped fields did not change but whose aliases did: the row text comes from `source`."""
    need = {}
    for pid, rev, title, aliases in candidates:  # a cursor: the updates below run after it is drained
        if pid in skip:
            continue
        if alias_map_new.get(title, "") != aliases:
            need[pid] = (rev, title)
    need = dict(need)
    txt = source(set(need))
    for pid, (rev, title) in need.items():
        if pid not in txt:
            continue
        hd, lead, ib = txt[pid]
        out.row("alias_only", pid, rev, title, hd, lead, ib, alias_map_new.get(title, ""))
        db.execute("UPDATE docs SET aliases = ? WHERE page_id = ?", (alias_map_new.get(title, ""), pid))
    return len(need)


# ---------------------------------------------------------------- catch-up against a newer full dump
def need_parser() -> None:
    if W is None:
        sys.exit(f"wikidelta: this command parses wikitext and needs mwparserfromhell ({_WERR})")


def process_part(dump: str, state: str, out: str, name: str, limit: int | None = None) -> dict:
    """One dump file (a whole dump, or one of Wikimedia's split multistream parts) against the baseline state, read-only.
    Writes OUT/NAME/{stage.tsv.gz, same.tsv, seen.txt, rd.tsv.gz, summary.json, DONE}; any machine can run any part, and
    `merge` combines them (a page lives in exactly one part, a redirect likewise)."""
    need_parser()
    d = os.path.join(out, name)
    if os.path.exists(os.path.join(d, "DONE")):
        return json.load(open(os.path.join(d, "summary.json")))
    os.makedirs(d, exist_ok=True)
    t0 = time.time()
    ro = sqlite3.connect(f"file:{state}?mode=ro", uri=True)
    n = dict(pages_read=0, redirects=0, parsed=0, disambiguation=0, new=0, change=0, same_hash=0)
    with gzip.open(os.path.join(d, "stage.tsv.gz"), "wt", encoding="utf-8", compresslevel=3) as sf, \
            gzip.open(os.path.join(d, "rd.tsv.gz"), "wt", encoding="utf-8", compresslevel=3) as rf, \
            open(os.path.join(d, "same.tsv"), "w") as same, open(os.path.join(d, "seen.txt"), "w") as seen:
        for kind, pid, rid, title, x in pages(dump):
            if kind == "redirect":
                n["redirects"] += 1
                rf.write(W._copy_field(title) + "\t" + W._copy_field(x) + "\n")
                continue
            n["pages_read"] += 1
            if limit and n["pages_read"] > limit:
                break
            st = ro.execute("SELECT rev_id, title, core FROM docs WHERE page_id = ?", (pid,)).fetchone()
            if st and st[0] == rid:
                seen.write(f"{pid}\n")
                continue
            row = W.parse_article((pid, rid, title, x))
            n["parsed"] += 1
            if row is None:
                n["disambiguation"] += 1
                continue
            seen.write(f"{pid}\n")
            _p, _r, title, hd, lead, ib = row
            if st and st[1] == title and st[2] == core_hash(title, hd, lead, ib):
                n["same_hash"] += 1
                same.write(f"{pid}\t{rid}\n")
            else:
                op = "change" if st else "new"
                n[op] += 1
                sf.write("\t".join([op, str(pid), str(rid)] + [W._copy_field(v) for v in (title, hd, lead, ib)]) + "\n")
    n.update(seconds=round(time.time() - t0), part=name)
    with open(os.path.join(d, "summary.json"), "w") as f:
        json.dump(n, f, sort_keys=True)
    open(os.path.join(d, "DONE"), "w").write("x\n")
    return n


def _part_job(args):
    return process_part(*args)


def cmd_part(a) -> None:
    log(json.dumps(process_part(a.dump, a.state, a.parts, a.name, a.limit), sort_keys=True))


def cmd_merge(a) -> None:
    """Combine the parts' results with the state: deletions (state ids no part saw), aliases from every part's redirects,
    alias-only rows (text from the baseline file), the new state."""
    t0 = time.time()
    db = open_state(a.state)
    parts = sorted(d for d in os.listdir(a.parts) if os.path.exists(os.path.join(a.parts, d, "DONE")))
    if a.expect and len(parts) != a.expect:
        sys.exit(f"{len(parts)} finished parts in {a.parts}, expected {a.expect}: not merging an incomplete set")
    os.makedirs(a.out, exist_ok=True)
    scratch = os.path.join(a.out, "scratch.sqlite")
    if os.path.exists(scratch):
        os.remove(scratch)
    sc = sqlite3.connect(scratch)
    sc.executescript("CREATE TABLE seen (page_id INTEGER PRIMARY KEY); CREATE TABLE rd (title TEXT PRIMARY KEY, target TEXT)")
    stats = dict(parts=len(parts), pages_read=0, redirects=0, parsed=0, disambiguation=0)
    for name in parts:
        d = os.path.join(a.parts, name)
        ps = json.load(open(os.path.join(d, "summary.json")))
        for k in ("pages_read", "redirects", "parsed", "disambiguation"):
            stats[k] += ps[k]
        with open(os.path.join(d, "seen.txt")) as f:
            sc.executemany("INSERT OR IGNORE INTO seen VALUES (?)", ((int(x),) for x in f if x.strip()))
        with gzip.open(os.path.join(d, "rd.tsv.gz"), "rt", encoding="utf-8", newline="") as f:
            sc.executemany("INSERT OR REPLACE INTO rd VALUES (?,?)", (tuple(unescape(x) or "" for x in l.rstrip("\n").split("\t")) for l in f))
        with open(os.path.join(d, "same.tsv")) as f:
            for l in f:
                pid, rid = l.split()
                db.execute("UPDATE docs SET rev_id = ? WHERE page_id = ?", (int(rid), int(pid)))
    sc.commit()
    amap: dict = {}
    for t, tgt in sc.execute("SELECT title, target FROM rd"):
        amap.setdefault(tgt, []).append(t)
    amap = {k: alias_text(v) for k, v in amap.items()}
    out = Out(a.out)
    written: set[int] = set()
    parsed_ok_same: set[int] = set()
    n = {"new": 0, "change": 0}
    for name in parts:
        with gzip.open(os.path.join(a.parts, name, "stage.tsv.gz"), "rt", encoding="utf-8", newline="") as sf:
            for line in sf:
                op, pid, rid, title, hd, lead, ib = line.rstrip("\n").split("\t")
                title_u, pid_i = unescape(title) or "", int(pid)
                al = amap.get(title_u, "")
                core = core_hash(title_u, unescape(hd) or "", unescape(lead) or "", unescape(ib) or "")
                st = db.execute("SELECT title, core FROM docs WHERE page_id = ?", (pid_i,)).fetchone()
                if st and st[0] == title_u and st[1] == core:     # a new revision, the same shipped row: only the rev id moves
                    out.n["same_hash"] += 1
                    db.execute("UPDATE docs SET rev_id = ? WHERE page_id = ?", (int(rid), pid_i))
                    parsed_ok_same.add(pid_i)
                    continue
                out.changed.write("\t".join([op, pid, rid, title, hd, lead, ib, W._copy_field(al)]) + "\n")
                n[op] += 1
                db.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?,?,?)", (pid_i, int(rid), title_u, core, al))
                written.add(pid_i)
    out.n.update(n)
    if not a.partial:
        seen = {r[0] for r in sc.execute("SELECT page_id FROM main.seen")}
        for pid, title in db.execute("SELECT page_id, title FROM docs").fetchall():
            if pid in seen or pid in written:
                continue
            out.delete(pid, title)
            db.execute("DELETE FROM docs WHERE page_id = ?", (pid,))
        del seen
    cand = db.execute("SELECT page_id, rev_id, title, aliases FROM docs")
    n_alias = alias_only_rows(db, amap, cand, out, lambda ids: rows_from_base(a.base_articles, ids), written)
    db.execute("DELETE FROM redirects")
    db.executemany("INSERT INTO redirects (title, target) VALUES (?, ?)", sc.execute("SELECT title, target FROM main.rd").fetchall())
    db.execute("INSERT OR REPLACE INTO meta VALUES ('dump', ?)", (a.dump_name,))
    sc.close()
    os.remove(scratch)
    summary = out.finish(dict(stats, mode="catchup", seconds=round(time.time() - t0), alias_candidates=n_alias,
                              docs_after=db.execute("SELECT count(*) FROM docs").fetchone()[0]))
    if a.no_commit:
        db.rollback()
    else:
        db.commit()
    log(json.dumps(summary, sort_keys=True))


def cmd_catchup(a) -> None:
    """Every --dump (a file, or `cmd:` stream) as a part, `--procs` of them at a time on this machine, then the merge."""
    need_parser()
    jobs = [(d, a.state, os.path.join(a.out, "parts"), (a.names[i] if a.names else f"p{i:03d}"), a.limit) for i, d in enumerate(a.dump)]
    with Pool(a.procs, initializer=W._low_priority) as pool:
        for r in pool.imap_unordered(_part_job, jobs):
            log(f"part {r['part']}: {r['pages_read']:,} pages, {r['parsed']:,} parsed, {r['seconds']} s")
    a.parts, a.expect, a.partial = os.path.join(a.out, "parts"), len(jobs), bool(a.limit)
    a.dump_name = a.dump_name or "enwiki-catchup"
    cmd_merge(a)


# ---------------------------------------------------------------- one day of the incremental dump
def iter_revisions(path: str):
    """(page_id, ns, title, is_redirect, redirect_target, rev_id, timestamp, text) for every revision in the file."""
    f = bz2.open(open_binary(path), "rb") if path.endswith(".bz2") or path.startswith("cmd:") else open(path, "rb")
    try:
        for _ev, el in ET.iterparse(f, events=("end",)):
            if el.tag != NS + "page":
                continue
            ns = el.findtext(NS + "ns")
            if ns == "0":
                red = el.find(NS + "redirect")
                for rev in el.findall(NS + "revision"):
                    yield (int(el.findtext(NS + "id")), el.findtext(NS + "title") or "", red is not None,
                           (red.get("title") or "") if red is not None else "", int(rev.findtext(NS + "id")),
                           rev.findtext(NS + "timestamp") or "", rev.findtext(NS + "text") or "")
            el.clear()
    except (EOFError, ET.ParseError) as e:
        log(f"stopped at end of readable data ({type(e).__name__})")


def cmd_incr(a, deletes=None) -> None:
    need_parser()
    t0 = time.time()
    db = open_state(a.state)
    out = Out(a.out)
    now = datetime.fromisoformat(a.now.replace("Z", "+00:00")) if a.now else datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=a.hold_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    best: dict[int, tuple] = {}
    stats = {"revisions": 0, "held_back": 0}
    for pid, title, is_red, target, rid, ts, text in iter_revisions(a.pages):
        stats["revisions"] += 1
        if ts > cutoff:
            stats["held_back"] += 1                  # has not stood the hold yet: the next day's file does not repeat it
            continue
        if pid not in best or rid > best[pid][0]:
            best[pid] = (rid, title, is_red, target, text)
    work, redirect_changes, touched_targets = [], 0, set()
    for pid, (rid, title, is_red, target, text) in best.items():
        st = db.execute("SELECT rev_id, title, core, aliases FROM docs WHERE page_id = ?", (pid,)).fetchone()
        rd = db.execute("SELECT title, target FROM redirects WHERE title = ?", (title,)).fetchone()   # by title: the state's redirects carry no page id
        if st and st[0] >= rid:
            continue                                  # not newer than what we hold
        if is_red:
            if rd and rd[0] == title and rd[1] == target:
                continue
            if rd:
                touched_targets.add(rd[1])
            db.execute("INSERT OR REPLACE INTO redirects (title, target, page_id) VALUES (?,?,?)", (title, target, pid))
            touched_targets.add(target)
            redirect_changes += 1
            if st:                                    # an article that became a redirect
                out.delete(pid, st[1])
                db.execute("DELETE FROM docs WHERE page_id = ?", (pid,))
            continue
        if rd:                                        # a redirect that became an article
            touched_targets.add(rd[1])
            db.execute("DELETE FROM redirects WHERE title = ?", (title,))
            redirect_changes += 1
        work.append((pid, rid, title, text))
    with Pool(a.procs, initializer=W._low_priority) as pool:
        parsed = list(pool.imap(W.parse_article, work, chunksize=32))
    got = set()
    for (pid, rid, title, _t), row in zip(work, parsed):
        st = db.execute("SELECT rev_id, title, core, aliases FROM docs WHERE page_id = ?", (pid,)).fetchone()
        if row is None:                               # a disambiguation page now
            if st:
                out.delete(pid, st[1])
                db.execute("DELETE FROM docs WHERE page_id = ?", (pid,))
            continue
        _p, _r, title, hd, lead, ib = row
        if st and st[1] != title:
            touched_targets.add(st[1])                # renamed: the redirects aimed at the old title follow the new one
        got.add(pid)
        al = alias_text(t for (t,) in db.execute("SELECT title FROM redirects WHERE target = ?", (title,)))
        core = core_hash(title, hd, lead, ib)
        if st is None:
            out.row("new", pid, rid, title, hd, lead, ib, al)
        elif st[2] != core or st[3] != al or st[1] != title:
            out.row("change", pid, rid, title, hd, lead, ib, al)
        else:
            out.n["same_hash"] += 1
            db.execute("UPDATE docs SET rev_id = ? WHERE page_id = ?", (rid, pid))
            continue
        db.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?,?,?)", (pid, rid, title, core, al))
    cand = []
    for tgt in touched_targets:
        r = db.execute("SELECT page_id, rev_id, title, aliases FROM docs WHERE title = ?", (tgt,)).fetchone()
        if r:
            cand.append(r)
    amap = {r[2]: alias_text(t for (t,) in db.execute("SELECT title FROM redirects WHERE target = ?", (r[2],))) for r in cand}
    src = (lambda ids: {}) if a.no_api else (lambda ids: rows_from_api(db, ids))
    n_alias = alias_only_rows(db, amap, cand, out, src, got)
    # deletions: an explicit file, the test hook, or the delete log of the window (the dumps carry none)
    n_del = 0
    del_file = getattr(a, "deletes_file", None)
    if del_file and deletes is None:
        def deletes(_since, _until):
            rows_del = []
            with open(del_file, encoding="utf-8") as df:
                for line in df:
                    line = line.rstrip("\r\n")
                    if line:
                        p_s, _, t = line.partition("\t")
                        rows_del.append((int(p_s), unescape(t) or t))
            return rows_del
    if deletes is not None or not a.no_api:
        # the window starts where the last run's ended; without a start the API would walk the whole delete log
        prev = db.execute("SELECT value FROM meta WHERE key = 'incr_cutoff'").fetchone()
        since = a.since or (prev[0] if prev else "")
        if not since and deletes is None:
            raise SystemExit("incr: no --since and the state has no incr_cutoff from an earlier run; pass --since for the first day")
        for pid, title in (deletes or api_deletes)(since, cutoff):
            if db.execute("SELECT 1 FROM docs WHERE page_id = ?", (pid,)).fetchone():
                out.delete(pid, title)
                db.execute("DELETE FROM docs WHERE page_id = ?", (pid,))
                n_del += 1
    db.execute("INSERT OR REPLACE INTO meta VALUES ('incr_day', ?)", (a.day,))
    db.execute("INSERT OR REPLACE INTO meta VALUES ('incr_cutoff', ?)", (cutoff,))
    summary = out.finish(dict(stats, mode="incr", day=a.day, cutoff=cutoff, pages=len(best), redirect_changes=redirect_changes,
                              alias_candidates=n_alias, deleted_by_log=n_del, seconds=round(time.time() - t0),
                              docs_after=db.execute("SELECT count(*) FROM docs").fetchone()[0]))
    if a.no_commit:
        db.rollback()
    else:
        db.commit()
    log(json.dumps(summary, sort_keys=True))


def cmd_combine(a) -> None:
    """ONE change set from the catch-up and the daily runs, relative to the release's baseline: the last row of a page wins,
    `new` means the page is not in the baseline state, a page deleted at the end is a deletion only if the baseline held it.
    A page is `alias_only` only if every row it has in every input is: one real change anywhere makes it a `change`.
    Two passes, so the rows themselves are never held in memory (the rc16 catch-up alone is 4.5 GB compressed): the first
    reads ids and operations, the second copies each page's row from the last input that has it. Rows come out in input order, not sorted."""
    t0 = time.time()
    base = sqlite3.connect(f"file:{a.baseline}?mode=ro", uri=True)
    last: dict[int, int] = {}                      # page -> index of the input holding its last row; -1 = deleted at the end
    real: set[int] = set()                         # pages with at least one row that is not alias_only
    n_in = {"rows": 0, "deleted": 0}
    for k, d in enumerate(a.dirs):
        with gzip.open(os.path.join(d, "changed.tsv.gz"), "rt", encoding="utf-8", newline="") as f:
            for line in f:
                op, _, rest = line.partition("\t")
                pid = int(rest.partition("\t")[0])
                if last.get(pid, 0) == -1:
                    real.discard(pid)              # deleted, then back: what counts is what happened since
                last[pid] = k
                if op != "alias_only":
                    real.add(pid)
                n_in["rows"] += 1
        with open(os.path.join(d, "deleted.tsv"), encoding="utf-8", newline="") as f:
            for line in f:
                if line.strip():
                    pid = int(line.split("\t")[0])
                    last[pid] = -1
                    real.add(pid)                  # a page that comes back after a deletion is not "aliases only"
                    n_in["deleted"] += 1
    out = Out(a.out)
    for k, d in enumerate(a.dirs):
        written: set[int] = set()                  # one row a page per input is what wikidelta writes; a repeat is dropped
        with gzip.open(os.path.join(d, "changed.tsv.gz"), "rt", encoding="utf-8", newline="") as f:
            for line in f:
                rest = line.partition("\t")[2]
                pid = int(rest.partition("\t")[0])
                if last.get(pid) != k or pid in written:
                    continue
                written.add(pid)
                _combine_write(out, base, pid, rest, pid in real)
    for pid in sorted(p for p, k in last.items() if k == -1):
        r = base.execute("SELECT title FROM docs WHERE page_id = ?", (pid,)).fetchone()
        if r:
            out.delete(pid, r[0])
    summary = out.finish(dict(mode="combined", inputs=len(a.dirs), rows_in=n_in["rows"], deletions_in=n_in["deleted"],
                              seconds=round(time.time() - t0)))
    log(json.dumps(summary, sort_keys=True))


def _combine_write(out, base, pid: int, rest: str, real: bool) -> None:
    in_base = base.execute("SELECT 1 FROM docs WHERE page_id = ?", (pid,)).fetchone() is not None
    op = "new" if not in_base else ("change" if real else "alias_only")
    out.changed.write(op + "\t" + rest)
    out.n[op] += 1


def main() -> None:
    multiprocessing.set_start_method("fork", force=True)   # the workers call a module function; forkserver (3.14 default) re-imports __main__
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("state")
    s.add_argument("--articles", required=True); s.add_argument("--redirects", required=True)
    s.add_argument("--out", required=True); s.add_argument("--dump", required=True)
    c = sub.add_parser("catchup")
    c.add_argument("--dump", required=True, nargs="+", help="dump files (or cmd: streams), one part each")
    c.add_argument("--names", nargs="+"); c.add_argument("--state", required=True); c.add_argument("--out", required=True)
    c.add_argument("--base-articles", required=True); c.add_argument("--dump-name")
    c.add_argument("--procs", type=int, default=max(1, (os.cpu_count() or 2) - 2)); c.add_argument("--limit", type=int)
    c.add_argument("--no-commit", action="store_true")
    pt = sub.add_parser("part", help="one part, read-only against the state (any machine); merge combines them")
    pt.add_argument("--dump", required=True); pt.add_argument("--state", required=True); pt.add_argument("--parts", required=True)
    pt.add_argument("--name", required=True); pt.add_argument("--limit", type=int)
    m = sub.add_parser("merge")
    m.add_argument("--state", required=True); m.add_argument("--parts", required=True); m.add_argument("--out", required=True)
    m.add_argument("--base-articles", required=True); m.add_argument("--dump-name", required=True)
    m.add_argument("--expect", type=int, help="number of parts that must be finished"); m.add_argument("--partial", action="store_true")
    m.add_argument("--no-commit", action="store_true")
    cb = sub.add_parser("combine", help="one change set from a catch-up and the daily runs after it")
    cb.add_argument("--baseline", required=True, help="the state of the release being updated (before the catch-up)")
    cb.add_argument("--out", required=True); cb.add_argument("dirs", nargs="+", help="change-set folders, oldest first")
    i = sub.add_parser("incr")
    i.add_argument("--state", required=True); i.add_argument("--out", required=True); i.add_argument("--pages", required=True)
    i.add_argument("--day", required=True); i.add_argument("--hold-hours", type=float, default=8.0)
    i.add_argument("--now"); i.add_argument("--since", help="start of the delete-log window (ISO)")
    i.add_argument("--procs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    i.add_argument("--deletes-file", help="TSV file (page_id TAB title) of deleted pages instead of fetching the live delete log")
    i.add_argument("--no-api", action="store_true"); i.add_argument("--no-commit", action="store_true")
    a = ap.parse_args()
    {"state": cmd_state, "catchup": cmd_catchup, "part": cmd_part, "merge": cmd_merge, "combine": cmd_combine, "incr": cmd_incr}[a.cmd](a)


if __name__ == "__main__":
    main()
