#!/usr/bin/env python3
"""Stack Exchange question view counts as how-to demand. Stdlib only (+ `7z` for an archive).

    se_demand.py extract SITE PATH [--out F] [--all] [--min-views N]
        PATH is a site's Posts.xml, or its <site>.7z from the dump (streamed with 7z, as
        importer/stackexchange.py does; the importer's download dir is /stackexchange in its container,
        SE_DUMP=stackexchange_20260630 — point this at the same files, nothing is downloaded here).
        -> site TAB qid TAB views TAB score TAB accepted TAB title TAB key TAB tags
    se_demand.py merge F... [--out F]
        -> key TAB views TAB questions TAB accepted TAB best title TAB sites     (summed over sites)

What is read from Posts.xml (one <row> per post; the dump's own schema): PostTypeId (1 = question),
Id, Title, Tags, ViewCount, Score, AcceptedAnswerId, AnswerCount. Only questions are read; answers and
bodies are not (the grounding text comes from the se_docs / page_docs, not from here). By default
only how-to questions are kept (topics.howto_phrase finds "how to / how do i / how can i"); --all
keeps every question, keyed whole.

Views are the demand score: ViewCount is lifetime views of the question page, so an old question
outranks a newer one of equal interest — acceptable for a head list. The merge sums views over the
sites that share a key, so "how to cook rice" on cooking and on lifehacks is one topic.

Sites (HOWTO_SITES): the ones whose questions are how-tos in the allowed categories. The importer's
list (stackexchange.SITES) is the superset the pipeline downloads; the four marked EXTRA are not
in it and would be one more 7z each (small: crafts and lifehacks are under 10 MB).
"""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import topics  # noqa: E402

# site -> the category its questions mostly fall in (a hint for the bake-off split; categories.py decides)
HOWTO_SITES = {
    "cooking.stackexchange.com": "cooking",
    "diy.stackexchange.com": "household",
    "gardening.stackexchange.com": "gardening",
    "superuser.com": "software",
    "askubuntu.com": "software",
    "unix.stackexchange.com": "software",
    "apple.stackexchange.com": "software",
    "android.stackexchange.com": "software",
    "webapps.stackexchange.com": "software",
    "raspberrypi.stackexchange.com": "software",
    "travel.stackexchange.com": "travel",
    "outdoors.stackexchange.com": "travel",
    "woodworking.stackexchange.com": "crafts",
    "homebrew.stackexchange.com": "cooking",
    "photo.stackexchange.com": "crafts",
    "gaming.stackexchange.com": "software",
    "3dprinting.stackexchange.com": "crafts",
    "english.stackexchange.com": "school",
    "pets.stackexchange.com": "household",
    "bicycles.stackexchange.com": "household",
    "crafts.stackexchange.com": "crafts",          # EXTRA: not in stackexchange.SITES
    "lifehacks.stackexchange.com": "household",    # EXTRA
    "math.stackexchange.com": "school",            # EXTRA (large: ~2 GB 7z; optional)
    "ell.stackexchange.com": "school",             # EXTRA
}
# Sites the importer downloads whose questions fall in a denied category — read for nothing here.
NOT_READ = ("law.stackexchange.com", "health.stackexchange.com", "money.stackexchange.com",
            "mechanics.stackexchange.com", "security.stackexchange.com", "electronics.stackexchange.com",
            "serverfault.com", "dba.stackexchange.com", "softwareengineering.stackexchange.com",
            "networkengineering.stackexchange.com")
FIELDS = ("site", "qid", "views", "score", "accepted", "title", "key", "tags")


def tags_of(raw: str) -> str:
    """'<linux><bash>' (old) or '|linux|bash|' (2024+) -> 'linux bash' (as importer/stackexchange.py)."""
    import re
    return " ".join(t for t in re.split(r"[<>|]+", raw or "") if t)


def rows_of(stream):
    """A Posts.xml stream -> each <row>'s attributes (streamed; a 3 GB file never sits in memory)."""
    for _ev, el in ET.iterparse(stream, events=("end",)):
        if el.tag == "row":
            yield el.attrib
        el.clear()


