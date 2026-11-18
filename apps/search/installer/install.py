#!/usr/bin/env python3
"""Install the logout home search engine's data: download a signed release, check it, make it `current` (ADR-063).

    install.py install --source URL|DIR --pubkey KEY --dest BASE [--quick] [--switch] [--keep-download]
    install.py start   --dest BASE [--port 8090] [--engine search_api.py] [--no-smoke] [--print-env [--format posix|cmd|powershell|json]]
    install.py addon   install|remove NAME --dest BASE [--source ... --pubkey ...]    |    addon list --dest BASE [--source ... --pubkey ...]
    install.py status  --dest BASE

Standard library only; Windows, macOS and Linux (pathlib-free os.path code, no shell). Python 3.9+.

  --source   where the release is hosted: https://host/path  (or http://, file:///path, or a plain folder). It holds
             manifest.json, manifest.json.sig and files/<path>[.xz|.zst]  (what `assemble.py build --publish` writes).
             `hf:ORG/NAME` is the public Hugging Face dataset (apps/search/release/hf_dataset.py): its signed latest.json on `main`
             names the newest release, which is then read from that release's tag (an upload never changes a tag's files, so a
             resumed download cannot mix two releases). `hf:ORG/NAME@TAG` pins one release. LOGOUT_HF_ENDPOINT overrides the host.
  --pubkey   the PINNED public key: a PEM file, a file holding 64 hex characters, or `ed25519:<64 hex>` / `ed25519:<base64>`.
             The key is never read from the source. It is the only thing that makes the download trustworthy.
  --dest     the install folder BASE. Result:   BASE/releases/<release id>/   and   BASE/current -> releases/<id>
             (exactly what apps/search/release/update_apply.py expects for the monthly updates).

What happens, in order (nothing is written to BASE/releases/<id> or BASE/current until the very last step):
  1. fetch manifest.json + manifest.json.sig; verify the signature with the pinned key; refuse anything but a FULL release
     of a format this code knows; refuse a file path that is absolute or climbs out of the folder.
  2. already installed and still intact?  (re-run) -> say so and stop: no download.
  3. check the free disk space.
  4. for every file: download to BASE/.download/<id>/ (HTTP Range resume; the .part file is hashed), compare size + sha256 of the
     stored bytes with the manifest, decode (xz / zstd / as is) into the STAGE folder BASE/releases/.install-<id>/, compare size +
     sha256 of the real file. A file already in the stage folder that matches is skipped (that is what makes a re-run resume).
  5. the engine's own release check (release_check.check_release) on the stage folder, files made read-only, fsync.
  6. ONE atomic rename: stage -> BASE/releases/<id>, then the `current` pointer (one rename of a fresh symlink).
  Any mismatch (size, hash, signature, release check) removes the stage folder and exits 2: the bad bytes are never kept.
  Killing the program leaves the stage folder (hidden, resumable) and no `current`; run it again.

Windows: `current` is a symlink; creating one needs Developer Mode or an elevated prompt. Without it the data is installed and
verified but there is no pointer, and the program says so (start the engine on BASE/releases/<id> instead).
"""
import argparse
import base64
import concurrent.futures
import fnmatch
import hashlib
import http.client
import json
import lzma
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.join(HERE, "..", "release")):      # a flat bundle, or the repo layout
    if os.path.isfile(os.path.join(_p, "release_check.py")):
        sys.path.insert(0, os.path.abspath(_p))
        break
import release_check as rc  # noqa: E402  (stdlib + registry_idents)
import release_ptr  # noqa: E402  (stdlib: the same pointer code update_apply.py uses)

# Files handled at once (2026-11-14). Measured on a 16-thread laptop with a 400 Mbit line: the download ran at 49 MB/s,
# unpacking xz at 50 MB/s on one core, and they ran one after the other, file by file, so 18 GB of unpacking took longer
# than the 11 GB download. Several files at once puts the unpacking on several cores and under the downloading.
WORKERS = max(1, int(os.environ.get("LOGOUT_INSTALL_WORKERS") or min(4, os.cpu_count() or 1)))
_STOP = threading.Event()                                  # one file failed: the others stop at their next chunk
FAIL_AFTER = os.environ.get("LOGOUT_INSTALL_FAIL_AFTER", "")      # test hook: kill -9 self after N files are staged
FAKE_FREE = os.environ.get("LOGOUT_INSTALL_FAKE_FREE_BYTES", "")  # test hook: pretend the disk has this much room
CHUNK = 1 << 20
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


class InstallError(Exception):
    """A refusal: the message says what was wrong and what was left on disk."""


class NetError(InstallError):
    """The network or the host failed (a refused connection, HTTP 5xx, 429 "slow down"): nothing is wrong with what was
    downloaded, so it is kept and the next run resumes. retry_after: seconds the host asked us to wait, when it said."""
    retry_after = None


class Fatal(InstallError):
    """A refusal that retrying cannot change (a missing file, an HTTP 4xx)."""


class MirrorMissing(Fatal):
    """The second source has no such path (its signed name map does not list it)."""


def say(msg: str) -> None:
    if PROGRESS is not None:
        PROGRESS.clear_line()
    print(msg, flush=True)


class Progress:
    """What the install is doing, said while it does it (owner 2026-11-14: one file's name sat on the screen for many
    minutes while the 4.4 GB file after it downloaded, and nothing said whether it was stuck). On a terminal one line is
    rewritten every second; elsewhere (a log, a pipe) a line is printed every 15 seconds. Counts are kept per file as
    absolute numbers, so a retried or resumed download is never counted twice."""

    def __init__(self, dl_total: int, out_total: int, out=None):
        self.dl_total, self.out_total = max(dl_total, 1), max(out_total, 1)
        self.dl, self.unp, self.now = {}, {}, {}
        self.lock = threading.Lock()
        self.out = out or sys.stdout
        self.tty = bool(getattr(self.out, "isatty", lambda: False)())
        self.t0 = time.monotonic()
        self.base = None                 # bytes already there when the run began (a resumed install): not this run's speed
        self.shown = False
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._tick, daemon=True, name="install-progress")

    def start(self):
        self.thread.start()
        return self

    def downloaded(self, rel: str, n: int, what: str = "") -> None:
        with self.lock:
            self.dl[rel] = n
            if what:
                self.now[what] = "downloading"

    def unpacked(self, path: str, n: int) -> None:
        with self.lock:
            self.unp[path] = n
            self.now[path] = "unpacking"

    def phase(self, path: str, what) -> None:
        with self.lock:
            if what is None:
                self.now.pop(path, None)
            else:
                self.now[path] = what

    @staticmethod
    def _gb(n: float) -> str:
        return f"{n / 1e9:.1f}"

    def line(self, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        with self.lock:
            dl, unp, doing = sum(self.dl.values()), sum(self.unp.values()), dict(self.now)
        work, total = dl + unp, self.dl_total + self.out_total
        if self.base is None:
            self.base = work
        frac = min(work / total, 1.0)
        el = now - self.t0
        fresh = work - self.base
        left = ""
        if el >= 10 and fresh > 0 and frac < 1:
            secs = el * (total - work) / fresh
            left = " · about " + (f"{secs / 60:.0f} min" if secs >= 90 else "a minute" if secs >= 45 else "less than a minute") + " left"
        names = sorted(doing, key=lambda p: (doing[p] != "downloading", p))
        cur = ""
        if names:
            first = names[0].rsplit("/", 1)[-1]
            cur = f" · {doing[names[0]]} {first}" + (f" and {len(names) - 1} more" if len(names) > 1 else "")
        return (f"  {frac * 100:3.0f}%  {self._gb(dl)} of {self._gb(self.dl_total)} GB downloaded, "
                f"{self._gb(unp)} of {self._gb(self.out_total)} GB unpacked{left}{cur}")

    def clear_line(self) -> None:
        if self.tty and self.shown:
            self.out.write("\r\033[K")
            self.out.flush()
            self.shown = False

    def _tick(self) -> None:
        every = 1.0 if self.tty else 15.0
        while not self.stop.wait(every):
            text = self.line()
            if self.tty:
                self.out.write("\r\033[K" + text[:max(20, shutil.get_terminal_size((100, 20)).columns - 1)])
                self.shown = True
            else:
                self.out.write(text + "\n")
            self.out.flush()

    def finish(self) -> None:
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)
        self.clear_line()


PROGRESS = None


# ================================================================================================ ed25519 (RFC 8032), verify only
# The standard library has no ed25519; this is the RFC's reference verifier (about 10 ms per signature, one per install).
_P = 2 ** 255 - 19
_Q = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _inv(x):
    return pow(x, _P - 2, _P)


