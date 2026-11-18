#!/usr/bin/env python3
"""Assemble the parts of a home-engine release into ONE release folder, sign it, check it (ADR-063).

    assemble.py build   --release-id ID --out DIR  --registry F --signals F --lookup F --places-osm F --typo DIR --wiki DIR
                        --ivf DIR --nomic-dir DIR  (--rerank-dir DIR | --no-reranker)  [--places-overture F] [--hours-web F] [--pictures F] [--kinds F] [--anchor-phrases F] [--facts F]
                        [--sites DIR --pages DIR --anchors DIR] [--stopwords F] [--licence-text F ...] [--mode link|move|copy]
                        [--publish DIR [--compress xz|zstd|none] [--xz-preset 6]]
    assemble.py sign    DIR [DIR ...] --key PRIVATE.pem        sign DIR/manifest.json with an ed25519 key (a TEST key until the owner decides)
    assemble.py verify  DIR [--pubkey PUBLIC.pem]              the engine's own checks + every sha256 + no unlisted file
    assemble.py test-keygen OUT_PREFIX                         a throw-away TEST key pair (OUT_PREFIX.key / OUT_PREFIX.pub)

The folder it writes is the layout the engine reads (SEARCH-RELEASE-FORMAT.md; lookups_release.ReleaseLookups, release_root.py,
sem_ivf.py, search_api.py):

    manifest.json  manifest.json.sig                 release id, from_release=null (a FULL release), stamps, files, licences, transport
    registry.sqlite  signals.bin  lookup.sqlite      the document list, the monthly numbers, the exact lookups
    places-osm.sqlite  places-overture.sqlite  hours-web.sqlite  pictures.sqlite   ONE SQLite file per licence
    kinds.sqlite  anchor-phrases.sqlite              optional, own licence each: the open kinds tier (D2) and what sites call a site
    facts.sqlite                                     optional, CC0: Wikidata facts per article for the answer card
    text/typo  text/wiki  text/sites/{sites,pages,anchors}     Tantivy folders (read-only: lock files are never shipped)
    vectors/ivf                                      the meaning-search folder (sem_ivf.py)
    vectors/pages-ivf                                --pages-ivf: the same over web pages (release/pages_vec.py + ivf_build.py)
    models/nomic-embed-text-v1.5/                    the query encoder the IVF codes were made with (SEARCH_NOMIC_DIR)
    models/rerank-turbo/                             the CPU reranker (SEARCH_CE_DIR); REQUIRED, --no-reranker only for a test fixture
    LICENSES/NOTICE.txt  LICENSES/<text files>       what is in the release under which licence

Files are HARD-LINKED from the parts (`--mode link`, default; `move` renames them; `copy` is explicit): a release costs no second copy.
`--mode auto` links and copies only a file that cannot be linked because it sits on another filesystem (a build container's
model folder is its own bind mount: a link across mounts is EXDEV even on one disk); the pipeline uses it.
Refuses (nothing is left at --out): a missing required part, a part whose stamp (SQLite application_id / format / role / built_release,
Tantivy release.json, signals.bin header, ivf.json) is missing or is not for --release-id, Tantivy built by different versions, a file with no
licence tag, and any failure of release_check.check_release on the result. Same inputs -> same manifest.json bytes (no timestamps;
sorted keys; each file's xz/zstd encoder is single-threaded, files are encoded in parallel: --jobs does not change the bytes).

`--publish DIR` also writes what a download host serves: DIR/manifest.json(+.sig), DIR/files/<path>[.xz|.zst]. The manifest's
`transport` says per file which encoding it is stored in and the size and sha256 of the stored bytes; a file that does not shrink
(IVF codes, the ONNX model) is stored as is. `install.py` (apps/search/installer) reads exactly that.

Production signing: the owner decides who holds the key. This script only ever takes a key PATH; it never creates a key
except `test-keygen`, which refuses a path that does not say "test".
"""
import argparse
import errno
import json
import lzma
import os
import shutil
import sqlite3
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import release_check as rc  # noqa: E402  (stdlib + registry_idents)

ri = rc.ri
IVF_FORMAT = "logout-sem-ivf/1"            # == update_common.IVF_FORMAT == serve/api/sem_ivf.FORMAT (a test pins all three)
NOMIC_DIRNAME = "models/nomic-embed-text-v1.5"
RERANK_DIRNAME = "models/rerank-turbo"              # search_api.CE_DIR (SEARCH_CE_DIR); turning it off costs ~15 pt hit@1 (2026-10-06)
KINDS_MODEL_DIRNAME = "models/bge-small-en-v1.5"    # search_api.MODEL_DIR (SEARCH_MODEL_DIR): embeds the query for the kinds meaning match
RANKER_NAME = "ranker.json"                         # search_api.RANKER_PATH (SEARCH_RANKER): the learned ranker's weights
BRAND_SITES_NAME = "brand-sites.tsv"                # search_api.brand_sites() (SEARCH_BRAND_SITES_FILE): a brand's official site
TYPO_WORDS_NAME = "typo-words.txt"                  # search_api.typo_words() (SEARCH_TYPO_WORDS_FILE): the words of English
GOV_STATES_NAME = "gov-states.json"                 # search_api.gov_states() (SEARCH_GOV_STATES): whose state a government site is
MANIFEST_FORMAT = rc.FORMAT_VERSION

