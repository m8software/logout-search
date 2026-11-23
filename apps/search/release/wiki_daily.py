#!/usr/bin/env python3
"""Unattended daily Wikipedia refresh orchestrator (catch-up / incr -> embed -> text/ivf/sqlite release -> phase 4 -> update pkg).

    wiki_daily.py run --day YYYYMMDD --state STATE.sqlite --prev-release DIR --out-release DIR
                      --out-pkg DIR --work DIR [options] [--dry-run]

Reads daily Wikimedia incremental dump + delete log (with 8-hour vandalism hold), runs wikidelta.py incr, prepares embed
texts for a pluggable --embed-cmd (stubbed in offline tests; GPU pod / runner in production), updates SQLite release tables
and text/vector deltas, runs Phase 4 release_delta.py against stored answers, builds and self-tests a row-level update
package via update_pkg.py, and writes/updates signed package.json + latest.json manifest entries.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import types
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
SEARCH_DIR = os.path.dirname(HERE)
IMPORTER_DIR = os.path.join(SEARCH_DIR, "importer")
TWIN_DIR = os.path.join(SEARCH_DIR, "twin")
LOOKUP_TOOLS_DIR = os.path.join(SEARCH_DIR, "tools", "sqlite_lookups")

for _d in (HERE, IMPORTER_DIR, LOOKUP_TOOLS_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)

try:
    import psycopg  # noqa: F401 - release modules import it at top level without connecting here
except ImportError:
    _pg = types.ModuleType("psycopg")
    _pg.Error = Exception
    sys.modules["psycopg"] = _pg

import hf_dataset as hd  # noqa: E402
import trgm  # noqa: E402
import update_common as uc  # noqa: E402
import wikidelta as wd  # noqa: E402

INCR_URL_TEMPLATE = "https://dumps.wikimedia.org/other/incr/enwiki/{day}/enwiki-{day}-pages-meta-hist-incr.xml.bz2"
UA = wd.UA
N_TITLE = 1
CARD_LEAD_CHARS = 1000
N_ALIAS = 2


def log(msg: str) -> None:
    print(f"[wiki-daily {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _run(cmd: list[str], env: dict | None = None) -> str:
    merged = dict(os.environ)
    if env:
        merged.update(env)
    r = subprocess.run(cmd, env=merged, capture_output=True, text=True)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip()[-1200:]
        raise RuntimeError(f"command failed ({r.returncode}): {' '.join(cmd)}\n{tail}")
    return r.stdout


def fetch_incremental(pages: str | None, day: str, url_template: str, work_incr_dir: str, sha1sums: str | None = None,
                      dry_run: bool = False) -> dict:
    """Resolve local/cmd dump path or download from Wikimedia (resumable/atomic .part file, optional sha1 check)."""
    os.makedirs(work_incr_dir, exist_ok=True)
    target = pages or url_template.format(day=day)
    if target.startswith("cmd:"):
        return {"source": target, "path": target, "fetched": False}
    if target.startswith("file://"):
        local = target[7:]
        if not os.path.isfile(local):
            raise RuntimeError(f"incremental dump missing: {local}")
        return {"source": target, "path": local, "bytes": os.path.getsize(local), "fetched": False}
    if not target.startswith(("http://", "https://")):
        if not os.path.isfile(target):
            raise RuntimeError(f"incremental dump missing: {target}")
        return {"source": target, "path": os.path.abspath(target), "bytes": os.path.getsize(target), "fetched": False}

    fname = os.path.basename(target.split("?", 1)[0]) or f"enwiki-{day}-pages-meta-hist-incr.xml.bz2"
    dst = os.path.join(work_incr_dir, fname)
    if dry_run and not os.path.exists(dst):
        req = urllib.request.Request(target, method="HEAD", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {"source": target, "path": dst, "status": resp.status, "fetched": False, "would_download": True}
    if not os.path.isfile(dst):
        part = dst + ".part"
        req = urllib.request.Request(target, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=120) as resp, open(part, "wb") as out:
            while chunk := resp.read(1 << 20):
                out.write(chunk)
        os.replace(part, dst)
    if sha1sums:
        want = _read_sha1_entry(sha1sums, fname)
        if want:
            h = hashlib.sha1()
            with open(dst, "rb") as f:
                while chunk := f.read(1 << 20):
                    h.update(chunk)
            if h.hexdigest() != want:
                raise RuntimeError(f"{dst}: sha1 {h.hexdigest()} != expected {want}")
    return {"source": target, "path": dst, "bytes": os.path.getsize(dst), "fetched": True}


def _read_sha1_entry(src: str, filename: str) -> str | None:
    if src.startswith(("http://", "https://")):
        req = urllib.request.Request(src, headers={"User-Agent": UA})
        txt = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    else:
        txt = open(src, encoding="utf-8", errors="replace").read()
    for line in txt.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and os.path.basename(parts[1]) == filename:
            return parts[0].lower()
    return None


def _count_gz_lines(path: str) -> int:
    if not os.path.exists(path):
        return 0
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return sum(1 for _ in f)


def _read_delta_files(changed_gz: str, deleted_tsv: str):
    changed = []
    with gzip.open(changed_gz, "rt", encoding="utf-8", newline="") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            changed.append((
                p[0], int(p[1]), int(p[2]),
                wd.unescape(p[3]) or "",
                "",                                      # headings and infobox are used by the text delta, which reads the file itself
                (wd.unescape(p[5]) or "")[:CARD_LEAD_CHARS],     # cards hold the first 1000 characters: a 2 M-row catch-up stays small
                "",
                wd.unescape(p[7]) or "",
            ))
    deleted = []
    with open(deleted_tsv, encoding="utf-8", newline="") as f:
        for line in f:
            if line.strip():
                p = line.rstrip("\n").split("\t")
                deleted.append((int(p[0]), wd.unescape(p[1]) or p[1] if len(p) > 1 else ""))
    return changed, deleted


def _copy_release_skeleton(prev_rel: str, stage_rel: str, prev_manifest: dict) -> None:
    """Copy PREV_REL into stage_rel: writable copy of SQLite files that will be patched; hard links for unchanged files."""
    skip_dirs = ("text/wiki", "vectors/ivf")
    mutable_files = {"registry.sqlite", "lookup.sqlite", "pictures.sqlite"}
    skip_exact = {"manifest.json", "manifest.json.sig"}
    os.makedirs(stage_rel, exist_ok=True)
    for entry in prev_manifest.get("files", []):
        rel = entry["path"]
        if rel in skip_exact or any(rel == d or rel.startswith(d + "/") for d in skip_dirs):
            continue
        src = os.path.join(prev_rel, rel)
        dst = os.path.join(stage_rel, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if rel in mutable_files:
            shutil.copyfile(src, dst)
        else:
            try:
                os.link(src, dst)
            except OSError:
                shutil.copyfile(src, dst)


# Files a release carries from its predecessor that name their release. The engine refuses the pages' IVF folder when its
# `release` is not the engine's (sem_ivf.IvfIndex: "built for release rc15, engine expects rc16": rc16's first scratch engine
# stayed at 503 for that reason, 2026-11-17). The Tantivy sidecars are stamped likewise. SQLite files that stay at the
# predecessor's values keep their old built_release on purpose (derived data not refreshed this round).
SIDECARS = (("vectors/pages-ivf/ivf.json", "release", 1), ("text/sites/sites/release.json", "built_release", None),
            ("text/sites/pages/release.json", "built_release", None), ("text/sites/anchors/release.json", "built_release", None),
            ("text/typo/release.json", "built_release", None))


def restamp_sidecars(rel_dir: str, release_id: str) -> list[str]:
    """Stamp the carried-over sidecars with this release's id. Writes a NEW file and replaces the old, so a hard link to the
    predecessor's file is broken and the predecessor is never touched."""
    done = []
    for rel, key, indent in SIDECARS:
        p = os.path.join(rel_dir, rel)
        if not os.path.isfile(p):
            continue
        j = json.load(open(p, encoding="utf-8"))
        if j.get(key) == release_id:
            continue
        j[key] = release_id
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            f.write(json.dumps(j, indent=indent, sort_keys=True) + "\n")
        os.replace(p + ".tmp", p)
        done.append(rel)
    return done


