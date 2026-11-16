#!/usr/bin/env python3
"""Build the monthly update package release N -> release N+1 (spec: docs/internal/SEARCH-UPDATE-PACKAGE.md).

    update_pkg.py build OLD_RELEASE_DIR NEW_RELEASE_DIR OUT_PKG_DIR [--level 19] [--no-selftest] [--sign PRIVKEY.pem]
                                                                    [--no-link-from OLD_PATH_PREFIX ...]
    update_pkg.py info  PKG_DIR                      sizes per part (JSON)

Both release dirs are checked against their own manifest.json first. Every file of N+1 becomes one operation:

  link         same sha256 exists in N (under any name): nothing shipped, the client hard-links it
  sqlite-rows  a SQLite file whose schema did not change: the row diff (removed keys, upserted rows) + the FTS5 rowids to
               re-derive; the client patches a copy of N's file in place (no VACUUM). R*Tree/FTS5 pages are never shipped
  ivf-merge    the meaning-search folder: removed page_ids + new codes (the centres do not move); the client rewrites the folder
  blob         everything else (signals.bin, new Tantivy segment files, .del delete bitsets, meta.json, ...), zstd, once per sha256

The package is then APPLIED to a throwaway copy of N and every byte/row compared with N+1 (the self-test; --no-selftest skips it). A
file whose delta does not reproduce N+1 falls back to a whole blob, and the reason is in update.json. Output is deterministic.

update.json also carries `tantivy` (per index folder: segments, deleted docs, how many bytes are new, mode delta | whole | same) and
`ivf` (per meaning-search folder: delta or whole, and how far its fixed centres have drifted); a merged IVF folder past the limits in
update_common.IVF_LIMITS is REFUSED (ivf_build.py --previous re-clusters it instead; --allow-ivf-drift overrides), a re-clustered one
(the centres moved) ships whole and says why.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np  # noqa: E402

import update_common as uc  # noqa: E402


def load_manifest(rel: str):
    p = os.path.join(rel, "manifest.json")
    try:
        raw = open(p, "rb").read()
        return json.loads(raw), raw
    except (OSError, ValueError) as e:
        raise uc.UpdateError(f"{p}: unreadable ({e})")


def verify_release(rel: str, m: dict) -> None:
    for f in m["files"]:
        p = os.path.join(rel, f["path"])
        if not os.path.exists(p) or os.path.getsize(p) != f["bytes"] or uc.sha256_file(p) != f["sha256"]:
            raise uc.UpdateError(f"{rel}: {f['path']} does not match its manifest (missing, wrong size or sha256) - refusing to build from it")


def tombstones(old: str, new: str) -> list:
    db = sqlite3.connect(f"file:{new}/registry.sqlite?mode=ro", uri=True)
    db.execute("ATTACH ? AS o", (f"file:{old}/registry.sqlite?mode=ro",))
    lost = [r[0] for r in db.execute("SELECT doc_id FROM o.registry WHERE doc_id NOT IN (SELECT doc_id FROM main.registry) "
                                     "AND doc_id NOT IN (SELECT doc_id FROM main.tombstones)")]
    if lost:
        raise uc.UpdateError(f"{len(lost)} doc_ids left the registry without a tombstone (first: {lost[:3]}): a removed id must be tombstoned, never silently dropped")
    back = [r[0] for r in db.execute("SELECT doc_id FROM main.registry WHERE doc_id IN (SELECT doc_id FROM o.tombstones)")]
    if back:
        raise uc.UpdateError(f"{len(back)} tombstoned doc_ids are live again (first: {back[:3]}): ids are never reused")
    ids = [r[0] for r in db.execute("SELECT doc_id FROM main.tombstones WHERE doc_id NOT IN (SELECT doc_id FROM o.tombstones) ORDER BY doc_id")]
    db.close()
    return ids


def ivf_payload(old: str, new: str, d: str, out: str, release: str, level: int, notes: list, ivf_info: dict, allow_drift: bool = False):
    """The IVF delta of folder d (removed ids, new ids, new codes), written under out/ivf, or None (with a note) when the centres
    moved or merging the delta does not reproduce the new folder byte for byte."""
    delta = uc.ivf_delta(os.path.join(old, d), os.path.join(new, d))
    if delta is None:
        nm = json.load(open(os.path.join(new, d, "ivf.json")))
        notes.append(f"{d}: the centres moved or the format differs, the folder is shipped whole"
                     + (f" (re-clustered: {'; '.join(nm['rebuilt_because'])})" if nm.get("rebuilt_because") else ""))
        ivf_info[d] = {"mode": "whole", "why": "centres moved"}
        return None
    removed, added, codes = delta
    tmp = tempfile.mkdtemp(prefix="ivfchk-")
    try:
        uc.ivf_merge(os.path.join(old, d), tmp, removed, added, codes, release)
        bad = [n for n in uc.IVF_FILES if uc.sha256_file(os.path.join(tmp, n)) != uc.sha256_file(os.path.join(new, d, n))]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if bad:
        notes.append(f"{d}: merging the delta does not reproduce {bad} (the new folder was not built by ivf_merge): shipped whole")
        ivf_info[d] = {"mode": "whole", "why": "not built by ivf_merge"}
        return None
    nm = json.load(open(os.path.join(new, d, "ivf.json")))
    drift = uc.ivf_drift(uc.ivf_stats(os.path.join(new, d)), nm.get("trained") or uc.ivf_stats(os.path.join(old, d)), int(nm.get("merges", 1)))
    if drift["rebuild"] and not allow_drift:
        raise uc.UpdateError(f"{d}: the merged meaning-search folder is past the re-cluster limits ({'; '.join(drift['reasons'])}): "
                             "build it with ivf_build.py --previous (it re-clusters), or pass --allow-ivf-drift")
    ivf_info[d] = {"mode": "delta", "drift": drift["metrics"], "past_limits": drift["reasons"]}
    stage = os.path.join(out, "ivf", d.replace("/", "__"))
    os.makedirs(stage, exist_ok=True)
    removed.astype("<i4").tofile(os.path.join(stage, "removed_ids.i32"))
    added.astype("<i4").tofile(os.path.join(stage, "new_ids.i32"))
    codes.astype(np.uint8).tofile(os.path.join(stage, "new_codes.u8"))
    z = 0
    for n in ("removed_ids.i32", "new_ids.i32", "new_codes.u8"):
        z += uc.zst_compress(os.path.join(stage, n), os.path.join(stage, n + ".zst"), level)
        os.remove(os.path.join(stage, n))
    return {"dir": d, "removed_ids": int(len(removed)), "added_ids": int(len(added)), "zbytes": z,
            "payload": "ivf/" + d.replace("/", "__"),
            "base_sha256": {n: uc.sha256_file(os.path.join(old, d, n)) for n in uc.IVF_FILES}}


def tantivy_summary(old: str, new: str, ops: list, mo: dict, mn: dict, notes: list) -> list:
    """Per Tantivy index folder of N+1: how the month's package carries it."""
    seg_ext = (".idx", ".pos", ".term", ".fast", ".fieldnorm", ".store", ".del")
    old_dirs = {os.path.dirname(f["path"]) for f in mo["files"] if os.path.basename(f["path"]) == "meta.json"}
    out = []

    def seginfo(root, d):
        m = json.load(open(os.path.join(root, d, "meta.json")))
        segs = m.get("segments", [])
        return len(segs), sum(s["max_doc"] for s in segs), sum((s.get("deletes") or {}).get("num_deleted_docs", 0) for s in segs)
    for f in sorted(mn["files"], key=lambda f: f["path"]):
        if os.path.basename(f["path"]) != "meta.json":
            continue
        d = os.path.dirname(f["path"])
        mine = [o for o in ops if os.path.dirname(o["path"]) == d and o["path"].endswith(seg_ext)]
        blob = [o for o in mine if o["op"] != "link"]
        s1, n1, dead1 = seginfo(new, d)
        row = {"dir": d, "segments": s1, "docs": n1, "deleted_docs": dead1, "files": len(mine), "new_files": len(blob),
               "new_zbytes": sum(o.get("zbytes", 0) for o in blob), "linked_bytes": sum(o["bytes"] for o in mine if o["op"] == "link")}
        if d not in old_dirs:
            row["mode"] = "new"
        else:
            s0, n0, dead0 = seginfo(old, d)
            row["segments_before"] = s0
            # `.idx/.term/.store` carry the data: a segment of last month kept under another name (a link) = a delta; none = a new index.
            # (An empty `.pos` or a tiny `.fast` can be byte-identical between two unrelated indexes, so they do not count.)
            kept = any(o["op"] == "link" and o["path"].endswith((".idx", ".term", ".store")) for o in mine)
            row["mode"] = "same" if not blob else ("delta" if kept else "whole")
        if row["mode"] == "whole":
            notes.append(f"{d}: the whole index is new ({row['new_zbytes']} bytes zstd): a rebuild or a merge month, not a delta")
        if s1 >= 8:
            notes.append(f"{d}: {s1} segments: the next commit that adds a segment makes Tantivy merge on its own, and those merged bytes are not "
                         "reproducible (measured); replace or merge on purpose first (docs/internal/SEARCH-UPDATE-PACKAGE.md section 6)")
        out.append(row)
    return out