# One tag per file or folder (longest prefix wins). A file with no tag refuses the build (G3: nothing ships untagged).
# SQLite files stay one per licence; nothing is merged across licences.
LICENCES = {
    "registry.sqlite": "CC-BY-SA-4.0 (Wikipedia titles) + CC-BY-SA (Stack Exchange question titles and their authors' display names, "
                       "CC BY-SA 2.5/3.0/4.0 by contribution date; Wikibooks and Wikivoyage page titles, CC BY-SA 4.0) + CC0-1.0 (Wikidata, "
                       "SEC tickers, M8 layer) + public domain (MedlinePlus titles, Project Gutenberg catalog numbers) + third-party site "
                       "titles/URLs; place rows carry only the id",
    "signals.bin": "CC0-1.0 (Wikimedia pageviews/sitelinks, M8 layer); Common Crawl web-graph rank fields (Common Crawl ToU), see its header",
    "lookup.sqlite": "CC-BY-SA-4.0 (Wikipedia leads; UT1 blacklists, Universite Toulouse Capitole: the sites.ut1 junk flag) + CC0-1.0 (Wikidata, clickstream) + public domain (SEC) + M8 layer",
    "places-osm.sqlite": "ODbL-1.0 (OpenStreetMap contributors)",
    # the gap-fill is chosen against OpenStreetMap (what OSM lacks), which OSMF's Horizontal Layers guideline treats as
    # derived from OSM: ODbL as well (owner, 2026-10-09). Overture's own terms stay on its records
    "places-overture.sqlite": "ODbL-1.0 (rows selected against OpenStreetMap) + CDLA-Permissive-2.0 (Overture Maps Foundation; "
                              "Foursquare records Apache-2.0, AllThePlaces records CC0-1.0) + CC0-1.0 (chain branches from All the Places)",
    "hours-web.sqlite": "facts extracted from Common Crawl captures (Common Crawl ToU) + CC0-1.0 (opening hours from All the Places)",
    "pictures.sqlite": "per row: PD / CC0 / CC BY / CC BY-SA (Wikimedia Commons; the licence column)",
    "kinds.sqlite": "CC-BY-SA-4.0 (combined kind-label set: Wikidata CC0, Wikipedia categories/lists CC BY-SA, UT1 CC BY-SA, DMOZ CC BY 3.0, "
                    "Homebrew BSD-2, public-apis MIT, awesome lists per list, Flathub/Debian facts); kind-name vectors from bge-small-en-v1.5 (MIT)",
    "anchor-phrases.sqlite": "CC0-1.0 (M8 Software's layer); Common Crawl anchor phrases (third-party link text, Common Crawl ToU)",
    "facts.sqlite": "CC0-1.0 (Wikidata statements, labels and qualifiers; M8 Software's template table)",
    "text/typo": "CC-BY-SA-4.0 (keys derived from Wikipedia titles) + CC0-1.0",
    "text/wiki": "CC-BY-SA-4.0 (Wikipedia)",
    "text/sites": "CC0-1.0 (M8 Software's layer); third-party words and anchor phrases from Common Crawl (ToU)",
    # 505k of rc9's 587k page documents are open dumps (audit 2026-10-22 G1): the pages index is BY-SA, as an index of
    # Wikipedia is (rc6 audit R4). Longest prefix wins, so sites/ and anchors/ keep the tag above
    "text/sites/pages": "CC-BY-SA-4.0 (Stack Exchange question titles and words, CC BY-SA 2.5/3.0/4.0 by contribution date; "
                        "Wikibooks and Wikivoyage, CC BY-SA 4.0) + public domain (MedlinePlus) + CC0-1.0 (M8 layer); third-party "
                        "page words from Common Crawl (ToU)",
    "vectors/ivf": "CC-BY-SA-4.0 (codes derived from Wikipedia text)",
    "vectors/pages-ivf": "CC-BY-SA-4.0 (codes derived from Stack Exchange, Wikibooks and Wikivoyage text) + public domain (MedlinePlus); "
                         "codes derived from third-party page words from Common Crawl (ToU)",
    NOMIC_DIRNAME: "Apache-2.0 (nomic-embed-text-v1.5, Nomic AI)",
    RERANK_DIRNAME: "Apache-2.0 (jina-reranker-v1-turbo-en, Jina AI; modified: fine-tuned by M8 Software and quantized to int8)",
    KINDS_MODEL_DIRNAME: "MIT (bge-small-en-v1.5, BAAI)",
    RANKER_NAME: "CC0-1.0 (M8 Software: logistic-regression weights fitted on open labels)",
    "english.stop": "BSD-3-Clause (Snowball English stop list, Dr Martin Porter and Richard Boulton, snowballstem.org)",
    GOV_STATES_NAME: "public domain (CISA .gov registry: which state a state, county, city or district government domain belongs to)",
    BRAND_SITES_NAME: "BSD-3-Clause (name-suggestion-index: brand and operator names) + CC0-1.0 (Wikidata: their official websites)",
    TYPO_WORDS_NAME: "CC-BY-4.0 (Open English WordNet 2025: the spelling of each word, built on Princeton WordNet 3.1)",
    "LICENSES": "licence texts and notice written by the assembler",
}

SQLITE_PARTS = {          # arg -> (file, role)
    "registry": ("registry.sqlite", "registry"), "lookup": ("lookup.sqlite", "lookup"), "places_osm": ("places-osm.sqlite", "places-osm"),
    "places_overture": ("places-overture.sqlite", "places-overture"), "hours_web": ("hours-web.sqlite", "hours-web"),
    "pictures": ("pictures.sqlite", "pictures"),
    "kinds": ("kinds.sqlite", "kinds"), "anchor_phrases": ("anchor-phrases.sqlite", "anchor-phrases"),
    "facts": ("facts.sqlite", "facts"),
}
TANTIVY_PARTS = {"typo": "text/typo", "wiki": "text/wiki", "sites": "text/sites/sites", "pages": "text/sites/pages", "anchors": "text/sites/anchors"}
REQUIRED = ("registry", "signals", "lookup", "places_osm", "typo", "wiki", "ivf", "nomic_dir")


class AssembleError(Exception):
    """A refusal: the message says what was wrong; nothing was written at --out."""