def _recover_x(y, sign):
    if y >= _P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _I % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * _inv(5) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _add(P, Q):
    A, B = (P[1] - P[0]) * (Q[1] - Q[0]) % _P, (P[1] + P[0]) * (Q[1] + Q[0]) % _P
    C, D = 2 * P[3] * Q[3] * _D % _P, 2 * P[2] * Q[2] % _P
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _mul(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _eq(P, Q):
    return (P[0] * Q[2] - Q[0] * P[2]) % _P == 0 and (P[1] * Q[2] - Q[1] * P[2]) % _P == 0


def _decode(s: bytes):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def ed25519_verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    A, R = _decode(pub), _decode(sig[:32])
    if A is None or R is None:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= _Q:
        return False
    h = int.from_bytes(hashlib.sha512(sig[:32] + pub + msg).digest(), "little") % _Q
    return _eq(_mul(s, _G), _add(R, _mul(h, A)))


_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


def load_pubkey(arg: str) -> bytes:
    k = _load_pubkey(arg)
    if k not in MIRROR_KEYS:
        MIRROR_KEYS.append(k)                              # the second source's name map is checked with the keys in use
    return k


def _load_pubkey(arg: str) -> bytes:
    """32 raw bytes from a PEM file, a hex file, `ed25519:<hex|base64>` or bare 64 hex characters."""
    text = arg
    if os.path.isfile(arg):
        text = open(arg, "rb").read().decode("ascii", "replace").strip()
    elif arg.startswith("ed25519:"):
        text = arg[len("ed25519:"):]
    text = text.strip()
    try:
        if "BEGIN PUBLIC KEY" in text:
            der = base64.b64decode("".join(l for l in text.splitlines() if not l.startswith("-----")))
            if len(der) == 44 and der.startswith(_SPKI_PREFIX):
                return der[12:]
        elif re.fullmatch(r"[0-9a-fA-F]{64}", text):
            return bytes.fromhex(text)
        else:
            raw = base64.b64decode(text, validate=True)
            if len(raw) == 32:
                return raw
    except ValueError:
        pass
    raise InstallError(f"--pubkey {arg!r}: not an ed25519 public key (PEM file, 64 hex characters, or ed25519:<hex|base64>)")


# ================================================================================================ the source
class Source:
    """manifest/files from an http(s) URL, a file:// URL or a folder. `opened` counts what was asked, for the tests.
    A Hugging Face dataset URL has a second source behind it (GitHub release assets, see Mirror): a request the first one
    cannot serve is asked of the second; what comes back is checked exactly as before (signatures, sizes, sha256)."""

    def __init__(self, src: str, mirror: bool = True):
        self.src = src
        self.requests = []
        self.mirror = mirror_for(src) if mirror else None
        if re.match(r"^https?://", src):
            self.kind, self.base = "http", src.rstrip("/")
        elif src.startswith("file://"):
            self.kind, self.base = "dir", urllib.request.url2pathname(urllib.parse.urlsplit(src).path)
        else:
            self.kind, self.base = "dir", src

    def read_small(self, rel: str) -> bytes:
        f, _total, _res = self.open(rel, 0)
        with f:
            return f.read()

    def open(self, rel: str, offset: int, end: int | None = None):
        """(file object, total length or None, True when the bytes start at `offset`). end: the byte after the last one
        wanted (a slice: Range bytes=offset-(end-1)); the caller still stops reading there, whatever the host sends.
        A first source that cannot serve it (busy, down, 4xx) hands over to the mirror, which then goes first for the rest
        of this run; the first source's own error is the one reported when neither can."""
        global _PREFER_MIRROR
        if self.mirror is None:
            return self._open_primary(rel, offset, end)
        if _PREFER_MIRROR:
            try:
                return self.mirror.open(rel, offset, end)
            except InstallError:
                pass
        try:
            return self._open_primary(rel, offset, end)
        except InstallError as e:
            try:
                got = self.mirror.open(rel, offset, end)
            except MirrorMissing as e2:
                raise e2 if isinstance(e, NetError) else e          # the first source was only down: the second one says it is not published
            except InstallError:
                raise e
            if isinstance(e, NetError):
                _PREFER_MIRROR = True
            self.requests.append(("mirror:" + rel, offset))
            return got

    def _open_primary(self, rel: str, offset: int, end: int | None = None):
        self.requests.append((rel, offset))
        if self.kind == "dir":
            p = os.path.join(self.base, *rel.split("/"))
            try:
                f = open(p, "rb")
            except OSError as e:
                raise Fatal(f"{self.src}: cannot read {rel} ({e.strerror or e})")
            total = os.fstat(f.fileno()).st_size
            if offset > total:
                offset = 0
            f.seek(offset)
            return f, total, True
        url = self.base + "/" + "/".join(urllib.parse.quote(x) for x in rel.split("/"))
        rng = f"bytes={offset}-{end - 1}" if end is not None else (f"bytes={offset}-" if offset else None)
        req = urllib.request.Request(url, headers={"Range": rng} if rng else {})
        try:
            r = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:
            if e.code == 416 and offset:
                return self._open_primary(rel, 0)
            if e.code in (408, 429) or e.code >= 500:      # busy or rate-limited (Hugging Face answers 429 to an anonymous burst): wait, try again
                err = NetError(f"{url}: HTTP {e.code}")
                ra = (e.headers.get("Retry-After") or "").strip() if e.headers else ""
                err.retry_after = int(ra) if ra.isdigit() else None
                raise err
            raise Fatal(f"{url}: HTTP {e.code}")
        except (urllib.error.URLError, OSError) as e:
            raise NetError(f"{url}: {e}")
        if r.status == 206:
            m = re.match(r"bytes (\d+)-\d+/(\d+|\*)", r.headers.get("Content-Range", ""))
            total = int(m.group(2)) if m and m.group(2) != "*" else None
            return r, total, True
        length = r.headers.get("Content-Length")
        return r, int(length) if length else None, offset == 0


# ================================================================================================ the Hugging Face dataset
HF_ENDPOINT = os.environ.get("LOGOUT_HF_ENDPOINT", "https://huggingface.co")
HF_RE = re.compile(r"^hf:([A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95})(?:@([A-Za-z0-9][A-Za-z0-9._+-]{0,63}))?$")
LATEST_FORMAT = 1


def hf_base(repo: str, rev: str) -> str:
    return f"{HF_ENDPOINT.rstrip('/')}/datasets/{repo}/resolve/{urllib.parse.quote(rev, safe='')}"


CHANNELS = ("stable", "beta")
BETA_POINTER = "latest-beta.json"                        # the beta channel's pointer, signed like latest.json


# ================================================================================================ the second source
# GitHub release assets of MIRROR_REPO (owner 2026-11-15). Layout, written by release/github_mirror.py:
#   tag data-latest       the pointers: latest.json(.sig), latest-beta.json(.sig)
#   tag data-<release>    that release: manifest.json(.sig), its files and its update packages
#   tag data-lists        the answers: manifest.json(.sig), manifest-beta.json(.sig), parts/
#   tag data-addons       the add-ons: <name>/manifest.json, <name>/files/...
# Assets are flat and under 2 GiB each, so a tag's `mirror-map.json` (signed with the release key) says which assets make up each
# path: a path -> one asset, or several consecutive slices of one file (lookup.sqlite.xz is 4.4 GB). The map only translates names:
# the bytes still have to match the signed manifest's size and sha256, which is the trust root, so a wrong map can only make a
# download fail.
MIRROR_REPO = "m8software/logout-search"
MIRROR_MAP = "mirror-map.json"
MIRROR_KEYS: list = []                                    # the keys load_pubkey has seen: the map is checked with them
_PREFER_MIRROR = False
_ASSET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")


def mirror_base():
    b = os.environ.get("LOGOUT_MIRROR_BASE")
    if b:
        return b.rstrip("/")
    if os.environ.get("LOGOUT_MIRROR") == "0" or HF_ENDPOINT.rstrip("/") != "https://huggingface.co":
        return None                                       # tests and other endpoints never reach the real GitHub
    return f"https://github.com/{MIRROR_REPO}/releases/download"


def mirror_for(src: str):
    base = mirror_base()
    pre = HF_ENDPOINT.rstrip("/") + "/datasets/"
    if not base or not src.startswith(pre):
        return None
    m = re.match(r"^[^/]+/[^/]+/resolve/([^/]+)(?:/(.*))?$", src[len(pre):])
    if not m:
        return None
    rev, sub = urllib.parse.unquote(m.group(1)), (m.group(2) or "").strip("/")
    if sub == "":
        tag, prefix = ("data-latest" if rev == "main" else f"data-{rev}"), ""
    elif sub == "lists":
        tag, prefix = "data-lists", ""
    elif sub.startswith("addons/"):
        tag, prefix = "data-addons", sub[len("addons/"):] + "/"
    else:
        return None
    return Mirror(base, tag, prefix) if ID_RE.match(tag) else None


class Chain:
    """A byte range of one path that the mirror keeps as one or more assets, read as one stream."""

    def __init__(self, plain: "Source", parts: list):
        self.plain, self.parts, self.i, self.cur, self.left = plain, parts, 0, None, 0

    def _next(self) -> bool:
        self.close()
        if self.i >= len(self.parts):
            return False
        name, a, b = self.parts[self.i]
        self.i += 1
        r, _total, ok = self.plain._open_primary(name, a, b)
        if not ok and a:                                   # the host sent the whole asset: skip to the slice
            todo = a
            while todo:
                got = r.read(min(CHUNK, todo))
                if not got:
                    raise NetError(f"{name}: ended before byte {a}")
                todo -= len(got)
        self.cur, self.left = r, b - a
        return True

    def read(self, n: int = -1) -> bytes:
        while True:
            if self.cur is None and not self._next():
                return b""
            want = self.left if n is None or n < 0 else min(n, self.left)
            data = self.cur.read(want)
            if data:
                self.left -= len(data)
                if self.left <= 0:
                    self.close()
                return data
            if self.left > 0:
                raise ConnectionError(f"mirror asset ended {self.left} bytes short")
            self.close()

    def close(self) -> None:
        if self.cur is not None:
            try:
                self.cur.close()
            finally:
                self.cur = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class Mirror:
    def __init__(self, base: str, tag: str, prefix: str):
        self.base, self.tag, self.prefix = base, tag, prefix
        self.plain = Source(f"{base}/{tag}", mirror=False)
        self._files = None
        self._lock = threading.Lock()

    def files(self) -> dict:
        with self._lock:
            if self._files is None:
                body, sig = self.plain.read_small(MIRROR_MAP), self.plain.read_small(MIRROR_MAP + ".sig")
                if not MIRROR_KEYS:
                    raise Fatal("mirror: no pinned key is loaded to check the name map with")
                if not any(ed25519_verify(k, body, sig) for k in MIRROR_KEYS):
                    raise Fatal(f"mirror {self.tag}: the name map is not signed by the pinned key: not used")
                try:
                    m = json.loads(body)
                except ValueError as e:
                    raise Fatal(f"mirror {self.tag}: the name map is not JSON: {e}")
                if not isinstance(m, dict) or m.get("format") != 1 or m.get("tag") != self.tag or not isinstance(m.get("files"), dict):
                    raise Fatal(f"mirror {self.tag}: the name map is not format 1 for this tag")
                for path, ent in m["files"].items():
                    safe_rel(path)
                    assets = ent.get("assets") if isinstance(ent, dict) else None
                    if not assets or any(not isinstance(a, dict) or not _ASSET_RE.match(str(a.get("name"))) or not isinstance(a.get("bytes"), int)
                                         or a["bytes"] <= 0 for a in assets):
                        raise Fatal(f"mirror {self.tag}: the name map's entry for {path!r} is malformed")
                self._files = m["files"]
            return self._files

    def open(self, rel: str, offset: int, end: int | None = None):
        ent = self.files().get(self.prefix + rel)
        if ent is None:
            raise MirrorMissing(f"mirror {self.tag}: {self.prefix + rel} is not there")
        total = sum(a["bytes"] for a in ent["assets"])
        stop = total if end is None else min(end, total)
        parts, pos = [], 0
        for a in ent["assets"]:
            lo, hi = pos, pos + a["bytes"]
            pos = hi
            s, e = max(offset, lo), min(stop, hi)
            if s < e:
                parts.append((a["name"], s - lo, e - lo))
        return Chain(self.plain, parts), total, True


def read_latest(src: "Source", pub: bytes, name: str = "latest.json") -> dict:
    """latest.json on the dataset's `main`, signed with the release key: {format, release, revision, manifest_sha256, updates}.
    name: the pointer's file (latest-beta.json for the beta channel; its signature is name + ".sig")."""
    body, sig = src.read_small(name), src.read_small(name + ".sig")
    if not ed25519_verify(pub, body, sig):
        raise Fatal(f"{src.src}: {name} is not signed by the pinned key: refusing (nothing was downloaded)")
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise Fatal(f"{name} is not JSON: {e}")
    if doc.get("format") != LATEST_FORMAT:
        raise Fatal(f"{name} format {doc.get('format')!r}, this code reads {LATEST_FORMAT}: get a newer installer")
    for k in ("release", "revision"):
        if not isinstance(doc.get(k), str) or not ID_RE.match(doc[k]):
            raise Fatal(f"{name} {k} {doc.get(k)!r} is not a plain name")
    if not re.fullmatch(r"[0-9a-f]{64}", str(doc.get("manifest_sha256", ""))):
        raise Fatal(f"{name} has no manifest_sha256")
    ups = doc.get("updates") or {}
    if not isinstance(ups, dict) or any(not ID_RE.match(str(k)) for k in ups):
        raise Fatal(f"{name} updates is not a map of release ids")
    for p in ups.values():
        safe_rel(p)
    chain = doc.get("chain") or []                       # the daily hops, oldest first (hf_dataset.py stage --prev); absent = none
    if not isinstance(chain, list) or len(chain) > 400:
        raise Fatal("latest.json chain is not a list of hops")
    for h in chain:
        if not isinstance(h, dict) or not ID_RE.match(str(h.get("from"))) or not ID_RE.match(str(h.get("to"))) or not isinstance(h.get("bytes"), int):
            raise Fatal("latest.json chain has a hop that is not {from, to, package, bytes}")
        safe_rel(h.get("package"))
    return doc


def is_missing(e: Exception) -> bool:
    """A pointer or manifest that is simply not published (HTTP 404, or no such file in a folder source)."""
    t = str(e)
    return isinstance(e, Fatal) and ("HTTP 404" in t or "cannot read" in t or "is not there" in t)


def read_latest_channel(src: "Source", pub: bytes, channel: str = "stable") -> dict:
    """The pointer a channel follows. stable: latest.json. beta: latest-beta.json, or latest.json while no beta pointer is
    published (beta is never behind stable). A pointer that is there but not signed is refused, never skipped."""
    if channel == "beta":
        try:
            return read_latest(src, pub, BETA_POINTER)
        except Fatal as e:
            if not is_missing(e):
                raise
    return read_latest(src, pub)


def resolve_source(arg: str, pub: bytes):
    """--source -> (URL or folder, latest.json or None). Anything not `hf:` is returned as it is."""
    m = HF_RE.match(arg)
    if not m:
        if arg.startswith("hf:"):
            raise Fatal(f"--source {arg!r}: write hf:ORG/NAME or hf:ORG/NAME@TAG")
        return arg, None
    repo, rev = m.groups()
    if rev:
        return hf_base(repo, rev), None
    latest = read_latest(Source(hf_base(repo, "main")), pub)
    return hf_base(repo, latest["revision"]), latest


# ================================================================================================ decoding
def zstd_decoder():
    try:
        from compression import zstd            # Python 3.14+
        return lambda src, dst: _stream_decode(zstd.ZstdDecompressor(), src, dst)
    except ImportError:
        pass
    try:
        import zstandard
        return lambda src, dst: _zstandard_decode(zstandard, src, dst)
    except ImportError:
        pass
    exe = shutil.which("zstd")
    if exe:
        def run(src, dst):
            with open(dst, "wb") as fo:
                r = subprocess.run([exe, "-dc", src], stdout=fo, stderr=subprocess.PIPE)
            if r.returncode:
                raise InstallError(f"zstd could not decode {src}: {r.stderr.decode(errors='replace')[:200]}")
        return run
    raise InstallError("this release stores some files as zstd, and neither Python 3.14's compression.zstd, the `zstandard` package "
                       "nor the `zstd` command is available: install one of them (or use a release stored as xz)")


def _stream_decode(dec, src, dst, on=None):
    """on(bytes written so far): the install's progress line."""
    n = 0
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        for b in iter(lambda: fi.read(CHUNK), b""):
            o = dec.decompress(b)
            fo.write(o)
            n += len(o)
            if on is not None:
                on(n)


def _zstandard_decode(zstandard, src, dst):
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        zstandard.ZstdDecompressor().copy_stream(fi, fo)


def xz_decode(src, dst, on=None):
    try:
        _stream_decode(lzma.LZMADecompressor(format=lzma.FORMAT_XZ), src, dst, on)
    except lzma.LZMAError as e:
        raise InstallError(f"{src}: not valid xz data ({e})")


def file_digest(path: str):
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
            n += len(b)
    return h.hexdigest(), n


# ================================================================================================ manifest
def safe_rel(p: str) -> str:
    # an absolute path ("/x") has an empty first component, so the empty-component rule below refuses it too
    if (not isinstance(p, str) or not p or "\\" in p or ":" in p.split("/")[0]
            or any(x in ("", ".", "..") for x in p.split("/"))):
        raise InstallError(f"manifest lists the path {p!r}: absolute or climbing out of the release folder, refusing")
    return p


def check_manifest(body: bytes) -> dict:
    try:
        m = json.loads(body)
    except ValueError as e:
        raise InstallError(f"manifest.json is not JSON: {e}")
    lo, hi = rc.ENGINE_FORMATS
    if not (isinstance(m.get("format"), int) and lo <= m["format"] <= hi):
        raise InstallError(f"release format {m.get('format')!r}, this installer reads {lo}..{hi}: get a newer installer")
    if not isinstance(m.get("release"), str) or not ID_RE.match(m["release"]):
        raise InstallError(f"release id {m.get('release')!r} is not a plain name")
    if m.get("from_release") is not None:
        raise InstallError(f"this is an update from {m['from_release']!r}, not a full release: apply it with update_apply.py")
    if not isinstance(m.get("files"), list) or not m["files"]:
        raise InstallError("manifest lists no files")
    seen = set()
    for f in m["files"]:
        safe_rel(f.get("path"))
        if f["path"] in seen or f["path"] in ("manifest.json", "manifest.json.sig"):
            raise InstallError(f"manifest lists {f['path']} twice or lists the manifest itself")
        seen.add(f["path"])
        if not isinstance(f.get("bytes"), int) or f["bytes"] < 0 or not re.fullmatch(r"[0-9a-f]{64}", str(f.get("sha256", ""))):
            raise InstallError(f"{f['path']}: no size / sha256 in the manifest")
    for p, t in (m.get("transport", {}).get("files", {}) or {}).items():
        if p not in seen or t.get("encoding") not in ("xz", "zstd") or not isinstance(t.get("bytes"), int) \
                or not re.fullmatch(r"[0-9a-f]{64}", str(t.get("sha256", ""))):
            raise InstallError(f"transport entry for {p!r} is malformed or names a file the manifest does not list")
    return m


# ================================================================================================ the install
def installed_ok(final: str, m: dict, quick: bool) -> list:
    errs = []
    for f in m["files"]:
        p = os.path.join(final, *f["path"].split("/"))
        if not os.path.isfile(p) or os.path.getsize(p) != f["bytes"]:
            errs.append(f"{f['path']}: missing or wrong size")
        elif not quick and file_digest(p)[0] != f["sha256"]:
            errs.append(f"{f['path']}: sha256 differs from the manifest")
    return errs


RATE_SLEEP = time.sleep        # a test replaces it

# Several connections per large file, unpacked as it arrives (2026-11-14, owner: "do all of this"). Measured on a Fedora
# laptop: one connection to Hugging Face gave 35-46 MB/s from the start of a file but 2.3 MB/s on another stretch of the
# same file, and the install ran at about 9 MB/s: one slow stretch held a whole file, and each file was unpacked (one
# core) only after it had all arrived, then read a third time to hash it. Now a file of SEGMENT_MIN or more comes as
# SEGMENTS slices at once, each kept in its own .part.N (so a stopped install resumes each where it stopped), and the
# unpacker reads the slices in order as they fill, hashing what comes in and what goes out in the same pass.
SEGMENTS = max(1, int(os.environ.get("LOGOUT_INSTALL_SEGMENTS") or 4))
SEGMENT_MIN = int(os.environ.get("LOGOUT_INSTALL_SEGMENT_MIN") or 256 * 1024 * 1024)


class RangesRefused(InstallError):
    """The host sent the whole file to a request for a slice: fall back to one connection."""


def slices(size: int, n: int | None = None) -> list:
    n = n or (SEGMENTS if size >= SEGMENT_MIN else 1)
    edges = [size * i // n for i in range(n + 1)]
    return [(edges[i], edges[i + 1]) for i in range(n) if edges[i + 1] > edges[i]] or [(0, 0)]


class Segmented:
    """One file as slices downloading at once into part.0 .. part.N-1; chunks() gives its bytes in order as they arrive."""

    def __init__(self, src: Source, rel: str, part: str, size: int, what: str, stop: threading.Event):
        self.src, self.rel, self.size, self.what, self.stop = src, rel, size, what, stop
        self.ranges = slices(size)
        self.paths = [f"{part}.{i}" for i in range(len(self.ranges))]
        self.have = []
        for p, (a, b) in zip(self.paths, self.ranges):
            n = os.path.getsize(p) if os.path.exists(p) else 0
            if n > b - a:                                   # longer than its slice: not ours, start it again
                os.remove(p)
                n = 0
            self.have.append(n)
        self.err = None
        self.cond = threading.Condition()
        self.threads = []

    def start(self) -> "Segmented":
        for i, (a, b) in enumerate(self.ranges):
            if self.have[i] < b - a:
                t = threading.Thread(target=self._fetch, args=(i,), daemon=True, name=f"seg-{i}")
                t.start()
                self.threads.append(t)
        return self

    def _report(self) -> None:
        if PROGRESS is not None:
            PROGRESS.downloaded(self.rel, sum(self.have), self.what)

    def _fetch(self, i: int) -> None:
        a, b = self.ranges[i]
        tries = 8
        for attempt in range(tries):
            if self.stop.is_set() or self.err is not None:
                return
            try:
                start = a + self.have[i]
                body, _total, ok = self.src.open(self.rel, start, b if len(self.ranges) > 1 or a else None)
                if not ok:
                    if start:
                        body.close()
                        raise RangesRefused(f"{self.what}: the host ignores byte ranges")
                with body, open(self.paths[i], "ab") as out:
                    while self.have[i] < b - a:
                        if self.stop.is_set() or self.err is not None:
                            return
                        chunk = body.read(min(CHUNK, b - a - self.have[i]))
                        if not chunk:
                            break
                        out.write(chunk)
                        out.flush()
                        with self.cond:
                            self.have[i] += len(chunk)
                            self.cond.notify_all()
                        self._report()
                if self.have[i] >= b - a:
                    return
                raise ConnectionError(f"connection closed after {self.have[i]} of {b - a} bytes of slice {i}")
            except (RangesRefused, Fatal) as e:
                self._fail(e)
                return
            except NetError as e:
                if attempt == tries - 1:
                    self._fail(NetError(f"{self.what}: download failed after {tries} tries ({e}); what was downloaded is kept, run again to resume"))
                    return
                if e.retry_after is not None or "HTTP 429" in str(e):
                    wait = min(max(e.retry_after or 0, 30 * (attempt + 1)), 300)
                    print(f"  the host asked us to slow down; waiting {wait} s (try {attempt + 1} of {tries})", flush=True)
                    RATE_SLEEP(wait)
                    continue
            except (OSError, urllib.error.URLError, ConnectionError, http.client.HTTPException) as e:
                if attempt == tries - 1:
                    self._fail(NetError(f"{self.what}: download failed after {tries} tries ({e}); what was downloaded is kept, run again to resume"))
                    return
            time.sleep(min(2 ** attempt, 8) * (0 if self.src.kind == "dir" else 1))

    def _fail(self, e: Exception) -> None:
        with self.cond:
            if self.err is None:
                self.err = e
            self.cond.notify_all()

    def chunks(self):
        """The file's bytes in order, as the slices fill; raises what a slice failed with."""
        for i, (a, b) in enumerate(self.ranges):
            want, pos = b - a, 0
            f = None
            try:
                while pos < want:
                    with self.cond:
                        while self.have[i] <= pos and self.err is None and not self.stop.is_set():
                            self.cond.wait(1.0)
                        if self.err is not None:
                            raise self.err
                        if self.stop.is_set():
                            raise Fatal(f"{self.what}: stopped (another file failed)")
                        upto = self.have[i]
                    if f is None:
                        f = open(self.paths[i], "rb")
                    f.seek(pos)
                    while pos < upto:
                        buf = f.read(min(CHUNK, upto - pos))
                        if not buf:
                            break
                        pos += len(buf)
                        yield buf
            finally:
                if f is not None:
                    f.close()

    def halt(self) -> None:
        self._fail(Fatal(f"{self.what}: stopped"))
        for t in self.threads:
            t.join(timeout=5)

    def remove(self) -> None:
        for p in self.paths:
            if os.path.exists(p):
                os.remove(p)


def stream_install(src: Source, rel: str, part: str, tsize: int, tsha: str, enc: str, dec, tmp: str, dst: str,
                   out_bytes: int, out_sha: str, what: str, prog=None) -> bool:
    """One file the new way: its slices downloading at once (Segmented), unpacked as they arrive, the bytes in and out
    hashed in the same pass, then moved to dst. True when done; False when the host refused byte ranges (the caller
    then takes one connection, the old way). An error leaves the downloaded slices for the next run, unless they are
    wrong, then they are removed."""
    if prog is not None:
        prog.phase(what, "downloading")
    os.makedirs(os.path.dirname(part), exist_ok=True)   # files/vectors/ivf/…: the slices' folder (2026-11-14: every file in a subfolder failed)
    seg = Segmented(src, rel, part, tsize, what, _STOP).start()
    h_in, h_out, n_in, n_out = hashlib.sha256(), hashlib.sha256(), 0, 0
    try:
        with open(tmp, "wb") as fo:
            for b in seg.chunks():
                h_in.update(b)
                n_in += len(b)
                try:
                    o = dec.decompress(b)
                except Exception as e:  # noqa: BLE001 - any decoder's "not valid"
                    seg.halt()
                    seg.remove()
                    raise InstallError(f"{rel}: not valid {enc} data ({e}); run again to download it afresh")
                if o:
                    fo.write(o)
                    h_out.update(o)
                    n_out += len(o)
                    if prog is not None:
                        prog.unpacked(what, n_out)
    except RangesRefused:
        seg.halt()
        seg.remove()
        if os.path.exists(tmp):
            os.remove(tmp)
        return False
    except BaseException:
        seg.halt()
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    if n_in != tsize or h_in.hexdigest() != tsha:
        seg.remove()                                      # wrong bytes: never kept, the next run downloads afresh
        os.remove(tmp)
        raise InstallError(f"{rel}: downloaded {n_in} bytes / sha256 {h_in.hexdigest()[:16]}..., the manifest says "
                           f"{tsize} / {tsha[:16]}...: run again to download it afresh")
    if n_out != out_bytes or h_out.hexdigest() != out_sha:
        seg.remove()
        os.remove(tmp)
        raise InstallError(f"{what}: after decoding, {n_out} bytes / sha256 {h_out.hexdigest()[:16]}..., the manifest "
                           f"says {out_bytes} / {out_sha[:16]}...")
    if prog is not None:
        prog.downloaded(rel, tsize)
        prog.unpacked(what, out_bytes)
    os.replace(tmp, dst)
    seg.remove()
    if prog is not None:
        prog.phase(what, None)
    return True


def stream_decoder(enc: str):
    """A decoder with decompress(bytes) -> bytes for this transport encoding, or None when only a whole-file one exists
    (zstd through the `zstd` command): that file then takes the old way, download first, then unpack."""
    if enc == "identity":
        return types.SimpleNamespace(decompress=lambda b: b)
    if enc == "xz":
        return lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
    try:
        from compression import zstd                       # Python 3.14+
        return zstd.ZstdDecompressor()
    except ImportError:
        pass
    try:
        import zstandard
        return zstandard.ZstdDecompressor().decompressobj()
    except ImportError:
        return None


def download(src: Source, rel: str, part: str, size: int, sha: str, what: str) -> None:
    """src/rel -> part (resumable); the stored bytes must be `size` long and hash to `sha`."""
    os.makedirs(os.path.dirname(part), exist_ok=True)
    tries = 8                                              # a rate limit (429) lifts in minutes: wait it out before giving up
    for attempt in range(tries):
        offset = os.path.getsize(part) if os.path.exists(part) else 0
        if offset > size:
            os.remove(part)
            offset = 0
        h = hashlib.sha256()
        if offset:
            with open(part, "rb") as f:
                for b in iter(lambda: f.read(CHUNK), b""):
                    h.update(b)
        try:
            if offset == size:
                body, total, ok = None, size, True
            else:
                body, total, ok = src.open(rel, offset)
                if not ok:                                 # the host ignored Range: start over
                    offset, h = 0, hashlib.sha256()
            if body is not None:
                with body, open(part, "r+b" if offset else "wb") as out:
                    out.seek(offset)
                    out.truncate()
                    while True:
                        if _STOP.is_set():
                            raise Fatal(f"{what}: stopped (another file failed)")
                        b = body.read(CHUNK)
                        if not b:
                            break
                        out.write(b)
                        h.update(b)
                        offset += len(b)
                        if PROGRESS is not None:
                            PROGRESS.downloaded(rel, offset, what)
                if total is not None and offset < total:       # urllib returns a short body quietly when the host hangs up
                    raise ConnectionError(f"connection closed after {offset} of {total} bytes")
            break
        except Fatal:
            raise
        except NetError as e:
            if attempt == tries - 1:
                raise NetError(f"{what}: download failed after {tries} tries ({e}); what was downloaded is kept, run again to resume")
            if e.retry_after is not None or "HTTP 429" in str(e):
                wait = min(max(e.retry_after or 0, 30 * (attempt + 1)), 300)
                print(f"  the host asked us to slow down; waiting {wait} s (try {attempt + 1} of {tries})", flush=True)
                RATE_SLEEP(wait)
                continue
        except InstallError:
            if attempt == tries - 1:
                raise
        except (OSError, urllib.error.URLError, ConnectionError, http.client.HTTPException) as e:
            if attempt == tries - 1:
                raise NetError(f"{what}: download failed after {tries} tries ({e}); what was downloaded is kept, run again to resume")
        time.sleep(min(2 ** attempt, 8) * (0 if src.kind == "dir" else 1))
    got, n = h.hexdigest(), os.path.getsize(part)
    if n != size or got != sha:
        os.remove(part)
        raise InstallError(f"{what}: downloaded {n} bytes with sha256 {got[:16]}..., the signed manifest says {size} bytes / {sha[:16]}...: "
                           "the file is damaged or was tampered with")


def install(a) -> dict:
    t0 = time.time()
    base = os.path.abspath(a.dest)
    os.makedirs(os.path.join(base, "releases"), exist_ok=True)
    pub = load_pubkey(a.pubkey)
    where, latest = resolve_source(a.source, pub)
    src = Source(where)
    # ---- 1. the signed manifest
    body = src.read_small("manifest.json")
    sig = src.read_small("manifest.json.sig")
    if not ed25519_verify(pub, body, sig):
        raise InstallError("manifest.json.sig does not verify with the pinned public key: nothing was downloaded or written")
    if latest and hashlib.sha256(body).hexdigest() != latest["manifest_sha256"]:
        raise Fatal(f"the manifest under tag {latest['revision']!r} is not the one latest.json names: nothing was downloaded or written")
    m = check_manifest(body)
    rid = m["release"]
    final = os.path.join(base, "releases", rid)
    stage = os.path.join(base, "releases", f".install-{rid}")
    dl = os.path.join(base, ".download", rid)
    lock = acquire_lock(base)
    try:
        cur = release_ptr.current_id(base)
        # ---- 2. already there?
        if os.path.isdir(final):
            errs = installed_ok(final, m, a.quick)
            same = os.path.isfile(os.path.join(final, "manifest.json")) and open(os.path.join(final, "manifest.json"), "rb").read() == body
            if not errs and same:
                if cur == rid:
                    return {"release": rid, "already_installed": True, "downloaded_files": 0, "seconds": round(time.time() - t0, 2)}
                if cur and not a.switch:
                    raise InstallError(f"release {rid} is installed, but `current` is {cur!r}: run again with --switch to go to {rid}")
                point(base, rid, cur)
                return {"release": rid, "already_installed": True, "switched_current_from": cur, "downloaded_files": 0}
            raise InstallError(f"{final} exists but is not this release ({'; '.join(errs[:3]) or 'a different manifest'}): move it away and run again")
        if cur and cur != rid and not a.switch:
            raise InstallError(f"`current` is {cur!r}; installing {rid} next to it needs --switch (monthly updates move `current` themselves)")
        # ---- 3. room
        transport = (m.get("transport") or {}).get("files", {})
        need = m["total_bytes"] if isinstance(m.get("total_bytes"), int) else sum(f["bytes"] for f in m["files"])
        # room for the downloads in hand: one per worker, so the largest WORKERS of them
        biggest = sum(sorted((transport.get(f["path"], f)["bytes"] for f in m["files"]), reverse=True)[:WORKERS])
        have_staged = sum(os.path.getsize(os.path.join(stage, *f["path"].split("/"))) for f in m["files"]
                          if os.path.isfile(os.path.join(stage, *f["path"].split("/"))))
        free = int(FAKE_FREE) if FAKE_FREE else shutil.disk_usage(base).free
        if free < need - have_staged + biggest:
            raise InstallError(f"not enough disk space in {base}: {free // (1 << 20)} MiB free, "
                               f"{(need - have_staged + biggest) // (1 << 20)} MiB needed (release + the largest downloads in hand at once). Nothing was written.")
        # ---- 4. the files
        os.makedirs(stage, exist_ok=True)
        for d, _dirs, fs in os.walk(stage):
            for f in fs:
                fp = os.path.join(d, f)
                # a run that stopped after its files were made read-only (step 5) left them so: Windows then refuses to
                # remove, replace or rewrite them (owner, 2026-11-14: "Access is denied" on the stage's manifest.json).
                # They are made read-only again once the release is complete.
                if not os.access(fp, os.W_OK):
                    os.chmod(fp, 0o644)
                if f.endswith(".tmp"):
                    os.remove(fp)
        done = skipped = fetched = 0
        try:
            todo = []
            for f in m["files"]:
                dst = os.path.join(stage, *f["path"].split("/"))
                if os.path.isfile(dst) and os.path.getsize(dst) == f["bytes"] and file_digest(dst)[0] == f["sha256"]:
                    skipped += 1
                    continue
                todo.append(f)

            def one(f) -> str:
                """One file, start to finish: download (resumable, hashed), unpack, hash the result, move into place."""
                p, dst = f["path"], os.path.join(stage, *f["path"].split("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                t = transport.get(p)
                enc = t["encoding"] if t else "identity"
                rel = "files/" + p + {"xz": ".xz", "zstd": ".zst", "identity": ""}[enc]
                part = os.path.join(dl, *rel.split("/")) + ".part"
                prog = PROGRESS
                tsize, tsha = (t or f)["bytes"], (t or f)["sha256"]
                tmp = dst + ".tmp"
                dec = stream_decoder(enc)
                if dec is not None and not os.path.exists(part) and \
                        stream_install(src, rel, part, tsize, tsha, enc, dec, tmp, dst, f["bytes"], f["sha256"], p, prog):
                    return p                                          # an old whole-file .part resumes the old way, below
                if prog is not None:
                    prog.phase(p, "downloading")
                download(src, rel, part, tsize, tsha, p)
                if prog is not None:
                    prog.downloaded(rel, (t or f)["bytes"])         # all of it, however it got here (resumed, or already whole)
                    prog.phase(p, "unpacking")
                if enc == "identity":
                    os.replace(part, tmp)
                elif enc == "xz":
                    xz_decode(part, tmp, (lambda n: prog.unpacked(p, n)) if prog is not None else None)
                    os.remove(part)
                else:
                    zstd_decoder()(part, tmp)
                    os.remove(part)
                if prog is not None:
                    prog.unpacked(p, f["bytes"])
                    prog.phase(p, "checking")
                sha, n = file_digest(tmp)
                if n != f["bytes"] or sha != f["sha256"]:
                    os.remove(tmp)
                    raise InstallError(f"{p}: after decoding, {n} bytes / sha256 {sha[:16]}..., the manifest says {f['bytes']} / {f['sha256'][:16]}...")
                os.replace(tmp, dst)
                if prog is not None:
                    prog.phase(p, None)
                return p

            _STOP.clear()
            global PROGRESS
            if order_bytes := sum(transport.get(f["path"], f)["bytes"] for f in todo):
                say(f"  to download: {order_bytes / 1e9:.1f} GB ({len(todo)} files; {skipped} already here), "
                    f"{sum(f['bytes'] for f in todo) / 1e9:.1f} GB once unpacked")
                PROGRESS = Progress(order_bytes, sum(f["bytes"] for f in todo)).start()
            # the largest first: the longest job starts at once and the small ones fill in beside it
            order = sorted(todo, key=lambda f: -(transport.get(f["path"], f)["bytes"]))
            if WORKERS > 1 and len(order) > 1:
                say(f"  {len(order)} files to fetch, {WORKERS} at a time")
            with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
                futs = [pool.submit(one, f) for f in order]
                try:
                    for fut in concurrent.futures.as_completed(futs):
                        p = fut.result()
                        fetched += 1
                        done += 1
                        say(f"  {done + skipped:>4}/{len(m['files'])}  {p}")
                        if FAIL_AFTER and done == int(FAIL_AFTER):
                            os.kill(os.getpid(), signal.SIGKILL)
                except BaseException:
                    _STOP.set()                            # the files in hand stop at their next chunk; none is half-trusted
                    for x in futs:
                        x.cancel()
                    raise
                finally:
                    if PROGRESS is not None:
                        PROGRESS.finish()
                        PROGRESS = None
            # ---- 5. checks on the stage folder
            listed = {f["path"] for f in m["files"]}
            for d, _dirs, fs in os.walk(stage):
                for f in fs:
                    r = os.path.relpath(os.path.join(d, f), stage).replace(os.sep, "/")
                    if r not in listed:
                        os.remove(os.path.join(d, f))
            open(os.path.join(stage, "manifest.json"), "wb").write(body)
            open(os.path.join(stage, "manifest.json.sig"), "wb").write(sig)
            say("  every file is here and matches the signed list; checking the release as the engine will read it (a minute or two)")
            errs = rc.check_release(stage)
            if errs:
                raise InstallError("the release fails the engine's own checks: " + "; ".join(errs[:5]))
        except NetError:                                   # nothing downloaded is wrong: keep it, the next run resumes
            raise
        except InstallError:
            shutil.rmtree(stage, ignore_errors=True)
            shutil.rmtree(dl, ignore_errors=True)
            try:
                os.rmdir(os.path.join(base, ".download"))        # only when no other release's download is in it
            except OSError:
                pass
            raise
        for d, _dirs, fs in os.walk(stage):
            for f in fs:
                os.chmod(os.path.join(d, f), 0o444)
        release_ptr.fsync_tree(stage)
        # ---- 6. the one rename, then the pointer
        os.rename(stage, final)
        release_ptr.fsync_path(os.path.join(base, "releases"))
        prep = prepare_release(base, rid, final)                 # before `current` moves; a failure here never fails the install
        warn = point(base, rid, cur)
        if not a.keep_download:
            shutil.rmtree(os.path.join(base, ".download"), ignore_errors=True)
        r = {"release": rid, "installed": True, "files": len(m["files"]), "downloaded_files": fetched, "skipped_files": skipped,
             "total_bytes": need, "seconds": round(time.time() - t0, 2)}
        r["prepare"] = {"ok": prep["ok"], "seconds": prep["seconds"], **({"error": prep["error"]} if prep["error"] else {})}
        if warn:
            r["warning"] = warn
        return r
    finally:
        lock()


PREPARE_TIMEOUT_S = 900


def prepare_command() -> list:
    """What runs the engine in its prepare mode (SEARCH_PREPARE_ONLY=1): the bundle's own `engine`, or search_api.py in a checkout.
    LOGOUT_PREPARE_CMD (a JSON list) replaces it: the tests' stand-in."""
    v = os.environ.get("LOGOUT_PREPARE_CMD")
    if v:
        return json.loads(v)
    if getattr(sys, "frozen", False):
        return [sys.executable, "engine"]
    return [sys.executable, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "serve", "api", "search_api.py"))]


def prepare_state_path(base: str) -> str:
    return os.path.join(base, "state", "prepare.json")


def prepare_release(base: str, rid: str, release_dir: str, runner=subprocess.run) -> dict:
    """Build what the engine would otherwise build at its first start of a release (the warm plan, the page-id map, the channel
    index: about 8 s), beside the result store, while nobody waits. Run after the release is unpacked and before the `current`
    pointer moves, and after every data update. NEVER raises and never fails the install: a failed prepare only means the first
    start builds the files (slower). The last outcome is BASE/state/prepare.json, which `doctor` shows."""
    t0 = time.time()
    rec = {"release": rid, "at": int(t0), "ok": False, "seconds": 0.0, "error": None}
    try:
        env = engine_env(base, release_dir=release_dir)
        os.makedirs(os.path.join(base, "store"), exist_ok=True)              # the engine keeps the files beside the store: its folder must exist
        env["SEARCH_PREPARE_ONLY"] = "1"
        r = runner(prepare_command(), env={**os.environ, **env}, capture_output=True, text=True, timeout=PREPARE_TIMEOUT_S)
        rec["ok"] = r.returncode == 0 and "prepared" in (r.stdout or "")
        if not rec["ok"]:
            tail = ((r.stderr or "").strip().splitlines() or (r.stdout or "").strip().splitlines() or ["(no output)"])[-1]
            rec["error"] = f"exit {r.returncode}: {tail}"[:300]
    except Exception as e:  # noqa: BLE001 - see above
        rec["error"] = f"{type(e).__name__}: {e}"[:300]
    rec["seconds"] = round(time.time() - t0, 1)
    try:
        p = prepare_state_path(base)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=1, sort_keys=True)
        os.replace(p + ".tmp", p)
    except OSError:
        pass
    if os.environ.get("LOGOUT_INSTALL_QUIET") != "1":
        print(f"  warm-up files for {rid}: " + (f"prepared in {rec['seconds']} s" if rec["ok"] else
              f"NOT prepared ({rec['error']}); the first start builds them, slower"), file=sys.stderr, flush=True)   # stdout is the JSON result
    return rec


def prepare_if_needed(base: str, runner=subprocess.run):
    """An install made before the installer prepared the warm-up files (or one whose last record is for another release): prepare the
    CURRENT release once (setup calls it). -> the record, or None when there is nothing to do."""
    rid = release_ptr.current_id(base)
    if not rid:
        return None
    try:
        with open(prepare_state_path(base), encoding="utf-8") as f:
            last = json.load(f)
        if last.get("ok") and last.get("release") == rid:
            return None
    except (OSError, ValueError):
        pass
    return prepare_release(base, rid, os.path.join(base, "releases", rid), runner=runner)


def point(base: str, rid: str, cur) -> str:
    try:
        if cur and cur != rid:
            release_ptr.set_pointer(base, "previous", cur)
        release_ptr.set_pointer(base, "current", rid)
    except OSError as e:
        return (f"the data is installed in {os.path.join(base, 'releases', rid)} but the `current` pointer could not be made ({e}); "
                "on Windows this needs Developer Mode or an elevated prompt. Start the engine on that folder directly.")
    return ""


def acquire_lock(base: str):
    path = os.path.join(base, ".install.lock")
    f = open(path, "w")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise InstallError(f"another install is running on {base}")
    return f.close


# ================================================================================================ add-ons
# Optional data files that make the live engine better and are NOT part of a release (their licence or size is their own).
# Each is a separate, separately signed manifest, `addons/<name>/manifest.json[.sig]` + `addons/<name>/files/<file>[.xz|.zst]`, on
# the same host as the release and signed by the same key. Installed to BASE/addons/<name>/, outside releases/ so a monthly
# update never touches them. The file name and the engine setting each add-on drives are fixed HERE, never read from the
# manifest: a signed manifest says what the bytes are, this code decides what the engine does with them.
ADDONS = {
    "wikilinks": {"file": "wikilinks.sqlite", "env": "SEARCH_WIKILINKS",
                  "what": "the card's Wikipedia article links and what Wikipedia cites (SEARCH_WIKILINKS, with W and C in SEARCH_BREADTH)"},
    "se-howto": {"file": "se-howto.sqlite", "env": "SEARCH_SE_INDEX",
                 "what": "Stack Exchange how-to index: a short answer excerpt with credit on how-to searches (SEARCH_SE_INDEX)"},
    # what public reads beside its release (2026-11-14, silo parity): the card's site sections and its Wikidata links
    "sitenav": {"file": "sitenav.jsonl", "env": "SEARCH_SITENAV",
                "what": "the card's site sections: tickets, shop, support... from each official site's own menu (SEARCH_SITENAV)"},
    "cardlinks": {"file": "cardlinks.sqlite", "env": "SEARCH_CARDLINKS",
                  "what": "the card's accounts and More links, from Wikidata (SEARCH_CARDLINKS)"},
}
ADDON_FORMAT = 1


def check_addon_manifest(body: bytes, name: str) -> dict:
    try:
        m = json.loads(body)
    except ValueError as e:
        raise InstallError(f"add-on manifest is not JSON: {e}")
    if not isinstance(m, dict) or m.get("kind") != "addon" or m.get("format") != ADDON_FORMAT:
        raise InstallError(f"add-on manifest is not kind=addon format={ADDON_FORMAT}: get a newer installer, or the file is not an add-on manifest")
    if m.get("name") != name:
        raise InstallError(f"the manifest at addons/{name}/ is for {m.get('name')!r}: refusing")
    if not isinstance(m.get("version"), str) or not ID_RE.match(m["version"]):
        raise InstallError(f"add-on version {m.get('version')!r} is not a plain name")
    fits = m.get("fits_releases")
    if not isinstance(fits, list) or not fits or any(not isinstance(x, str) or not x for x in fits):
        raise InstallError("add-on manifest names no release it fits (fits_releases)")
    lic = m.get("licence")
    if not isinstance(lic, dict) or not lic.get("name") or not isinstance(m.get("notice"), str) or not m["notice"].strip():
        raise InstallError("add-on manifest carries no licence name / NOTICE text: refusing (the attribution must travel with the file)")
    files = m.get("files")
    want = ADDONS[name]["file"]
    if not isinstance(files, list) or [f.get("path") for f in files if isinstance(f, dict)] != [want]:
        raise InstallError(f"add-on {name} must list exactly one file, {want}")
    f = files[0]
    if not isinstance(f.get("bytes"), int) or f["bytes"] <= 0 or not re.fullmatch(r"[0-9a-f]{64}", str(f.get("sha256", ""))):
        raise InstallError(f"{want}: no size / sha256 in the add-on manifest")
    for p, t in (m.get("transport", {}).get("files", {}) or {}).items():
        if p != want or t.get("encoding") not in ("xz", "zstd") or not isinstance(t.get("bytes"), int) \
                or not re.fullmatch(r"[0-9a-f]{64}", str(t.get("sha256", ""))):
            raise InstallError(f"transport entry for {p!r} is malformed or names a file the add-on does not list")
    return m


def addon_fits(m: dict, rid) -> bool:
    return bool(rid) and any(fnmatch.fnmatchcase(rid, pat) for pat in m["fits_releases"])


def addon_source(arg: str, name: str) -> "Source":
    """--source -> the Source of addons/<name>/ (same host and layout as the release; `hf:ORG/NAME[@TAG]` reads `main` unless a tag is given)."""
    m = HF_RE.match(arg)
    if m:
        base = hf_base(m.group(1), m.group(2) or "main")
    elif arg.startswith("hf:"):
        raise Fatal(f"--source {arg!r}: write hf:ORG/NAME or hf:ORG/NAME@TAG")
    else:
        base = arg.rstrip("/")
    return Source(f"{base}/addons/{name}")


def fetch_addon_manifest(source: str, pub: bytes, name: str):
    src = addon_source(source, name)
    body, sig = src.read_small("manifest.json"), src.read_small("manifest.json.sig")
    if not ed25519_verify(pub, body, sig):
        raise InstallError(f"addons/{name}/manifest.json.sig does not verify with the pinned public key: nothing was downloaded or written")
    return src, body, sig, check_addon_manifest(body, name)


def addon_notice(m: dict) -> str:
    lic = m["licence"]
    head = [f"{m.get('title') or m['name']} (add-on {m['name']}, version {m['version']})",
            f"Licence: {lic['name']}" + (f"  {lic['url']}" if lic.get("url") else "")]
    return "\n".join(head) + "\n\n" + m["notice"].strip() + "\n"


def write_addon_notices(base: str) -> None:
    """BASE/addons/NOTICE.txt: every installed add-on's notice in one file, beside the releases' own LICENSES/NOTICE.txt (that one is part of
    the signed release and read-only, so the add-on text cannot be appended to it)."""
    d = os.path.join(base, "addons")
    parts = []
    for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        p = os.path.join(d, n, "NOTICE.txt")
        if not n.startswith(".") and os.path.isfile(p):
            parts.append(open(p, encoding="utf-8").read().rstrip("\n"))
    out = os.path.join(d, "NOTICE.txt")
    if parts:
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            f.write("Optional data installed next to the release (logout search add-ons)\n\n" + "\n\n----\n\n".join(parts) + "\n")
    elif os.path.exists(out):
        os.remove(out)


def installed_addon(base: str, name: str):
    """(manifest, folder) of an installed add-on, or (None, folder)."""
    d = os.path.join(base, "addons", name)
    try:
        return json.load(open(os.path.join(d, "manifest.json"), encoding="utf-8")), d
    except (OSError, ValueError):
        return None, d


def addon_install(a) -> dict:
    t0 = time.time()
    name = a.name
    base = os.path.abspath(a.dest)
    pub = load_pubkey(a.pubkey)
    src, body, sig, m = fetch_addon_manifest(a.source, pub, name)
    cur = release_ptr.current_id(base)
    if not cur:
        raise InstallError(f"{base} has no installed release: run `install.py install` first (an add-on is tied to a release)")
    if not addon_fits(m, cur):
        raise InstallError(f"add-on {name} fits releases {m['fits_releases']}, `current` is {cur!r}: nothing was downloaded or written")
    os.makedirs(os.path.join(base, "addons"), exist_ok=True)
    final = os.path.join(base, "addons", name)
    stage = os.path.join(base, "addons", f".install-{name}")
    old = os.path.join(base, "addons", f".old-{name}")
    dl = os.path.join(base, ".download", f"addon-{name}")
    f = m["files"][0]
    t = (m.get("transport") or {}).get("files", {}).get(f["path"])
    enc = t["encoding"] if t else "identity"
    lock = acquire_lock(base)
    try:
        if os.path.isdir(final):
            have, _d = installed_addon(base, name)
            if have is not None and json.dumps(have, sort_keys=True) == json.dumps(m, sort_keys=True) and not installed_ok(final, m, a.quick):
                write_addon_notices(base)
                return {"addon": name, "version": m["version"], "already_installed": True, "downloaded_files": 0}
        dst = os.path.join(stage, f["path"])
        rel = f["path"] + {"xz": ".xz", "zstd": ".zst", "identity": ""}[enc]
        part = os.path.join(dl, "files", rel) + ".part"
        staged = os.path.getsize(dst) if os.path.isfile(dst) else 0
        resumed = os.path.getsize(part) if os.path.isfile(part) else 0
        free = int(FAKE_FREE) if FAKE_FREE else shutil.disk_usage(base).free
        need = max(f["bytes"] - staged, 0) + max((t or f)["bytes"] - resumed, 0)
        if free < need:
            raise InstallError(f"not enough disk space in {base}: {free // (1 << 20)} MiB free, {need // (1 << 20)} MiB needed for {name}. Nothing was written.")
        os.makedirs(stage, exist_ok=True)
        try:
            if not (os.path.isfile(dst) and os.path.getsize(dst) == f["bytes"] and file_digest(dst)[0] == f["sha256"]):
                download(src, "files/" + rel, part, (t or f)["bytes"], (t or f)["sha256"], f["path"])
                tmp = dst + ".tmp"
                if enc == "identity":
                    os.replace(part, tmp)
                else:
                    (xz_decode if enc == "xz" else zstd_decoder())(part, tmp)
                    os.remove(part)
                sha, n = file_digest(tmp)
                if n != f["bytes"] or sha != f["sha256"]:
                    os.remove(tmp)
                    raise InstallError(f"{f['path']}: after decoding, {n} bytes / sha256 {sha[:16]}..., the manifest says {f['bytes']} / {f['sha256'][:16]}...")
                os.replace(tmp, dst)
            with open(os.path.join(stage, "manifest.json"), "wb") as o:
                o.write(body)
            with open(os.path.join(stage, "manifest.json.sig"), "wb") as o:
                o.write(sig)
            with open(os.path.join(stage, "NOTICE.txt"), "w", encoding="utf-8", newline="\n") as o:
                o.write(addon_notice(m))
        except NetError:                                   # nothing downloaded is wrong: keep it, the next run resumes
            raise
        except InstallError:
            shutil.rmtree(stage, ignore_errors=True)
            shutil.rmtree(dl, ignore_errors=True)
            raise
        for fn in os.listdir(stage):
            os.chmod(os.path.join(stage, fn), 0o444)
        release_ptr.fsync_tree(stage)
        if os.path.isdir(old):
            _rmtree_ro(old)
        if os.path.isdir(final):
            os.rename(final, old)
        os.rename(stage, final)
        if os.path.isdir(old):
            _rmtree_ro(old)
        shutil.rmtree(dl, ignore_errors=True)
        write_addon_notices(base)
        return {"addon": name, "version": m["version"], "installed": True, "bytes": f["bytes"], "resumed_from": resumed,
                "licence": m["licence"]["name"], "notice": os.path.join(base, "addons", "NOTICE.txt"), "seconds": round(time.time() - t0, 2)}
    finally:
        lock()


def _rmtree_ro(path: str) -> None:
    """rmtree for a folder whose files are read-only (Windows refuses to delete those; on POSIX the folder mode is what counts, and ours is writable)."""
    def fix(func, p, _exc):
        os.chmod(p, 0o666)
        func(p)
    shutil.rmtree(path, onerror=fix)


def addon_remove(a) -> dict:
    base = os.path.abspath(a.dest)
    final = os.path.join(base, "addons", a.name)
    lock = acquire_lock(base)
    try:
        for stale in (f".install-{a.name}", f".old-{a.name}"):
            if os.path.isdir(os.path.join(base, "addons", stale)):
                _rmtree_ro(os.path.join(base, "addons", stale))
        if not os.path.isdir(final):
            return {"addon": a.name, "removed": False, "note": "not installed"}
        _rmtree_ro(final)
        write_addon_notices(base)
        return {"addon": a.name, "removed": True}
    finally:
        lock()


def addon_list(a) -> dict:
    base = os.path.abspath(a.dest)
    cur = release_ptr.current_id(base)
    out = {"current": cur, "installed": [], "available": []}
    for name in ADDONS:
        m, d = installed_addon(base, name)
        if m is None:
            continue
        try:
            errs = installed_ok(d, {"files": m["files"]}, True)
            fits = addon_fits(m, cur)
        except (KeyError, TypeError):
            errs, fits = ["manifest unreadable"], False
        out["installed"].append({"name": name, "version": m.get("version"), "bytes": sum(f.get("bytes", 0) for f in m.get("files", [])),
                                 "licence": (m.get("licence") or {}).get("name"), "fits_current": fits, "intact": not errs,
                                 **({"problems": errs} if errs else {})})
    if a.source:
        if not a.pubkey:
            raise InstallError("addon list --source needs --pubkey")
        pub = load_pubkey(a.pubkey)
        for name in ADDONS:
            try:
                _s, _b, _g, m = fetch_addon_manifest(a.source, pub, name)
                out["available"].append({"name": name, "version": m["version"], "bytes": m["files"][0]["bytes"], "licence": m["licence"]["name"],
                                         "fits_releases": m["fits_releases"], "fits_current": addon_fits(m, cur), "what": ADDONS[name]["what"]})
            except InstallError as e:
                out["available"].append({"name": name, "unavailable": str(e)[:200]})
    return out


def addon_env(base: str, rid: str) -> dict:
    """Engine settings for the add-ons that are installed, intact (size) and fit the current release. A broken or non-fitting one is
    skipped with a line on stderr; the engine then runs as it does without it."""
    env = {}
    for name, spec in ADDONS.items():
        m, d = installed_addon(base, name)
        if m is None:
            continue
        try:
            fits, errs = addon_fits(m, rid), installed_ok(d, {"files": m["files"]}, True)
        except (KeyError, TypeError):
            fits, errs = False, ["manifest unreadable"]
        if not fits or errs:
            print(f"add-on {name}: not used ({'does not fit release ' + str(rid) if not fits else '; '.join(errs)}); "
                  f"run `addon install {name}` again", file=sys.stderr)
            continue
        env[spec["env"]] = os.path.join(d, spec["file"])
    return env


# ================================================================================================ the lists
# Hand-picked lists, finished answers and shopping departments (owner 2026-11-14: "the searchsilos should have all of the same
# things as public ... they need the updates when we add more or update the ones they already have"). Built on indexhost, where
# public's are, and published to the same dataset under lists/: a signed manifest naming a BASE (everything, replaces what an
# engine had) and the DELTAS made after it (only what was added or changed). Each part is a small SQLite file in the engine's
# store inbox format (result_store.py); this code checks it against the manifest, gives its rows this install's release, and
# drops it in BASE/store/inbox, where the running engine takes it in within a minute (no restart). Signed by the RELEASE key
# (owner 2026-11-14: one key for everything an install trusts). The separate lists key of the first publish (8e590b3b...)
# was dropped once the first base signed by the release key was out (2026-11-14).
LISTS_PUBKEY = "ed25519:40502f6841e8644b8f193c10af27e5b148f74297bf6da7067115a60792d138bc"
LISTS_PUBKEYS = (LISTS_PUBKEY,)
LISTS_FORMAT = 1
LISTS_VERIFY_MAX = 16 << 20                           # the re-verification file (about 2 MB) is fetched even on a metered connection up to this size
LISTS_BIG = 64 << 20                                  # on a metered connection, nothing larger than this is fetched by a LATER update (a new base waits).
                                                      # The first base of an install is always fetched (owner 2026-11-15): the install already downloads ~11 GB


def lists_source(arg: str) -> "Source":
    m = HF_RE.match(arg)
    if m:
        base = hf_base(m.group(1), m.group(2) or "main")
    elif arg.startswith("hf:"):
        raise Fatal(f"--source {arg!r}: write hf:ORG/NAME or hf:ORG/NAME@TAG")
    else:
        base = arg.rstrip("/")
    return Source(f"{base}/lists")


def check_lists_manifest(body: bytes) -> dict:
    try:
        m = json.loads(body)
    except ValueError as e:
        raise InstallError(f"lists manifest is not JSON: {e}")
    if not isinstance(m, dict) or m.get("kind") != "lists" or m.get("format") != LISTS_FORMAT:
        raise InstallError(f"lists manifest is not kind=lists format={LISTS_FORMAT}: get a newer program")
    if not isinstance(m.get("version"), str) or not ID_RE.match(m["version"]):
        raise InstallError("lists manifest version is not a plain name")
    fits = m.get("fits_releases")
    if not isinstance(fits, list) or not fits or any(not isinstance(x, str) or not x for x in fits):
        raise InstallError("lists manifest names no release it fits (fits_releases)")
    parts = m.get("parts")
    if not isinstance(parts, list) or not parts:
        raise InstallError("lists manifest has no parts")
    names = set()
    for i, t in enumerate(parts):
        ok = (isinstance(t, dict) and isinstance(t.get("name"), str) and ID_RE.match(t["name"]) and t["name"] not in names
              and isinstance(t.get("bytes"), int) and t["bytes"] > 0 and re.fullmatch(r"[0-9a-f]{64}", str(t.get("sha256", "")))
              and isinstance(t.get("xz_bytes"), int) and t["xz_bytes"] > 0 and re.fullmatch(r"[0-9a-f]{64}", str(t.get("xz_sha256", "")))
              and (t.get("reset") is True) == (i == 0))     # the first part is the base, and only it resets
        if not ok:
            raise InstallError(f"lists manifest part {i} is malformed")
        names.add(t["name"])
    if m.get("base") != parts[0]["name"]:
        raise InstallError("lists manifest base is not its first part")
    v = m.get("verify")      # the re-verification file (audit DB-03): optional, replaced at each publish
    if v is not None and not (isinstance(v, dict) and isinstance(v.get("name"), str) and ID_RE.match(v["name"]) and v["name"] not in names
                              and isinstance(v.get("bytes"), int) and v["bytes"] > 0 and isinstance(v.get("xz_bytes"), int) and v["xz_bytes"] > 0
                              and re.fullmatch(r"[0-9a-f]{64}", str(v.get("sha256", ""))) and re.fullmatch(r"[0-9a-f]{64}", str(v.get("xz_sha256", "")))):
        raise InstallError("lists manifest verify entry is malformed")
    lic = m.get("licence")
    if not isinstance(lic, dict) or not lic.get("name") or not isinstance(m.get("notice"), str) or not m["notice"].strip():
        raise InstallError("lists manifest carries no licence name / notice text")
    return m


def lists_state_path(base: str) -> str:
    return os.path.join(base, "store", "lists.json")


def lists_state(base: str) -> dict:
    try:
        s = json.load(open(lists_state_path(base), encoding="utf-8"))
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def _lists_save(base: str, s: dict) -> None:
    p = lists_state_path(base)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(s, f, indent=1, sort_keys=True)
    os.replace(p + ".tmp", p)


def lists_rekey(path: str, release: str) -> dict:
    """The part's rows are for the release they were chosen on (public's); here they serve this install's release, which the
    manifest says they fit. -> counts per table."""
    import sqlite3
    import zlib
    db = sqlite3.connect(path)
    try:
        have = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        meta = dict(db.execute("SELECT k, v FROM lists_meta").fetchall()) if "lists_meta" in have else {}
        db.create_function("zc", 1, lambda b: zlib.compress(b if isinstance(b, bytes) else str(b).encode(), 6), deterministic=True)
        n = {}
        for t in ("curated", "finished"):
            if t in have:
                # bodies travel as plain text (smaller once xz'd); the store keeps them zlib-compressed
                db.execute(f"UPDATE {t} SET release = ?" + (", body = zc(body)" if meta.get("body") == "plain" else ""), (release,))
                n[t] = db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        if "dept" in have:
            n["dept"] = db.execute("SELECT count(*) FROM dept").fetchone()[0]
        db.commit()
        return n
    finally:
        db.close()


AFFECTED_MAGIC = b"LOAFF1\n"                           # release/release_delta.py `affected --hashes-out`: 8-byte blake2b hashes of the names a data update may change


def name_hash(name: str) -> int:
    return int.from_bytes(hashlib.blake2b(name.encode("utf-8", "replace"), digest_size=8).digest(), "big")


def read_affected(path: str):
    """The set of name hashes in a package's affected.bin, or None when there is none (or it is not one: nothing is assumed)."""
    try:
        raw = open(path, "rb").read()
    except OSError:
        return None
    if not raw.startswith(AFFECTED_MAGIC) or (len(raw) - len(AFFECTED_MAGIC)) % 8:
        return None
    body = raw[len(AFFECTED_MAGIC):]
    return {int.from_bytes(body[i:i + 8], "big") for i in range(0, len(body), 8)}


def lists_follow(base: str, old: str, new: str, affected) -> dict:
    """After a data update old -> new: the hand-picked lists and finished answers this install holds for `old` stay valid for
    `new` except for the names the update may have changed (the publisher's release_delta, as hashes). They are re-keyed to `new`
    (time kept); the affected ones stay behind and are dropped, and the next lists update brings them for `new`. The lists
    state is moved to `new`, so the next update takes only the new parts, not the whole base again. Never raises: an update
    that cannot follow leaves the lists to be taken afresh, as before."""
    import sqlite3
    path = os.path.join(base, "store", "results.sqlite")
    if affected is None or not os.path.isfile(path) or old == new:
        return {"followed": False}
    try:
        db = sqlite3.connect(path, timeout=60)
        db.create_function("gone", 1, lambda k: 1 if name_hash(str(k).split("\x1f")[0]) in affected else 0, deterministic=True)
        n = {"followed": True}
        for t in ("curated", "finished"):
            if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (t,)).fetchone() is None:
                continue
            kept = db.execute(f"UPDATE OR IGNORE {t} SET release = ? WHERE release = ? AND NOT gone(key)", (new, old)).rowcount
            dropped = db.execute(f"DELETE FROM {t} WHERE release = ?", (old,)).rowcount
            n[t] = {"kept": kept, "dropped": dropped}
        db.commit()
        db.close()
        st = lists_state(base)
        if st.get("release") == old:
            st["release"] = new
            _lists_save(base, st)
        return n
    except Exception as e:  # noqa: BLE001 - following is an optimisation
        return {"followed": False, "why": f"{type(e).__name__}: {e}"[:200]}


