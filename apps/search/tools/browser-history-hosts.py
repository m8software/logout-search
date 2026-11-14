#!/usr/bin/env python3
"""Browser history -> host<TAB>visits<TAB>last_visit_unix, on stdout.
    tools/browser-history-hosts.py [EXTRA_HISTORY_FILE …]

Runs on the workstation and merges every history it finds (Brave, Chrome,
Chromium, Firefox incl. snap). Only HOSTNAMES and counts leave this machine —
never URLs, paths or query strings. History files are locked while a browser
runs, so each is copied first.

    tools/browser-history-hosts.py > hosts.tsv
"""
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

HOME = Path.home()
WEBKIT_EPOCH_OFFSET_S = 11_644_473_600  # Chromium counts µs from 1601-01-01

# (glob, query, timestamp -> unix seconds)
SOURCES = [
    (".config/BraveSoftware/Brave-Browser/*/History",
     "SELECT url, visit_count, last_visit_time FROM urls",
     lambda t: t // 1_000_000 - WEBKIT_EPOCH_OFFSET_S),
    (".config/google-chrome/*/History",
     "SELECT url, visit_count, last_visit_time FROM urls",
     lambda t: t // 1_000_000 - WEBKIT_EPOCH_OFFSET_S),
    (".config/chromium/*/History",
     "SELECT url, visit_count, last_visit_time FROM urls",
     lambda t: t // 1_000_000 - WEBKIT_EPOCH_OFFSET_S),
    (".mozilla/firefox/*/places.sqlite",
     "SELECT url, visit_count, last_visit_date FROM moz_places",
     lambda t: t // 1_000_000),
    ("snap/firefox/common/.mozilla/firefox/*/places.sqlite",
     "SELECT url, visit_count, last_visit_date FROM moz_places",
     lambda t: t // 1_000_000),
]

# Extra history files given on the command line — copies from another machine (buildbox's
# Brave and Edge: this workstation's own browsers hold ~60 hosts). Chromium
# format unless the file is a Firefox places.sqlite.
CHROMIUM = ("SELECT url, visit_count, last_visit_time FROM urls", SOURCES[0][2])
FIREFOX = SOURCES[3][1:]


def sources():
    for pattern, query, to_unix in SOURCES:
        for src in HOME.glob(pattern):
            yield src, query, to_unix
    for arg in sys.argv[1:]:
        src = Path(arg)
        if not src.is_file():
            sys.exit(f"no such history file: {src}")
        yield (src, *(FIREFOX if src.name.startswith("places") else CHROMIUM))


visits, last = defaultdict(int), defaultdict(int)
with tempfile.TemporaryDirectory() as d:
    if True:
        for src, query, to_unix in sources():
            copy = Path(d) / "h.sqlite"
            shutil.copy2(src, copy)
            n = 0
            db = sqlite3.connect(copy)
            for url, count, ts in db.execute(query):
                parts = urlsplit(url or "")
                if parts.scheme not in ("http", "https") or not parts.hostname:
                    continue
                host = parts.hostname.lower()
                visits[host] += count or 0
                last[host] = max(last[host], to_unix(ts) if ts else 0)
                n += 1
            db.close()
            print(f"{src}: {n} urls", file=sys.stderr)

for host in sorted(visits):
    print(f"{host}\t{visits[host]}\t{last[host]}")
print(f"{len(visits)} hosts", file=sys.stderr)
