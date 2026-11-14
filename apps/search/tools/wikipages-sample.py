#!/usr/bin/env python3
"""wikipages-sample.py PAGES.sqlite OUT.json: random samples of the cited-pages table by key probes (no scan):
  pick   : 60 pages cited by 3+ articles with >= 3 stored contexts (the sentence-pick comparison)
  sent   : 300 random stored sentences (the mid-clause rate)
A probe is a random https://<2 letters> key; the first qualifying row of the next 4,000 keys is taken."""
import json
import random
import sqlite3
import sys

random.seed(20261110)
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
letters = "abcdefghijklmnopqrstuvwxyz0123456789"
pick, sent, seen = [], [], set()
tries = 0
while (len(pick) < 60 or len(sent) < 300) and tries < 20000:
    tries += 1
    key = random.choice(["https://", "http://", "https://www.", "http://www."]) + "".join(random.choice(letters) for _ in range(2))
    for url, dom, title, na, ctx in con.execute("SELECT url, domain, title, n_articles, contexts FROM pages WHERE url >= ? LIMIT 4000", (key,)):
        if url in seen:
            break
        try:
            best = json.loads(ctx)
        except ValueError:
            continue
        if not best:
            continue
        if len(sent) < 300:
            seen.add(url)
            sent.append({"url": url, "article": best[0][0], "sentence": best[0][2]})
            break
        if len(pick) < 60 and na >= 3 and len(best) >= 3:
            seen.add(url)
            pick.append({"url": url, "title": title, "n_articles": na, "contexts": best})
            break
json.dump({"pick": pick, "sent": sent, "probes": tries}, open(sys.argv[2], "w"), ensure_ascii=False, indent=0)
print(len(pick), len(sent), tries)