def lists_update(base: str, source: str, pub, metered: bool = False, big: int = LISTS_BIG, channel: str = "stable",
                 skip=()) -> dict:
    """Fetch the lists parts this install has not taken yet and drop them in its store inbox. -> summary.
    pub: one pinned key (bytes) or several (any one of them must verify the manifest).
    channel: beta reads manifest-beta.json (or manifest.json while there is none). skip: versions that failed their
    self-test here (autoupdate.py): never fetched again; a newer version replaces them from its base."""
    t0 = time.time()
    base = os.path.abspath(base)
    src = lists_source(source)
    body = sig = None
    if channel == "beta":
        try:
            body, sig = src.read_small("manifest-beta.json"), src.read_small("manifest-beta.json.sig")
        except Fatal as e:
            if not is_missing(e):
                raise
            body = sig = None
    if body is None:
        body, sig = src.read_small("manifest.json"), src.read_small("manifest.json.sig")
    pubs = [pub] if isinstance(pub, (bytes, bytearray)) else list(pub)
    if not any(ed25519_verify(k, body, sig) for k in pubs):
        raise InstallError("lists/manifest.json.sig does not verify with the pinned key: nothing was downloaded or written")
    m = check_lists_manifest(body)
    cur = release_ptr.current_id(base)
    if not cur:
        raise InstallError(f"{base} has no installed release")
    if not addon_fits(m, cur):
        return {"lists": "skipped", "why": f"the lists fit {m['fits_releases']}, this install is {cur}", "version": m["version"]}
    if m["version"] in set(skip or ()):
        return {"lists": "skipped", "why": f"lists {m['version']} failed the self-test here: it waits for a newer version", "version": m["version"]}
    st = lists_state(base)
    quarantined = st.get("quarantine")                       # a failed version was switched off: the next one starts from its base
    fresh = st.get("base") != m["base"] or st.get("release") != cur or bool(quarantined)
    applied = set() if fresh else set(st.get("applied") or [])
    todo = [t for t in m["parts"] if t["name"] not in applied]
    ver = m.get("verify")
    vtodo = ver if ver and (fresh or st.get("verify") != ver["xz_sha256"]) else None
    if not todo and not vtodo:
        return {"lists": "up to date", "version": m["version"], "base": m["base"], "parts": len(m["parts"])}
    size = sum(t["xz_bytes"] for t in todo)
    vsize = vtodo["xz_bytes"] if vtodo else 0
    first = not st.get("base") or bool(st.get("first_pending"))     # no base taken yet, or the first one was cut short: metered does not hold it back
    if metered and size > big and not first or (metered and not todo and vsize > LISTS_VERIFY_MAX):
        return {"lists": "waiting", "why": f"{size >> 20} MiB to fetch on a metered connection; it waits for an ordinary one",
                "version": m["version"]}
    store = os.path.join(base, "store")
    inbox, work = os.path.join(store, "inbox"), os.path.join(store, ".lists")
    if quarantined:
        inbox = os.path.join(store, "held")                  # the running engine has the answers off and would take a part out of the
                                                             # inbox and delete it unread: the parts wait here until it starts again
    os.makedirs(inbox, exist_ok=True)
    os.makedirs(work, exist_ok=True)
    free = int(FAKE_FREE) if FAKE_FREE else shutil.disk_usage(base).free
    need = sum(t["xz_bytes"] + 2 * t["bytes"] for t in todo) + (vtodo["xz_bytes"] + 2 * vtodo["bytes"] if vtodo else 0)
    if free < need:
        raise InstallError(f"not enough disk space in {base}: {free >> 20} MiB free, {need >> 20} MiB needed for the lists")
    if fresh:
        st = {"base": m["base"], "release": cur, "applied": [], "n": int(st.get("n") or 0)}     # (and no verify taken yet)
        if first:
            st["first_pending"] = True                       # an interrupted first fetch resumes even on a metered connection
    rows = {}
    for t in todo:
        xz = os.path.join(work, t["name"] + ".sqlite.xz")
        download(src, f"parts/{t['name']}.sqlite.xz", xz, t["xz_bytes"], t["xz_sha256"], f"lists {t['name']}")
        tmp = os.path.join(work, t["name"] + ".sqlite")
        xz_decode(xz, tmp)
        os.remove(xz)
        sha, n = file_digest(tmp)
        if (n, sha) != (t["bytes"], t["sha256"]):
            os.remove(tmp)
            raise InstallError(f"lists {t['name']}: after decoding {n} bytes / {sha[:16]}..., the manifest says {t['bytes']} / {t['sha256'][:16]}...")
        for k, v in lists_rekey(tmp, cur).items():
            rows[k] = rows.get(k, 0) + v
        st["n"] = int(st.get("n") or 0) + 1
        os.replace(tmp, os.path.join(inbox, f"lists-{st['n']:08d}-{t['name']}.sqlite"))   # the engine reads the inbox in name order
        st["applied"] = sorted(set(st["applied"]) | {t["name"]})
        st["version"] = m["version"]
        _lists_save(base, st)
    if vtodo:
        # the re-verification of answers that came out unchanged, after every part (the inbox is read in name order): the
        # rows it names are then the ones this install holds. Skipped when it is not the same publish as the parts taken.
        xz = os.path.join(work, vtodo["name"] + ".sqlite.xz")
        download(src, f"parts/{vtodo['name']}.sqlite.xz", xz, vtodo["xz_bytes"], vtodo["xz_sha256"], f"lists {vtodo['name']}")
        tmp = os.path.join(work, vtodo["name"] + ".sqlite")
        xz_decode(xz, tmp)
        os.remove(xz)
        sha, n = file_digest(tmp)
        if (n, sha) != (vtodo["bytes"], vtodo["sha256"]):
            os.remove(tmp)
            raise InstallError(f"lists {vtodo['name']}: after decoding {n} bytes / {sha[:16]}..., the manifest says {vtodo['bytes']} / {vtodo['sha256'][:16]}...")
        st["n"] = int(st.get("n") or 0) + 1
        os.replace(tmp, os.path.join(inbox, f"lists-{st['n']:08d}-{vtodo['name']}.sqlite"))
        st["verify"] = vtodo["xz_sha256"]
        st["version"] = m["version"]
        _lists_save(base, st)
    if st.pop("first_pending", None):
        _lists_save(base, st)
    return {"lists": "fetched", "verify": vtodo["name"] if vtodo else None, "version": m["version"], "base": m["base"], "parts_fetched": [t["name"] for t in todo],
            "bytes": size, "rows": rows, "seconds": round(time.time() - t0, 1), **({"was_quarantined": quarantined} if quarantined else {})}


