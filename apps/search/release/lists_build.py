#!/usr/bin/env python3
"""Publish the hand-picked lists, finished answers and shopping departments for installs (`install.py lists update`).

Owner 2026-11-14: "the searchsilos should have all of the same things as public ... they need the updates when we add more or
update the ones they already have". Public and indexhost's engines get these through their store inbox (ship key); an install
gets them from the dataset, under lists/:

    lists/manifest.json(.sig)          kind=lists, signed with the release key (install.LISTS_PUBKEY)
    lists/parts/<name>.sqlite.xz       the first part is the BASE (everything; reset=1), then one DELTA per publish after it

A part holds the store tables (result_store.py: curated, finished, dept) with each body as plain JSON text (the install
compresses it for its store: xz over plain text is about a quarter smaller than over zlib bodies), and lists_meta(k, v).
A delta has only the rows written since the publish before it (`at` above the manifest's `until`); an empty delta is not
published. `--base` starts again from everything and drops the old parts (weekly: what was removed upstream goes then).

    lists_build.py build  --store results.sqlite --out DIR --key LISTS.pem --release R --fits R [--fits R2] [--base]
                          [--manifest CURRENT_MANIFEST.json]    (the published one; none = a base is built)
    lists_build.py upload --out DIR --repo ORG/NAME --token-file F [--dry-run]

Standard library (sqlite3, lzma) plus hf_dataset.sign; upload needs huggingface_hub.
"""
import argparse
import hashlib
import json
import lzma
import os
import sqlite3
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "installer"))
sys.path.insert(0, HERE)
import install as inst  # noqa: E402
import hf_dataset  # noqa: E402  (sign())

NOTICE = ("Search lists chosen ahead of time for names people search: each list names web pages (address, title and a short "
          "description taken from the page or from the open sources of the logout search data release), ordered by Logout's "
          "own models. The descriptions keep the licences of their sources, listed in the release's LICENSES folder; the "
          "selection, order and grades are Logout's and are shared under CC BY-SA 4.0.")
OVERLAP_S = 12 * 3600
BASE_SHARE = float(os.environ.get("LISTS_BASE_SHARE", "0.5"))    # a delta carrying this share of the store becomes a base


def window_share(store: str, release: str, since: float) -> tuple[int, int]:
    """(rows of the release's curated + finished tables written after since, all of them)."""
    src = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    try:
        new = total = 0
        for t in ("curated", "finished"):
            a, b = src.execute(f"SELECT count(*), coalesce(sum(at > ?), 0) FROM {t} WHERE release = ?", (since, release)).fetchone()
            total += a; new += b
        return new, total
    finally:
        src.close()
LICENCE = {"name": "CC BY-SA 4.0 (the selection); sources as in the release", "url": "https://creativecommons.org/licenses/by-sa/4.0/"}


def sha256_file(p: str):
    h, n = hashlib.sha256(), 0
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
            n += len(b)
    return h.hexdigest(), n


def name_of(key: str) -> str:
    """The search a store key is for: "gmail login" and "gmail login\x1ffull" (the submitted form) are one name."""
    return " ".join(key.split("\x1f", 1)[0].lower().split())


def read_allow(path: str | None):
    """The names that may be published (one a line), or None for no filter.: the store also holds answers for
    visitors' searches and for names taken from Amazon's search suggestions; neither may be in a public file."""
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return {" ".join(l.lower().split()) for l in f if l.strip()}


