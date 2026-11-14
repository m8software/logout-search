"""The engine-side release checks, with no build-side imports.

What the engine and update_apply.py read from every stamp and what they refuse: the manifest (and its
signature when a key is given), the file list (present, right size; with hashes=True also sha256),
every SQLite file's application_id / format / role / built_release, every Tantivy folder's release.json
sidecar, signals.bin's header. Imports only the standard library and registry_idents (itself
sqlite3-only): no psycopg, no exporter. release_v1.check_release is this function; the build side
keeps its names (ENGINE_FORMATS ...) by importing them from here.

FORMAT_VERSION and APPLICATION_ID duplicate export.py's (export.py cannot be imported on a client);
tests/unit/test-search-release-check.sh fails if the two ever differ.
"""
import hashlib
import json
import os
import re
import sqlite3
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import registry_idents as ri  # noqa: E402

# = docs_policy.PUBLISH_POLICY, spelled out: this file travels with the installer and imports nothing else
# (tests/unit/test-search-docs-policy.sh holds the two equal)
DESCR_PUBLISH_POLICY = "max=160;unchecked=drop;wayback=drop;noindex=drop"

FORMAT_VERSION = 1
APPLICATION_ID = 0x6C6F7331           # "los1"
TYPE_SHIFT = ri.TYPE_SHIFT
SIGNALS_MAGIC = b"LOSIGNL1"


