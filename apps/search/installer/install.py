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
import time
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


def say(msg: str) -> None:
    print(msg, flush=True)


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
    """manifest/files from an http(s) URL, a file:// URL or a folder. `opened` counts what was asked, for the tests."""

    def __init__(self, src: str):
        self.src = src
        self.requests = []
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

    def open(self, rel: str, offset: int):
        """(file object, total length or None, True when the bytes start at `offset`)."""
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
        req = urllib.request.Request(url, headers={"Range": f"bytes={offset}-"} if offset else {})
        try:
            r = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:
            if e.code == 416 and offset:
                return self.open(rel, 0)
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


def read_latest(src: "Source", pub: bytes) -> dict:
    """latest.json on the dataset's `main`, signed with the release key: {format, release, revision, manifest_sha256, updates}."""
    body, sig = src.read_small("latest.json"), src.read_small("latest.json.sig")
    if not ed25519_verify(pub, body, sig):
        raise Fatal(f"{src.src}: latest.json is not signed by the pinned key: refusing (nothing was downloaded)")
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise Fatal(f"latest.json is not JSON: {e}")
    if doc.get("format") != LATEST_FORMAT:
        raise Fatal(f"latest.json format {doc.get('format')!r}, this code reads {LATEST_FORMAT}: get a newer installer")
    for k in ("release", "revision"):
        if not isinstance(doc.get(k), str) or not ID_RE.match(doc[k]):
            raise Fatal(f"latest.json {k} {doc.get(k)!r} is not a plain name")
    if not re.fullmatch(r"[0-9a-f]{64}", str(doc.get("manifest_sha256", ""))):
        raise Fatal("latest.json has no manifest_sha256")
    ups = doc.get("updates") or {}
    if not isinstance(ups, dict) or any(not ID_RE.match(str(k)) for k in ups):
        raise Fatal("latest.json updates is not a map of release ids")
    for p in ups.values():
        safe_rel(p)
    return doc


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


def _stream_decode(dec, src, dst):
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        for b in iter(lambda: fi.read(CHUNK), b""):
            fo.write(dec.decompress(b))


def _zstandard_decode(zstandard, src, dst):
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        zstandard.ZstdDecompressor().copy_stream(fi, fo)


def xz_decode(src, dst):
    try:
        _stream_decode(lzma.LZMADecompressor(format=lzma.FORMAT_XZ), src, dst)
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
                        b = body.read(CHUNK)
                        if not b:
                            break
                        out.write(b)
                        h.update(b)
                        offset += len(b)
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
        biggest = max((transport.get(f["path"], f)["bytes"] for f in m["files"]), default=0)
        have_staged = sum(os.path.getsize(os.path.join(stage, *f["path"].split("/"))) for f in m["files"]
                          if os.path.isfile(os.path.join(stage, *f["path"].split("/"))))
        free = int(FAKE_FREE) if FAKE_FREE else shutil.disk_usage(base).free
        if free < need - have_staged + biggest:
            raise InstallError(f"not enough disk space in {base}: {free // (1 << 20)} MiB free, "
                               f"{(need - have_staged + biggest) // (1 << 20)} MiB needed (release + the largest download). Nothing was written.")
        # ---- 4. the files
        os.makedirs(stage, exist_ok=True)
        for d, _dirs, fs in os.walk(stage):
            for f in fs:
                if f.endswith(".tmp"):
                    os.remove(os.path.join(d, f))
        done = skipped = fetched = 0
        zdec = None
        try:
            for f in m["files"]:
                p, dst = f["path"], os.path.join(stage, *f["path"].split("/"))
                if os.path.isfile(dst) and os.path.getsize(dst) == f["bytes"] and file_digest(dst)[0] == f["sha256"]:
                    skipped += 1
                    continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                t = transport.get(p)
                enc = t["encoding"] if t else "identity"
                rel = "files/" + p + {"xz": ".xz", "zstd": ".zst", "identity": ""}[enc]
                part = os.path.join(dl, *rel.split("/")) + ".part"
                download(src, rel, part, (t or f)["bytes"], (t or f)["sha256"], p)
                fetched += 1
                tmp = dst + ".tmp"
                if enc == "identity":
                    os.replace(part, tmp)
                elif enc == "xz":
                    xz_decode(part, tmp)
                    os.remove(part)
                else:
                    zdec = zdec or zstd_decoder()
                    zdec(part, tmp)
                    os.remove(part)
                sha, n = file_digest(tmp)
                if n != f["bytes"] or sha != f["sha256"]:
                    os.remove(tmp)
                    raise InstallError(f"{p}: after decoding, {n} bytes / sha256 {sha[:16]}..., the manifest says {f['bytes']} / {f['sha256'][:16]}...")
                os.replace(tmp, dst)
                done += 1
                say(f"  {done + skipped:>4}/{len(m['files'])}  {p}")
                if FAIL_AFTER and done == int(FAIL_AFTER):
                    os.kill(os.getpid(), signal.SIGKILL)
            # ---- 5. checks on the stage folder
            listed = {f["path"] for f in m["files"]}
            for d, _dirs, fs in os.walk(stage):
                for f in fs:
                    r = os.path.relpath(os.path.join(d, f), stage).replace(os.sep, "/")
                    if r not in listed:
                        os.remove(os.path.join(d, f))
            open(os.path.join(stage, "manifest.json"), "wb").write(body)
            open(os.path.join(stage, "manifest.json.sig"), "wb").write(sig)
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
        warn = point(base, rid, cur)
        if not a.keep_download:
            shutil.rmtree(os.path.join(base, ".download"), ignore_errors=True)
        r = {"release": rid, "installed": True, "files": len(m["files"]), "downloaded_files": fetched, "skipped_files": skipped,
             "total_bytes": need, "seconds": round(time.time() - t0, 2)}
        if warn:
            r["warning"] = warn
        return r
    finally:
        lock()


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
}
ADDON_FORMAT = 1
BREADTH_PLAIN, BREADTH_WIKI = "JDL", "JDLWC"        # SEARCH_BREADTH without / with the wikilinks file (W and C need it)


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
def engine_env(base: str, port: int = 8090) -> dict:
    """The environment search_api.py needs to answer from BASE/current (names read from search_api.py, not guessed)."""
    cur = os.path.join(base, "current")
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
    env["SEARCH_BREADTH"] = os.environ.get("SEARCH_BREADTH", BREADTH_WIKI if "SEARCH_WIKILINKS" in env else BREADTH_PLAIN)
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
    t = sp.add_parser("status")
    t.add_argument("--dest", required=True)
    a = ap.parse_args()
    try:
        if a.cmd == "install":
            r = install(a)
            print(json.dumps(r, sort_keys=True))
            if r.get("installed"):
                say(after_install_help(os.path.abspath(a.dest)))
        elif a.cmd == "start":
            start(a)
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