def lists_command(a) -> dict:
    base = os.path.abspath(a.dest)
    if a.action == "status":
        st = lists_state(base)
        inbox = os.path.join(base, "store", "inbox")
        waiting = sorted(f for f in os.listdir(inbox) if f.endswith(".sqlite")) if os.path.isdir(inbox) else []
        return {"version": st.get("version"), "base": st.get("base"), "release": st.get("release"),
                "parts": len(st.get("applied") or []), "waiting_in_inbox": waiting}
    if not a.source:
        raise InstallError("lists update needs --source")
    metered = False
    if a.metered == "yes":
        metered = True
    elif a.metered == "auto":
        try:
            import netcost
            metered = bool(netcost.metered())
        except Exception:  # noqa: BLE001
            metered = False
    return lists_update(base, a.source, [load_pubkey(a.pubkey)] if a.pubkey else [load_pubkey(k) for k in LISTS_PUBKEYS], metered,
                        channel=a.channel, skip=[v for v in a.skip.split(",") if v])


# ================================================================================================ smoke test
# Three fixed searches through the path the page uses. "wikipedia" is the engine's own /readyz probe, so it must work.
SMOKE_NAME, SMOKE_PLAIN, SMOKE_HOWTO = "wikipedia", "history of the bicycle", "how to remove a stripped screw"