def _table_names(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _set_meta(db: sqlite3.Connection, key: str, value: str) -> None:
    if "meta" in _table_names(db):
        db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))


def _update_registry(reg_path: str, changed: list, deleted: list, release_id: str) -> dict:
    db = sqlite3.connect(reg_path)
    meta = dict(db.execute("SELECT key, value FROM meta")) if "meta" in _table_names(db) else {}
    shift = int(meta.get("type_shift", 28))
    lo, hi = 1 << shift, 2 << shift
    by_pid = {ext: doc_id for doc_id, ext in db.execute(
        "SELECT doc_id, ext FROM registry WHERE doc_id >= ? AND doc_id < ?", (lo, hi))}
    hi_live = db.execute("SELECT max(doc_id) FROM registry WHERE doc_id >= ? AND doc_id < ?", (lo, hi)).fetchone()[0]
    hi_tomb = db.execute("SELECT max(doc_id) FROM tombstones WHERE doc_id >= ? AND doc_id < ?", (lo, hi)).fetchone()[0]
    next_id = max([x for x in (hi_live, hi_tomb) if x is not None], default=lo - 1) + 1

    upserted_ids: dict[int, tuple[int, str, str, str]] = {}
    stats = {"updated": 0, "inserted": 0, "tombstoned": 0}
    for op, pid, _rid, title, _hd, lead, _ib, aliases in changed:
        if pid in by_pid:
            did = by_pid[pid]
            db.execute("UPDATE registry SET title = ? WHERE doc_id = ?", (title, did))
            stats["updated"] += 1
        else:
            did = next_id
            next_id += 1
            by_pid[pid] = did
            db.execute("INSERT INTO registry (doc_id, ext, host_id, path, title, alt) VALUES (?, ?, NULL, NULL, ?, NULL)",
                       (did, pid, title))
            stats["inserted"] += 1
        upserted_ids[pid] = (did, title, lead, aliases)

    deleted_dids: list[int] = []
    for pid, _title in deleted:
        did = by_pid.pop(pid, None)
        if did is None:
            continue
        db.execute("DELETE FROM registry WHERE doc_id = ?", (did,))
        db.execute("INSERT OR IGNORE INTO tombstones (doc_id, removed_release) VALUES (?, ?)", (did, release_id))
        deleted_dids.append(did)
        stats["tombstoned"] += 1

    _set_meta(db, "built_release", release_id)
    db.commit()
    db.close()
    return {"stats": stats, "upserted": upserted_ids, "deleted_dids": deleted_dids}