def _sha256_file(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


# Engine-side check (reference for the engine and the updater, T6/T7): what it reads from every
# stamp and what it refuses. The engine refuses the WHOLE release on any failure, with the message.
ENGINE_FORMATS = (1, 1)                 # format versions this engine reads (inclusive range)
ENGINE_TANTIVY = "0.26."                # index files readable by the bundled Tantivy
SQLITE_ROLES = {"registry.sqlite": "registry", "pictures.sqlite": "pictures"}
REQUIRED_SIGNALS = {"wiki.sitelinks": "<u2", "site.cc_hc_pos": "<u4"}
# the site popularity rank: our own (G3 D2, importer/own_rank.py) or, in a release built before that, Tranco's.
# The engine and the updater read either (lookups_release.Signals) so a release in the field still applies.
RANK_SIGNALS = ("site.own_rank", "site.tranco_rank")


SUGGEST_SITE_CUT = 100000     # release/suggest_own.py SITE_CUT


def _signal_u4(sp: str, name: str):
    """One <u4 field of signals.bin as bytes (4 per document sequence number), or None when the file has no such field."""
    with open(sp, "rb") as f:
        if f.read(8) != SIGNALS_MAGIC:
            return None
        n = struct.unpack("<I", f.read(4))[0]
        h = json.loads(f.read(n))
        for x in h.get("fields", []):
            if x.get("name") == name and x.get("dtype") == "<u4":
                f.seek(12 + n + x["offset"])
                return f.read(4 * x["count"])
    return None


def suggest_own_errors(rel: str, path: str, db, meta: dict) -> list:
    """The publish gate for suggestions (rc5 plan W2): the lookup file must say its suggestion set was built from our own
    rank with no browser history (release/suggest_own.py -> build.py), and every site it can suggest must be inside that
    rank's top SUGGEST_SITE_CUT in THIS release's signals.bin. The second check reads the shipped files, not a label: a
    set selected by Tranco or by a history holds sites our rank does not have that high."""
    errs = []
    # G3 D4c: a published lookup file was staged under the description policy (build.py stage_capture_policy)
    if meta.get("descr_policy") != DESCR_PUBLISH_POLICY:
        errs.append(f"{path}: site descriptions were not staged under the publish policy (descr_policy "
                    f"{meta.get('descr_policy')!r}, expected {DESCR_PUBLISH_POLICY!r}): restage without DESCR_UNCHECKED=keep (G3 D4c)")
    if meta.get("suggest_rank_source") != "own_rank":
        errs.append(f"{path}: suggestions were not built from our own rank (suggest_rank_source "
                    f"{meta.get('suggest_rank_source')!r}): run release/suggest_own.py before staging (G3 D2)")
    if meta.get("suggest_history") != "none":
        errs.append(f"{path}: suggestions may be selected by a browser history (suggest_history {meta.get('suggest_history')!r})")
    sp = os.path.join(rel, "signals.bin")
    own = _signal_u4(sp, "site.own_rank") if os.path.exists(sp) else None
    if own is None:
        return errs + [f"{path}: no site.own_rank in signals.bin to check the suggested sites against"]
    try:
        rows = db.execute("SELECT i.doc_id FROM suggest_items i JOIN dict d ON d.id = i.kind WHERE d.s = 'site'").fetchall()
    except sqlite3.DatabaseError as e:
        return errs + [f"{path}: suggest_items unreadable: {e}"]
    mask, bad, first = (1 << TYPE_SHIFT) - 1, 0, None
    for (doc_id,) in rows:
        seq = doc_id & mask
        r = struct.unpack_from("<I", own, 4 * seq)[0] if 4 * seq + 4 <= len(own) else 0
        if doc_id >> TYPE_SHIFT != ri.TYPES["site"] or not 1 <= r <= SUGGEST_SITE_CUT:
            bad, first = bad + 1, first if first is not None else (doc_id, r)
    if bad:
        errs.append(f"{path}: {bad} of {len(rows)} suggested sites are outside our rank's top {SUGGEST_SITE_CUT} "
                    f"(first: doc_id {first[0]}, rank {first[1]}; 0 = none) — the set was not selected by our rank (G3 D2)")
    return errs


# The publish gate's "nothing internal" rule (owner, 2026-10-23: the dataset names no tool it does not use; owner,
# 2026-10-10: nothing personal, no machine names, no tracker numbers). It reads what a release says about itself (SQLite
# meta, tombstone release ids, JSON sidecars, signals.bin's header, manifest.json, NOTICE.txt), never the third-party
# rows, where these letters occur as ordinary words.
INTERNAL_WORDS = re.compile(r"(?i)jev|postgres|pg_dump|(?<![a-z0-9])(testbox|buildbox|indexhost|homeserver|owner)(?![a-z0-9])|"
                            r"docs/(legal|internal)/|(?<![a-z0-9])G\d+ D\d+|#\d{3,4}(?![0-9])|(?<![0-9.])10\.(8|47)\.0\.\d|"
                            r"/home/|/opt/logout")


def internal_errors(rel: str, listed) -> list:
    """Where a release's own words name a machine, a person, a tracker number or a tool it does not use."""
    errs = []

    def scan(where: str, text: str):
        m = INTERNAL_WORDS.search(text or "")
        if m:
            errs.append(f"{where}: says {m.group(0)!r} (…{text[max(0, m.start() - 40):m.end() + 40]}…): internal words do not ship")
    for path in sorted(listed):
        full = os.path.join(rel, path)
        if path.endswith(".sqlite"):
            db = sqlite3.connect(f"file:{full}?mode=ro", uri=True)
            try:
                for k, v in db.execute("SELECT key, value FROM meta"):
                    if not str(k).startswith(("town:", "pid_hwm:")):
                        scan(f"{path} meta {k}", f"{k} {v}")
                if path == "registry.sqlite":
                    for (v,) in db.execute("SELECT DISTINCT removed_release FROM tombstones"):
                        scan(f"{path} tombstones", str(v))
            except sqlite3.DatabaseError as e:
                errs.append(f"{path}: {e}")
            db.close()
        elif path.endswith((".json", "NOTICE.txt", "SHA256SUMS", "logout-analyzer.txt")) and not path.endswith("tokenizer.json"):
            scan(path, open(full, encoding="utf-8", errors="replace").read())
    scan("manifest.json", open(os.path.join(rel, "manifest.json"), encoding="utf-8").read())
    sp = os.path.join(rel, "signals.bin")
    if os.path.exists(sp):
        with open(sp, "rb") as f:
            if f.read(8) == SIGNALS_MAGIC:
                scan("signals.bin header", f.read(struct.unpack("<I", f.read(4))[0]).decode("utf-8", "replace"))
    return errs


def check_release(rel: str, pubkey: str = None, hashes: bool = False, tranco_free: bool = False) -> list:
    """The refusals above. tranco_free=True (release_v1.py check --tranco-free, the publish gate of G3 D2) also
    refuses a signals.bin with no site.own_rank or with any field named for, sourced from, licensed as or noted
    as Tranco-derived, and any internal word in what the release says about itself (internal_errors). The default accepts
    all of these, so a release already in the field still applies."""
    errs = []
    try:
        body = open(os.path.join(rel, "manifest.json"), "rb").read()
        m = json.loads(body)
    except (OSError, ValueError) as e:
        return [f"manifest.json unreadable: {e}"]
    if pubkey:
        from cryptography.hazmat.primitives import serialization
        try:
            serialization.load_pem_public_key(open(pubkey, "rb").read()).verify(
                open(os.path.join(rel, "manifest.json.sig"), "rb").read(), body)
        except Exception as e:  # noqa: BLE001 — any failure is a refusal
            errs.append(f"manifest signature invalid ({type(e).__name__})")
    fmt = m.get("format")
    if not (isinstance(fmt, int) and ENGINE_FORMATS[0] <= fmt <= ENGINE_FORMATS[1]):
        errs.append(f"release format {fmt} not in {ENGINE_FORMATS}: update the engine")
    listed = {f["path"]: f for f in m.get("files", [])}
    for path, f in listed.items():
        p = os.path.join(rel, path)
        if not os.path.exists(p) or os.path.getsize(p) != f["bytes"]:
            errs.append(f"{path}: missing or wrong size")
        elif hashes and _sha256_file(p) != f.get("sha256"):
            errs.append(f"{path}: sha256 differs from the manifest")
    rr = (m.get("engine", {}) or {}).get("reranker")
    if rr is not None:                       # a release that says it ships the reranker must list the files the engine loads from it
        d = rr.get("cpu") if isinstance(rr, dict) else None
        if not isinstance(d, str) or not d.startswith("models/") or ".." in d.split("/"):
            errs.append(f"engine.reranker: {rr!r} does not name a models/<dir> folder")
        else:
            if f"{d}/tokenizer.json" not in listed:
                errs.append(f"{d}/tokenizer.json: the manifest declares a reranker but does not list it")
            if f"{d}/model_int8.onnx" not in listed and f"{d}/model.onnx" not in listed:
                errs.append(f"{d}/model_int8.onnx: the manifest declares a reranker but lists no model file")
    for path in listed:
        if not path.endswith(".sqlite"):
            continue
        db = sqlite3.connect(f"file:{os.path.join(rel, path)}?mode=ro", uri=True)
        try:
            if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                errs.append(f"{path}: not a logout release file (application_id)")
            uv = db.execute("PRAGMA user_version").fetchone()[0]
            if uv != fmt:
                errs.append(f"{path}: format {uv} != release format {fmt}")
            meta = dict(db.execute("SELECT key, value FROM meta"))
            want = SQLITE_ROLES.get(path, path.rsplit(".", 1)[0])
            if meta.get("role") != want:
                errs.append(f"{path}: role {meta.get('role')!r}, expected {want!r}")
            if not meta.get("built_release"):
                errs.append(f"{path}: no built_release")
            if tranco_free and meta.get("role") == "lookup":
                errs += suggest_own_errors(rel, path, db, meta)
            if path == "registry.sqlite" and meta.get("layout") != ri.LAYOUT:
                errs.append(f"{path}: registry layout {meta.get('layout')!r}, the engine reads {ri.LAYOUT!r}")
        except sqlite3.DatabaseError as e:
            errs.append(f"{path}: {e}")
        db.close()
    stop_sha = (m.get("engine", {}).get("analyzer", {}).get("stopwords", {}) or {}).get("sha256")
    for d, _, fs in os.walk(os.path.join(rel, "text")):
        if "meta.json" not in fs:
            continue
        rp = os.path.relpath(d, rel)
        try:
            side = json.load(open(os.path.join(d, "release.json")))
        except (OSError, ValueError):
            errs.append(f"{rp}: no release.json sidecar")
            continue
        if side.get("format") != fmt:
            errs.append(f"{rp}: format {side.get('format')} != {fmt}")
        if not str(side.get("tantivy", "")).startswith(ENGINE_TANTIVY):
            errs.append(f"{rp}: built with Tantivy {side.get('tantivy')}, engine reads {ENGINE_TANTIVY}x")
        # the typo index holds whole keys under the raw tokenizer: it is built with no stop list and its sidecar carries none
        # (build.py typo(), typo_month.py). Every other index must name the release's own list; a stamp, where present, must match.
        if stop_sha and side.get("stopwords_sha256") != stop_sha and ("stopwords_sha256" in side or side.get("role") != "typo"):
            errs.append(f"{rp}: stop list differs from the release's english.stop")
    sp = os.path.join(rel, "signals.bin")
    if os.path.exists(sp):
        with open(sp, "rb") as f:
            if f.read(8) != SIGNALS_MAGIC:
                errs.append("signals.bin: bad magic")
            else:
                h = json.loads(f.read(struct.unpack("<I", f.read(4))[0]))
                if h.get("format") != fmt:
                    errs.append(f"signals.bin: format {h.get('format')} != {fmt}")
                have = {x["name"]: x["dtype"] for x in h.get("fields", [])}
                for name, dt in REQUIRED_SIGNALS.items():
                    if have.get(name) != dt:
                        errs.append(f"signals.bin: field {name} missing or not {dt}")
                if not any(have.get(n) == "<u4" for n in RANK_SIGNALS):
                    errs.append(f"signals.bin: no site rank ({' or '.join(RANK_SIGNALS)}) as <u4")
                if tranco_free:
                    for x in h.get("fields", []):
                        if "tranco" in f"{x.get('name')} {x.get('source')} {x.get('licence')} {x.get('note')}".lower():
                            errs.append(f"signals.bin: field {x.get('name')} is Tranco-derived (G3 D2)")
                if h.get("type_shift") != TYPE_SHIFT:
                    errs.append("signals.bin: doc_id layout differs")
    if "gov-states.json" in listed:          # whose state a government site is: the shape the engine reads, or the box ignores it
        import gov_states
        errs += [f"gov-states.json: {e}" for e in gov_states.check(os.path.join(rel, "gov-states.json"))]
    if "brand-sites.tsv" in listed:          # a brand's official site
        import brand_sites
        errs += [f"brand-sites.tsv: {e}" for e in brand_sites.check(os.path.join(rel, "brand-sites.tsv"))]
    if "typo-words.txt" in listed:           # the words of English
        import typo_words
        errs += [f"typo-words.txt: {e}" for e in typo_words.check(os.path.join(rel, "typo-words.txt"))]
    if tranco_free:
        errs += internal_errors(rel, listed)
    return errs
