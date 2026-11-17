"""HTTP side of the regression suite: one Target per running engine (an install's local port, staging, public).
Standard library only, so the suite runs with the bundle's own Python on Windows."""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class Target:
    """An engine's HTTP port. `prefix` is '' for an install (/instant) and '/api' for the public gateway (/api/instant)."""

    def __init__(self, base, prefix=None, timeout=60, rate=0.0):
        self.base = base.rstrip("/")
        self.rate = rate                  # seconds between two requests, all threads together (be gentle with public)
        self._gate = threading.Lock()
        self._next = 0.0
        self.throttled = 0
        self.timeout = timeout
        self.prefix = prefix
        self.errors = 0
        self.requests = 0

    def raw(self, path, params=None, timeout=None):
        """-> (status, body bytes, seconds, error text). Never raises; a refused connection is status 0."""
        for attempt in range(4):
            r = self._raw(path, params, timeout)
            if r[0] == 0 and attempt == 0 and ("resolution" in r[3] or "Name or service" in r[3]):
                time.sleep(2)                    # one DNS hiccup is the network's, not the engine's
                continue
            if r[0] != 429 or attempt == 3:     # a throttle is not an engine answer: wait and ask again (counted)
                return r
            self.throttled += 1
            time.sleep(3 * (attempt + 1))

    def _raw(self, path, params, timeout):
        url = self.base + path + (("?" + urllib.parse.urlencode(params)) if params else "")
        if self.rate:
            with self._gate:
                now = time.monotonic()
                wait = self._next - now
                self._next = max(now, self._next) + self.rate
            if wait > 0:
                time.sleep(wait)
        t0 = time.monotonic()
        self.requests += 1
        try:
            with urllib.request.urlopen(url, timeout=timeout or self.timeout) as r:
                return r.status, r.read(), time.monotonic() - t0, ""
        except urllib.error.HTTPError as e:
            return e.code, e.read(), time.monotonic() - t0, f"HTTP {e.code}"
        except Exception as e:  # noqa: BLE001 - timeouts, resets, DNS: all "no answer" to a test
            self.errors += 1
            return 0, b"", time.monotonic() - t0, f"{type(e).__name__}: {e}"

    def detect(self):
        """Find where /instant lives (an install: /instant; public: /api/instant). -> True when one answers JSON."""
        if self.prefix is not None:
            return True
        for p in ("", "/api"):
            st, body, _, _ = self.raw(p + "/instant", {"q": "wikipedia", "results": 1, "full": 1})
            if st == 200 and body[:1] == b"{":
                self.prefix = p
                return True
        self.prefix = ""
        return False

    def instant(self, q, results=1, full=1, **extra):
        """-> (answer dict or None, seconds, error text). The request the page makes."""
        params = {"q": q, "results": results, "full": full}
        params.update(extra)
        st, body, secs, err = self.raw(self.prefix + "/instant", params)
        if st != 200:
            return None, secs, err or f"HTTP {st}"
        try:
            return json.loads(body), secs, ""
        except ValueError as e:
            return None, secs, f"not JSON: {e}"

    def suggest(self, prefix_text):
        """The typing request: results=0."""
        return self.instant(prefix_text, results=0, full=0)

    def json_of(self, path, params=None):
        st, body, secs, err = self.raw(path, params, timeout=20)
        if st != 200:
            return None, st, err
        try:
            return json.loads(body), st, ""
        except ValueError:
            return None, st, "not JSON"

    def storez(self):
        d, st, err = self.json_of("/storez")
        return d if isinstance(d, dict) else None

    def readyz(self):
        st, body, secs, err = self.raw("/readyz", timeout=20)
        return st, body.decode("utf-8", "replace")[:200]


def host(u):
    h = urllib.parse.urlsplit(u or "").hostname or ""
    return h[4:] if h.startswith("www.") else h


def facts(d):
    """(card title or '-', official-site host or '-', first-result host or '-') of an /instant answer (the golden triple)."""
    card = d.get("card") or {}
    off = next((l.get("url") for l in card.get("links", []) if l.get("title") == "Official site"), None)
    res = d.get("results") or []
    return (card.get("title") or "-", host(off) or "-", host(res[0].get("url")) if res else "-")


def normal(d):
    """An answer without its live fields (time, news, weather, scores, timings): what parity and parallel==serial compare."""
    card = d.get("card") or {}
    return {
        "card": card.get("title"),
        "official": next((l.get("url") for l in card.get("links", []) if l.get("title") == "Official site"), None),
        "description": card.get("description"),
        "intent": d.get("intent"),
        "confident": d.get("confident"),
        "results": [r.get("url") for r in (d.get("results") or [])[:8]],
        "answer": (d.get("answer") or {}).get("answer") if isinstance(d.get("answer"), dict) else d.get("answer"),
    }


def percentile(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(round(p * (len(v) - 1))))] if v else 0.0