def _update_lookup(lookup_path: str, reg_info: dict, release_id: str) -> dict:
    if not os.path.exists(lookup_path):
        return {"skipped": "missing lookup.sqlite"}
    db = sqlite3.connect(lookup_path)
    tables = _table_names(db)
    _set_meta(db, "built_release", release_id)
    upserted = reg_info["upserted"]
    deleted_dids = reg_info["deleted_dids"]
    counts = {"cards_upserted": 0, "cards_deleted": 0, "names_updated": 0, "aliases_updated": 0, "trgm_updated": 0}

    if "cards" in tables:
        for _pid, (did, title, lead, _aliases) in upserted.items():
            cur = db.execute("SELECT c FROM cards WHERE doc_id = ?", (did,)).fetchone()
            obj = json.loads(zlib.decompress(cur[0])) if cur else {"t": 0}
            obj["l"] = lead[:CARD_LEAD_CHARS]        # rc15 cards hold the first 1000 characters of the lead (build.py LEAD_CHARS)
            if not isinstance(obj.get("t"), int):
                obj["t"] = title
            db.execute("INSERT OR REPLACE INTO cards VALUES (?, ?)",
                       (did, zlib.compress(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode(), 6)))
            counts["cards_upserted"] += 1
            if counts["cards_upserted"] % 100_000 == 0:
                db.commit()                         # bounded rollback journal
        for did in deleted_dids:
            counts["cards_deleted"] += db.execute("DELETE FROM cards WHERE doc_id = ?", (did,)).rowcount

    # names (PRIMARY KEY (name, kind, doc_id, flags)) and suggest_keys (PRIMARY KEY (key, doc_id, pos)) cannot be searched by doc_id:
    # a per-article DELETE scans the whole table, which on a 2 M-row catch-up ran for hours without finishing (2026-11-16).
    # One DELETE over the whole table with the ids in a temp table instead, then a bulk insert.
    db.execute("CREATE TEMP TABLE upd (doc_id INTEGER PRIMARY KEY)")
    db.execute("CREATE TEMP TABLE gone (doc_id INTEGER PRIMARY KEY)")
    db.executemany("INSERT OR IGNORE INTO upd VALUES (?)", ((did,) for (did, _t, _l, _a) in upserted.values()))
    db.executemany("INSERT OR IGNORE INTO gone VALUES (?)", ((did,) for did in deleted_dids))
    log(f"lookup: cards done ({counts['cards_upserted']:,} upserted, {counts['cards_deleted']:,} deleted)")
    if "names" in tables:
        db.execute("DELETE FROM names WHERE doc_id IN (SELECT doc_id FROM upd) AND kind IN (?, ?)", (N_TITLE, N_ALIAS))
        db.execute("DELETE FROM names WHERE doc_id IN (SELECT doc_id FROM gone)")
        batch = []
        for _pid, (did, title, _lead, aliases) in upserted.items():
            if title:
                batch.append((trgm.pg_lower(title), N_TITLE, did))
            for al in (a.strip() for a in aliases.split(" | ") if a.strip()):
                batch.append((trgm.pg_lower(al), N_ALIAS, did))
            counts["names_updated"] += 1
            if len(batch) >= 200_000:
                db.executemany("INSERT OR IGNORE INTO names VALUES (?, ?, ?, 0)", batch)
                batch.clear()
        db.executemany("INSERT OR IGNORE INTO names VALUES (?, ?, ?, 0)", batch)
        db.commit()

    log("lookup: names done")
    if "aliases" in tables:
        db.execute("DELETE FROM aliases WHERE doc_id IN (SELECT doc_id FROM upd) OR doc_id IN (SELECT doc_id FROM gone)")
        db.executemany("INSERT OR REPLACE INTO aliases (alias, doc_id) VALUES (?, ?)",
                       ((trgm.pg_lower(al), did) for (did, _t, _l, aliases) in upserted.values()
                        for al in (a.strip() for a in aliases.split(" | ") if a.strip())))
        counts["aliases_updated"] = len(upserted)
        db.commit()

    if "suggest_items" in tables:
        db.execute("DELETE FROM suggest_items WHERE doc_id IN (SELECT doc_id FROM gone)")
    if "suggest_keys" in tables:
        db.execute("DELETE FROM suggest_keys WHERE doc_id IN (SELECT doc_id FROM gone)")
    db.commit()

    if "wiki_trgm" in tables:
        for _pid, (did, title, _lead, _aliases) in upserted.items():
            db.execute("DELETE FROM wiki_trgm WHERE rowid = ?", (did,))
            if title:
                db.execute("INSERT INTO wiki_trgm (rowid, t) VALUES (?, ?)", (did, trgm.padded(title)))
            counts["trgm_updated"] += 1
            if counts["trgm_updated"] % 100_000 == 0:
                db.commit()
                log(f"lookup: {counts['trgm_updated']:,} trigram rows")
        for did in deleted_dids:
            db.execute("DELETE FROM wiki_trgm WHERE rowid = ?", (did,))

    db.commit()
    db.close()
    return counts


