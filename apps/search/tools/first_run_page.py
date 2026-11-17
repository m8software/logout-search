#!/usr/bin/env python3
"""The page a fresh install opens, as a browser sees it (Gitea first-run, owner 2026-11-17: "out of the gate, no extra steps").

    first_run_page.py [--url https://search.logout.cloud/#engine=1] [--channel msedge|chrome] [--exe PATH] [--wait 90] [--insecure]

Opens the page in a clean headless browser profile (no memory of any engine, as after a new install), waits for the pill to say
the answer comes from this computer's own engine, then types a search and presses Enter. Passes (exit 0) only when:
  * the pill shows the engine within --wait seconds, with no refresh;
  * the search is answered, and ZERO requests were made to /api/instant on the public site (nothing was sent to it);
Prints what it saw as JSON. Used by the hosted Windows job (.github/workflows/windows-customer-test.yml, headless Edge) right
after setup, and locally against a stand-in rendezvous. Needs `pip install playwright`; with --channel msedge no download is made.
"""
import argparse
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", default="https://search.logout.cloud/#engine=1")
    ap.add_argument("--channel", default="msedge")
    ap.add_argument("--exe", help="a browser executable instead of --channel")
    ap.add_argument("--wait", type=float, default=90.0)
    ap.add_argument("--insecure", action="store_true", help="ignore certificate errors (a stand-in server)")
    ap.add_argument("--query", default="weather today")
    a = ap.parse_args()
    out = {"ok": False, "pill_after_s": None, "public_instant_requests": None, "answer": "", "pill": ""}
    with sync_playwright() as p:
        kw = {"executable_path": a.exe} if a.exe else {"channel": a.channel}
        b = p.chromium.launch(headless=True, args=["--no-sandbox"] + (["--ignore-certificate-errors"] if a.insecure else []), **kw)
        ctx = b.new_context(ignore_https_errors=a.insecure)
        page = ctx.new_page()
        public = []
        page.on("request", lambda r: public.append(r.url) if "/api/instant" in r.url else None)
        t0 = time.time()
        page.goto(a.url)
        pill = lambda: page.evaluate("(() => { const m = document.getElementById('mode'); return m.hidden ? 'hidden' : (m.dataset.hint || m.textContent); })()")
        while time.time() - t0 < a.wait:
            time.sleep(1)
            if "your own engine" in pill() or "Answered by your own" in pill():
                out["pill_after_s"] = round(time.time() - t0, 1)
                break
        out["pill"] = pill()
        n0 = len(public)
        page.click("#f input")
        page.keyboard.type(a.query, delay=30)
        page.keyboard.press("Enter")
        for _ in range(30):
            time.sleep(1)
            txt = page.evaluate("document.getElementById('out') ? document.getElementById('out').innerText : ''")
            if "result" in txt and "Reconnecting" not in txt:
                out["answer"] = txt[:80].replace("\n", " | ")
                break
        out["public_instant_requests"] = len(public)
        out["ok"] = out["pill_after_s"] is not None and bool(out["answer"]) and len(public) == 0
        b.close()
    print(json.dumps(out, indent=1))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
