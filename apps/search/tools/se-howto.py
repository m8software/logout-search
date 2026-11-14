#!/usr/bin/env python3
"""se-howto.py: a Stack Exchange question index for how-to searches, MATCHING ONLY (2026-11-10).

    se-howto.py stage STAGE.sqlite DIR [DIR ...]     every answered question of every <site>.7z in the folders
    se-howto.py stats STAGE.sqlite                   rows by score floor (to choose one)
    se-howto.py cut   STAGE.sqlite OUT.sqlite [--min-score N] [--answer accepted|positive]

stage: Posts.xml is streamed out of each 7z (importer/stackexchange.py `_posts`); a question is kept when it has an
answer. Kept per question: title, tags, score, views, whether an answer is accepted, the best answer's score, the first
300 characters of the question and of the best answer (accepted, else the highest score).
cut: questions with an accepted answer, or (positive) a best answer scored >= 1, and a question score >= N. The shipped
file keeps only what a result shows: site, question id (the address is https://<site>/questions/<id>), the question's
own title, score, views, flags, the answer id, both post dates (the licence version depends on the answer's date), a
readable snippet of the best answer (code blocks and markup removed, cut at a sentence end, about 300 characters) and the
two authors the licence asks to credit (asker, and the author of the answer
whose words rank the question: "<user id>|<name>", the id gives the profile link; no id = a deleted account). The text that ranks a question (title, tags, the two excerpts) goes into a CONTENTLESS
FTS5 index: words and positions, no readable text (ADR-059; docs/legal/SEARCH-DATA-SOURCES.md, Stack Exchange).
"""
import argparse
import glob
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "importer"))
import stackexchange as se  # noqa: E402  (importer/stackexchange.py: _posts, text_of, tags_of)

EXC = 300
SNIP = 300
_PRE = re.compile(r"<pre\b.*?</pre>", re.S | re.I)
_INLINE = re.compile(r"</?(?:a|code|em|strong|i|b|span|sub|sup|kbd|u|s|del|ins|mark|small)\b[^>]*>", re.I)
_BREAKS = re.compile(r"<(?:br|/p|/li|/h[1-6]|/blockquote)[^>]*>", re.I)
_SENT_END = re.compile(r"[.!?][\"')\]]?(?=\s|$)")


def snippet(body: str, limit: int = SNIP) -> str:
    """An answer's HTML -> plain text for a result's snippet: code blocks removed, markup removed, whitespace folded,
    cut at the last sentence end within `limit` characters (never mid-word); no sentence end in reach: cut at a word
    and add an ellipsis. An answer that is only code gives ''."""
    t = se.text_of(_BREAKS.sub(" ", _INLINE.sub("", _PRE.sub(" \u2026 ", body or ""))), 20000)
    t = re.sub(r"(?:\u2026\s*){2,}", "\u2026 ", t).lstrip("\u2026 ").strip()     # a removed code block shows as an ellipsis
    if len(t) <= limit:
        return t
    win = t[:limit]
    ends = [m.end() for m in _SENT_END.finditer(win) if m.end() >= 80]
    if ends:
        return win[:ends[-1]].strip()
    cut = win.rsplit(" ", 1)[0].rstrip(",;:- ")
    return (cut if len(cut) >= 40 else win.strip()) + "…"