def _get_json(port: int, q: str, timeout: float):
    url = f"http://127.0.0.1:{port}/instant?" + urllib.parse.urlencode({"full": "1", "q": q})
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def smoke_failures(port: int, has_se: bool, timeout: float = 8.0, get=_get_json) -> list:
    """What is wrong with the engine's answers to three fixed searches ([] = fine)."""
    bad = []
    checks = [("name search", SMOKE_NAME, lambda d: bool(d.get("card")) or bool(d.get("results")), "no card and no results"),
              ("plain search", SMOKE_PLAIN, lambda d: bool(d.get("results")), "no results")]
    if has_se:
        checks.append(("how-to search (Stack Exchange add-on)", SMOKE_HOWTO,
                       lambda d: any(isinstance(r, dict) and r.get("excerpt") is True for r in d.get("results") or []),
                       'no row with "excerpt": true'))
    for label, q, ok, why in checks:
        try:
            d = get(port, q, timeout)
        except (OSError, ValueError, http.client.HTTPException) as e:      # URLError and HTTPError are OSErrors
            bad.append(f"{label} {q!r}: {type(e).__name__}: {e}")
            continue
        if not isinstance(d, dict) or not ok(d):
            bad.append(f"{label} {q!r}: {why}")
    return bad


def run_smoke(port: int, has_se: bool, get=_get_json) -> int:
    """0 when the engine answers; else 1 after saying what failed and what /readyz says."""
    t0 = time.monotonic()
    bad = smoke_failures(port, has_se, get=get)
    if not bad:
        say(f"smoke test: {'3' if has_se else '2'} searches answered ({time.monotonic() - t0:.1f} s)")
        return 0
    say("smoke test FAILED:")
    for b in bad:
        say("  " + b)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=5) as r:
            say(f"/readyz: {r.status} {r.read(300).decode(errors='replace')}")
    except urllib.error.HTTPError as e:
        say(f"/readyz: {e.code} {e.read(300).decode(errors='replace')}")
    except (OSError, http.client.HTTPException) as e:
        say(f"/readyz: no answer ({e})")
    return 1