def _update_pictures(pic_path: str, deleted_dids: list[int], release_id: str) -> dict:
    if not os.path.exists(pic_path):
        return {"skipped": "missing pictures.sqlite"}
    db = sqlite3.connect(pic_path)
    tables = _table_names(db)
    removed = 0
    if "pictures" in tables:
        for did in deleted_dids:
            removed += db.execute("DELETE FROM pictures WHERE doc_id = ?", (did,)).rowcount
    _set_meta(db, "built_release", release_id)
    db.commit()
    db.close()
    return {"removed": removed}


def purge_tombstoned(rel_dir: str) -> dict:
    """Rows of every other SQLite file that still name a doc_id the registry has tombstoned (facts, lists, pictures, ...):
    update_apply.check_tombstones refuses a release that keeps them (rc16's first package: facts.sqlite.facts, 30,564 rows).
    A file that needs a delete is first given its own copy: it may be a hard link to the previous release's file."""
    reg = sqlite3.connect(f"file:{os.path.join(rel_dir, 'registry.sqlite')}?mode=ro", uri=True)
    tomb = [(r[0],) for r in reg.execute("SELECT doc_id FROM tombstones")] if "tombstones" in _table_names(reg) else []
    reg.close()
    out: dict = {}
    if not tomb:
        return out
    for dp, dirs, fns in os.walk(rel_dir):
        dirs.sort()
        for fn in sorted(fns):
            path = os.path.join(dp, fn)
            if not fn.endswith(".sqlite") or os.path.samefile(path, os.path.join(rel_dir, "registry.sqlite")):
                continue
            rel = os.path.relpath(path, rel_dir).replace(os.sep, "/")

            def hits(db):
                plain, _r, _f, _s = uc.sqlite_objects(db)
                db.execute("CREATE TEMP TABLE _tomb (id INTEGER PRIMARY KEY)")
                db.executemany("INSERT INTO _tomb VALUES (?)", tomb)
                found = {}
                for t in plain:
                    if "doc_id" in [r[1] for r in db.execute(f"PRAGMA table_info({uc._q(t)})")]:
                        n = db.execute(f"SELECT count(*) FROM {uc._q(t)} WHERE doc_id IN (SELECT id FROM _tomb)").fetchone()[0]
                        if n:
                            found[t] = n
                return found

            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            found = hits(db)
            db.close()
            if not found:
                continue
            if os.stat(path).st_nlink > 1:
                shutil.copyfile(path, path + ".own")
                os.replace(path + ".own", path)
            db = sqlite3.connect(path)
            db.execute("CREATE TEMP TABLE _tomb (id INTEGER PRIMARY KEY)")
            db.executemany("INSERT INTO _tomb VALUES (?)", tomb)
            for t in found:
                db.execute(f"DELETE FROM {uc._q(t)} WHERE doc_id IN (SELECT id FROM _tomb)")
            db.commit()
            db.close()
            out[rel] = found
    return out


def _licence_for(rel: str, prev_licences: dict[str, str]) -> str:
    if rel in prev_licences:
        return prev_licences[rel]
    if rel.startswith("text/wiki/"):
        return prev_licences.get("__text_wiki__", "CC BY-SA 4.0 (Wikipedia text)")
    if rel.startswith("vectors/ivf/"):
        return prev_licences.get("__vectors_ivf__", "Apache-2.0 (nomic-embed-text-v1.5) / CC BY-SA 4.0")
    return prev_licences.get("__default__", "CC0-1.0")


