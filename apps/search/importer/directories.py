"""Human-curated website directories as "what kind of site is this" labels
(search-site-kinds, 2026-09-28).

Two sources, both fetched on an AWS build instance (never from the home IP):

  dmoz     The Open Directory Project RDF dump (content.rdf.u8.gz, CC BY 3.0 —
           "Data from the Open Directory Project, dmoz.org, licensed CC BY 3.0").
           DMOZ closed 2017-03-17; the dump comes from an archive.org copy. Output:
             url \t domain \t category_path \t title \t description
           Top/World, Top/Regional, Top/Adult and Top/Kids_and_Teens/International
           are skipped; "Top/" is dropped from the path. A URL listed under several
           topics gives several rows — the categories are the signal.

  awesome  The lists linked from github.com/sindresorhus/awesome. Per list its
           LICENSE (then, failing that, a License section of its README) decides:
           only permissive licences are kept (PERMISSIVE). Output:
             url \t domain \t list_repo \t license_spdx \t section_path \t link_text \t description
           plus a side file (--lists) with one row per list:
             repo \t spdx \t licence_source(file|readme|none) \t kept \t items \t note

Politeness: every request is paced (archive.org >= 1 s apart, raw.githubusercontent.com
>= 0.5 s apart, 404s included). A 429/503 from archive.org or a 403/429 from GitHub
stops the command at once — no retry — and what was written so far is kept.
Only directory data is fetched; the listed sites themselves are never contacted.

    directories.py dmoz --out dmoz.tsv.gz [--src URL] [--work DIR] [--deadline-min N]
    directories.py dmoz --out dmoz.tsv.gz --rdf content.rdf.u8.gz   (parse a local file)
    directories.py awesome --out awesome.tsv.gz --lists lists.tsv [--deadline-min N]
    directories.py awesome --out F --lists F --readme-dir DIR        (parse local files)

site-directories2 (2026-09-29) adds five sources in one shared schema,
    domain \t url \t phrase \t name \t description
one row per (site, kind phrase), each from a single bulk file (licences and verdicts:
docs/legal/SEARCH-DATA-SOURCES.md):

  ut1         UT1 blacklists all.tar.gz (CC BY-SA 4.0). Default-deny category map: safe
              kinds -> --out, adult/malware/phishing/gambling etc. -> --unsafe-out.
  homebrew    formulae.brew.sh formula.json + cask.json (BSD-2-Clause).
  publicapis  public-apis/public-apis README tables (MIT).
  flathub     Flathub appstream.xml.gz; summary kept only under a BY-SA-compatible
              metadata_license.
  debian      stable/main Packages.xz: Section + debtags, one-line synopsis only.

    directories.py <source> --out F.tsv.gz [--src-file LOCAL] [--work DIR] [--stats F.json]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "logout-search-import/1 (personal search index; polite, sequential)"
IA_SEARCH = "https://archive.org/advancedsearch.php"
IA_META = "https://archive.org/metadata/"
IA_DOWNLOAD = "https://archive.org/download/"
# Exact Wayback captures of the dump (CDX, 2026-09-28): 20170312 is the last full one
# before the 2017-03-17 closure (whose own capture is a 404 — a bare "2017" timestamp
# resolves to it); 20170303 is the earlier dump, captured identically three times.
# The archive.org item dmoz-rdf-20150327 holds only WARCs of rdf.dmoz.org, no plain file.
WAYBACK_DMOZ = tuple(f"https://web.archive.org/web/{ts}id_/http://rdf.dmoz.org/rdf/content.rdf.u8.gz"
                     for ts in ("20170312160530", "20170303054902"))
RAW = "https://raw.githubusercontent.com/"
AWESOME_ROOT = ("sindresorhus/awesome", "readme.md")

SKIP_TOPICS = ("Top/World", "Top/Regional", "Top/Adult", "Top/Kids_and_Teens/International")

# Filenames probed for a licence and a README, most common first (raw.githubusercontent.com
# has no directory listing; each miss is one paced 404).
LICENSE_NAMES = ("LICENSE", "license", "LICENSE.md", "license.md", "LICENSE.txt",
                 "COPYING", "LICENCE", "UNLICENSE")
README_NAMES = ("README.md", "readme.md", "Readme.md", "README.markdown", "readme.markdown")

PERMISSIVE = {"CC0-1.0", "CC-BY-4.0", "CC-BY-3.0", "MIT", "Unlicense", "Apache-2.0",
              "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD", "WTFPL"}

BADGE_HOSTS = ("shields.io", "badgen.net", "travis-ci.org", "travis-ci.com", "circleci.com",
               "codecov.io", "coveralls.io", "awesome.re", "badge.fury.io", "app.netlify.com",
               "gitter.im", "david-dm.org", "codeclimate.com", "img.shields.io", "badges.gitter.im",
               "api.netlify.com", "ci.appveyor.com")
IMAGE_EXT = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico)(\?.*)?$", re.I)


def log(msg: str) -> None:
    print(f"[directories] {time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


class Stop(RuntimeError):
    """The server asked us to stop (or the time budget ran out): end, keep the results."""


class Polite:
    """One host's pacing, stop statuses and wall-clock budget. The opener is injectable."""

    def __init__(self, gap: float, stop_codes: tuple[int, ...], deadline: float | None = None,
                 opener=urllib.request.urlopen, sleep=time.sleep, now=time.monotonic) -> None:
        self.gap, self.stop_codes, self.deadline = gap, stop_codes, deadline
        self.opener, self.sleep, self.now = opener, sleep, now
        self.last = None
        self.requests = 0

    def open(self, url: str, headers: dict | None = None, timeout: int = 60):
        """Paced GET. Returns the response (caller closes it) or None on 404/410.
        Raises Stop on a stop status or when past the deadline."""
        if self.deadline is not None and self.now() > self.deadline:
            raise Stop("time budget used up")
        t = self.now()
        if self.last is not None and t - self.last < self.gap:
            self.sleep(self.gap - (t - self.last))
        self.last = self.now()
        self.requests += 1
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
        try:
            return self.opener(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in self.stop_codes:
                raise Stop(f"HTTP {e.code} from {url} — stop signal") from e
            if e.code in (404, 410):
                return None
            raise

    def text(self, url: str) -> str | None:
        r = self.open(url)
        if r is None:
            return None
        with r:
            return r.read().decode("utf-8", "replace")


# --------------------------------------------------------------------------- helpers

_WS = re.compile(r"[\t\r\n]+")


def clean(s: str) -> str:
    """One TSV field: no tabs/newlines, collapsed spaces."""
    return re.sub(r" {2,}", " ", _WS.sub(" ", s or "")).strip()


def domain_of(url: str) -> str:
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


# --------------------------------------------------------------------------- DMOZ

_PAGE = re.compile(r'<ExternalPage\s+about="([^"]*)"\s*>(.*?)</ExternalPage>', re.S)
_TITLE = re.compile(r"<d:Title>(.*?)</d:Title>", re.S)
_DESC = re.compile(r"<d:Description>(.*?)</d:Description>", re.S)
_TOPIC = re.compile(r"<topic>(.*?)</topic>", re.S)


def _field(rx: re.Pattern, body: str) -> str:
    m = rx.search(body)
    return clean(html.unescape(m.group(1))) if m else ""


def dmoz_rows(stream, stats: dict):
    """Yield (url, domain, category_path, title, description) from a content.rdf text
    stream. Regex over an accumulating buffer, not an XML parser: the dumps are known to
    carry invalid characters, and a block is only cut once its </ExternalPage> is read.
    A truncated stream (EOFError from gzip) ends the scan and sets stats['truncated']."""
    buf = ""
    while True:
        try:
            chunk = stream.read(1 << 20)
        except EOFError:
            stats["truncated"] = True
            chunk = ""
        if not chunk:
            break
        buf += chunk
        end = 0
        for m in _PAGE.finditer(buf):
            end = m.end()
            stats["pages"] = stats.get("pages", 0) + 1
            row = _dmoz_row(m.group(1), m.group(2), stats)
            if row:
                yield row
        if end:
            buf = buf[end:]
        elif len(buf) > (8 << 20):          # no block in 8 MB: skip junk, keep a tail
            buf = buf[-(1 << 20):]


def _dmoz_row(about: str, body: str, stats: dict):
    url = clean(html.unescape(about))
    topic = _field(_TOPIC, body)
    if any(topic == p or topic.startswith(p + "/") for p in SKIP_TOPICS):
        stats["skipped_topic"] = stats.get("skipped_topic", 0) + 1
        return None
    dom = domain_of(url)
    if not url.lower().startswith(("http://", "https://")) or not dom:
        stats["skipped_url"] = stats.get("skipped_url", 0) + 1
        return None
    path = topic[4:] if topic.startswith("Top/") else topic
    return url, dom, path, _field(_TITLE, body), _field(_DESC, body)


def _ia_candidates(ia: Polite) -> list[tuple[str, str, int, str]]:
    """archive.org items named dmoz-rdf*: (url, identifier, size, md5) of each item's
    content.rdf.u8.gz, newest item first."""
    q = urllib.parse.urlencode({"q": "identifier:dmoz-rdf* OR (title:dmoz AND title:rdf)",
                                "fl[]": "identifier", "rows": 50, "output": "json",
                                "sort[]": "identifier desc"})
    body = ia.text(f"{IA_SEARCH}?{q}")
    ids = [d["identifier"] for d in json.loads(body or "{}").get("response", {}).get("docs", [])]
    log(f"archive.org search: {len(ids)} candidate items: {' '.join(ids[:20])}")
    out = []
    for ident in ids[:15]:
        meta = json.loads(ia.text(IA_META + ident) or "{}")
        for f in meta.get("files", []):
            if f.get("name", "").rsplit("/", 1)[-1] == "content.rdf.u8.gz":
                out.append((IA_DOWNLOAD + ident + "/" + urllib.parse.quote(f["name"]), ident,
                            int(f.get("size") or 0), f.get("md5", "")))
    out.sort(key=lambda c: c[1], reverse=True)
    return out


def _download(ia: Polite, url: str, dest: str, want_size: int = 0) -> None:
    """Stream url to dest. Network drops resume with a Range request (at most 3 times);
    a stop status raises Stop immediately."""
    have = os.path.getsize(dest) if os.path.exists(dest) else 0
    for attempt in range(4):
        try:
            r = ia.open(url, {"Range": f"bytes={have}-"} if have else None, timeout=120)
            if r is None:
                raise FileNotFoundError(url)
            with r, open(dest, "ab" if have and r.status == 206 else "wb") as f:
                if not (have and r.status == 206):
                    have = 0
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
                    have += len(b)
            if not want_size or have >= want_size:
                return
            raise OSError(f"short read {have}/{want_size}")
        except (OSError, urllib.error.URLError) as e:
            if isinstance(e, FileNotFoundError) or attempt == 3:
                raise
            log(f"download interrupted at {have} bytes ({e}) — resuming in 10 s")
            time.sleep(10)


def _gzip_ok(path: str) -> bool:
    try:
        with gzip.open(path, "rb") as f:
            while f.read(1 << 22):
                pass
        return True
    except (EOFError, OSError, gzip.BadGzipFile):
        return False


def _md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def cmd_dmoz(a) -> int:
    try:
        return _dmoz(a)
    except Stop as e:
        log(f"STOP: {e}")
        return 3


def _dmoz(a) -> int:
    stats: dict = {}
    rdf = a.rdf
    source = rdf or ""
    if not rdf:
        ia = Polite(1.0, (429, 503))
        os.makedirs(a.work, exist_ok=True)
        rdf = os.path.join(a.work, "content.rdf.u8.gz")
        cands = [(a.src, "given", 0, "")] if a.src else _ia_candidates(ia)
        cands += [(u, "wayback", 0, "") for u in WAYBACK_DMOZ]
        for url, ident, size, md5 in cands:
            log(f"downloading {url} ({size or '?'} bytes)")
            if os.path.exists(rdf):
                os.remove(rdf)
            try:
                _download(ia, url, rdf, size)
            except FileNotFoundError:
                log(f"404: {url}")
                continue
            got = _md5(rdf)
            ok = (not md5 or got == md5) and _gzip_ok(rdf)
            log(f"{url}: {os.path.getsize(rdf)} bytes md5 {got} (want {md5 or '-'}) gzip {'ok' if ok else 'BAD'}")
            source = f"{url} ({os.path.getsize(rdf)} bytes, md5 {got})"
            if ok:
                break
            stats["incomplete_source"] = url
        else:
            if not os.path.exists(rdf):
                log("no DMOZ dump could be downloaded")
                return 1
            log("no complete dump — parsing the last one as far as it goes")
    n = 0
    with gzip.open(rdf, "rt", encoding="utf-8", errors="replace") as src, \
            gzip.open(a.out, "wt", encoding="utf-8") as out:
        for row in dmoz_rows(src, stats):
            out.write("\t".join(row) + "\n")
            n += 1
    stats["rows"] = n
    stats["source"] = source
    stats["attribution"] = "Data from the Open Directory Project (dmoz.org), CC BY 3.0"
    log(f"dmoz: {json.dumps(stats)}")
    if a.stats:
        with open(a.stats, "w") as f:
            json.dump(stats, f, indent=1)
    return 2 if stats.get("truncated") else 0


# --------------------------------------------------------------------------- awesome

_LINK = re.compile(r"(!?)\[((?:[^\[\]]|\[[^\]]*\])*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_HTML_TAG = re.compile(r"<[^>]+>")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]|:[a-z0-9_+-]+:")
_FENCE = re.compile(r"^\s*(```|~~~)")
_REFLINK = re.compile(r"(!?)\[([^\[\]]*)\]\[[^\[\]]*\]")


def md_text(s: str) -> str:
    """Markdown/HTML inline -> plain text: links become their text, images vanish."""
    s = _LINK.sub(lambda m: "" if m.group(1) else m.group(2), s)
    s = _REFLINK.sub(lambda m: "" if m.group(1) else m.group(2), s)   # [text][ref], ![img][ref]
    s = _HTML_TAG.sub("", s)
    s = re.sub(r"(\*\*|__|`|~~)", "", s)
    s = re.sub(r"(?<!\w)[*_](\S[^*_]*?)[*_](?!\w)", r"\1", s)
    return clean(html.unescape(_EMOJI.sub("", s)))


def is_awesome_link(url: str) -> bool:
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower()
    if host.endswith("awesome.re"):
        return True
    if host in ("github.com", "www.github.com", "gitlab.com"):
        parts = [x for x in p.path.split("/") if x]
        return len(parts) >= 2 and "awesome" in parts[1].lower()
    return False


def skip_link(text: str, url: str, is_image: bool) -> str | None:
    """Why a README link is not an item (None = keep)."""
    if is_image or text.lstrip().startswith("!["):
        return "image"
    if not url.lower().startswith(("http://", "https://")):
        return "anchor/relative"
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if any(host == b or host.endswith("." + b) for b in BADGE_HOSTS) or IMAGE_EXT.search(url):
        return "badge"
    if is_awesome_link(url):
        return "awesome-list"
    return None


_CONTENTS = re.compile(r"^(table of )?contents$", re.I)
_SKIP_SECTIONS = re.compile(r"^(table of )?contents$|^license$|^licence$|^contribut", re.I)


def awesome_items(text: str):
    """Yield (section_path, url, link_text, description) from one list's README."""
    heads: list[tuple[int, str]] = []
    fenced = False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        h = _HEADING.match(line)
        if h:
            level = len(h.group(1))
            heads = [x for x in heads if x[0] < level] + [(level, md_text(h.group(2)))]
            continue
        b = _BULLET.match(line)
        body = b.group(1) if b else (line.strip().strip("|") if line.lstrip().startswith("|") else None)
        if body is None:
            continue
        m = _LINK.search(body)
        if not m or body[:m.start()].strip(" *_`[|") != "":
            continue                        # the item's own link comes first
        # Levels below the first one (the list's title) name the section.
        names = [n for lvl, n in heads if lvl > 1 and n] or [n for _, n in heads[1:] if n]
        if names and _SKIP_SECTIONS.search(names[-1]):
            continue
        if len(names) > 1 and _CONTENTS.match(names[0]):
            names = names[1:]               # "Contents > GUI": items nested under the TOC heading
        if skip_link(m.group(2), m.group(3), bool(m.group(1))):
            continue
        rest = body[m.end():]
        if line.lstrip().startswith("|"):
            rest = " ".join(c for c in rest.split("|"))
        desc = md_text(rest)
        desc = re.sub(r"^[\s\-–—:|,.]+", "", desc)
        yield " > ".join(names), m.group(3), md_text(m.group(2)), desc


def list_repos(root_readme: str) -> list[tuple[str, str | None]]:
    """(owner/repo, file or None) for every GitHub list linked from the root README."""
    out, seen = [], set()
    for m in _LINK.finditer(root_readme):
        url = m.group(3)
        p = urllib.parse.urlsplit(url)
        if (p.hostname or "").lower() not in ("github.com", "www.github.com"):
            continue
        parts = [x for x in p.path.split("/") if x]
        if len(parts) < 2:
            continue
        repo = f"{parts[0]}/{parts[1]}"
        if repo.lower() == AWESOME_ROOT[0] or parts[0] in ("sponsors", "topics", "features"):
            continue
        fname = None
        if len(parts) >= 5 and parts[2] == "blob" and parts[-1].lower().endswith((".md", ".markdown")):
            fname = "/".join(parts[4:])
        key = (repo.lower(), fname)
        if key not in seen:
            seen.add(key)
            out.append((repo, fname))
    return out


def spdx_of(text: str) -> str | None:
    """SPDX id of a licence text, or 'other:<hint>' for a non-permissive one, None if unknown."""
    t = re.sub(r"\s+", " ", text)
    tl = t.lower()
    if "cc0" in tl or "publicdomain/zero" in tl or "creative commons legal code cc0" in tl:
        return "CC0-1.0"
    if "creativecommons.org" in tl or "creative commons" in tl or "attribution" in tl and "international" in tl:
        if re.search(r"noncommercial|by-nc|\bnc\b", tl):
            return "other:CC-NC"
        if re.search(r"sharealike|by-sa", tl):
            return "other:CC-BY-SA"
        if re.search(r"noderivatives|noderivs|by-nd", tl):
            return "other:CC-ND"
        if re.search(r"licenses/by/4\.0|attribution 4\.0", tl):
            return "CC-BY-4.0"
        if re.search(r"licenses/by/3\.0|attribution 3\.0", tl):
            return "CC-BY-3.0"
    if "free and unencumbered software released into the public domain" in tl or "unlicense" in tl:
        return "Unlicense"
    if "gnu general public license" in tl or "gnu affero" in tl or "gnu lesser" in tl:
        return "other:GPL-family"
    if "mozilla public license" in tl:
        return "other:MPL"
    if "apache license" in tl and "2.0" in tl:
        return "Apache-2.0"
    if "permission is hereby granted, free of charge" in tl:
        return "MIT"
    if "permission to use, copy, modify, and/or distribute this software for any purpose" in tl:
        return "0BSD" if "with or without fee is hereby granted." in tl and "copyright notice" not in tl else "ISC"
    if "redistribution and use in source and binary forms" in tl:
        return "BSD-3-Clause" if "neither the name" in tl or "names of its contributors" in tl else "BSD-2-Clause"
    if "do what the fuck you want" in tl:
        return "WTFPL"
    return None


def readme_license(text: str) -> str | None:
    """A licence declared in the README's License section (or a CC badge link anywhere)."""
    sec, grab = [], False
    fenced = False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
        if fenced:
            continue
        h = _HEADING.match(line)
        if h:
            grab = bool(re.search(r"licen[cs]e", h.group(2), re.I))
            continue
        if grab:
            sec.append(line)
    if sec:
        s = spdx_of("\n".join(sec))
        if s:
            return s
    m = re.search(r"creativecommons\.org/(publicdomain/zero/1\.0|licenses/by(?:-[a-z-]+)?/[34]\.0)", text, re.I)
    return spdx_of(m.group(0)) if m else None


def cmd_awesome(a) -> int:
    deadline = time.monotonic() + a.deadline_min * 60 if a.deadline_min else None
    gh = Polite(a.gap, (403, 429), deadline)
    stats = {"lists": 0, "kept": 0, "dropped": {}, "no_readme": 0, "rows": 0, "stop": None,
             "licence_source": {"file": 0, "readme": 0}}
    local = a.readme_dir

    def fetch(repo: str, name: str) -> str | None:
        if local:
            p = os.path.join(local, repo.replace("/", "__"), name)
            return open(p, encoding="utf-8", errors="replace").read() if os.path.exists(p) else None
        return gh.text(f"{RAW}{repo}/HEAD/{urllib.parse.quote(name)}")

    rc = 0
    with gzip.open(a.out, "wt", encoding="utf-8") as out, open(a.lists, "w", encoding="utf-8") as lists:
        try:
            root = fetch(*AWESOME_ROOT)
            if root is None:
                log(f"root list {AWESOME_ROOT} not found (HEAD ref) — stopping")
                return 1
            repos = list_repos(root)
            log(f"root list: {len(repos)} linked GitHub lists")
            if a.limit:
                repos = repos[:a.limit]
            for i, (repo, fname) in enumerate(repos, 1):
                stats["lists"] += 1
                spdx, src, note = None, "none", ""
                for ln in LICENSE_NAMES:
                    lt = fetch(repo, ln)
                    if lt is not None:
                        spdx, src = spdx_of(lt), "file"
                        if spdx is None:
                            spdx, note = "unknown", f"unrecognised {ln}"
                        break
                if spdx is not None and spdx not in PERMISSIVE:
                    stats["dropped"][spdx] = stats["dropped"].get(spdx, 0) + 1
                    lists.write(f"{repo}\t{spdx}\t{src}\tno\t0\t{note}\n")
                    continue
                readme, used = None, None
                for rn in ([fname] if fname else README_NAMES):
                    readme = fetch(repo, rn)
                    if readme is not None:
                        used = rn
                        break
                if readme is None:
                    stats["no_readme"] += 1
                    lists.write(f"{repo}\t{spdx or 'none'}\t{src}\tno\t0\tno readme\n")
                    continue
                if spdx is None:
                    spdx = readme_license(readme)
                    src = "readme" if spdx else "none"
                if spdx is None or spdx not in PERMISSIVE:
                    k = spdx or "none"
                    stats["dropped"][k] = stats["dropped"].get(k, 0) + 1
                    lists.write(f"{repo}\t{k}\t{src}\tno\t0\t{used}\n")
                    continue
                stats["kept"] += 1
                stats["licence_source"][src] += 1
                n = 0
                for sec, url, text, desc in awesome_items(readme):
                    dom = domain_of(url)
                    if not dom:
                        continue
                    out.write("\t".join(clean(x) for x in (url, dom, repo, spdx, sec, text, desc)) + "\n")
                    n += 1
                stats["rows"] += n
                lists.write(f"{repo}\t{spdx}\t{src}\tyes\t{n}\t{used}\n")
                if i % 25 == 0:
                    log(f"{i}/{len(repos)} lists, {stats['kept']} kept, {stats['rows']} rows, {gh.requests} requests")
        except Stop as e:
            stats["stop"] = str(e)
            log(f"STOP: {e} — keeping what was written")
            rc = 3
    stats["requests"] = gh.requests
    log(f"awesome: {json.dumps(stats)}")
    if a.stats:
        with open(a.stats, "w") as f:
            json.dump(stats, f, indent=1)
    return rc


# --------------------------------------------------------------------------- kind files
#
# The site-directories2 sources (licences: docs/legal/SEARCH-DATA-SOURCES.md) all write
# one schema, one row per (site, kind phrase):
#     domain \t url \t phrase \t name \t description
# A build step reads every file in kinds-extra/, so nothing else may be written there.

UT1_URL = "https://dsi.ut-capitole.fr/blacklists/download/all.tar.gz"
BREW_URLS = ("https://formulae.brew.sh/api/formula.json", "https://formulae.brew.sh/api/cask.json")
PUBLICAPIS = ("public-apis/public-apis", "README.md")
FLATHUB_URL = "https://dl.flathub.org/repo/appstream/x86_64/appstream.xml.gz"
DEBIAN_URL = "https://deb.debian.org/debian/dists/stable/main/binary-amd64/Packages.xz"
STOP_CODES = (403, 429, 503)

# Homepages on these hosts are a project page on someone else's site, not the app's own.
CODE_HOSTS = {"github.com", "gitlab.com", "sourceforge.net", "codeberg.org", "bitbucket.org",
              "salsa.debian.org", "launchpad.net", "gitee.com", "sr.ht", "git.sr.ht",
              "gitlab.gnome.org", "invent.kde.org", "framagit.org", "pypi.org", "npmjs.com",
              "www.npmjs.com", "crates.io", "hackage.haskell.org", "metacpan.org", "cpan.org"}
_IP = re.compile(r"^(\d{1,3}(\.\d{1,3}){3}|\[?[0-9a-f:]+\]?)$", re.I)


class KindOut:
    """Gzip TSV writer for the kind schema; dedupes (domain, phrase) within a file."""

    def __init__(self, path: str) -> None:
        self.f = gzip.open(path, "wt", encoding="utf-8")
        self.seen: set[tuple[str, str]] = set()
        self.rows = 0
        self.code_host = 0

    def write(self, url: str, phrase: str, name: str = "", desc: str = "", domain: str = "") -> bool:
        dom = domain or domain_of(url)
        phrase = clean(phrase).lower()
        if not dom or not phrase or "." not in dom or _IP.match(dom):
            return False
        if dom in CODE_HOSTS:
            self.code_host += 1
            return False
        if (dom, phrase) in self.seen:
            return False
        self.seen.add((dom, phrase))
        desc = clean(desc)
        if len(desc) > 300:
            desc = desc[:297].rsplit(" ", 1)[0] + "..."
        self.f.write("\t".join((dom, clean(url), phrase, clean(name), desc)) + "\n")
        self.rows += 1
        return True

    def close(self) -> None:
        self.f.close()


def _fetch_to(url: str, dest: str, host: Polite) -> str:
    """Download url to dest (one paced request); raises FileNotFoundError on 404."""
    r = host.open(url, timeout=300)
    if r is None:
        raise FileNotFoundError(url)
    with r, open(dest, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
    log(f"{url}: {os.path.getsize(dest)} bytes md5 {_md5(dest)}")
    return dest


def _source(a, url: str, name: str, host: Polite) -> str:
    """The local file to parse: --src-file if given, else a fresh download into --work."""
    if a.src_file:
        return a.src_file
    os.makedirs(a.work, exist_ok=True)
    return _fetch_to(url, os.path.join(a.work, name), host)


def _finish(stats: dict, out: KindOut, a, extra: dict | None = None) -> int:
    out.close()
    stats.update(rows=out.rows, skipped_code_host=out.code_host, **(extra or {}))
    log(f"{a.cmd}: {json.dumps(stats)}")
    if a.stats:
        with open(a.stats, "w") as f:
            json.dump(stats, f, indent=1)
    return 0


def _stopping(fn):
    """Run a source command; a stop status or missing source ends it with rc 3 / 1."""
    def run(a) -> int:
        try:
            return fn(a)
        except Stop as e:
            log(f"STOP: {e}")
            return 3
        except FileNotFoundError as e:
            log(f"source missing: {e}")
            return 1
    return run


def _camel_words(s: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", s).lower()


# --------------------------------------------------------------------------- UT1

# Default-deny: a category reaches ut1.tsv.gz only through UT1_SAFE, ut1-unsafe.tsv.gz
# only through UT1_UNSAFE; anything else (including categories added after 2026-09-29)
# is logged and written nowhere.
UT1_SAFE = {
    "astrology": ("astrology",), "audio-video": ("audio and video streaming",),
    "bank": ("bank", "online banking"), "bitcoin": ("cryptocurrency",), "blog": ("blog",),
    "celebrity": ("celebrity",), "chat": ("chat",), "child": ("sites for kids",),
    "cooking": ("cooking", "recipes"), "dating": ("dating",), "download": ("software download",),
    "educational_games": ("educational games",), "filehosting": ("file hosting", "file sharing"),
    "financial": ("finance",), "forums": ("forum",), "games": ("games", "online games"),
    "jobsearch": ("job search", "jobs"), "manga": ("manga",), "mobile-phone": ("mobile phones",),
    "press": ("news", "press"), "radio": ("radio", "online radio"), "shopping": ("shopping",),
    "shortener": ("url shortener", "link shortener"), "social_networks": ("social network",),
    "sports": ("sports",), "translation": ("translation",), "vpn": ("vpn",),
    "webhosting": ("web hosting",), "webmail": ("webmail", "email"),
    "remote-control": ("remote desktop", "remote control"), "doh": ("dns over https",),
    "associations_religieuses": ("religious organisation",), "update": ("software updates",),
    "ai": ("artificial intelligence", "ai"),
}
UT1_UNSAFE = {
    "adult": "adult", "mixed_adult": "mixed adult", "lingerie": "lingerie", "agressif": "hate/aggressive",
    "arjel": "gambling", "gambling": "gambling", "cryptojacking": "cryptojacking",
    "dangerous_material": "dangerous material", "ddos": "ddos", "dialer": "dialer",
    "drogue": "drugs", "hacking": "hacking", "malware": "malware", "phishing": "phishing",
    "sect": "sect", "stalkerware": "stalkerware", "warez": "warez", "fakenews": "fake news",
    "publicite": "advertising", "marketingware": "marketingware", "tricheur": "cheating",
    "residential-proxies": "residential proxy",
}


def _ut1_extract(tar_path: str, dest: str) -> str:
    """Unpack the tarball; return the directory holding the category dirs."""
    import shutil
    import tarfile
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    with tarfile.open(tar_path, "r:*") as t:
        t.extractall(dest, filter="data")
    for root, dirs, _ in os.walk(dest):
        if "adult" in dirs or "webmail" in dirs:
            return root
    return dest


@_stopping
def cmd_ut1(a) -> int:
    host = Polite(1.0, STOP_CODES)
    tar = _source(a, UT1_URL, "ut1-all.tar.gz", host)
    base = _ut1_extract(tar, os.path.join(a.work, "ut1"))
    top = os.path.dirname(base)
    if a.evidence:                      # the tarball's own licence/readme, as primary evidence
        import shutil
        os.makedirs(a.evidence, exist_ok=True)
        for d in {base, top}:
            for fn in os.listdir(d):
                p = os.path.join(d, fn)
                if os.path.isfile(p) and re.match(r"(readme|licen[cs]e|copying|about)", fn, re.I):
                    shutil.copy(p, os.path.join(a.evidence, fn))
    # Aliases (symlinked dirs) resolve to one target; classify by alias AND target, so an
    # alias of an unsafe category can never reach the safe file, and read each target once.
    groups: dict[str, set[str]] = {}
    for name in sorted(os.listdir(base)):
        p = os.path.join(base, name)
        if os.path.isdir(p):
            groups.setdefault(os.path.realpath(p), set()).add(name)
    safe, unsafe = KindOut(a.out), KindOut(a.unsafe_out)
    stats: dict = {"categories": {}, "unknown": {}, "excluded": {}, "aliases": {}}
    for real, names in sorted(groups.items()):
        names = names | {os.path.basename(real)}
        cat = os.path.basename(real)
        dom_file = os.path.join(real, "domains")
        if len(names) > 1:
            stats["aliases"][cat] = sorted(names)
        if not os.path.isfile(dom_file):
            continue
        if any(n in UT1_UNSAFE for n in names):
            kind, out = "unsafe", unsafe
            phrases = (UT1_UNSAFE[next(n for n in sorted(names) if n in UT1_UNSAFE)],)
        elif all(n in UT1_SAFE for n in names):
            kind, out, phrases = "safe", safe, UT1_SAFE[cat]
        else:
            kind, out, phrases = "unknown", None, ()
        n = 0
        with open(dom_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                d = line.strip().lower().rstrip(".")
                if not d or d.startswith("#") or _IP.match(d):
                    continue
                d = d[4:] if d.startswith("www.") else d
                n += 1
                if out is not None:
                    for ph in phrases:
                        out.write(f"https://{d}/", ph, domain=d)
        (stats["unknown"] if kind == "unknown" else stats["categories"])[cat] = \
            n if kind == "unknown" else f"{kind}:{n}"
        if kind == "unknown":
            log(f"UT1 category '{cat}' ({n} domains) is on neither list — written nowhere")
    unsafe.close()
    return _finish(stats, safe, a, {"unsafe_rows": unsafe.rows,
                                    "attribution": "UT1 blacklists, Université Toulouse Capitole "
                                                   "(Fabrice Prigent), CC BY-SA 4.0"})


# --------------------------------------------------------------------------- Homebrew

_LIBRARY = re.compile(r"\b(librar(y|ies)|bindings?|headers?|sdk|framework|module|plugin)\b", re.I)


def _short_phrase(desc: str) -> str | None:
    """A cask/formula desc is written as a kind ("Password manager"): usable as a phrase
    when it is short. None when it is a sentence."""
    d = clean(desc).rstrip(".")
    if not d or len(d) > 50 or len(d.split()) > 6:
        return None
    return d.lower()


@_stopping
def cmd_homebrew(a) -> int:
    host = Polite(1.0, STOP_CODES)
    out = KindOut(a.out)
    stats: dict = {"formulae": 0, "casks": 0, "no_homepage": 0}
    files = a.src_file.split(",") if a.src_file else None
    os.makedirs(a.work, exist_ok=True)
    for i, url in enumerate(BREW_URLS):
        path = files[i] if files else _fetch_to(url, os.path.join(a.work, url.rsplit("/", 1)[-1]), host)
        is_cask = url.endswith("cask.json")
        with open(path, encoding="utf-8") as f:
            items = json.load(f)
        for it in items:
            stats["casks" if is_cask else "formulae"] += 1
            hp = it.get("homepage") or ""
            if not hp.startswith(("http://", "https://")):
                stats["no_homepage"] += 1
                continue
            desc = it.get("desc") or ""
            if is_cask:
                names = it.get("name") or []
                name = names[0] if names else it.get("token", "")
                base = "mac app"
            else:
                name = it.get("name", "")
                base = "software library" if _LIBRARY.search(desc) else "command-line tool"
            out.write(hp, base, name, desc)
            sp = _short_phrase(desc)
            if sp:
                out.write(hp, sp, name, desc)
    return _finish(stats, out, a, {"attribution": "Homebrew formulae/casks metadata, "
                                   "© Homebrew contributors, BSD-2-Clause"})


# --------------------------------------------------------------------------- public-apis

_API_HOST = re.compile(r"^(api|apis|dev|developers?|docs|documentation)\.")


def publicapis_rows(text: str):
    """(category, url, name, description) from the public-apis README tables."""
    cat, fenced = "", False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        h = _HEADING.match(line)
        if h:
            cat = md_text(h.group(2))
            continue
        if not cat or not line.lstrip().startswith("|") or _SKIP_SECTIONS.search(cat) or cat.lower() == "index":
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        m = _LINK.search(cells[0])
        if not m or m.group(1) or not m.group(3).lower().startswith(("http://", "https://")):
            continue
        yield cat, m.group(3), md_text(m.group(2)), md_text(cells[1])


@_stopping
def cmd_publicapis(a) -> int:
    gh = Polite(0.6, STOP_CODES)
    if a.src_file:
        text = open(a.src_file, encoding="utf-8").read()
    else:
        text = gh.text(f"{RAW}{PUBLICAPIS[0]}/HEAD/{PUBLICAPIS[1]}")
        if text is None:
            raise FileNotFoundError(PUBLICAPIS)
    out = KindOut(a.out)
    stats: dict = {"items": 0, "categories": set()}
    for cat, url, name, desc in publicapis_rows(text):
        stats["items"] += 1
        stats["categories"].add(cat)
        c = cat.lower()
        # dev.bitly.com -> bitly.com: the row labels the service, not its docs host.
        dom = _API_HOST.sub("", domain_of(url))
        dom = dom if "." in dom else domain_of(url)
        out.write(url, f"{c} api", name, desc, domain=dom)
        out.write(url, "api", name, desc, domain=dom)
    stats["categories"] = len(stats["categories"])
    return _finish(stats, out, a, {"attribution": "public-apis/public-apis, © 2022 public-apis, MIT"})


# --------------------------------------------------------------------------- Flathub

FDO_SKIP = {"GTK", "Qt", "GNOME", "KDE", "XFCE", "DDE", "Java", "Motif", "ConsoleOnly",
            "Application", "Core", "Documentation", "Adult", "Amusement"}
FDO_PHRASES = {"AudioVideo": "audio and video", "System": "system tool", "Development": "development tool",
               "Game": "game", "Utility": "utility", "P2P": "peer-to-peer", "IDE": "ide",
               "TV": "tv", "HamRadio": "ham radio", "3DGraphics": "3d graphics",
               "2DGraphics": "2d graphics", "IRCClient": "irc client", "Email": "email client",
               "WebBrowser": "web browser", "Settings": "settings", "Network": "network"}
# Licences that can be merged with the CC BY-SA label set (the AppStream-permitted set
# minus GFDL). Others (GFDL-*, GPL-*, absent): facts only, the summary is left out.
FLATHUB_TEXT_OK = {"FSFAP", "MIT", "0BSD", "CC0-1.0", "CC-BY-3.0", "CC-BY-4.0", "CC-BY-SA-3.0",
                   "CC-BY-SA-4.0", "BSL-1.0", "FTL", "FSFUL", "FSFULLR"}
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def fdo_phrase(cat: str) -> str | None:
    if cat.startswith("X-") or cat in FDO_SKIP:
        return None
    return FDO_PHRASES.get(cat) or _camel_words(cat)


def _untranslated(el, tag: str) -> str:
    for c in el.findall(tag):
        if c.get(_XML_LANG) in (None, "", "C", "en", "en_US"):
            return "".join(c.itertext())
    return ""


@_stopping
def cmd_flathub(a) -> int:
    import xml.etree.ElementTree as ET
    host = Polite(1.0, STOP_CODES)
    path = _source(a, FLATHUB_URL, "flathub-appstream.xml.gz", host)
    out = KindOut(a.out)
    stats: dict = {"components": 0, "types": {}, "metadata_license": {}, "no_homepage": 0,
                   "no_category": 0, "facts_only": 0}
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rb") as f:
        for _, el in ET.iterparse(f, events=("end",)):
            if el.tag != "component":
                continue
            stats["components"] += 1
            ctype = el.get("type") or "?"
            stats["types"][ctype] = stats["types"].get(ctype, 0) + 1
            lic = (el.findtext("metadata_license") or "").strip() or "absent"
            stats["metadata_license"][lic] = stats["metadata_license"].get(lic, 0) + 1
            if ctype not in ("desktop-application", "desktop", "console-application", "web-application"):
                el.clear()
                continue
            hp = ""
            for u in el.findall("url"):
                if u.get("type") == "homepage":
                    hp = (u.text or "").strip()
            cats = [c.text.strip() for c in el.findall("categories/category") if c.text]
            name, summary = _untranslated(el, "name"), _untranslated(el, "summary")
            el.clear()
            if not hp.startswith(("http://", "https://")):
                stats["no_homepage"] += 1
                continue
            phrases = [p for p in (fdo_phrase(c) for c in cats) if p]
            if not phrases:
                stats["no_category"] += 1
                phrases = ["desktop app"] if ctype.startswith("desktop") else []
            if lic not in FLATHUB_TEXT_OK:
                summary = ""
                stats["facts_only"] += 1
            for p in phrases:
                out.write(hp, p, name, summary)
    return _finish(stats, out, a, {"attribution": "Flathub AppStream metadata; per-app metadata_license"})


# --------------------------------------------------------------------------- Debian

DEBIAN_SECTIONS = {"web": "web", "mail": "email", "net": "networking", "games": "game",
                   "graphics": "graphics", "sound": "audio", "video": "video", "editors": "text editor",
                   "text": "text processing", "utils": "utility", "admin": "system administration",
                   "science": "science", "math": "mathematics", "education": "education",
                   "database": "database", "httpd": "web server", "news": "news", "comm": "communication",
                   "hamradio": "ham radio", "electronics": "electronics", "shells": "shell",
                   "vcs": "version control", "devel": "development tool",
                   "interpreters": "programming language", "otherosfs": "emulator"}
DEBTAGS = {"web::browser": "web browser", "web::server": "web server", "web::blog": "blog",
           "web::wiki": "wiki", "web::forum": "forum", "web::cms": "cms", "web::portal": "web portal",
           "web::search-engine": "search engine", "web::webmail": "webmail",
           "mail::user-agent": "email client", "mail::transport-agent": "mail server",
           "network::vpn": "vpn", "network::firewall": "firewall", "use::chatting": "chat",
           "use::downloading": "download manager", "use::playing": "media player",
           "use::editing": "editor", "use::viewing": "viewer", "use::gameplaying": "game",
           "use::organizing": "organizer", "use::learning": "education",
           "use::storing": "backup", "use::timekeeping": "time tracking",
           "use::converting": "file converter", "use::searching": "search",
           "security::cryptography": "cryptography", "security::antivirus": "antivirus",
           "sound::player": "audio player", "sound::recorder": "audio recorder",
           "sound::mixer": "audio mixer", "sound::compression": "audio encoder"}


def debian_stanzas(text):
    """Yield dict per Packages stanza (only the fields we read; continuation lines dropped)."""
    cur: dict = {}
    for line in text:
        line = line.rstrip("\n")
        if not line:
            if cur:
                yield cur
            cur = {}
            continue
        if line[0] in " \t":
            if cur.get("_last") == "Tag":         # Tag: may wrap
                cur["Tag"] += " " + line.strip()
            continue
        k, _, v = line.partition(":")
        cur[k] = v.strip()
        cur["_last"] = k
    if cur:
        yield cur


@_stopping
def cmd_debian(a) -> int:
    import lzma
    host = Polite(1.0, STOP_CODES)
    path = _source(a, DEBIAN_URL, "debian-Packages.xz", host)
    out = KindOut(a.out)
    stats: dict = {"packages": 0, "with_homepage": 0, "with_tag": 0, "skipped_section": 0}
    opener = lzma.open if path.endswith(".xz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as f:
        for st in debian_stanzas(f):
            stats["packages"] += 1
            if st.get("Tag"):
                stats["with_tag"] += 1
            hp = st.get("Homepage", "")
            if not hp.startswith(("http://", "https://")):
                continue
            stats["with_homepage"] += 1
            sec = st.get("Section", "").rsplit("/", 1)[-1]
            phrases = []
            if sec in DEBIAN_SECTIONS:
                phrases.append(DEBIAN_SECTIONS[sec])
            for t in re.split(r"[,\s]+", st.get("Tag", "")):
                if t in DEBTAGS:
                    phrases.append(DEBTAGS[t])
                elif t.startswith("field::"):
                    phrases.append(t[7:].replace("-", " "))
                elif t.startswith("game::"):
                    phrases.append(t[6:].replace("-", " ") + " game")
            if not phrases:
                stats["skipped_section"] += 1
                continue
            synopsis = st.get("Description", "")      # first line only; the long text is per-package licensed
            for p in phrases:
                out.write(hp, p, st.get("Package", ""), synopsis)
    return _finish(stats, out, a, {"attribution": "Debian package metadata (Packages, main), "
                                   "https://www.debian.org/"})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("ut1", "homebrew", "publicapis", "flathub", "debian"):
        k = sub.add_parser(name)
        k.add_argument("--out", required=True)
        k.add_argument("--src-file", help="parse this local file instead of downloading "
                       "(homebrew: formula.json,cask.json)")
        k.add_argument("--work", default=os.path.expanduser("~/work"))
        k.add_argument("--stats")
        if name == "ut1":
            k.add_argument("--unsafe-out", required=True)
            k.add_argument("--evidence", help="copy the tarball's README/LICENSE files here")
    d = sub.add_parser("dmoz")
    d.add_argument("--out", required=True)
    d.add_argument("--rdf", help="parse this local content.rdf.u8.gz instead of downloading")
    d.add_argument("--src", help="download this URL instead of searching archive.org")
    d.add_argument("--work", default=os.path.expanduser("~/work"))
    d.add_argument("--stats")
    w = sub.add_parser("awesome")
    w.add_argument("--out", required=True)
    w.add_argument("--lists", required=True)
    w.add_argument("--readme-dir", help="local <owner>__<repo>/<file> tree instead of fetching")
    w.add_argument("--gap", type=float, default=0.6, help="seconds between GitHub requests (>= 0.5)")
    w.add_argument("--deadline-min", type=float, default=0)
    w.add_argument("--limit", type=int, default=0)
    w.add_argument("--stats")
    a = ap.parse_args(argv)
    if a.cmd == "awesome" and a.gap < 0.5:
        ap.error("--gap must be >= 0.5 (GitHub <= 2 requests/s)")
    return {"dmoz": cmd_dmoz, "awesome": cmd_awesome, "ut1": cmd_ut1, "homebrew": cmd_homebrew,
            "publicapis": cmd_publicapis, "flathub": cmd_flathub, "debian": cmd_debian}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