def build(old: str, new: str, out: str, level: int = 19, selftest: bool = True, sign: str = None, rows_threshold: float = 0.5,
          allow_ivf_drift: bool = False, apply_limit_s: float = None, whole_factor: float = None, no_link_from=()) -> dict:
    apply_limit_s = uc.APPLY_LIMIT_S if apply_limit_s is None else apply_limit_s
    whole_factor = uc.WHOLE_MAX_FACTOR if whole_factor is None else whole_factor
    old, new, out = os.path.abspath(old), os.path.abspath(new), os.path.abspath(out)
    mo, rawo = load_manifest(old)
    mn, rawn = load_manifest(new)
    for m in (mo, mn):
        if not isinstance(m.get("format"), int) or not m.get("release"):
            raise uc.UpdateError("a manifest without an integer format and a release id")
    if mo["format"] != mn["format"]:
        raise uc.UpdateError(f"release format {mo['format']} -> {mn['format']}: that is a full release, not an update")
    if mo["release"] == mn["release"]:
        raise uc.UpdateError("old and new are the same release id")
    verify_release(old, mo)
    verify_release(new, mn)
    if os.path.exists(out):
        raise uc.UpdateError(f"{out} exists: a package is never built on top of another")
    os.makedirs(os.path.join(out, "blobs"))
    os.makedirs(os.path.join(out, "rows"))
    by_sha = {}
    for f in sorted(mo["files"], key=lambda f: f["path"]):
        # --no-link-from: an old path the package must not name (a folder renamed so its old name is no longer published):
        # its files ship as blobs instead of being copied from the installed release
        if not any(f["path"] == x or f["path"].startswith(x.rstrip("/") + "/") for x in no_link_from):
            by_sha.setdefault(f["sha256"], f["path"])
    old_paths = {f["path"]: f for f in mo["files"]}
    ivf_dirs = sorted({os.path.dirname(f["path"]) for f in mn["files"] if os.path.basename(f["path"]) == "ivf.json"})
    ivf_ok, notes, ivf_info = {}, [], {}
    for d in ivf_dirs:
        if os.path.exists(os.path.join(old, d, "ivf.json")):
            ivf_ok[d] = ivf_payload(old, new, d, out, mn["release"], level, notes, ivf_info, allow_ivf_drift)
    ops, blobs = [], {}
    pending_sqlite = []

    def add_blob(sha, src_path, why=None):
        if sha not in blobs:
            z = os.path.join(out, "blobs", sha + ".zst")
            blobs[sha] = uc.zst_compress(src_path, z, level)
        return {"blob": f"blobs/{sha}.zst", "zbytes": blobs[sha]}

    for f in sorted(mn["files"], key=lambda f: f["path"]):
        p, sha = f["path"], f["sha256"]
        base = {"path": p, "sha256": sha, "bytes": f["bytes"], "licence": f.get("licence")}
        if sha in by_sha:
            ops.append(dict(base, op="link", from_path=by_sha[sha]))
        elif p.endswith(".sqlite") and p in old_paths:
            pending_sqlite.append((p, f))
        elif os.path.dirname(p) in ivf_ok and ivf_ok[os.path.dirname(p)] is not None and os.path.basename(p) in uc.IVF_FILES:
            ops.append(dict(base, op="ivf-merge", dir=os.path.dirname(p), ivf=ivf_ok[os.path.dirname(p)]))
        else:
            ops.append(dict(base, op="blob", **add_blob(sha, os.path.join(new, p))))
    # SQLite files: a row diff each, registry first (the lookup's FTS5 re-derive reads the new registry)
    pending_sqlite.sort(key=lambda x: (x[0] != "registry.sqlite", x[0]))
    for p, f in pending_sqlite:
        role = os.path.basename(p)[:-len(".sqlite")]
        base = {"path": p, "sha256": f["sha256"], "bytes": f["bytes"], "licence": f.get("licence")}
        whole_z = None
        try:
            diff = uc.sqlite_diff(os.path.join(old, p), os.path.join(new, p), role, os.path.join(new, "registry.sqlite"), narrow=True)
            rows_name = "rows/" + p.replace("/", "__") + ".jsonl"
            raw_bytes = uc.write_rows(diff, os.path.join(out, rows_name))
            z = uc.zst_compress(os.path.join(out, rows_name), os.path.join(out, rows_name + ".zst"), level)
            os.remove(os.path.join(out, rows_name))
            # the row diff must beat shipping the file (both zstd'd at the same level): a month that rewrites most rows is a whole file
            tmpz = os.path.join(out, "blobs", f["sha256"] + ".zst")
            whole_z = uc.zst_compress(os.path.join(new, p), tmpz, level)
            changed = sum(len(d["removed"]) + len(d["upsert"]) for d in diff["tables"].values()) + sum(len(d["touched"]) for d in diff["fts"].values())
            file_rows = uc.count_rows(os.path.join(new, p))
            est_s = uc.estimate_apply_seconds(changed, file_rows)
            if z > rows_threshold * whole_z:
                os.remove(os.path.join(out, rows_name + ".zst"))
                blobs[f["sha256"]] = whole_z
                raise uc.UpdateError(f"whole: the row diff ({z} bytes zstd) is more than {rows_threshold:g} x the file ({whole_z} bytes zstd)")
            # cost rule: a diff that is cheap to download but slow to apply is replaced by the file when the file is not much bigger
            if uc.ship_whole_for_cost(est_s, whole_z, z, apply_limit_s, whole_factor):
                os.remove(os.path.join(out, rows_name + ".zst"))
                blobs[f["sha256"]] = whole_z
                raise uc.UpdateError(f"whole: applying the row diff is estimated at {est_s:.0f} s ({changed} changed rows, {file_rows} rows in the file; limit "
                                     f"{apply_limit_s:g} s) and the file is {whole_z / z:.1f}x the diff's download ({whole_z} vs {z} bytes zstd; at most {whole_factor:g}x)")
            os.remove(tmpz)
        except uc.UpdateError as e:
            why = str(e).removeprefix("whole: ")
            notes.append(f"{p}: shipped whole ({why})")
            ops.append(dict(base, op="blob", why=why, **add_blob(f["sha256"], os.path.join(new, p))))
            continue
        ops.append(dict(base, op="sqlite-rows", rows=rows_name + ".zst", rows_format=uc.ROWS_FORMAT, zbytes=z, raw_bytes=raw_bytes, role=role,
                        est_apply_s=round(est_s, 1), changed_rows=changed, file_rows=file_rows,
                        base_content_sha256=uc.sqlite_content_sha256(os.path.join(old, p)),
                        content_sha256=uc.sqlite_content_sha256(os.path.join(new, p)),
                        tables={t: {"removed": len(d["removed"]), "upserted": len(d["upsert"])} for t, d in diff["tables"].items()},
                        fts={t: len(d["touched"]) for t, d in diff["fts"].items()}))
    tomb = tombstones(old, new)
    tomb_doc = {"from_release": mo["release"], "to_release": mn["release"], "count": len(tomb), "doc_ids": tomb}
    tb = json.dumps(tomb_doc, separators=(",", ":")).encode()
    open(os.path.join(out, "tombstones.json"), "wb").write(tb)
    shutil.copyfile(os.path.join(new, "manifest.json"), os.path.join(out, "manifest.json"))
    if os.path.exists(os.path.join(new, "manifest.json.sig")):
        shutil.copyfile(os.path.join(new, "manifest.json.sig"), os.path.join(out, "manifest.json.sig"))
    upd = {"format": uc.UPDATE_FORMAT, "from_release": mo["release"], "to_release": mn["release"],
           "from_manifest_sha256": uc.sha256_bytes(rawo), "to_manifest_sha256": uc.sha256_bytes(rawn),
           "release_format": mn["format"],
           "requires": {"release_format": mn["format"], "tantivy": mn.get("engine", {}).get("tantivy"),
                        "registry_layout": mn.get("engine", {}).get("registry", {}).get("layout"),
                        "ivf_format": uc.IVF_FORMAT, "signals_magic": "LOSIGNL1"},
           "tombstones": {"count": len(tomb), "sha256": uc.sha256_bytes(tb)},
           "files": ops, "notes": notes, "tantivy": tantivy_summary(old, new, ops, mo, mn, notes), "ivf": ivf_info}
    body = json.dumps(upd, indent=1, sort_keys=True).encode()
    open(os.path.join(out, "update.json"), "wb").write(body)
    if sign:
        from cryptography.hazmat.primitives import serialization
        key = serialization.load_pem_private_key(open(sign, "rb").read(), password=None)
        open(os.path.join(out, "update.json.sig"), "wb").write(key.sign(body))
    if selftest:
        selftest_apply(old, new, out, mn)
    return info(out)