# The Overture line names the providers whose records places-overture.sqlite holds, from that file's meta (written by the
# lookup builder from the places release, audit 2026-10-22 G6); overture_notice() puts it in place of this marker.
OVERTURE_LINE = "<overture providers>"
# the file's meta.licence must say what its manifest tag says (G6: rc9's said CDLA only)
META_LICENCE_MUST_MATCH = ("places-overture",)

# Credit and source for everything the tags above name, and how to have a site's description removed (G3, audit
# docs/internal/audits/2026-10-09-silo-licensing-audit.md §5, 2026-10-22 §6). The licence texts are the files beside this notice.
ATTRIBUTION = [
    "Sources and credit",
    "",
    "Wikipedia: contains text from English Wikipedia (https://en.wikipedia.org), (c) Wikipedia contributors, CC BY-SA 4.0",
    "    (https://creativecommons.org/licenses/by-sa/4.0/). Modified: truncated, tokenised, indexed and embedded. Authors: see",
    "    each article's history page.",
    "Wikidata, Wikimedia pageviews and clickstream: CC0 1.0.",
    "Wikimedia Commons: each picture's author, licence and licence address are in its row of pictures.sqlite; the file name",
    "    is its title on https://commons.wikimedia.org. Pictures are resized.",
    "OpenStreetMap: contains information from OpenStreetMap (https://www.openstreetmap.org/copyright), made available under",
    "    the Open Database License (https://opendatacommons.org/licenses/odbl/1-0/). places-overture.sqlite holds the places",
    "    OpenStreetMap lacks and is offered under the same licence.",
    OVERTURE_LINE,
    "UT1 blacklists, Universite Toulouse Capitole (https://dsi.ut-capitole.fr/blacklists/): CC BY-SA 4.0; modified (reduced to a",
    "    flag and to category labels).",
    "Open Directory Project: with content from the Open Directory Project (DMOZ, dmoz.org), (c) AOL Inc., CC BY 3.0 Unported",
    "    (https://creativecommons.org/licenses/by/3.0/); modified (reduced to category labels).",
    "Homebrew: BSD-2-Clause, Copyright (c) 2009-present, Homebrew contributors. public-apis: MIT, Copyright (c) 2022 public-apis.",
    "All the Places (https://www.alltheplaces.xyz), CC0 1.0: chain branches and opening hours, as the brands' own store locators",
    "    state them; a branch we had no place for is in places-overture.sqlite, hours are in hours-web.sqlite. Hours may be out of date.",
    "U.S. Securities and Exchange Commission: company tickers (public information).",
    "Common Crawl (https://commoncrawl.org): site titles, descriptions, link text, word lists, opening hours and web-graph ranks",
    "    were derived from Common Crawl's archive; use is also subject to Common Crawl's Terms of Use",
    "    (https://commoncrawl.org/terms-of-use). A site's title and description are that site's own words and are not licensed",
    "    by M8 Software; they are shown the way a search engine shows them (cut short, beside the site's address, never where",
    "    the site asked not to be shown). Opening hours are as published when captured and may be out of date.",
    "Stack Exchange, CC BY-SA 2.5/3.0/4.0 as marked on each post, by the date of each contribution",
    "    (https://creativecommons.org/licenses/by-sa/2.5/, https://creativecommons.org/licenses/by-sa/3.0/,",
    "    https://creativecommons.org/licenses/by-sa/4.0/). Source: the Stack Exchange Network. The titles, addresses and word",
    "    lists of questions on 30 English Stack Exchange network sites (never Stack Overflow), from the network's official data",
    "    dump (archive.org/details/stackexchange). Each question's author is shown with the question and linked to their",
    "    profile; the question's address links to it and to all its authors. Modified: reduced to a title, an address and",
    "    sorted words.",
    "Wikibooks and Wikivoyage (English): titles, addresses and word lists of pages, (c) their contributors, CC BY-SA 4.0",
    "    (https://creativecommons.org/licenses/by-sa/4.0/). Modified: reduced to a title, an address and sorted words. Authors: see",
    "    each page's history.",
    "MedlinePlus: Source: MedlinePlus, National Library of Medicine (https://medlineplus.gov). Health topic titles and",
    "    addresses; US government work, public domain.",
    "Project Gutenberg: book and author links use Project Gutenberg's catalog (https://www.gutenberg.org), whose catalog data",
    "    are granted to the public domain. No book text is included.",
    "CISA .gov registry (https://github.com/cisagov/dotgov-data, current-full.csv): gov-states.json lists which state each state,",
    "    county, city or district government domain belongs to. A work of the United States government, public domain.",
    "name-suggestion-index (https://github.com/osmlab/name-suggestion-index): the names in brand-sites.tsv, BSD-3-Clause, Copyright",
    "    2026, name-suggestion-index contributors; see BSD-3-Clause-name-suggestion-index.txt beside this notice. The sites in that",
    "    file are Wikidata's official website values, CC0.",
    "Open English WordNet 2025 (https://en-word.net/): typo-words.txt lists its words, reduced to their spelling. Copyright (c)",
    "    2019-present, The Open English WordNet Team, CC BY 4.0 (http://creativecommons.org/licenses/by/4.0/), provided as is, no",
    "    warranty. Based on Princeton WordNet: WordNet 3.1 Copyright 2011 by Princeton University. All rights reserved. See",
    "    LICENSE-OEWN.md and WNDB_License-OEWN.txt beside this notice.",
    "Snowball: english.stop, BSD-3-Clause, Copyright (c) 2001 Dr Martin Porter, Copyright (c) 2002 Richard Boulton.",
    "BAAI bge-small-en-v1.5: MIT, Copyright (c) 2022 staoxiao. nomic-embed-text-v1.5 (Nomic AI) and jina-reranker-v1-turbo-en",
    "    (Copyright (c) 2023 Jina AI GmbH): Apache-2.0.",
    "M8 Software's own layer (rankings, selections, formats): CC0 1.0.",
    "",
    "Site owners: to have your site's description left out of the next release, write to help@logout.cloud.",
]