# ================================================================================================ start / status
PUBLIC_TUNING = {"SEARCH_LINK_SITE_LOW_GUARD": "1", "SEARCH_LINK_SITE_MIN_SITELINKS": "1", "SEARCH_PLACE_UNDER_THING": "-40",
                 "SEARCH_PRODUCT_SITE_FIRST": "2", "SEARCH_PRODUCT_TIER_WIDE": "1", "SEARCH_TYPO_NAMES": "links,places",
                 "SEARCH_TYPO_WORD_DF": "3000", "SEARCH_ANSWER_DEAD": "1",
                 # the list cleanup public runs (2026-11-14, owner: "every single feature public has the search silos get"):
                 # every row of the first 10 scored, a row under 0 left out from the 4th on
                 "SEARCH_BREADTH": "JDLF", "SEARCH_SCORE_ALL": "1", "SEARCH_BREADTH_FLOOR": "0", "SEARCH_SCORE_ALL_TOP": "10",
                 # hand-picked lists and finished answers, read from the store the lists arrive in (`lists update`)
                 "SEARCH_CURATED": "1", "SEARCH_CURATED_FILL": "1", "SEARCH_FINISHED": "1"}


# Public's settings an install does NOT copy, each with why (tests/unit/test-search-lists.sh: every other SEARCH_* public's engine
# runs with must be set by engine_env, so a setting added to public and forgotten here fails the test).
PUBLIC_ONLY = {
    "SEARCH_HOME": "public has no home town; an install's is the owner's own (settings)",
    "SEARCH_NEWS_DB": "the Postgres news database's name: an install reads the news file (SEARCH_NEWS_SQLITE)",
    "SEARCH_MINE_BOX": "public has no feeds of its own to show; an install shows its owner's",
    "SEARCH_SHOP_NAMES": "HELD: a list of Amazon's search suggestions; publishing it waits for a licence decision",
    "SEARCH_SHOP_BOX": "goes with SEARCH_SHOP_NAMES",
    "SEARCH_CE_GOV_DIR": "public names the same model as SEARCH_CE_DIR (st3); unset, government tasks use SEARCH_CE_DIR: the same",
    "SEARCH_SQLITE_CACHE_KB": "memory per machine, not results",
    "SEARCH_ORT_PREPACK": "speed/memory per machine, not results",
    "SEARCH_CE_THREADS": "CPU per machine, not results",
    "SEARCH_CACHE_MAX": "memory per machine, not results",
}


