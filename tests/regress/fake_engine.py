#!/usr/bin/env python3
"""A stand-in engine for testing the suite itself: answers /instant from the recorded golden answers
(apps/search/twin/golden/fixture.json) with the known-wrong ones corrected, plus /healthz, /readyz and /storez.
  fake_engine.py PORT [--break lg:card,gmail:first]   (a deliberate regression the suite must catch)
Never started by the suite; the unit test starts it."""
import http.server
import json
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = json.load(open(os.path.join(HERE, "..", "..", "apps", "search", "twin", "golden", "fixture.json"), encoding="utf-8"))


def card(title, site=None, desc="a thing that exists in the world today"):
    links = [{"title": "Official site", "url": site}] if site else []
    return {"title": title, "description": desc, "lead": desc + ". More text.", "links": links}


def row(url):
    return {"url": url, "title": url, "source": "site", "content": ""}


GOOD = {   # the answers the release-2 engine gives where the recorded ones were wrong
    "after effects": (card("Adobe After Effects", "https://www.adobe.com/products/aftereffects.html"), ["https://www.adobe.com/products/aftereffects.html"]),
    "cortana": (card("Cortana", "https://www.microsoft.com/cortana"), ["https://www.microsoft.com/cortana"]),
    "meet": (card("Google Meet", "https://meet.google.com"), ["https://meet.google.com"]),
    "prime": (card("Amazon Prime", "https://www.amazon.com/prime"), ["https://www.amazon.com/prime"]),
    "messenger": (card("Facebook Messenger", "https://messenger.com"), ["https://messenger.com"]),
    "acrobat": (card("Adobe Acrobat", "https://www.adobe.com/acrobat"), ["https://www.adobe.com/acrobat"]),
    "voice": (card("Google Voice"), ["https://voice.google.com"]),
    "word": (card("Word"), ["https://en.wikipedia.org/wiki/Word"]),
    "trump": (card("Donald Trump", "https://donaldjtrump.com"), ["https://donaldjtrump.com"]),
    "donald trump": (card("Donald Trump", "https://donaldjtrump.com"), ["https://donaldjtrump.com"]),
    "us": (None, ["https://en.wikipedia.org/wiki/United_States"]),
    "ceo of microsoft": (None, ["https://en.wikipedia.org/wiki/Phil_Spencer_(business_executive)"]),
}
SUGGEST = {"starb": "Starbucks", "goog": "Google", "chevr": "Chevrolet", "harr": "Harry Potter", "amaz": "Amazon",
           "face": "Facebook", "netf": "Netflix", "micro": "Microsoft"}
BREAK = {}
STATE = {"finished_hit": 0, "hit": 0, "miss": 0}


def answer(q, results):
    if q in SUGGEST and not results:
        return {"suggest": [{"title": SUGGEST[q], "url": "https://x/" + q}, {"title": SUGGEST[q] + " other", "url": "https://y/" + q}],
                "phrases": [q + " store"], "suggest_lead": 1, "results": []}
    if q in GOOD:
        c, urls = GOOD[q]
        d = {"card": c, "results": [row(u) for u in urls], "intent": "nav", "confident": True}
    elif q in FIX:
        f = FIX[q]
        d = {"card": dict(f["card"], lead=(f["card"].get("description") or "x") + ". More text.") if f.get("card") else None, "results": f["results"], "intent": "nav", "confident": True}
    else:
        d = {"card": None, "results": [row("https://example.com/" + urllib.parse.quote(q))], "intent": "other", "confident": False}
    if q == "ceo of microsoft":
        d["answer"] = {"title": "Microsoft", "answer": "Satya Nadella", "question": "ceo"}
    for b in BREAK.get(q, []):
        if b == "card" and d.get("card"):
            d["card"] = dict(d["card"], title=d["card"]["title"] + " X")
        if b == "first":
            d["results"] = [row("https://wrong.example/")] + d["results"][1:]
    d.setdefault("suggest", []); d.setdefault("phrases", []); d["ms"] = 5
    return d


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(u.query)
        if u.path == "/healthz":
            return self.send(200, b"ok", "text/plain")
        if u.path == "/readyz":
            return self.send(200, b"ready", "text/plain")
        if u.path == "/storez":
            return self.send(200, {"fp": "fake", "rows": {"search": 1}, "curated": {"fake": 10}, "up_s": 100, **STATE})
        if u.path == "/instant":
            q = qs.get("q", [""])[0]
            STATE["finished_hit"] += 1
            return self.send(200, answer(q, qs.get("results", ["1"])[0] != "0"))
        self.send(404, {"error": "not found"})


if __name__ == "__main__":
    port = int(sys.argv[1])
    if "--break" in sys.argv:
        for it in sys.argv[sys.argv.index("--break") + 1].split(","):
            n, what = it.split(":")
            BREAK.setdefault(n, []).append(what)
    http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