def overture_notice(meta) -> list:
    """The Overture Maps lines of the NOTICE: "Data from [provider name]" for the providers in places-overture's meta
    (`providers`, a JSON list; None = the release has no such file). A file that does not record them (built before
    2026-10-22) gets Overture's general line, which says so; Foursquare's notice is named whenever Foursquare is, or may
    be, among them."""
    try:
        prov = json.loads((meta or {}).get("providers") or "null")
    except ValueError:
        prov = None
    head = "Overture Maps Foundation (https://overturemaps.org): places, CDLA-Permissive-2.0"
    if prov:
        head += ", data from " + ", ".join(prov) + "."
    elif meta is not None:
        head += ". places-overture.sqlite does not record which providers' records it holds."
    else:
        head += "."
    lines = [head]
    if not prov or "Foursquare" in prov:
        lines.append("    Foursquare records: Copyright 2024 Foursquare Labs, Inc. All rights reserved. Apache-2.0, see "
                     "NOTICE-Foursquare-OS-Places.txt.")
    if not prov or "AllThePlaces" in prov or "All the Places" in prov:
        lines.append("    AllThePlaces records: CC0 1.0.")
    lines.append("    Rows were selected and reformatted.")
    return lines


def licence_of(path: str) -> str:
    for prefix in sorted(LICENCES, key=len, reverse=True):
        if path == prefix or path.startswith(prefix + "/"):
            return LICENCES[prefix]
    raise AssembleError(f"no licence tag for {path}: refusing to build a release with an untagged file")


# ------------------------------------------------------------------------------------------------ stamps of the parts

def sqlite_stamp(path: str, role: str, release: str) -> None:
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        appid = db.execute("PRAGMA application_id").fetchone()[0]
        uv = db.execute("PRAGMA user_version").fetchone()[0]
        meta = dict(db.execute("SELECT key, value FROM meta"))
        db.close()
    except sqlite3.DatabaseError as e:
        raise AssembleError(f"{path}: not a release SQLite file ({e})")
    if appid != rc.APPLICATION_ID or uv != MANIFEST_FORMAT:
        raise AssembleError(f"{path}: stamp is application_id {appid:#x} / format {uv}, a release file has {rc.APPLICATION_ID:#x} / {MANIFEST_FORMAT}")
    if meta.get("role") != role:
        raise AssembleError(f"{path}: stamp says role {meta.get('role')!r}, this part must be {role!r}")
    if meta.get("built_release") != release:
        raise AssembleError(f"{path}: built for release {meta.get('built_release')!r}, this release is {release!r}")
    if role == "registry" and meta.get("layout") != ri.LAYOUT:
        raise AssembleError(f"{path}: registry layout {meta.get('layout')!r}, the engine reads {ri.LAYOUT!r}")
    return meta


def tantivy_stamp(path: str, release: str) -> dict:
    if not os.path.isfile(os.path.join(path, "meta.json")):
        raise AssembleError(f"{path}: not a Tantivy index folder (no meta.json)")
    try:
        side = json.load(open(os.path.join(path, "release.json")))
    except (OSError, ValueError):
        raise AssembleError(f"{path}: no release.json sidecar (the release stamp of a Tantivy index)")
    if side.get("built_release") != release:
        raise AssembleError(f"{path}: built for release {side.get('built_release')!r}, this release is {release!r}")
    if side.get("format") != MANIFEST_FORMAT:
        raise AssembleError(f"{path}: format {side.get('format')!r}, want {MANIFEST_FORMAT}")
    if not str(side.get("tantivy", "")).startswith(rc.ENGINE_TANTIVY):
        raise AssembleError(f"{path}: written by Tantivy {side.get('tantivy')!r}, the engine reads {rc.ENGINE_TANTIVY}x")
    return side


def signals_stamp(path: str, release: str) -> dict:
    with open(path, "rb") as f:
        if f.read(8) != rc.SIGNALS_MAGIC:
            raise AssembleError(f"{path}: not a signals.bin (bad magic)")
        h = json.loads(f.read(struct.unpack("<I", f.read(4))[0]))
    if h.get("built_release") != release:
        raise AssembleError(f"{path}: built for release {h.get('built_release')!r}, this release is {release!r}")
    return h


def ivf_stamp(path: str, release: str) -> None:
    try:
        meta = json.load(open(os.path.join(path, "ivf.json"), encoding="utf-8"))
    except (OSError, ValueError):
        raise AssembleError(f"{path}: no ivf.json (not a meaning-search folder)")
    if meta.get("format") != IVF_FORMAT:
        raise AssembleError(f"{path}: meaning-search format {meta.get('format')!r}, the engine reads {IVF_FORMAT!r}")
    if meta.get("release") != release:
        raise AssembleError(f"{path}: built for release {meta.get('release')!r}, this release is {release!r}")
    for n in ("centroids.u8", "codes.u8", "ids.i32", "offsets.i64"):
        if not os.path.isfile(os.path.join(path, n)):
            raise AssembleError(f"{path}: {n} is missing")
    if os.path.getsize(os.path.join(path, "ids.i32")) != 4 * meta.get("rows", -1):
        raise AssembleError(f"{path}: ids.i32 does not hold the {meta.get('rows')} rows ivf.json says")


# ------------------------------------------------------------------------------------------------ placing files