def open_posts(path: str):
    """-> (stream, close): Posts.xml itself, or Posts.xml streamed out of a site's 7z."""
    if path.endswith(".7z"):
        p = subprocess.Popen(["7z", "x", "-so", path, "Posts.xml"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        def close():
            p.stdout.close()
            p.wait()
        return p.stdout, close
    f = open(path, "rb")
    return f, f.close


def questions(rows, how_to_only: bool = True, min_views: int = 0):
    """Row attribute dicts -> (qid, views, score, accepted, title, key, tags) for the questions kept."""
    for a in rows:
        if a.get("PostTypeId") != "1":
            continue
        title = (a.get("Title") or "").strip()
        if not title:
            continue
        views = int(a.get("ViewCount") or 0)
        if views < min_views:
            continue
        phrase = topics.howto_phrase(title)
        if how_to_only and phrase is None:
            continue
        k = topics.key(title)
        if not k:
            continue
        yield (int(a["Id"]), views, int(a.get("Score") or 0), 1 if a.get("AcceptedAnswerId") else 0,
               title, k, tags_of(a.get("Tags") or ""))


def cmd_extract(site: str, path: str, out, how_to_only: bool, min_views: int) -> int:
    stream, close = open_posts(path)
    n = 0
    try:
        w = csv.writer(out, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_NONE, escapechar="\\")
        w.writerow(["#" + FIELDS[0], *FIELDS[1:]])
        for q in questions(rows_of(stream), how_to_only, min_views):
            w.writerow([site, *[str(v).replace("\t", " ") for v in q]])
            n += 1
    finally:
        close()
    print(f"[se-demand] {site}: {n:,} questions from {os.path.basename(path)}", file=sys.stderr)
    return n


def merge(files: list[str]):
    """extract outputs -> {key: [views, questions, accepted, best title, best views, sites]}."""
    agg: dict[str, list] = defaultdict(lambda: [0, 0, 0, "", -1, set()])
    for path in files:
        with open(path, encoding="utf-8", newline="") as f:
            for row in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
                if not row or row[0].startswith("#") or len(row) < 8:
                    continue
                site, _qid, views, _score, accepted, title, k, _tags = row[:8]
                views = int(views)
                a = agg[k]
                a[0] += views
                a[1] += 1
                a[2] += int(accepted)
                if views > a[4]:
                    a[3], a[4] = title, views
                a[5].add(site)
    return agg


def cmd_merge(files: list[str], out) -> int:
    agg = merge(files)
    print("#key\tviews\tquestions\taccepted\ttitle\tsites", file=out)
    for k, a in sorted(agg.items(), key=lambda kv: (-kv[1][0], kv[0])):
        print(f"{k}\t{a[0]}\t{a[1]}\t{a[2]}\t{a[3]}\t{','.join(sorted(a[5]))}", file=out)
    return len(agg)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract")
    e.add_argument("site", help="e.g. cooking.stackexchange.com (any site; HOWTO_SITES is the planned list)")
    e.add_argument("path", help="Posts.xml or <site>.7z")
    e.add_argument("--out", default="-")
    e.add_argument("--all", action="store_true", help="every question, not only how-to titles")
    e.add_argument("--min-views", type=int, default=0)
    m = sub.add_parser("merge")
    m.add_argument("files", nargs="+")
    m.add_argument("--out", default="-")
    s = sub.add_parser("sites")
    a = ap.parse_args(argv)
    if a.cmd == "sites":
        for site, cat in HOWTO_SITES.items():
            print(f"{site}\t{cat}")
        return 0
    out = sys.stdout if a.out == "-" else open(a.out, "w", encoding="utf-8")
    try:
        if a.cmd == "extract":
            cmd_extract(a.site, a.path, out, not a.all, a.min_views)
        else:
            cmd_merge(a.files, out)
    finally:
        if out is not sys.stdout:
            out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