# What has been published, by content (2026-11-17). A delta used to be "rows written since the last publish, and the 12 h
# before it again": the night after a base it re-sent 118,283 answers the base already carried (302 MB; every install
# downloaded it and took 1.4 GB in). Time says when a row was written, not whether an install has it. The publisher now
# keeps <out>/lists/published.sqlite: for every published row the sha1 of its content, without the stamps of what built
# it (eng, pk, dev: a re-stamped answer is the same answer). A delta carries the rows whose content is not there.
STATE = "published.sqlite"
STAMPS = ("eng", "pk", "dev")
# ... and without each result row's reading-model score ("ce"). Measured 2026-11-18 on 200 answers of each of two deltas:
# 108 and 92 differed from the published row in nothing but that number (the same rows in the same order, the same text
# and card); an answer first built on the CPU and rebuilt on the GPU gets it from another build of the model. Nothing
# shown or decided from a stored answer reads it: the page's "How this was ranked" takes its numbers from /explain, the
# engine uses ce only while it builds the list. Row order, row text, card, links, kinds, places, cur_at stay in the hash.
# A field that is null counts as absent: release 2 added "cur_at_card", null for most answers, and that alone made 53% of a
# delta's rows "changed" (1,605 of 3,000 sampled). An install treats the two alike except that a row WITH the null field
# stops being served if a hand-picked list for its card's name appears later; a row without it is kept until rebuilt.
ROW_SCORES = ("ce",)
HASH_V = "2"                                         # the rule the table was made with; another rule = seed it again


def content_hash(table: str, text: bytes) -> bytes:
    if table == "finished":
        try:
            b = json.loads(text)
            if isinstance(b, dict):
                b = {k: v for k, v in b.items() if k not in STAMPS and v is not None}     # a null field is an absent one
                if isinstance(b.get("results"), list):
                    b["results"] = [{k: v for k, v in r.items() if k not in ROW_SCORES} if isinstance(r, dict) else r for r in b["results"]]
                text = json.dumps(b, sort_keys=True, separators=(",", ":")).encode()
        except ValueError:
            pass
    return hashlib.sha1(text).digest()


def state_open(out: str, create: bool = False):
    """The published-content table, or None when there is none yet (then a delta falls back to the write times)."""
    p = os.path.join(out, "lists", STATE)
    if not create and not os.path.isfile(p):
        return None
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE IF NOT EXISTS pub (tbl TEXT NOT NULL, key TEXT NOT NULL, h BLOB NOT NULL, PRIMARY KEY (tbl, key)) WITHOUT ROWID")
    return db


def state_rule(db, write: bool = False) -> None:
    """The table says which rule made its hashes; a table of another rule would call every row changed. Refused, not guessed."""
    if write:
        db.execute("INSERT OR REPLACE INTO pub VALUES ('meta', 'hash', ?)", (HASH_V.encode(),))
        return
    r = db.execute("SELECT h FROM pub WHERE tbl = 'meta' AND key = 'hash'").fetchone()
    if (r[0].decode() if r else "1") != HASH_V:
        raise SystemExit(f"{STATE} was made with content rule {r[0].decode() if r else '1'}, this publisher uses {HASH_V}: run "
                         "`lists_build.py seed --out OUT PART...` with the published parts first (nothing was built)")


def content_share(store: str, release: str, state, allow=None) -> tuple[int, int]:
    """(publishable rows of the store whose content is not what was published, all publishable rows)."""
    src = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    differ = total = 0
    try:
        for t in ("curated", "finished"):
            for key, body in src.execute(f"SELECT key, body FROM {t} WHERE release = ?", (release,)):
                if allow is not None and name_of(key) not in allow:
                    continue
                try:
                    text = zlib.decompress(body)
                except zlib.error:
                    continue
                total += 1
                old = state.execute("SELECT h FROM pub WHERE tbl = ? AND key = ?", (t, key)).fetchone()
                differ += old is None or old[0] != content_hash(t, text)
    finally:
        src.close()
    return differ, total