def selftest_apply(old: str, new: str, pkg: str, mn: dict) -> None:
    """Apply the package to a throwaway copy of OLD and compare with NEW: every non-SQLite file byte for byte, every SQLite file
    by logical content."""
    import update_apply as ua
    base = tempfile.mkdtemp(prefix="pkgchk-")
    try:
        ua.adopt(base, old)
        ua.apply(base, pkg, insecure_no_signature=True)
        res = os.path.join(base, "current")
        for f in mn["files"]:
            a, b = os.path.join(res, f["path"]), os.path.join(new, f["path"])
            if f["path"].endswith(".sqlite"):
                if uc.sqlite_content_sha256(a) != uc.sqlite_content_sha256(b):
                    raise uc.UpdateError(f"self-test: {f['path']} differs in content from the new release")
            elif uc.sha256_file(a) != f["sha256"]:
                raise uc.UpdateError(f"self-test: {f['path']} differs from the new release")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def info(pkg: str) -> dict:
    upd = json.load(open(os.path.join(pkg, "update.json")))
    parts = {"blobs": 0, "rows": 0, "ivf": 0, "meta": 0}
    for d, _, fs in os.walk(pkg):
        for f in fs:
            p = os.path.join(d, f)
            rel = os.path.relpath(p, pkg)
            k = rel.split("/")[0] if rel.split("/")[0] in parts else "meta"
            parts[k] += os.path.getsize(p)
    by_op = {}
    for o in upd["files"]:
        by_op.setdefault(o["op"], {"files": 0, "release_bytes": 0})
        by_op[o["op"]]["files"] += 1
        by_op[o["op"]]["release_bytes"] += o["bytes"]
    return {"from": upd["from_release"], "to": upd["to_release"], "package_bytes": sum(parts.values()), "parts": parts, "by_op": by_op,
            "tombstones": upd["tombstones"]["count"], "notes": upd["notes"],
            "release_bytes": sum(o["bytes"] for o in upd["files"])}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    b = sp.add_parser("build")
    b.add_argument("old")
    b.add_argument("new")
    b.add_argument("out")
    b.add_argument("--level", type=int, default=19)
    b.add_argument("--no-selftest", action="store_true")
    b.add_argument("--sign")
    b.add_argument("--rows-threshold", type=float, default=0.5,
                   help="ship a SQLite file as a row diff only when the zstd'd diff is at most this share of the zstd'd file (default 0.5)")
    b.add_argument("--allow-ivf-drift", action="store_true",
                   help="ship a merged meaning-search folder even though its centres have drifted past update_common.IVF_LIMITS")
    b.add_argument("--apply-limit-s", type=float, default=None,
                   help=f"ship a SQLite file whole when applying its row diff is estimated above this many seconds (default {uc.APPLY_LIMIT_S:g}) ...")
    b.add_argument("--whole-factor", type=float, default=None,
                   help=f"... and the whole file is at most this many times the diff's zstd bytes (default {uc.WHOLE_MAX_FACTOR:g})")
    b.add_argument("--no-link-from", action="append", default=[], metavar="OLD_PATH_PREFIX",
                   help="never copy a file from this path (or folder) of the old release: ship it in the package, so update.json does not name it")
    b.add_argument("--affected", metavar="FILE", help="release_delta.py affected --hashes-out: the names this update may change, as hashes; "
                   "shipped as affected.bin so an install keeps its stored lists and answers for every other name")
    i = sp.add_parser("info")
    i.add_argument("pkg")
    a = ap.parse_args()
    try:
        r = build(a.old, a.new, a.out, a.level, not a.no_selftest, a.sign, a.rows_threshold, a.allow_ivf_drift,
                  a.apply_limit_s, a.whole_factor, a.no_link_from) if a.cmd == "build" else info(a.pkg)
        if a.cmd == "build" and a.affected:
            shutil.copyfile(a.affected, os.path.join(os.path.abspath(a.out), "affected.bin"))
    except uc.UpdateError as e:
        print(f"update_pkg: REFUSED: {e}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(r, sort_keys=True))


if __name__ == "__main__":
    main()
