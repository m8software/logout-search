#!/usr/bin/env python3
"""How much people want each eval query's answer — the weights for compare-evals.py --demand.

    tools/eval-demand.py QUERIES.tsv OUT.tsv --release DIR [--signal pop|searches]
    tools/eval-demand.py QUERIES.tsv OUT.tsv --db serve        (LEGACY Postgres path, deleted at phase 7)

--release reads a release folder: suggest_keys in lookup.sqlite + the pop / demand arrays of signals.bin
(SEARCH-POSTGRES-EXIT.md phase 3). Same keys, same max, same log scale as the SQL below.

QUERIES.tsv: query <TAB> kind (a judged eval set). OUT.tsv: query <TAB> kind <TAB> pop.

pop is suggest_items.pop — the 0..1 popularity autocomplete already ranks by (Wikidata
sitelinks + Wikipedia pageviews for articles, Tranco rank for sites; log-scaled, so a few
giant names do not swamp the set). A query takes the best pop among its candidate keys:
the whole query squeezed to letters and digits as suggest_keys stores it, and the same
without a platform or question word ("gillette twitter" -> gillette, "what is lake
karachay" -> lake karachay). No match = 0: the uniform sample of Wikipedia titles is
mostly articles nobody looks up, and that is exactly what the weighting should say.

LEGACY path: read-only, one query over indexhost's serve database (via the homeserver jump). The eval
queries are synthetic (built from the index), not anyone's search history.
"""
import argparse
import os
import re
import subprocess
import sys

JUMP, INDEXHOST = "user@jump-host", "user@index-host"
STRIP = re.compile(r"^(what is|what are|who is|who was|how to|how do|where is)\s+|"
                   r"\s+(instagram|twitter|x|youtube|tiktok|spotify|imdb|github|facebook|channel|"
                   r"facts|explained|history|meaning|website|official site|subreddit|reddit)$")


def tight(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def keys(q: str) -> list[str]:
    out, cur = [tight(q)], q.lower().strip()
    for _ in range(3):                                  # "what is x facts" -> x
        nxt = STRIP.sub("", cur).strip()
        if nxt == cur or not nxt:
            break
        cur = nxt
        out.append(tight(cur))
    if re.fullmatch(r"[a-z0-9-]+\.[a-z.]{2,}", q.strip().lower()):   # a typed domain -> its label
        out.append(tight(q.split(".")[0]))
    return [k for k in dict.fromkeys(out) if len(k) >= 2]


def lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("queries"); ap.add_argument("out"); ap.add_argument("--db", default="serve")
    ap.add_argument("--release", default=os.environ.get("SEARCH_RELEASE_DIR", ""),
                    help="read this release folder instead of Postgres (default $SEARCH_RELEASE_DIR; unset = LEGACY Postgres)")
    ap.add_argument("--signal", choices=("pop", "searches"), default="pop",
                    help="pop = autocomplete popularity (leans on language editions: a village with "
                         "40 bot-made editions scores like a known name); searches = monthly searches "
                         "that reached the article (Wikipedia clickstream), log-scaled to 0..1 "
                         "(1M/month = 1) — closer to what people actually look for")
    a = ap.parse_args()
    rows = [l.rstrip("\n").split("\t")[:2] for l in open(a.queries, encoding="utf-8") if l.strip()]
    if a.release:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import release_src
        rel = release_src.open_release(a.release)
        pop = {}
        for i, (q, _) in enumerate(rows):
            v = rel.demand_for_keys(keys(q), "pop" if a.signal == "pop" else "searches")
            if v is not None:
                pop[i] = v
        write(rows, pop, a.out, "release " + str(rel.release_id))
        return
    print("eval-demand: LEGACY Postgres path (pass --release DIR); removed at phase 7 of SEARCH-POSTGRES-EXIT.md", file=sys.stderr)
    pairs = [(i, k) for i, (q, _) in enumerate(rows) for k in keys(q)]
    values = ",\n".join(f"({i}, {lit(k)})" for i, k in pairs)
    sql = ("CREATE TEMP TABLE qk (i int, key text);\n"
           f"INSERT INTO qk VALUES\n{values};\n"
           "\\pset format unaligned\n\\pset tuples_only on\n"
           "SELECT qk.i || chr(9) || max(" + ("it.pop" if a.signal == "pop" else "least(1, log(1 + coalesce(it.demand, 0)) / 6)") +
           ") FROM qk JOIN suggest_keys sk ON sk.key = qk.key "
           "JOIN suggest_items it ON it.id = sk.id GROUP BY qk.i;\n")
    cmd = ["ssh", "-o", "BatchMode=yes", JUMP,
           f"ssh -o BatchMode=yes {INDEXHOST} 'sudo -n docker exec -i search-postgres psql -U search -d {a.db} -q -v ON_ERROR_STOP=1'"]
    r = subprocess.run(cmd, input=sql, capture_output=True, text=True, check=True)
    pop = {int(i): float(p) for i, p in (line.split("\t") for line in r.stdout.splitlines() if "\t" in line)}
    write(rows, pop, a.out, "postgres")


def write(rows, pop, out, source) -> None:
    with open(out, "w", encoding="utf-8") as f:
        for i, (q, kind) in enumerate(rows):
            f.write(f"{q}\t{kind}\t{pop.get(i, 0.0):.4f}\n")
    found = sum(1 for i in range(len(rows)) if pop.get(i, 0) > 0)
    print(f"{len(rows)} queries, {found} matched an autocomplete item ({source}) -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