def state_seed(out: str, parts: list[str]) -> dict:
    """Make the table from published part files (decoded .sqlite, oldest first): what installs hold today."""
    db = state_open(out, create=True)
    db.execute("DELETE FROM pub")
    state_rule(db, write=True)
    n = 0
    for f in parts:
        src = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
        for t in ("curated", "finished"):
            for key, body in src.execute(f"SELECT key, body FROM {t}"):
                text = body if isinstance(body, bytes) else str(body).encode()
                db.execute("INSERT OR REPLACE INTO pub VALUES (?, ?, ?)", (t, key, content_hash(t, text)))
                n += 1
        src.close()
    db.commit()
    rows = db.execute("SELECT count(*) FROM pub WHERE tbl != 'meta'").fetchone()[0]
    db.close()
    return {"seeded": rows, "rows_read": n, "parts": len(parts)}


def write_part(store: str, out_db: str, release: str, since: float, reset: bool, new_after: float = 0.0, dept: bool = False,
               allow=None, state=None) -> dict:
    """Rows of `store` written after `since` (all when reset) -> out_db. -> {table: rows, "until": newest at, "new": rows
    after new_after, "held": rows left out because their name is not in `allow`}."""
    if os.path.exists(out_db):
        os.remove(out_db)
    src = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    dst = sqlite3.connect(out_db)
    dst.execute("PRAGMA journal_mode=OFF")
    dst.execute("CREATE TABLE curated (release TEXT NOT NULL, key TEXT NOT NULL, body BLOB NOT NULL, at REAL NOT NULL, PRIMARY KEY (release, key)) WITHOUT ROWID")
    dst.execute("CREATE TABLE finished (release TEXT NOT NULL, key TEXT NOT NULL, body BLOB NOT NULL, at REAL NOT NULL, PRIMARY KEY (release, key)) WITHOUT ROWID")
    dst.execute("CREATE TABLE dept (key TEXT PRIMARY KEY, p REAL NOT NULL, d TEXT NOT NULL, c REAL NOT NULL, at REAL NOT NULL) WITHOUT ROWID")
    dst.execute("CREATE TABLE lists_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    n, until, new, held, same = {}, since, 0, 0, 0
    if state is not None and reset:
        state.execute("DELETE FROM pub")
        state_rule(state, write=True)
    lo = -1.0 if reset else since
    for t in ("curated", "finished"):
        c = 0
        for key, body, at in src.execute(f"SELECT key, body, at FROM {t} WHERE release = ? AND at > ?", (release, lo)):
            if allow is not None and name_of(key) not in allow:
                held += 1
                continue
            try:
                text = zlib.decompress(body)
                json.loads(text)
            except (zlib.error, ValueError):
                continue                                   # a damaged body is not passed on
            if state is not None:                          # by content: only what the last published state does not hold
                h = content_hash(t, text)
                old = state.execute("SELECT h FROM pub WHERE tbl = ? AND key = ?", (t, key)).fetchone()
                if not reset and old and old[0] == h:
                    same += 1
                    continue
                state.execute("INSERT OR REPLACE INTO pub VALUES (?, ?, ?)", (t, key, h))
            dst.execute(f"INSERT INTO {t} VALUES (?, ?, ?, ?)", (release, key, text, at))
            c += 1
            new += at > new_after
            until = max(until, at)
        n[t] = c
    have = dept and src.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'dept'").fetchone()
    c = 0
    for row in (src.execute("SELECT key, p, d, c, at FROM dept WHERE at > ?", (lo,)) if have else []):
        if state is not None:
            h = hashlib.sha1(json.dumps(row[1:4]).encode()).digest()
            old = state.execute("SELECT h FROM pub WHERE tbl = 'dept' AND key = ?", (row[0],)).fetchone()
            if not reset and old and old[0] == h:
                same += 1
                continue
            state.execute("INSERT OR REPLACE INTO pub VALUES ('dept', ?, ?)", (row[0], h))
        dst.execute("INSERT INTO dept VALUES (?, ?, ?, ?, ?)", row)
        c += 1
        new += row[4] > new_after
        until = max(until, row[4])
    n["dept"] = c
    dst.executemany("INSERT INTO lists_meta VALUES (?, ?)", [("reset", "1" if reset else "0"), ("body", "plain"), ("release", release),
                                                              ("rows_curated", str(n["curated"])), ("rows_finished", str(n["finished"]))])   # a base is checked against them (DB-05)
    dst.commit()
    dst.execute("VACUUM")
    dst.close()
    src.close()
    n["until"], n["new"], n["held"], n["same"] = until, new, held, same
    if state is not None:
        state.commit()
    return n


VERIFY_KEEP_S = 22 * 86400      # a confirmation older than the install's age gate (21 days) is worth nothing


def key_hash(key: str) -> int:
    """48-bit hash of a store key: the same function as result_store.key_hash (a test holds the two together)."""
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:6], "big")