def stage(db, dirs):
    t0 = time.time()
    con = sqlite3.connect(db)
    con.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;"
                      "CREATE TABLE IF NOT EXISTS q (site TEXT, qid INTEGER, title TEXT, tags TEXT, score INTEGER, views INTEGER,"
                      " acc INTEGER, ascore INTEGER, qtext TEXT, atext TEXT, qown TEXT, aown TEXT, aid INTEGER, qdate TEXT, adate TEXT, snip TEXT,"
                      " PRIMARY KEY (site, qid)) WITHOUT ROWID;"
                      "CREATE TABLE IF NOT EXISTS u (site TEXT, uid INTEGER, name TEXT, PRIMARY KEY (site, uid)) WITHOUT ROWID;")
    seen_sites = set()
    for d in dirs:
        for path in sorted(glob.glob(os.path.join(d, "*.7z"))):
            site = os.path.basename(path)[:-3]
            if ".meta." in site or site in seen_sites:
                continue
            seen_sites.add(site)
            qs, best = {}, {}
            for a in se._posts(path):
                k = a.get("PostTypeId")
                if k == "1":
                    if int(a.get("AnswerCount", 0) or 0) > 0:
                        qs[int(a["Id"])] = [a.get("Title", ""), se.tags_of(a.get("Tags", "")), int(a.get("Score", 0)),
                                            int(a.get("ViewCount", 0) or 0), a.get("AcceptedAnswerId"),
                                            se.text_of(a.get("Body", ""), EXC), se.owner_of(a), (a.get("CreationDate") or "")[:10]]
                elif k == "2":
                    qid = int(a.get("ParentId", 0) or 0)
                    q = qs.get(qid)
                    if q is None:
                        continue
                    key = se.best_key(q[4], a)
                    if qid not in best or key > best[qid][0]:
                        best[qid] = (key, se.text_of(_PRE.sub(" ", a.get("Body", "")), EXC), se.owner_of(a), int(a["Id"]),
                                     (a.get("CreationDate") or "")[:10], snippet(a.get("Body", "")))
            rows = []
            want = set()
            for qid, (title, tags, score, views, acc, qtext, qo, qdate) in qs.items():
                b = best.get(qid)
                if not b:
                    continue
                for o in (qo, b[2]):
                    if o[0] is not None:
                        want.add(o[0])
                rows.append((site, qid, title, tags, score, views, 1 if acc else 0, b[0][1], qtext, b[1],
                             f"{qo[0] or ''}|{qo[1] or ''}", f"{b[2][0] or ''}|{b[2][1] or ''}", b[3], qdate, b[4], b[5]))
            con.executemany("INSERT OR REPLACE INTO q VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            names = se.user_names(se._posts(path, "Users.xml"), want)
            con.executemany("INSERT OR REPLACE INTO u VALUES (?,?,?)", [(site, k, v) for k, v in names.items()])
            con.commit()
            print(f"[se] {site}: {len(rows):,} answered questions ({(time.time() - t0) / 60:.1f} min)", file=sys.stderr, flush=True)
    print(f"[se] {len(seen_sites)} sites, {con.execute('SELECT count(*) FROM q').fetchone()[0]:,} questions", file=sys.stderr)


def stats(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    print("floor  accepted-or-positive   accepted only   (rows)")
    for f in (0, 1, 2, 3, 5, 10, 20):
        a = con.execute("SELECT count(*) FROM q WHERE score >= ? AND (acc = 1 OR ascore >= 1)", (f,)).fetchone()[0]
        b = con.execute("SELECT count(*) FROM q WHERE score >= ? AND acc = 1", (f,)).fetchone()[0]
        print(f"{f:5d}  {a:12,}  {b:12,}")
    print("by site, score>=3 and (accepted or positive answer):")
    for site, n, tot in con.execute("SELECT site, sum(score >= 3 AND (acc = 1 OR ascore >= 1)), count(*) FROM q GROUP BY site ORDER BY 2 DESC"):
        print(f"  {site:42s} {n:8,} of {tot:8,}")


def cut(db, out, min_score, answer, detail="full"):
    t0 = time.time()
    if os.path.exists(out):
        os.remove(out)
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dst = sqlite3.connect(out)
    dst.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;"
                      "CREATE TABLE s (id INTEGER PRIMARY KEY, site TEXT NOT NULL, qid INTEGER NOT NULL, title TEXT NOT NULL,"
                      " score INTEGER NOT NULL, views INTEGER NOT NULL, acc INTEGER NOT NULL, asker TEXT NOT NULL, answerer TEXT NOT NULL,"
                      " aid INTEGER NOT NULL, qdate TEXT NOT NULL, adate TEXT NOT NULL, snip TEXT NOT NULL);"
                      "CREATE VIRTUAL TABLE fts USING fts5(title, tags, qtext, atext, content='', "
                      f"tokenize='porter unicode61 remove_diacritics 2'{'' if detail == 'full' else ', detail=' + detail});")
    where = "score >= ? AND " + ("acc = 1" if answer == "accepted" else "(acc = 1 OR ascore >= 1)")
    n = 0
    batch_s, batch_f = [], []
    names = {(r[0], r[1]): r[2] for r in src.execute("SELECT site, uid, name FROM u")}

    def credit(site, own):
        uid, _, shown = own.partition("|")
        if uid and (site, int(uid)) in names:
            return f"{uid}|{names[(site, int(uid))]}"        # a live account: id (the profile link) and name
        return f"|{shown}"                                    # a deleted one: only the name the post carries, if any
    for site, qid, title, tags, score, views, acc, ascore, qtext, atext, qown, aown, aid, qdate, adate, snip in src.execute(
            f"SELECT site, qid, title, tags, score, views, acc, ascore, qtext, atext, qown, aown, aid, qdate, adate, snip FROM q "
            f"WHERE {where} ORDER BY site, qid", (min_score,)):
        n += 1
        batch_s.append((n, site, qid, title, score, views, acc, credit(site, qown), credit(site, aown), aid, qdate, adate, snip))
        batch_f.append((n, title, tags, qtext, atext))
        if len(batch_s) >= 50_000:
            dst.executemany("INSERT INTO s VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", batch_s)
            dst.executemany("INSERT INTO fts(rowid, title, tags, qtext, atext) VALUES (?,?,?,?,?)", batch_f)
            batch_s.clear(); batch_f.clear()
    dst.executemany("INSERT INTO s VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", batch_s)
    dst.executemany("INSERT INTO fts(rowid, title, tags, qtext, atext) VALUES (?,?,?,?,?)", batch_f)
    dst.execute("INSERT INTO fts(fts) VALUES('optimize')")
    dst.commit()
    dst.execute("VACUUM")
    print(f"[se] {out}: {n:,} questions, {os.path.getsize(out) / 1e9:.3f} GB, {(time.time() - t0) / 60:.1f} min "
          f"(floor {min_score}, answer {answer}, detail {detail})", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stage"); s.add_argument("db"); s.add_argument("dirs", nargs="+")
    t = sub.add_parser("stats"); t.add_argument("db")
    c = sub.add_parser("cut"); c.add_argument("db"); c.add_argument("out")
    c.add_argument("--min-score", type=int, default=1); c.add_argument("--answer", choices=("accepted", "positive"), default="positive")
    c.add_argument("--detail", choices=("full", "column", "none"), default="full",
                   help="none: terms per question without positions (a bag of words, like the release's sorted words)")
    a = ap.parse_args()
    if a.cmd == "stage":
        stage(a.db, a.dirs)
    elif a.cmd == "stats":
        stats(a.db)
    else:
        cut(a.db, a.out, a.min_score, a.answer, a.detail)


if __name__ == "__main__":
    main()
