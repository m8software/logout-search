#!/usr/bin/env python3
"""Fetch the grounding sources the bake-off topics name -> sources.jsonl. Stdlib only.

    sources_fetch.py [--topics topics-bakeoff.tsv] [--out bakeoff/sources.jsonl] [--show]

One {id, url, title, text, licence, author, fetched} line per distinct source id in the topics file
(column 3; "-" = none). The bake-off needs 50 pages, so they are read from the public APIs instead of
the full dumps imports; the ids, urls and licences are the same ones the dump rows carry.

    wikibooks:<Title>[#<Section>]     en.wikibooks.org  MediaWiki API, plain-text extract   CC BY-SA 4.0
    wikivoyage:<Title>[#<Section>]    en.wikivoyage.org same                                CC BY-SA 4.0
    stackexchange:<host>/questions/<id>   api.stackexchange.com: the accepted answer, else the
                                      top-voted one; licence as the API states it per post (CC BY-SA 2.5-4.0)

`#Section` keeps only that section (and its subsections) of a long page - "Paris#By Métro" - because
bakeoff.py passes the first SOURCE_CHARS characters to the model. A missing page, section or answer is
an error: the topic list must name sources that exist. Text is stored whole up to MAX_CHARS; it is CC
BY-SA text with its url and author, which is the attribution the licence asks for.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bakeoff  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
UA = {"User-Agent": "m8server-howto-bakeoff/0.1 (help@logout.cloud)", "Accept-Encoding": "gzip"}
MAX_CHARS = 8000
WIKI = {"wikibooks": "en.wikibooks.org", "wikivoyage": "en.wikivoyage.org"}


def get(url: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as resp:
        body = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
    return json.loads(body)


def section_of(text: str, name: str) -> str | None:
    """The named section of a MediaWiki plain extract (exsectionformat=wiki: '== Name ==' lines), with
    its subsections, up to the next heading of the same or a higher level."""
    lines, out, level = text.split("\n"), None, 0
    for line in lines:
        m = re.match(r"^(=+)\s*(.*?)\s*=+\s*$", line)
        if m and out is None and m.group(2).lower() == name.lower():
            out, level = [], len(m.group(1))
            continue
        if m and out is not None and len(m.group(1)) <= level:
            break
        if out is not None:
            out.append(line)
    return "\n".join(out).strip() if out is not None else None


def tidy(text: str) -> str:
    text = re.sub(r"^(=+)\s*(.*?)\s*=+\s*$", lambda m: m.group(2) + ":", text, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fetch_wiki(kind: str, rest: str) -> dict:
    title, _, section = rest.partition("#")
    host = WIKI[kind]
    d = get(f"https://{host}/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "prop": "extracts", "explaintext": 1, "exsectionformat": "wiki", "redirects": 1,
        "titles": title, "format": "json", "formatversion": 2}))
    page = d["query"]["pages"][0]
    if page.get("missing") or not page.get("extract"):
        raise LookupError(f"{kind}: no page {title!r}")
    text = page["extract"]
    if section:
        text = section_of(text, section)
        if not text:
            raise LookupError(f"{kind}: page {title!r} has no section {section!r}")
    url = f"https://{host}/wiki/" + urllib.parse.quote(page["title"].replace(" ", "_"), safe=":/()',")
    if section:
        url += "#" + urllib.parse.quote(section.replace(" ", "_"), safe="")
    return {"url": url, "title": page["title"] + (f" - {section}" if section else ""), "text": tidy(text),
            "licence": "CC BY-SA 4.0", "author": f"{kind.capitalize()} contributors"}


def strip_html(body: str) -> str:
    body = re.sub(r"<(script|style)\b.*?</\1>", "", body, flags=re.S | re.I)
    body = re.sub(r"<li[^>]*>", "- ", body, flags=re.I)
    body = re.sub(r"</(p|li|h\d|pre|blockquote|ul|ol)>|<br\s*/?>", "\n", body, flags=re.I)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(re.sub(r"<[^>]+>", "", body))).strip()


def fetch_se(rest: str) -> dict:
    m = re.fullmatch(r"([a-z0-9.]+)/questions/(\d+)", rest)
    if not m:
        raise LookupError(f"stackexchange: bad id {rest!r}")
    host, qid = m.group(1), m.group(2)
    site = host.replace(".stackexchange.com", "").replace(".com", "")
    q = get(f"https://api.stackexchange.com/2.3/questions/{qid}?" + urllib.parse.urlencode({"site": site}))
    if not q.get("items"):
        raise LookupError(f"stackexchange: no question {rest!r}")
    a = get(f"https://api.stackexchange.com/2.3/questions/{qid}/answers?" + urllib.parse.urlencode(
        {"site": site, "order": "desc", "sort": "votes", "filter": "withbody", "pagesize": 10}))
    answers = a.get("items") or []
    if not answers:
        raise LookupError(f"stackexchange: question {rest!r} has no answer")
    best = next((x for x in answers if x.get("is_accepted")), answers[0])
    title = html.unescape(q["items"][0]["title"])
    return {"url": f"https://{host}/a/{best['answer_id']}", "title": title,
            "text": f"Question: {title}\n\nAnswer:\n{strip_html(best['body'])}",
            "licence": best.get("content_license", "CC BY-SA"),
            "author": html.unescape((best.get("owner") or {}).get("display_name", "a Stack Exchange user"))}


def fetch(source_id: str) -> dict:
    kind, _, rest = source_id.partition(":")
    row = fetch_wiki(kind, rest) if kind in WIKI else fetch_se(rest) if kind == "stackexchange" else None
    if row is None:
        raise LookupError(f"unknown source kind in {source_id!r}")
    row["text"] = row["text"][:MAX_CHARS]
    return {"id": source_id, **row, "fetched": dt.date.today().isoformat()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--topics", default=bakeoff.DEFAULT_TOPICS)
    ap.add_argument("--out", default=os.path.join(HERE, "bakeoff", "sources.jsonl"))
    ap.add_argument("--show", action="store_true", help="print the first 400 characters of each source")
    a = ap.parse_args(argv)
    ids = sorted({t["source"] for t in bakeoff.read_topics(a.topics) if t["source"] != "-"})
    rows, bad = [], 0
    for sid in ids:
        try:
            rows.append(fetch(sid))
        except (LookupError, OSError, KeyError, ValueError) as e:
            bad += 1
            print(f"[sources] FAILED {sid}: {e}", file=sys.stderr)
            continue
        if a.show:
            print(f"=== {sid}  ({len(rows[-1]['text'])} chars, {rows[-1]['licence']})\n{rows[-1]['text'][:400]}\n")
        time.sleep(0.2)
    if bad:
        print(f"[sources] {bad} of {len(ids)} sources failed; nothing written", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[sources] {len(rows)} sources -> {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