def write_verify(store: str, out_db: str, release: str, until: float, allow=None, now: float | None = None) -> dict:
    """The re-verification file (audit DB-03). put_finished keeps `at` for an answer the builder rebuilt unchanged (so the
    publish does not ship it again) and notes the time of the confirmation in finished_checked; installs refuse a finished
    answer whose newest confirmation is older than 21 days, so without this file the stable answers would stop being served
    on installs 21 days after they were first built. Rows: finished answers with at <= until (they are in the published
    parts) whose confirmation is newer than their change and younger than VERIFY_KEEP_S -> verified_h(h, hr): the 48-bit key
    hash and the hour of the confirmation. About 6 bytes a row in xz: 300,000 rows = about 1.8 MB, replaced at each publish
    (never accumulated), so an install pays it once per publish whatever the number of nights since its last. -> {rows}"""
    now = time.time() if now is None else now
    if os.path.exists(out_db):
        os.remove(out_db)
    src = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    dst = sqlite3.connect(out_db)
    dst.execute("PRAGMA journal_mode=OFF")
    dst.execute("CREATE TABLE verified_h (h INTEGER PRIMARY KEY, hr INTEGER NOT NULL)")
    dst.execute("CREATE TABLE lists_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    c = 0
    try:
        has = src.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'finished_checked'").fetchone()
        for key, chk in (src.execute("SELECT f.key, c.checked FROM finished f JOIN finished_checked c ON c.release = f.release AND c.key = f.key "
                                     "WHERE f.release = ? AND f.at <= ? AND c.checked > f.at AND c.checked > ?", (release, until, now - VERIFY_KEEP_S)) if has else []):
            if allow is not None and name_of(key) not in allow:
                continue
            dst.execute("INSERT OR REPLACE INTO verified_h VALUES (?, ?)", (key_hash(key), int(chk // 3600)))
            c += 1
    finally:
        src.close()
    dst.executemany("INSERT INTO lists_meta VALUES (?, ?)", [("verify", "1"), ("release", release), ("rows", str(c))])
    dst.commit()
    dst.execute("VACUUM")
    dst.close()
    return {"rows": c}


def pack(raw: str) -> dict:
    """xz a built file and describe it for the manifest (the raw file is removed)."""
    sha, size = sha256_file(raw)
    xz = raw + ".xz"
    with open(raw, "rb") as fi, lzma.open(xz, "wb", preset=6) as fo:
        for b in iter(lambda: fi.read(1 << 20), b""):
            fo.write(b)
    os.remove(raw)
    xsha, xsize = sha256_file(xz)
    return {"bytes": size, "sha256": sha, "xz_bytes": xsize, "xz_sha256": xsha}


def build(a) -> dict:
    os.makedirs(os.path.join(a.out, "lists", "parts"), exist_ok=True)
    prev = None
    if a.manifest and os.path.isfile(a.manifest):
        prev = json.load(open(a.manifest, encoding="utf-8"))
    base = a.base or prev is None or prev.get("source_release") != a.release
    overlap = OVERLAP_S
    probe = None if base else state_open(a.out)
    if probe is not None:
        state_rule(probe)
        # with the published-content table, "is this a base?" is asked of the content too: the day after an engine
        # release most rows are REWRITTEN (58% on 2026-11-17) while a fifth CHANGED; by write time that was a 515 MB base
        differ, total = content_share(a.store, a.release, probe, read_allow(a.allow))
        probe.close()
        if total and differ >= BASE_SHARE * total:
            base = True
            print(f"lists: {differ} of {total} publishable rows differ from what is published: a base, not a delta", flush=True)
    elif not base:
        # A delta repeats OVERLAP_S before the last publish (a row built earlier but taken into this store later is not
        # missed). When most of the store was written in that window (a fill or a rebuild of everything, 2026-11-15: a
        # "delta" carried all 301,275 answers, 512 MB, on top of the base), the repeat is dropped; when most of it was
        # written since the last publish itself, a base is published instead (installs would otherwise fetch both).
        # Re-stamped answers (impact_check.py) keep their time, so they never count as written.
        until = float(prev["until"])
        in_window, total = window_share(a.store, a.release, until - OVERLAP_S)
        since_last, _ = window_share(a.store, a.release, until)
        if total and since_last >= BASE_SHARE * total:
            base = True
            print(f"lists: {since_last} of {total} rows are new since the last publish: a base, not a delta", flush=True)
        elif total and in_window >= BASE_SHARE * total:
            overlap = 0
            print(f"lists: {in_window} of {total} rows fall in the {OVERLAP_S // 3600} h repeat: the delta starts at the last publish", flush=True)
    t = time.time()
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(t)) + f"{int(t * 1000) % 1000:03d}Z"     # two publishes never share a name
    name = ("b-" if base else "d-") + stamp
    raw = os.path.join(a.out, "lists", "parts", name + ".sqlite")
    # a delta repeats the last OVERLAP_S before the previous publish: a row built before then but taken into this store
    # after it would otherwise be missed (the install keeps a row only when it is newer than its own, so a repeat is free)
    allow = read_allow(a.allow)
    if allow is not None and a.with_dept:
        raise SystemExit("--with-dept publishes names from Amazon's search suggestions: not together with --allow")
    # With the published-content table a delta reads the whole store and keeps what differs (new_after -1: every row it
    # keeps is new); a base makes the table. Without one (a delta before any base made it) the write times decide, as before.
    state = state_open(a.out, create=base)
    if state is not None and not base:
        n = write_part(a.store, raw, a.release, -1.0, False, -1.0, a.with_dept, allow, state)
        print(f"lists: {n['same']} rows are what installs already hold (same content): not sent again", flush=True)
    else:
        n = write_part(a.store, raw, a.release, 0.0 if base else float(prev["until"]) - overlap, base, 0.0 if base else float(prev["until"]), a.with_dept, allow, state)
    if state is not None:
        state.close()
    if allow is not None:
        print(f"lists: {n['held']} rows held back (their names are not open-data or hand-added names)", flush=True)
    n["until"] = max(n["until"], 0.0 if base else float(prev["until"]))
    vraw = os.path.join(a.out, "lists", "parts", "v-" + stamp + ".sqlite")
    vn = write_verify(a.store, vraw, a.release, n["until"], allow)
    vold = (prev or {}).get("verify") if not base else None
    if not base and not n["new"]:
        os.remove(raw)
        # no new content; a confirmation that moved is still published (a small manifest + verify file), or the installs'
        # stable answers would age out while nothing changes
        if not vn["rows"] or (vold and vold.get("sha256") == sha256_file(vraw)[0]):     # the same confirmations: nothing to publish
            os.remove(vraw)
            return {"published": False, "why": "nothing new since the last publish", "until": prev["until"]}
        m = dict(prev)
        m["version"] = stamp
        vpart = dict(pack(vraw), name="v-" + stamp, rows=vn["rows"], checked_until=time.time())
        m["verify"] = vpart
        body = (json.dumps(m, indent=1, sort_keys=True) + "\n").encode()
        inst.check_lists_manifest(body)
        with open(os.path.join(a.out, "lists", "manifest.json"), "wb") as f:
            f.write(body)
        with open(os.path.join(a.out, "lists", "manifest.json.sig"), "wb") as f:
            f.write(hf_dataset.sign(a.key, body))
        return {"published": True, "part": None, "base": False, "verify_rows": vn["rows"], "xz_bytes": vpart["xz_bytes"], "parts": len(m["parts"]),
                "download_from_scratch": sum(p["xz_bytes"] for p in m["parts"]) + vpart["xz_bytes"]}
    part = dict(pack(raw), name=name, rows={k: n[k] for k in ("curated", "finished", "dept")})
    xsize = part["xz_bytes"]
    vpart = dict(pack(vraw), name="v-" + stamp, rows=vn["rows"], checked_until=time.time()) if vn["rows"] else None
    if base:
        part["reset"] = True
        parts = [part]
    else:
        parts = list(prev["parts"]) + [part]
    m = {"format": inst.LISTS_FORMAT, "kind": "lists", "version": stamp, "source_release": a.release, "fits_releases": a.fits,
         "base": parts[0]["name"], "until": n["until"], "parts": parts, "licence": LICENCE, "notice": NOTICE}
    if vpart:
        m["verify"] = vpart
    body = (json.dumps(m, indent=1, sort_keys=True) + "\n").encode()
    inst.check_lists_manifest(body)                  # what we write must be what the installer accepts
    with open(os.path.join(a.out, "lists", "manifest.json"), "wb") as f:
        f.write(body)
    with open(os.path.join(a.out, "lists", "manifest.json.sig"), "wb") as f:
        f.write(hf_dataset.sign(a.key, body))
    return {"published": True, "part": name, "base": base, "rows": part["rows"], "xz_bytes": xsize, "parts": len(parts),
            "download_from_scratch": sum(p["xz_bytes"] for p in parts) + (vpart["xz_bytes"] if vpart else 0), "verify_rows": vn["rows"]}


BETA_MANIFEST = ("manifest-beta.json", "manifest-beta.json.sig")


def upload(a) -> dict:
    beta = getattr(a, "channel", "stable") == "beta"      #: a beta publish moves only manifest-beta.json; promote with `promote`
    m = json.load(open(os.path.join(a.out, "lists", "manifest.json"), encoding="utf-8"))
    want = {f"lists/parts/{p['name']}.sqlite.xz" for p in m["parts"] + ([m["verify"]] if m.get("verify") else [])}
    local = [p for p in want if os.path.isfile(os.path.join(a.out, p))]
    plan = {"repo": a.repo, "version": m["version"], "upload": sorted(local), "bytes": sum(os.path.getsize(os.path.join(a.out, p)) for p in local)}
    if a.dry_run:
        return dict(plan, dry_run=True)
    from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi
    api = HfApi(token=open(a.token_file).read().strip())
    everything = set(api.list_repo_files(a.repo, repo_type="dataset"))
    remote = {p for p in everything if p.startswith("lists/parts/")}
    ops = [CommitOperationAdd(path_in_repo=p, path_or_fileobj=os.path.join(a.out, p)) for p in sorted(local) if p not in remote]
    if ops:
        api.create_commit(a.repo, repo_type="dataset", operations=ops, commit_message=f"lists {m['version']}: parts")
    # the manifest last, so a reader never sees a manifest naming a part that is not there yet; parts no manifest names go
    names = [(b, l) for b, l in zip(BETA_MANIFEST, ("manifest.json", "manifest.json.sig"))] if beta else \
        [(n, n) for n in ("manifest.json", "manifest.json.sig")]
    if not beta and f"lists/{BETA_MANIFEST[0]}" in everything:      # a stable publish moves beta with it: beta is never behind stable
        names += [(b, l) for b, l in zip(BETA_MANIFEST, ("manifest.json", "manifest.json.sig"))]
    ops = [CommitOperationAdd(path_in_repo=f"lists/{rn}", path_or_fileobj=os.path.join(a.out, "lists", ln)) for rn, ln in names]
    # parts the OTHER channel's manifest still names are kept (the stable manifest while publishing beta, and the reverse)
    keep = set()
    other = ("manifest.json" if beta else BETA_MANIFEST[0])
    try:
        from huggingface_hub import hf_hub_download
        keep = {f"lists/parts/{p['name']}.sqlite.xz"
                for p in json.load(open(hf_hub_download(a.repo, f"lists/{other}", repo_type="dataset", token=api.token)))["parts"]}
    except Exception:  # noqa: BLE001 - no other manifest: nothing to keep
        pass
    ops += [CommitOperationDelete(path_in_repo=p) for p in sorted(remote - want - keep)]
    api.create_commit(a.repo, repo_type="dataset", operations=ops, commit_message=f"lists {m['version']}")
    for p in local:                                  # uploaded: the local copy is not needed again
        os.remove(os.path.join(a.out, p))
    return dict(plan, removed=sorted(remote - want - keep), channel="beta" if beta else "stable")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    b = sp.add_parser("build")
    b.add_argument("--store", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--key", required=True, help="PEM ed25519 private key (the release key)")
    b.add_argument("--release", required=True, help="the release the lists were chosen on (the store's curated/finished rows)")
    b.add_argument("--fits", action="append", required=True, help="a release id or pattern an install may use them with (repeatable)")
    b.add_argument("--base", action="store_true", help="start again from everything; the old parts are dropped on upload")
    b.add_argument("--with-dept", action="store_true", help="also the shopping departments (names from Amazon's search suggestions: "
                   "HELD until the owner decides on their licence)")
    b.add_argument("--manifest", help="the manifest now published (default: OUT/lists/manifest.json)")
    b.add_argument("--allow", help="a file of names (one a line): only their lists and answers are published (open-data "
                                   "and hand-added names; no visitor searches, no names from Amazon's suggestions)")
    sd = sp.add_parser("seed", help="make the published-content table from the part files installs hold now (decoded .sqlite, oldest first)")
    sd.add_argument("--out", required=True)
    sd.add_argument("parts", nargs="+")

    u = sp.add_parser("upload")
    u.add_argument("--out", required=True)
    u.add_argument("--repo", required=True)
    u.add_argument("--token-file")
    u.add_argument("--dry-run", action="store_true")
    u.add_argument("--channel", choices=("stable", "beta"), default="stable",
                   help="beta: parts go up as usual, manifest-beta.json is the pointer moved (installs on the beta channel follow it)")
    pr = sp.add_parser("promote", help="manifest.json := manifest-beta.json (one commit, nothing uploaded): the beta lists become the stable ones")
    pr.add_argument("--repo", required=True)
    pr.add_argument("--token-file", required=True)
    pr.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "seed":
        print(json.dumps(state_seed(a.out, a.parts), sort_keys=True))
        return 0
    if a.cmd == "promote":
        from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download
        api = HfApi(token=open(a.token_file).read().strip())
        got = [hf_hub_download(a.repo, f"lists/{n}", repo_type="dataset", token=api.token) for n in BETA_MANIFEST]
        v = json.load(open(got[0]))["version"]
        if not a.dry_run:
            api.create_commit(a.repo, repo_type="dataset", commit_message=f"lists {v} (promoted from beta)",
                              operations=[CommitOperationAdd(path_in_repo=f"lists/{n}", path_or_fileobj=g)
                                          for n, g in zip(("manifest.json", "manifest.json.sig"), got)])
        print(json.dumps({"promote": v, "dry_run": a.dry_run}))
        return 0
    if a.cmd == "build":
        a.manifest = a.manifest or os.path.join(a.out, "lists", "manifest.json")
        r = build(a)
    else:
        if not a.dry_run and not a.token_file:
            raise SystemExit("--token-file is needed")
        r = upload(a)
    print(json.dumps(r, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