def write_release_manifest(rel_dir: str, prev_manifest: dict, release_id: str, sign_key: str | None = None) -> tuple[dict, bytes]:
    prev_licences = {f["path"]: f.get("licence", "") for f in prev_manifest.get("files", []) if f.get("licence")}
    for f in prev_manifest.get("files", []):
        if f["path"].startswith("text/wiki/") and f.get("licence"):
            prev_licences.setdefault("__text_wiki__", f["licence"])
        elif f["path"].startswith("vectors/ivf/") and f.get("licence"):
            prev_licences.setdefault("__vectors_ivf__", f["licence"])
    files = []
    for dp, dirs, fns in os.walk(rel_dir):
        dirs.sort()
        for fn in sorted(fns):
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, rel_dir).replace(os.sep, "/")
            if rel in ("manifest.json", "manifest.json.sig") or fn.endswith(".lock"):
                continue
            files.append({"path": rel, "bytes": os.path.getsize(p), "sha256": uc.sha256_file(p),
                          "licence": _licence_for(rel, prev_licences)})
    files.sort(key=lambda x: x["path"])
    m = dict(prev_manifest)
    m.pop("transport", None)
    m["release"] = release_id
    m["created"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    m["files"] = files
    m["total_bytes"] = sum(f["bytes"] for f in files)
    body = json.dumps(m, indent=1, sort_keys=True).encode()
    open(os.path.join(rel_dir, "manifest.json"), "wb").write(body)
    if sign_key:
        open(os.path.join(rel_dir, "manifest.json.sig"), "wb").write(hd.sign(sign_key, body))
    return m, body


def write_package_manifest(pkg_dir: str, from_release: str, to_release: str, sign_key: str | None = None) -> dict:
    """Write hf_dataset-style package.json (and .sig if sign_key) listing every file in the update package."""
    listed = []
    for rel in hd.walk(pkg_dir):
        if rel in ("package.json", "package.json.sig"):
            continue
        p = os.path.join(pkg_dir, *rel.split("/"))
        listed.append({"path": rel, "bytes": os.path.getsize(p), "sha256": uc.sha256_file(p)})
    doc = {"format": hd.PACKAGE_FORMAT, "from_release": from_release, "to_release": to_release, "files": listed}
    body = json.dumps(doc, indent=1, sort_keys=True).encode()
    open(os.path.join(pkg_dir, "package.json"), "wb").write(body)
    if sign_key:
        open(os.path.join(pkg_dir, "package.json.sig"), "wb").write(hd.sign(sign_key, body))
    return doc


def update_release_pointer(pointer_path: str, release_id: str, manifest_sha256: str, from_release: str,
                           pkg_rel_path: str, sign_key: str | None = None) -> dict:
    """Create/update signed latest.json-style pointer registering the new release and row-level update package entry."""
    os.makedirs(os.path.dirname(os.path.abspath(pointer_path)), exist_ok=True)
    if os.path.isfile(pointer_path):
        cur = json.load(open(pointer_path, encoding="utf-8"))
        updates = dict(cur.get("updates") or {})
    else:
        updates = {}
    updates[from_release] = pkg_rel_path
    doc = {"format": hd.LATEST_FORMAT, "release": release_id, "revision": release_id,
           "manifest_sha256": manifest_sha256, "updates": updates}
    body = json.dumps(doc, indent=1, sort_keys=True).encode()
    open(pointer_path, "wb").write(body)
    if sign_key:
        open(pointer_path + ".sig", "wb").write(hd.sign(sign_key, body))
    return doc


def _run_embed_step(cmd_template: str | None, texts_gz: str, vec_dir: str, release_id: str, day: str, n_texts: int) -> dict:
    os.makedirs(vec_dir, exist_ok=True)
    if cmd_template:
        formatted = cmd_template.format(texts=texts_gz, out_dir=vec_dir, release=release_id, day=day)
        _run(shlex.split(formatted), env={"WIKI_DAILY_TEXTS": texts_gz, "WIKI_DAILY_VECS_DIR": vec_dir,
                                          "WIKI_DAILY_RELEASE": release_id, "WIKI_DAILY_DAY": day})
        return {"mode": "custom", "texts": n_texts}
    if n_texts == 0:
        import numpy as np
        np.save(os.path.join(vec_dir, "nomic-0000.ids.npy"), np.zeros((0,), np.int64))
        np.save(os.path.join(vec_dir, "nomic-0000.vecs.npy"), np.zeros((0, uc.DIM_BYTES * 8), np.float16))
        return {"mode": "empty", "texts": 0}
    raise RuntimeError("changed texts require --embed-cmd to generate vectors (nomic-*.ids.npy / nomic-*.vecs.npy)")


def run_daily(a) -> dict:
    t0 = time.time()
    prev_rel = os.path.abspath(a.prev_release)
    out_rel = os.path.abspath(a.out_release)
    out_pkg = os.path.abspath(a.out_pkg)
    work = os.path.abspath(a.work)
    state_path = os.path.abspath(a.state)
    prev_manifest = json.load(open(os.path.join(prev_rel, "manifest.json"), encoding="utf-8"))
    prev_id = prev_manifest["release"]
    release_id = a.release or f"{prev_id}-w{a.day}"

    if not a.dry_run:
        if os.path.exists(out_rel):
            raise RuntimeError(f"refusing: {out_rel} already exists")
        if os.path.exists(out_pkg):
            raise RuntimeError(f"refusing: {out_pkg} already exists")

    stage_root = os.path.join(work, "dry-run" if a.dry_run else release_id)
    delta_dir = os.path.join(stage_root, "delta")
    embed_dir = os.path.join(stage_root, "embed")
    vecs_dir = os.path.join(stage_root, "vecs")
    ans_delta_dir = os.path.join(stage_root, "release-delta")
    os.makedirs(stage_root, exist_ok=True)
    os.makedirs(embed_dir, exist_ok=True)

    log(f"start day={a.day} prev={prev_id} next={release_id} dry_run={a.dry_run}")
    if a.delta_dir:                       # a change set made elsewhere (rc16: the catch-up + the daily runs, wikidelta.py combine)
        delta_dir = os.path.abspath(a.delta_dir)
        fetch_info = {"skipped": "delta-dir", "path": None}
    else:
        fetch_info = fetch_incremental(a.pages, a.day, a.incr_url_template, os.path.join(work, "incr", a.day), a.sha1sums, a.dry_run)

    # Change set against state (on dry-run use a scratch copy + --no-commit so live state never changes)
    use_state = state_path
    if a.dry_run:
        use_state = os.path.join(stage_root, "state-copy.sqlite")
        shutil.copyfile(state_path, use_state)
    wd_py = getattr(a, "wikidelta_python", None) or sys.executable
    incr_cmd = [wd_py, os.path.join(IMPORTER_DIR, "wikidelta.py"), "incr",
                "--state", use_state, "--out", delta_dir, "--pages", fetch_info["path"],
                "--day", a.day, "--hold-hours", str(a.hold_hours), "--procs", str(a.procs)]
    if a.now:
        incr_cmd += ["--now", a.now]
    if a.since:
        incr_cmd += ["--since", a.since]
    if a.deletes_file:
        incr_cmd += ["--deletes-file", os.path.abspath(a.deletes_file)]
    if a.no_api:
        incr_cmd.append("--no-api")
    if a.dry_run:
        incr_cmd.append("--no-commit")
    if a.delta_dir:
        delta_summary = json.load(open(os.path.join(delta_dir, "summary.json"), encoding="utf-8"))
        texts_gz = os.path.join(embed_dir, "texts.tsv.gz")
        _run([sys.executable, os.path.join(HERE, "wiki_ivf_delta.py"), "texts", os.path.join(delta_dir, "changed.tsv.gz"), texts_gz])
        n_texts = _count_gz_lines(texts_gz)
    elif not (a.dry_run and fetch_info.get("would_download")):
        _run(incr_cmd)
        delta_summary = json.load(open(os.path.join(delta_dir, "summary.json"), encoding="utf-8"))
        texts_gz = os.path.join(embed_dir, "texts.tsv.gz")
        _run([sys.executable, os.path.join(HERE, "wiki_ivf_delta.py"), "texts", os.path.join(delta_dir, "changed.tsv.gz"), texts_gz])
        n_texts = _count_gz_lines(texts_gz)
    else:
        delta_summary = {"skipped_parse": "dry_run_remote_not_fetched"}
        texts_gz = os.path.join(embed_dir, "texts.tsv.gz")
        n_texts = None

    if a.dry_run:
        report = {
            "dry_run": True, "day": a.day, "prev_release": prev_id, "release": release_id,
            "fetch": fetch_info, "wikidelta": delta_summary, "embed_texts": n_texts,
            "planned": {
                "embed_cmd": a.embed_cmd.format(texts=texts_gz, out_dir=vecs_dir, release=release_id, day=a.day) if a.embed_cmd else None,
                "out_release": out_rel, "out_pkg": out_pkg,
                "store": os.path.abspath(a.store) if a.store else None,
                "updates_manifest": os.path.abspath(a.updates_manifest) if a.updates_manifest else None,
            },
            "seconds": round(time.time() - t0, 2),
        }
        log(f"dry-run complete: {json.dumps(report, sort_keys=True)}")
        return report

    if a.vecs_dir:                        # embeddings made elsewhere (rc16: wiki_embed_rent.py on one RunPod GPU)
        vecs_dir = os.path.abspath(a.vecs_dir)
        embed_info = {"skipped": "vecs-dir", "dir": vecs_dir}
    else:
        embed_info = _run_embed_step(a.embed_cmd, texts_gz, vecs_dir, release_id, a.day, n_texts)

    # Assemble new release in staging, then atomically rename into out_rel
    rel_stage = out_rel + f".staging-{os.getpid()}"
    if os.path.exists(rel_stage):
        shutil.rmtree(rel_stage)
    try:
        t_step = time.time()
        _copy_release_skeleton(prev_rel, rel_stage, prev_manifest)
        restamped = restamp_sidecars(rel_stage, release_id)
        log(f"step skeleton copy: {round(time.time() - t_step)} s; restamped {restamped}"); t_step = time.time()
        changed, deleted = _read_delta_files(os.path.join(delta_dir, "changed.tsv.gz"), os.path.join(delta_dir, "deleted.tsv"))
        reg_info = _update_registry(os.path.join(rel_stage, "registry.sqlite"), changed, deleted, release_id)
        log(f"step registry: {round(time.time() - t_step)} s"); t_step = time.time()
        lookup_info = _update_lookup(os.path.join(rel_stage, "lookup.sqlite"), reg_info, release_id)
        log(f"step lookup: {round(time.time() - t_step)} s"); t_step = time.time()
        pic_info = _update_pictures(os.path.join(rel_stage, "pictures.sqlite"), reg_info["deleted_dids"], release_id)
        weekly_info = None
        if getattr(a, "weekly_dir", None):   # the weekly page views, sitelinks and facts (wiki_weekly.py): never fails the day
            try:
                import wiki_weekly
                weekly_info = wiki_weekly.apply(rel_stage, prev_rel, release_id, os.path.abspath(a.weekly_dir))
            except Exception as e:           # the hard-linked signals.bin / facts.sqlite of the previous release stay
                weekly_info = {"failed": f"{type(e).__name__}: {str(e)[:300]}"}
                log(f"WEEKLY FAILED (the day's build goes on with last week's numbers): {weekly_info['failed']}")
            log(f"step weekly: {round(time.time() - t_step)} s"); t_step = time.time()
        purged = purge_tombstoned(rel_stage)
        log(f"step pictures + purge of deleted ids: {round(time.time() - t_step)} s; {purged}"); t_step = time.time()

        os.makedirs(os.path.join(rel_stage, "text"), exist_ok=True)
        os.makedirs(os.path.join(rel_stage, "vectors"), exist_ok=True)
        _run([sys.executable, os.path.join(HERE, "wiki_text_delta.py"), "apply",
              os.path.join(prev_rel, "text", "wiki"), os.path.join(rel_stage, "text", "wiki"),
              "--prev-registry", os.path.join(prev_rel, "registry.sqlite"),
              "--registry", os.path.join(rel_stage, "registry.sqlite"),
              "--changed", os.path.join(delta_dir, "changed.tsv.gz"),
              "--deleted", os.path.join(delta_dir, "deleted.tsv"),
              "--release", release_id])
        uc.tv_sort_segments(os.path.join(rel_stage, "text", "wiki"))
        log(f"step text delta: {round(time.time() - t_step)} s"); t_step = time.time()

        if a.wait_vecs:                    # the embeddings are still being made elsewhere: do the text steps meanwhile, wait here
            t_wait = time.time()
            while not os.path.exists(os.path.join(vecs_dir, "manifest.json")):
                if time.time() - t_wait > 4 * 3600:
                    raise RuntimeError(f"{vecs_dir}/manifest.json did not appear in 4 h")
                time.sleep(20)
            log(f"embeddings ready after {round(time.time() - t_wait)} s of waiting")
        ivf_merge_cmd = [sys.executable, os.path.join(HERE, "wiki_ivf_delta.py"), "merge",
                         os.path.join(prev_rel, "vectors", "ivf"), os.path.join(rel_stage, "vectors", "ivf"), release_id,
                         "--vectors", vecs_dir, "--changed", os.path.join(delta_dir, "changed.tsv.gz"),
                         "--deleted", os.path.join(delta_dir, "deleted.tsv"), "--merges-limit", str(a.merges_limit)]
        if a.no_drift_check:
            ivf_merge_cmd.append("--no-drift-check")
        _run(ivf_merge_cmd)
        log(f"step vector merge: {round(time.time() - t_step)} s"); t_step = time.time()

        recall_info = {"skipped": True}
        if not a.no_recall_check:
            rout = _run([sys.executable, os.path.join(HERE, "wiki_ivf_delta.py"), "recall",
                         os.path.join(rel_stage, "vectors", "ivf"), "--queries", str(a.recall_queries),
                         "--min-recall", str(a.min_recall)])
            recall_info = json.loads(rout.strip().splitlines()[-1])

        log(f"step recall check: {round(time.time() - t_step)} s"); t_step = time.time()
        _m, mbody = write_release_manifest(rel_stage, prev_manifest, release_id, a.sign)
        os.makedirs(os.path.dirname(out_rel), exist_ok=True)
        os.rename(rel_stage, out_rel)
        log(f"step manifest + rename: {round(time.time() - t_step)} s")
    except BaseException:
        shutil.rmtree(rel_stage, ignore_errors=True)
        raise

    # Phase 4 stored-answers release delta
    _run([sys.executable, os.path.join(TWIN_DIR, "release_delta.py"), "delta", prev_rel, out_rel, ans_delta_dir])
    rd_report = json.load(open(os.path.join(ans_delta_dir, "delta.json"), encoding="utf-8"))
    store_info = None
    if a.store:
        store_abs = os.path.abspath(a.store)
        equal_path = os.path.abspath(a.equal) if a.equal else None
        if rd_report.get("index_changed") and a.engine_api and equal_path is None:
            equal_path = os.path.join(ans_delta_dir, "equal.txt")
            _run([sys.executable, os.path.join(TWIN_DIR, "release_delta.py"), "recheck",
                  "--store", store_abs, "--release", prev_id, "--api", a.engine_api, "--out", equal_path])
        keys_out = os.path.join(ans_delta_dir, "affected_keys.tsv")
        aff_cmd = [sys.executable, os.path.join(TWIN_DIR, "release_delta.py"), "affected",
                   "--store", store_abs, "--old-release", prev_id, "--delta", ans_delta_dir, "--keys-out", keys_out]
        if equal_path and os.path.isfile(equal_path):
            aff_cmd += ["--equal", equal_path]
        store_info = json.loads(_run(aff_cmd).strip().splitlines()[-1])
        store_info["affected_keys_file"] = keys_out
        if a.restamp:
            if not a.eng:
                raise RuntimeError("--restamp requires --eng FP")
            rcmd = [sys.executable, os.path.join(TWIN_DIR, "release_delta.py"), "restamp",
                    "--store", store_abs, "--old-release", prev_id, "--new-release", release_id,
                    "--delta", ans_delta_dir, "--eng", a.eng]
            if equal_path and os.path.isfile(equal_path):
                rcmd += ["--equal", equal_path]
            store_info["restamp"] = json.loads(_run(rcmd).strip().splitlines()[-1])

    # Row-level update package + manifest entries
    pkg_cmd = [sys.executable, os.path.join(HERE, "update_pkg.py"), "build", prev_rel, out_rel, out_pkg]
    if a.no_selftest:
        pkg_cmd.append("--no-selftest")
    if a.sign:
        pkg_cmd += ["--sign", os.path.abspath(a.sign)]
    pkg_info = json.loads(_run(pkg_cmd).strip().splitlines()[-1])
    pkg_manifest = write_package_manifest(out_pkg, prev_id, release_id, a.sign)

    pointer_info = None
    if a.updates_manifest:
        pointer_info = update_release_pointer(os.path.abspath(a.updates_manifest), release_id,
                                              uc.sha256_bytes(mbody), prev_id, f"updates/{prev_id}", a.sign)

    result = {
        "dry_run": False, "day": a.day, "prev_release": prev_id, "release": release_id,
        "fetch": fetch_info, "wikidelta": delta_summary, "embed": embed_info,
        "registry": reg_info["stats"], "lookup": lookup_info, "pictures": pic_info, "weekly": weekly_info,
        "ivf_recall": recall_info, "release_delta": rd_report, "store_affected": store_info,
        "update_pkg": pkg_info, "package_files": len(pkg_manifest["files"]),
        "updates_manifest": pointer_info, "seconds": round(time.time() - t0, 2),
    }
    open(os.path.join(stage_root, "daily-summary.json"), "w", encoding="utf-8").write(json.dumps(result, indent=1, sort_keys=True) + "\n")
    log(f"done day={a.day} release={release_id} pkg_bytes={pkg_info['package_bytes']}")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    rs = sub.add_parser("restamp", help="stamp the carried-over sidecars of an assembled release and rewrite its manifest")
    rs.add_argument("rel_dir"); rs.add_argument("--sign")
    pg = sub.add_parser("purge", help="remove rows that name a tombstoned doc_id from every SQLite file of a built release, and rewrite its manifest")
    pg.add_argument("rel_dir")
    pg.add_argument("--sign")
    r = sub.add_parser("run", help="run one daily incremental Wikipedia update end to end")
    r.add_argument("--day", required=True, help="incremental dump day YYYYMMDD")
    r.add_argument("--state", required=True, help="wikidelta state sqlite path")
    r.add_argument("--prev-release", required=True, help="previous release directory")
    r.add_argument("--out-release", required=True, help="output release directory (must not exist)")
    r.add_argument("--out-pkg", required=True, help="output update package directory (must not exist)")
    r.add_argument("--work", required=True, help="scratch/work directory for fetched dumps, change set, vectors, delta")
    r.add_argument("--release", help="new release id (default: <prev_release>-w<day>)")
    r.add_argument("--pages", help="local path, file://, cmd:, or http(s):// incremental XML(.bz2)")
    r.add_argument("--incr-url-template", default=INCR_URL_TEMPLATE)
    r.add_argument("--sha1sums", help="optional Wikimedia sha1sums file/URL to verify download")
    r.add_argument("--deletes-file", help="optional TSV (page_id TAB title) of deletions instead of Wikimedia API")
    r.add_argument("--no-api", action="store_true", help="skip live Wikimedia API calls (alias text / delete log)")
    r.add_argument("--weekly-dir", help="a prepared weekly folder (wiki_weekly.py prepare): page views, sitelinks and facts "
                                        "are refreshed in this day's release; a failure there leaves them as they were")
    r.add_argument("--hold-hours", type=float, default=8.0, help="vandalism hold window in hours (default 8)")
    r.add_argument("--now", help="ISO UTC reference time for hold cutoff (default: current UTC)")
    r.add_argument("--since", help="ISO UTC start of delete-log window")
    r.add_argument("--procs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    r.add_argument("--wikidelta-python", help="python executable for wikidelta.py (default: current interpreter)")
    r.add_argument("--delta-dir", help="use this change set (changed.tsv.gz, deleted.tsv, summary.json) instead of fetching and parsing a day")
    r.add_argument("--vecs-dir", help="use these embeddings (nomic-* or shard-* npy files) instead of running --embed-cmd")
    r.add_argument("--wait-vecs", action="store_true", help="with --vecs-dir: wait for manifest.json there before the vector merge")
    r.add_argument("--embed-cmd", help="pluggable command creating nomic-*.{ids,vecs}.npy in {out_dir} from {texts}")
    r.add_argument("--merges-limit", type=int, default=31)
    r.add_argument("--no-drift-check", action="store_true")
    r.add_argument("--no-recall-check", action="store_true")
    r.add_argument("--recall-queries", type=int, default=200)
    r.add_argument("--min-recall", type=float, default=0.90)
    r.add_argument("--store", help="optional ResultStore sqlite to compute Phase 4 affected keys (and --restamp)")
    r.add_argument("--equal", help="optional retrieval-equal names file from release_delta.py recheck")
    r.add_argument("--engine-api", help="optional new engine HTTP URL for release_delta.py recheck")
    r.add_argument("--restamp", action="store_true", help="restamp unaffected finished answers in --store")
    r.add_argument("--eng", help="new engine fingerprint required when --restamp is set")
    r.add_argument("--sign", help="optional Ed25519 PEM private key to sign manifests and update package")
    r.add_argument("--updates-manifest", help="optional latest.json path to update with the new release and update entry")
    r.add_argument("--no-selftest", action="store_true", help="skip update_pkg.py self-test apply check")
    r.add_argument("--dry-run", action="store_true", help="inspect/plan only; leave state, out-release, out-pkg, store untouched")
    a = ap.parse_args()
    if a.cmd == "restamp":
        mp = json.load(open(os.path.join(a.rel_dir, "manifest.json"), encoding="utf-8"))
        done = restamp_sidecars(a.rel_dir, mp["release"])
        write_release_manifest(a.rel_dir, mp, mp["release"], a.sign)
        print(json.dumps({"restamped": done, "release": mp["release"]}))
        return 0
    if a.cmd == "purge":
        mp = json.load(open(os.path.join(a.rel_dir, "manifest.json"), encoding="utf-8"))
        done = purge_tombstoned(a.rel_dir)
        write_release_manifest(a.rel_dir, mp, mp["release"], a.sign)
        print(json.dumps({"purged": done, "release": mp["release"]}))
        return 0
    print(json.dumps(run_daily(a), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