def _move_lists_parts(base: str, frm: str, to: str) -> None:
    """lists-*.sqlite from store/<frm> to store/<to> (names kept: the engine reads the inbox in name order). Never raises."""
    try:
        src, dst = os.path.join(base, "store", frm), os.path.join(base, "store", to)
        names = sorted(n for n in os.listdir(src) if n.startswith("lists-") and n.endswith(".sqlite"))
        if names:
            os.makedirs(dst, exist_ok=True)
        for n in names:
            os.replace(os.path.join(src, n), os.path.join(dst, n))
    except OSError:
        pass


def engine_env(base: str, port: int = 8090, release_dir: str | None = None) -> dict:
    """The environment search_api.py needs to answer from BASE/current (names read from search_api.py, not guessed).
    release_dir: a release folder of its own instead of the `current` pointer (prepare_release runs before the pointer moves)."""
    cur = release_dir or os.path.join(base, "current")
    if not os.path.isfile(os.path.join(os.path.realpath(cur), "manifest.json")):
        raise InstallError(f"{cur} is not an installed release: run `install.py install` first")
    real = os.path.realpath(cur)
    m = json.load(open(os.path.join(real, "manifest.json")))
    env = {
        "SEARCH_RELEASE_DIR": cur,                                  # release_root.resolve: BASE/current -> the folder it names at start
        "SEARCH_LOOKUP_BACKEND": "release",                         # lookup.sqlite, places-*, hours-web, registry, signals, text/typo
        "SEARCH_POSTGRES": "off",                                   # a home box has no database (ADR-063); a release with no web index is otherwise refused at start
        "SEARCH_WIKI_BACKEND": "tantivy-release",                   # <release>/text/wiki
        "SEARCH_SEM_BACKEND": "ivf",                                # meaning search over the IVF folder
        "SEARCH_SEM_IVF": os.path.join(cur, "vectors", "ivf"),      # search_api's default is a server path: must be set
        "SEARCH_NOMIC_DIR": os.path.join(cur, *m.get("engine", {}).get("vectors", {}).get("query_model", "models/nomic-embed-text-v1.5").split("/")),
        "SEARCH_RELEASE_ID": m["release"],                          # sem_ivf refuses a folder built for another release
        "SEARCH_API_PORT": str(port),
        "SEARCH_API_BIND": "127.0.0.1",                             # search_api.BIND: the browser is on this machine; nothing on the LAN may query it
        "LOGOUT_TV_OVERLAY_DIR": os.path.join(base, "state", "tv-overlay"),   # tv_open: lock files beside the data, same drive (Windows hard links)
    }
    rr = (m.get("engine", {}).get("reranker") or {}).get("cpu")
    if rr:
        rdir = os.path.join(real, *rr.split("/"))
        for need in ("tokenizer.json", ("model_int8.onnx", "model.onnx")):
            names = (need,) if isinstance(need, str) else need
            if not any(os.path.isfile(os.path.join(rdir, n)) for n in names):
                raise InstallError(f"{rdir}: {names[0]} is missing: the release says it ships the reranker (reinstall it)")
        env["SEARCH_CE_DIR"] = os.path.join(cur, *rr.split("/"))    # search_api.CE_DIR: the CPU cross-encoder (turning it off costs ~15 pt hit@1)
        env["SEARCH_CE_DEVICE"] = "cpu"                             # the iGPU default dir is a server path (/models/...): not on a home box
        env["SEARCH_REQUIRE_RERANKER"] = "1"                        # the engine refuses to start without it instead of quietly ranking worse
    # rc5 W7: the kinds model and the ranker weights come from the release, not from whatever the engine image holds
    km = m.get("engine", {}).get("kinds_model")
    if km:
        kdir = os.path.join(real, *km.split("/"))
        for n in ("model.onnx", "tokenizer.json"):
            if not os.path.isfile(os.path.join(kdir, n)):
                raise InstallError(f"{kdir}: {n} is missing: the release says it ships the kinds model (reinstall it)")
        env["SEARCH_MODEL_DIR"] = os.path.join(cur, *km.split("/"))  # search_api.MODEL_DIR
        env["SEARCH_REQUIRE_KINDS_MODEL"] = "1"                      # refuse to start rather than drop the kinds meaning match silently
    rk = (m.get("engine", {}).get("ranker") or {}).get("file")
    if rk:
        if not os.path.isfile(os.path.join(real, rk)):
            raise InstallError(f"{os.path.join(real, rk)} is missing: the release says it ships the ranker weights (reinstall it)")
        env["SEARCH_RANKER"] = os.path.join(cur, rk)                 # search_api.RANKER_PATH
    if os.path.isdir(os.path.join(real, "text", "sites", "sites")):
        env["SEARCH_WEB_BACKEND"] = "tantivy-release"               # <release>/text/sites/{sites,pages,anchors}
    if os.path.isfile(os.path.join(real, "sitenav.jsonl")):
        env["SEARCH_SITENAV"] = os.path.join(cur, "sitenav.jsonl")
    if os.path.isfile(os.path.join(real, "cardlinks.sqlite")):
        env["SEARCH_CARDLINKS"] = os.path.join(cur, "cardlinks.sqlite")
    if os.path.isdir(os.path.join(real, "models", "rerank-gov")):
        env["SEARCH_CE_GOV_DIR"] = os.path.join(cur, "models", "rerank-gov")
    env.update(addon_env(base, m["release"]))                        # optional signed add-ons (BASE/addons): wikilinks, se-howto
    # news, scores and weather (installer/logout_search.py's collectors write it; `start` makes it before the engine starts)
    env["SEARCH_NEWS_BACKEND"] = "sqlite"
    env["SEARCH_NEWS_SQLITE"] = os.path.join(base, "state", "news.sqlite")
    radar_dir = os.path.join(base, "state", "radar")
    os.makedirs(radar_dir, exist_ok=True)
    env["SEARCH_RADAR_DIR"] = os.environ.get("SEARCH_RADAR_DIR", radar_dir)
    # the result settings search.logout.cloud runs with (infra/search-public/docker-compose.yml), so an engine at home ranks
    # as the public site does (owner 2026-11-14). A value already in the environment wins.
    for k, v in PUBLIC_TUNING.items():
        env[k] = os.environ.get(k, v)
    if lists_state(base).get("quarantine"):                          # answers that failed their self-test: this engine answers live (autoupdate.py)
        env["SEARCH_CURATED"] = env["SEARCH_FINISHED"] = "0"
        _move_lists_parts(base, "inbox", "held")                     # an engine with them off deletes an inbox part unread: keep them out of its way
    else:
        _move_lists_parts(base, "held", "inbox")                     # parts fetched while it was off: this engine has the answers on
    if "SEARCH_SE_INDEX" in env:
        env["SEARCH_SE_POOL"] = os.environ.get("SEARCH_SE_POOL", "8")
    # the store: hand-picked lists, finished answers and departments arrive in its inbox (`lists update`); this engine's own
    # computed rows are kept there too (only the warmer writes them, and a home engine runs none)
    os.makedirs(os.path.join(base, "store", "inbox"), exist_ok=True)
    env["SEARCH_STORE"] = os.environ.get("SEARCH_STORE", os.path.join(base, "store", "results.sqlite"))
    # glibc: 2 malloc arenas instead of 8 per core for the engine's ~40 threads. rc8 under 4 GB / 4 CPUs: anonymous memory
    # after 1,000 searches 2.23 -> 1.94 GB, latency unchanged (audits/2026-10-12-box-memory-levers.md). Ignored
    # off glibc; a value already in the environment wins.
    env["MALLOC_ARENA_MAX"] = os.environ.get("MALLOC_ARENA_MAX", "2")
    return env