def place(src: str, dst: str, mode: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if mode == "move":
        shutil.move(src, dst)
    elif mode == "copy":
        shutil.copyfile(src, dst)
    else:
        try:
            os.link(src, dst)
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            if mode == "auto":
                shutil.copyfile(src, dst)
                return
            raise AssembleError(f"{src} and the output are on different filesystems: hard links are impossible; "
                                "put --out beside the parts, or pass --mode copy / --mode move / --mode auto")


def tree_files(root: str):
    """Every regular file under root, sorted, relative; Tantivy lock files are never part of a release."""
    out = []
    for d, dirs, fs in os.walk(root):
        dirs.sort()
        for f in sorted(fs):
            if f.endswith(".lock"):
                continue
            out.append(os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/"))
    return out


# ------------------------------------------------------------------------------------------------ transport encodings

def _digest_and_size(path: str):
    import hashlib
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
            n += len(b)
    return h.hexdigest(), n


def encode_file(src: str, dst: str, encoding: str, preset: int) -> None:
    if encoding == "xz":
        c = lzma.LZMACompressor(format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC64, preset=preset)
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            for b in iter(lambda: fi.read(1 << 22), b""):
                fo.write(c.compress(b))
            fo.write(c.flush())
    elif encoding == "zstd":
        if shutil.which("zstd") is None:
            raise AssembleError("--compress zstd (the default) needs the `zstd` command on this machine: install it "
                                "(apt install zstd / dnf install zstd), or publish with --compress xz")
        r = subprocess.run(["zstd", "-q", "-19", "-T1", "-f", "-o", dst, src], capture_output=True)
        if r.returncode:
            raise AssembleError(f"zstd failed on {src}: {r.stderr.decode()[:200]}")
    else:
        raise AssembleError(f"unknown encoding {encoding!r}")


SUFFIX = {"xz": ".xz", "zstd": ".zst"}


def _store_as_is(src: str, dst: str) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def write_publish(rel: str, manifest_files: list, pub: str, compress: str, preset: int, mode: str,
                  jobs: int = 1, reuse: str | None = None) -> dict:
    """DIR/files/<path>[.xz|.zst]; returns the manifest's `transport` map (only files not stored as is).
    JOBS files are encoded at once (each with its own single-threaded encoder, so the bytes do not depend on JOBS).
    REUSE, a previous release's publish folder: a file whose sha256 it lists unchanged takes its stored bytes from there
    (checked against that manifest's transport sha256) instead of being encoded again. rc12's publish took ~90 min of
    one core."""
    from concurrent.futures import ThreadPoolExecutor
    prev_sha, prev_t = {}, {}
    if reuse:
        pm = json.load(open(os.path.join(reuse, "manifest.json")))
        if (pm.get("transport") or {}).get("compress") == compress:
            prev_sha = {f["path"]: f["sha256"] for f in pm.get("files", [])}
            prev_t = pm["transport"].get("files") or {}

    def one(f):
        p = f["path"]
        src = os.path.join(rel, p)
        dst = os.path.join(pub, "files", p)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if compress == "none" or f["bytes"] == 0:
            _store_as_is(src, dst)
            return p, None, "as is"
        if prev_sha.get(p) == f["sha256"]:
            t = prev_t.get(p)
            if t is None:                        # it did not shrink last time either: the same bytes, the same rule
                _store_as_is(src, dst)
                return p, None, "reused"
            old = os.path.join(reuse, "files", p + SUFFIX[compress])
            if os.path.exists(old) and _digest_and_size(old) == (t["sha256"], t["bytes"]):
                _store_as_is(old, dst + SUFFIX[compress])
                return p, dict(t), "reused"
        tmp = dst + SUFFIX[compress]
        encode_file(src, tmp, compress, preset)
        sha, size = _digest_and_size(tmp)
        if size < 0.97 * f["bytes"]:
            return p, {"encoding": compress, "bytes": size, "sha256": sha}, "encoded"
        os.remove(tmp)                           # does not shrink: stored as is (deterministic rule)
        _store_as_is(src, dst)
        return p, None, "encoded"

    transport, how = {}, {}
    big_first = sorted(manifest_files, key=lambda f: -f["bytes"])   # the longest encodes start first
    with ThreadPoolExecutor(max(1, jobs)) as pool:
        for p, t, h in pool.map(one, big_first):
            how[h] = how.get(h, 0) + 1
            if t is not None:
                transport[p] = t
    print(json.dumps({"stage": "publish", "files": how, "jobs": jobs}), file=sys.stderr)
    return transport


# ------------------------------------------------------------------------------------------------ build

def build(a) -> dict:
    out = os.path.abspath(a.out)
    if os.path.exists(out) and os.listdir(out):
        raise AssembleError(f"{out} exists and is not empty: refusing to mix two releases")
    parts = vars(a)
    for need in REQUIRED:
        if not parts.get(need):
            raise AssembleError(f"missing required part --{need.replace('_', '-')}")
    if a.rerank_dir and a.no_reranker:
        raise AssembleError("--rerank-dir and --no-reranker together: pick one")
    if not a.rerank_dir and not a.no_reranker:
        raise AssembleError("missing required part --rerank-dir (the reranker is part of a home release: without it the engine loses ~15 pt hit@1; "
                            "--no-reranker is for test fixtures only)")
    # rc5 W7: the release carries everything the engine reads. kinds.sqlite's vectors are useless without the model that
    # embeds the query (the match switched itself off, silently, on a box), and the ranker weights came from whatever
    # image ran, not the repo's.
    if a.kinds and not a.kinds_model_dir and not a.no_kinds_model:
        raise AssembleError("--kinds without --kinds-model-dir: the kinds meaning match needs bge-small-en-v1.5 in the release "
                            "(--no-kinds-model is for test fixtures only)")
    if a.kinds_model_dir:
        for n in ("model.onnx", "tokenizer.json"):
            if not os.path.isfile(os.path.join(a.kinds_model_dir, n)):
                raise AssembleError(f"--kinds-model-dir {a.kinds_model_dir}: {n} is missing (search_api._Embedder reads both)")
    ranker = None
    if a.ranker:
        try:
            rj = json.load(open(a.ranker, encoding="utf-8"))
            ranker = {"file": RANKER_NAME, "trained_on": int(rj["trained_on"]), "features": len(rj["features"])}
            if len(rj["weights"]) not in (len(rj["features"]), len(rj["features"]) + 1):
                raise ValueError("weights do not match features")
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise AssembleError(f"--ranker {a.ranker}: not a ranker.json (features, weights, trained_on): {e}")
    elif not a.no_ranker:
        raise AssembleError("missing required part --ranker (apps/search/serve/api/ranker.json: the release names the weights it was "
                            "accepted with; --no-ranker is for test fixtures only)")
    if a.gov_states:
        import gov_states as gsm
        if not os.path.isfile(a.gov_states):
            raise AssembleError(f"--gov-states {a.gov_states}: no such file")
        gerrs = gsm.check(a.gov_states)
        if gerrs:
            raise AssembleError(f"--gov-states {a.gov_states}: " + "; ".join(gerrs) + " (build it with release/gov_states.py build)")
    if a.brand_sites:
        import brand_sites as bsm
        berrs = bsm.check(a.brand_sites)
        if berrs:
            raise AssembleError(f"--brand-sites {a.brand_sites}: " + "; ".join(berrs) + " (build it with release/brand_sites.py build)")
    if a.typo_words:
        import typo_words as twm
        terrs = twm.check(a.typo_words)
        if terrs:
            raise AssembleError(f"--typo-words {a.typo_words}: " + "; ".join(terrs) + " (build it with release/typo_words.py build)")
    if not a.release_id or any(c in a.release_id for c in "/\\ ") or a.release_id.startswith("."):
        raise AssembleError(f"release id {a.release_id!r}: no slashes, spaces or leading dot")
    rid = a.release_id
    overture_meta = None                  # None: no places-overture part
    # ---- every part: present, stamped for THIS release
    for key, (fname, role) in SQLITE_PARTS.items():
        if parts.get(key):
            if not os.path.isfile(parts[key]):
                raise AssembleError(f"--{key.replace('_', '-')} {parts[key]}: no such file")
            meta = sqlite_stamp(parts[key], role, rid)
            if role in META_LICENCE_MUST_MATCH and meta.get("licence") != LICENCES[fname]:
                raise AssembleError(f"{parts[key]}: its meta.licence is {meta.get('licence')!r}, its manifest tag is "
                                    f"{LICENCES[fname]!r}: the two must agree (rebuild it with the lookup builder)")
            if role == "places-overture":
                overture_meta = meta
    if not os.path.isfile(a.signals):
        raise AssembleError(f"--signals {a.signals}: no such file")
    sig_header = signals_stamp(a.signals, rid)
    sides = {}
    for key in TANTIVY_PARTS:
        if parts.get(key):
            sides[key] = tantivy_stamp(parts[key], rid)
    if {s["tantivy"] for s in sides.values()} and len({s["tantivy"] for s in sides.values()}) != 1:
        raise AssembleError("the Tantivy indexes were written by different versions: " + ", ".join(f"{k} {s['tantivy']}" for k, s in sorted(sides.items())))
    if sides["typo"].get("role") != "typo" or sides["wiki"].get("role") != "wiki":
        raise AssembleError("text/typo and text/wiki must carry roles typo and wiki in their release.json")
    if any(parts.get(k) for k in ("sites", "pages", "anchors")) and not (parts.get("sites") and parts.get("pages") and parts.get("anchors")):
        raise AssembleError("the web text indexes come together: give --sites, --pages and --anchors, or none")
    ivf_stamp(a.ivf, rid)
    if a.pages_ivf:
        ivf_stamp(a.pages_ivf, rid)
    for n in ("model.onnx", "tokenizer.json"):
        if not os.path.isfile(os.path.join(a.nomic_dir, n)):
            raise AssembleError(f"--nomic-dir {a.nomic_dir}: {n} is missing (sem_ivf.NomicCPU reads both)")
    if a.rerank_dir:
        if not os.path.isfile(os.path.join(a.rerank_dir, "tokenizer.json")):
            raise AssembleError(f"--rerank-dir {a.rerank_dir}: tokenizer.json is missing (search_api._CrossEncoder reads it)")
        if not any(os.path.isfile(os.path.join(a.rerank_dir, n)) for n in ("model_int8.onnx", "model.onnx")):
            raise AssembleError(f"--rerank-dir {a.rerank_dir}: model_int8.onnx (or model.onnx) is missing (search_api._CrossEncoder reads one)")
    # ---- place them in a work folder beside --out, rename at the end
    work = out + ".partial"
    if os.path.exists(work):
        shutil.rmtree(work)
    os.makedirs(work)
    try:
        for key, (fname, _role) in SQLITE_PARTS.items():
            if parts.get(key):
                place(parts[key], os.path.join(work, fname), a.mode)
        place(a.signals, os.path.join(work, "signals.bin"), a.mode)
        for key, dest in TANTIVY_PARTS.items():
            if parts.get(key):
                for f in tree_files(parts[key]):
                    place(os.path.join(parts[key], f), os.path.join(work, dest, f), a.mode)
        for f in tree_files(a.ivf):
            place(os.path.join(a.ivf, f), os.path.join(work, "vectors", "ivf", f), a.mode)
        for f in tree_files(a.pages_ivf) if a.pages_ivf else ():
            place(os.path.join(a.pages_ivf, f), os.path.join(work, "vectors", "pages-ivf", f), a.mode)
        for f in tree_files(a.nomic_dir):
            place(os.path.join(a.nomic_dir, f), os.path.join(work, NOMIC_DIRNAME, f), a.mode)
        if a.rerank_dir:
            for f in tree_files(a.rerank_dir):
                place(os.path.join(a.rerank_dir, f), os.path.join(work, RERANK_DIRNAME, f), a.mode)
        if a.kinds_model_dir:
            for f in tree_files(a.kinds_model_dir):
                place(os.path.join(a.kinds_model_dir, f), os.path.join(work, KINDS_MODEL_DIRNAME, f), a.mode)
        if a.ranker:
            shutil.copyfile(a.ranker, os.path.join(work, RANKER_NAME))      # a small file from the code tree: copied, never moved
        if a.gov_states:
            shutil.copyfile(a.gov_states, os.path.join(work, GOV_STATES_NAME))   # small, checked above; the web-input stage read the same file
        if a.brand_sites:
            shutil.copyfile(a.brand_sites, os.path.join(work, BRAND_SITES_NAME))   # small, checked above
        if a.typo_words:
            shutil.copyfile(a.typo_words, os.path.join(work, TYPO_WORDS_NAME))   # small, checked above
        stop = None
        if a.stopwords:
            place(a.stopwords, os.path.join(work, "english.stop"), a.mode)
            stop = rc._sha256_file(os.path.join(work, "english.stop"))
        os.makedirs(os.path.join(work, "LICENSES"), exist_ok=True)
        # every text a licence in LICENCES asks to travel with the data (release/licenses/), then the caller's
        std = os.path.join(HERE, "licenses")
        for t in sorted(os.path.join(std, f) for f in os.listdir(std)) + sorted(a.licence_text or []):
            shutil.copyfile(t, os.path.join(work, "LICENSES", os.path.basename(t)))
        # ---- file list, licences, NOTICE (written first so the manifest lists it; it names paths, never hashes)
        paths = sorted(p for p in tree_files(work))
        by_lic: dict = {}
        for p in paths:
            by_lic.setdefault(licence_of(p), []).append(p)
        notice = ["Logout home search engine, release " + rid, "",
                  "Every file of this release, grouped by the licence of its content (one SQLite file per licence).", ""]
        for lic in sorted(by_lic):
            notice.append(lic)
            notice += ["    " + p for p in by_lic[lic]]
            notice.append("")
        if a.rerank_dir:
            notice += ["Modification notice (Apache-2.0 section 4): " + RERANK_DIRNAME + " is a modified version of jinaai/jina-reranker-v1-turbo-en",
                       "(Apache-2.0, Jina AI), fine-tuned by M8 Software and quantized to int8.", ""]
        notice += [x for line in ATTRIBUTION for x in (overture_notice(overture_meta) if line == OVERTURE_LINE else [line])] + [""]
        notice.append("The release is signed (manifest.json.sig); manifest.json lists every file with its size and sha256.")
        open(os.path.join(work, "LICENSES", "NOTICE.txt"), "w", encoding="utf-8", newline="\n").write("\n".join(notice) + "\n")
        files = []
        for p in sorted(tree_files(work)):
            sha, size = _digest_and_size(os.path.join(work, p))
            files.append({"path": p, "bytes": size, "sha256": sha, "licence": licence_of(p)})
        licences: dict = {}
        for f in files:
            licences.setdefault(f["licence"], []).append(f["path"])
        analyzer = {"name": sides["typo"].get("analyzer")}
        if stop:
            analyzer["stopwords"] = {"file": "english.stop", "sha256": stop,
                                     "words": sum(1 for w in open(os.path.join(work, "english.stop"), encoding="utf-8") if w.strip())}
        m = {"format": MANIFEST_FORMAT, "release": rid, "from_release": None,
             "engine": {"tantivy": next(iter(s["tantivy"] for s in sides.values())), "analyzer": analyzer,
                        "registry": {"doc_id": "type << 28 | seq", "layout": ri.LAYOUT},
                        "signals": [{"name": x["name"], "dtype": x["dtype"]} for x in sig_header.get("fields", [])],
                        "vectors": {"ivf": IVF_FORMAT, "query_model": NOMIC_DIRNAME}},
             "files": files, "licences": {k: licences[k] for k in sorted(licences)}, "total_bytes": sum(f["bytes"] for f in files)}
        if a.rerank_dir:
            m["engine"]["reranker"] = {"cpu": RERANK_DIRNAME}
        if a.kinds_model_dir:
            m["engine"]["kinds_model"] = KINDS_MODEL_DIRNAME
        if ranker:
            m["engine"]["ranker"] = dict(ranker, sha256=next(f["sha256"] for f in files if f["path"] == RANKER_NAME))
        if a.publish:
            pub = os.path.abspath(a.publish)
            if os.path.exists(pub) and os.listdir(pub):
                raise AssembleError(f"--publish {pub} exists and is not empty")
            m["transport"] = {"layout": "files/<path>[.xz|.zst]", "compress": a.compress,
                              "files": write_publish(work, files, pub + ".partial", a.compress, a.xz_preset, a.mode,
                                                     jobs=a.jobs, reuse=a.reuse_publish)}
        body = json.dumps(m, indent=1, sort_keys=True).encode()
        open(os.path.join(work, "manifest.json"), "wb").write(body)
        errs = verify_dir(work, None)
        if errs:
            raise AssembleError("the assembled release fails its own checks: " + "; ".join(errs[:6]))
        if a.publish:
            open(os.path.join(pub + ".partial", "manifest.json"), "wb").write(body)
            if os.path.isdir(pub):
                os.rmdir(pub)
            os.rename(pub + ".partial", pub)
        if os.path.isdir(out):
            os.rmdir(out)
        os.rename(work, out)
    except BaseException:
        if a.mode == "move":            # the parts were MOVED in: deleting the work folder would delete them
            print(f"assemble: --mode move failed after moving the parts; they are in {work} (nothing was deleted)", file=sys.stderr)
            raise
        shutil.rmtree(work, ignore_errors=True)
        if a.publish:
            shutil.rmtree(os.path.abspath(a.publish) + ".partial", ignore_errors=True)
        raise
    return {"release": rid, "out": out, "files": len(files), "total_bytes": m["total_bytes"],
            "licences": {k: len(v) for k, v in sorted(licences.items())},
            "transport_bytes": (sum(t["bytes"] for t in m["transport"]["files"].values())
                                + sum(f["bytes"] for f in files if f["path"] not in m["transport"]["files"])) if a.publish else None}


# ------------------------------------------------------------------------------------------------ sign / verify

def load_private(path: str):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    try:
        key = serialization.load_pem_private_key(open(path, "rb").read(), password=None)
    except (OSError, ValueError) as e:
        raise AssembleError(f"--key {path}: cannot read a PEM private key ({e})")
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise AssembleError(f"--key {path}: not an ed25519 key")
    return key


def sign(dirs: list, keyfile: str) -> dict:
    out = {}
    key = load_private(keyfile)
    bodies = {open(os.path.join(d, "manifest.json"), "rb").read() for d in dirs}
    if len(bodies) != 1:
        raise AssembleError("the directories hold different manifest.json files: nothing signed")
    body = bodies.pop()
    sig = key.sign(body)
    for d in dirs:
        open(os.path.join(d, "manifest.json.sig"), "wb").write(sig)
        out[d] = len(sig)
    return out


def verify_dir(rel: str, pubkey) -> list:
    errs = rc.check_release(rel, pubkey=pubkey, hashes=True)
    try:
        m = json.load(open(os.path.join(rel, "manifest.json")))
    except (OSError, ValueError):
        return errs
    listed = {f["path"] for f in m.get("files", [])}
    for p in tree_files(rel):
        if p not in listed and p not in ("manifest.json", "manifest.json.sig"):
            errs.append(f"{p}: in the folder but not in the manifest")
    if m.get("from_release") is not None:
        errs.append(f"from_release is {m['from_release']!r}: a full release has none")
    for f in m.get("files", []):
        if not f.get("licence"):
            errs.append(f"{f.get('path')}: no licence tag")
    return errs


def test_keygen(prefix: str) -> dict:
    if "test" not in os.path.basename(prefix).lower():
        raise AssembleError("test-keygen only writes keys whose name says 'test' (production key custody is the owner's decision)")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    k = ed25519.Ed25519PrivateKey.generate()
    open(prefix + ".key", "wb").write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    os.chmod(prefix + ".key", 0o600)
    open(prefix + ".pub", "wb").write(k.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return {"private": prefix + ".key", "public": prefix + ".pub", "note": "TEST KEY ONLY"}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    b = sp.add_parser("build")
    b.add_argument("--release-id", required=True)
    b.add_argument("--out", required=True)
    for k in ("registry", "signals", "lookup", "places-osm", "places-overture", "hours-web", "pictures", "kinds", "anchor-phrases", "facts", "typo", "wiki", "sites", "pages",
              "anchors", "ivf", "nomic-dir", "rerank-dir", "kinds-model-dir", "ranker", "stopwords"):
        b.add_argument("--" + k)
    b.add_argument("--no-reranker", action="store_true", help="test fixtures only: a release without " + RERANK_DIRNAME)
    b.add_argument("--no-kinds-model", action="store_true", help="test fixtures only: --kinds without models/bge-small-en-v1.5")
    b.add_argument("--no-ranker", action="store_true", help="test fixtures only: a release without ranker.json")
    b.add_argument("--gov-states", help="gov-states.json (release/gov_states.py build): which state a government site belongs to")
    b.add_argument("--pages-ivf", help="the pages' meaning-search folder, built for this release id: search_api finds vectors/pages-ivf*")
    b.add_argument("--brand-sites", help="brand-sites.tsv (release/brand_sites.py build): a brand's or operator's official site")
    b.add_argument("--typo-words", help="typo-words.txt (release/typo_words.py build): the words of English for the typo path")
    b.add_argument("--licence-text", action="append")
    b.add_argument("--mode", choices=["link", "move", "copy", "auto"], default="link")
    b.add_argument("--publish")
    # zstd by default (2026-11-14, owner: "do all of this"): about the size of xz but unpacked many times faster, and the
    # installer unpacks it as it downloads (installer/install.py stream_decoder; the program carries zstandard). The
    # `zstd` command must be on the machine that publishes (indexhost has 1.5.4); --compress xz for the old way.
    b.add_argument("--compress", choices=["xz", "zstd", "none"], default="zstd")
    b.add_argument("--xz-preset", type=int, default=6)
    b.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1), help="files encoded at once (the bytes do not depend on it)")
    b.add_argument("--reuse-publish", help="a previous release's publish folder: files with an unchanged sha256 take its stored bytes")
    s = sp.add_parser("sign")
    s.add_argument("dirs", nargs="+")
    s.add_argument("--key", required=True)
    v = sp.add_parser("verify")
    v.add_argument("dir")
    v.add_argument("--pubkey")
    k = sp.add_parser("test-keygen")
    k.add_argument("prefix")
    a = ap.parse_args()
    try:
        if a.cmd == "build":
            r = build(a)
        elif a.cmd == "sign":
            r = {"signed": sign(a.dirs, a.key)}
        elif a.cmd == "verify":
            errs = verify_dir(a.dir, a.pubkey)
            r = {"ok": not errs, "errors": errs}
            print(json.dumps(r, sort_keys=True))
            sys.exit(0 if not errs else 1)
        else:
            r = test_keygen(a.prefix)
    except AssembleError as e:
        print(f"assemble: REFUSED: {e}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(r, sort_keys=True))


if __name__ == "__main__":
    main()