def format_env(env: dict, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(env, indent=1, sort_keys=True)
    if fmt == "cmd":
        return "\n".join(f"set {k}={v}" for k, v in sorted(env.items()))
    if fmt == "powershell":
        return "\n".join(f"$env:{k} = '{v}'" for k, v in sorted(env.items()))
    return "\n".join(f"export {k}='{v}'" for k, v in sorted(env.items()))


def _readyz(port: int) -> int:
    """The engine's /readyz status: 200 when searches can answer, 503 while it is starting, 0 when nothing listens."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (OSError, http.client.HTTPException):
        return 0


def wait_ready(port: int, alive, timeout: float = 300.0, get=_readyz, sleep=time.sleep, clock=time.monotonic) -> str:
    """Poll /readyz until the engine can answer searches (the port answers /healthz long before a search
    returns anything). -> "ready", "exited" (the engine process ended first) or "timeout". `alive()` says whether
    the engine process still runs."""
    t0, told = clock(), 0.0
    while True:
        if not alive():
            return "exited"
        if get(port) == 200:
            say(f"ready: http://127.0.0.1:{port}  ({clock() - t0:.0f} s)")
            say("the first few searches take 2-5 s while the indexes come into memory; then they are fast")
            return "ready"
        waited = clock() - t0
        if waited >= timeout:
            say(f"still not ready after {waited:.0f} s: the engine runs on, but searches may answer nothing yet (its log is above)")
            return "timeout"
        if waited - told >= 15:
            told = waited
            say(f"starting... {waited:.0f} s (loading the indexes and models)")
        sleep(1.0)


def start(a) -> None:
    base = os.path.abspath(a.dest)
    env = engine_env(base, a.port)
    if a.print_env:
        print(format_env(env, a.format or ("cmd" if os.name == "nt" else "posix")))
        return
    if "SEARCH_CE_DIR" not in env:
        say("warning: this release ships no reranker: results will rank noticeably worse (~15 pt hit@1)")
    engine = a.engine
    if not engine:
        for c in (os.path.join(HERE, "search_api.py"), os.path.join(HERE, "..", "serve", "api", "search_api.py")):
            if os.path.isfile(c):
                engine = c
                break
    if not engine:
        raise InstallError("search_api.py not found next to the installer: pass --engine PATH")
    say(f"starting the engine on http://127.0.0.1:{a.port} from {os.path.realpath(os.path.join(base, 'current'))}")
    proc = subprocess.Popen([sys.executable, engine], env={**os.environ, **env})
    try:
        state = wait_ready(a.port, lambda: proc.poll() is None)      # says "ready" only when a search can answer
        if state == "ready" and not a.no_smoke and run_smoke(a.port, "SEARCH_SE_INDEX" in env):
            proc.terminate()
            proc.wait()
            sys.exit(1)
        sys.exit(proc.wait())
    except KeyboardInterrupt:
        proc.terminate()
        sys.exit(proc.wait())


def after_install_help(base: str) -> str:
    return "\n".join([
        "", f"Installed. Start the engine with:", f"    {os.path.basename(sys.executable)} {os.path.abspath(__file__)} start --dest {base}",
        "or set the environment yourself (the engine reads it at start):",
        "    " + f"{os.path.basename(sys.executable)} {os.path.abspath(__file__)} start --dest {base} --print-env"])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    i = sp.add_parser("install")
    i.add_argument("--source", required=True)
    i.add_argument("--pubkey", required=True)
    i.add_argument("--dest", required=True)
    i.add_argument("--quick", action="store_true", help="on a re-run, compare sizes only instead of hashing every installed file")
    i.add_argument("--switch", action="store_true", help="allow `current` to move to this release when another one is current")
    i.add_argument("--keep-download", action="store_true")
    s = sp.add_parser("start")
    s.add_argument("--dest", required=True)
    s.add_argument("--port", type=int, default=8090)
    s.add_argument("--engine")
    s.add_argument("--print-env", action="store_true")
    s.add_argument("--format", choices=["posix", "cmd", "powershell", "json"])
    s.add_argument("--no-smoke", action="store_true", help="skip the three fixed searches run once the engine reports ready")
    ad = sp.add_parser("addon", help="optional signed data next to the release: install NAME | remove NAME | list")
    ad.add_argument("action", choices=["install", "remove", "list"])
    ad.add_argument("name", nargs="?", choices=sorted(ADDONS))
    ad.add_argument("--dest", required=True)
    ad.add_argument("--source", help="install/list: the same --source as the release (hf:ORG/NAME or a URL/folder); add-ons are under addons/<name>/")
    ad.add_argument("--pubkey", help="install/list: the PINNED public key, as for `install`")
    ad.add_argument("--quick", action="store_true", help="install: on a re-run compare sizes only")
    ls = sp.add_parser("lists", help="hand-picked lists and finished answers, as public has them: update | status")
    ls.add_argument("action", choices=["update", "status"])
    ls.add_argument("--dest", required=True)
    ls.add_argument("--source", help="update: the same --source as the release (hf:ORG/NAME or a URL/folder); the lists are under lists/")
    ls.add_argument("--pubkey", help="update: the key that signed the lists (default: the release key)")
    ls.add_argument("--channel", choices=CHANNELS, default="stable", help="update: beta follows manifest-beta.json")
    ls.add_argument("--skip", default="", help="update: comma-separated lists versions that failed their self-test here (never fetched again)")
    ls.add_argument("--metered", choices=["auto", "yes", "no"], default="no",
                    help="update: on a metered connection a later update larger than 64 MiB waits; the first base of an install is always fetched (auto: ask the system)")
    t = sp.add_parser("status")
    t.add_argument("--dest", required=True)
    a = ap.parse_args()
    try:
        if a.cmd == "install":
            r = install(a)
            if os.environ.get("LOGOUT_INSTALL_QUIET") != "1":     # the program's own setup says what comes next itself
                print(json.dumps(r, sort_keys=True))
                if r.get("installed"):
                    say(after_install_help(os.path.abspath(a.dest)))
        elif a.cmd == "start":
            start(a)
        elif a.cmd == "lists":
            print(json.dumps(lists_command(a), sort_keys=True))
        elif a.cmd == "addon":
            if a.action != "list" and not a.name:
                raise InstallError(f"addon {a.action} needs a name: one of {', '.join(sorted(ADDONS))}")
            if a.action == "install" and not (a.source and a.pubkey):
                raise InstallError("addon install needs --source and --pubkey")
            r = {"install": addon_install, "remove": addon_remove, "list": addon_list}[a.action](a)
            print(json.dumps(r, sort_keys=True))
            if r.get("installed") is True:
                say(f"Attribution text for this add-on: {r['notice']}\nRestart the engine to use it: it is picked up at `start`.")
        else:
            b = os.path.abspath(a.dest)
            rd = os.path.join(b, "releases")
            print(json.dumps({"current": release_ptr.current_id(b), "previous": release_ptr.current_id(b, "previous"),
                              "releases": sorted(os.listdir(rd)) if os.path.isdir(rd) else []}, sort_keys=True))
    except InstallError as e:
        print(f"install: REFUSED: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
