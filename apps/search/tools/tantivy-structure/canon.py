#!/usr/bin/env python3
"""Make a Tantivy index directory reproducible (release format v1, rule 7).

    canon.py check DIR      does any segment file contain its own segment id (hex or raw bytes)?
    canon.py canon DIR      T5's tv_normalize (below) plus dropping meta.json's `payload`
    canon.py ref DIR        release_v1.py::tv_normalize verbatim; strip DIR drops the payload
                            (equivalence test: canon on copy 1 == strip + ref on copy 2)

Tantivy names each segment with a random UUIDv4 (SegmentId::generate_random). That id is in
the file names and in meta.json only — `check` proves it is not inside the files — so renaming
a segment to a hash of its bytes changes nothing Tantivy reads, and two builds from the same
input with the same writer settings end up byte-identical, names included. .managed.json is a
serialised HashSet (random order); it is rewritten sorted. The meta.json payload (a merge
note with the source segments' random ids, unused by tantivy) is dropped. Run after the last writer closed.
"""
import hashlib
import json
import mmap
import os
import re
import sys
import uuid


def sha(p):
    d = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            d.update(blk)
    return d.hexdigest()


def segments(path):
    meta = json.load(open(os.path.join(path, "meta.json")))
    return meta, [s["segment_id"] for s in meta["segments"]]


def files_of(path, sid):
    short = sid.replace("-", "")
    return sorted(f for f in os.listdir(path) if f.startswith(short + "."))


def check(path):
    _, sids = segments(path)
    found = []
    for sid in sids:
        u = uuid.UUID(sid)
        needles = [u.hex.encode(), str(u).encode(), u.bytes, u.bytes_le]
        for f in files_of(path, sid):
            with open(os.path.join(path, f), "rb") as fh:
                if os.fstat(fh.fileno()).st_size == 0:
                    continue
                data = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
                for n in needles:
                    if data.find(n) >= 0:
                        found.append(f"{f}: contains {n!r}")
                data.close()
    print(json.dumps({"segments": len(sids), "files_with_own_id": found}))
    return not found


def sha256_file(p):
    return sha(p)


def canon(path):
    """Exactly release_v1.py's tv_normalize (T5, rule 7: content-derived UUID per segment, meta.json
    text-replaced, .managed.json sorted, empty lock files removed), plus one step it lacks: the
    `payload` that merge_filtered_segments writes into meta.json (a note naming the source segments'
    random ids) is removed first. Keep this function identical to tv_normalize; `tests` compare them."""
    meta_p = os.path.join(path, "meta.json")
    meta_txt = open(meta_p, encoding="utf-8").read()
    meta_txt = re.sub(r',\s*"payload":\s*"[^"]*"', "", meta_txt)
    segs = [s_["segment_id"] for s_ in json.loads(meta_txt).get("segments", [])]
    names = os.listdir(path)
    mapping = {}
    for sid in segs:
        short = sid.replace("-", "")
        h = hashlib.sha256()
        for f in sorted(n for n in names if n.startswith(short)):
            h.update(f[len(short):].encode())
            h.update(bytes.fromhex(sha256_file(os.path.join(path, f))))
        new = str(uuid.UUID(bytes=h.digest()[:16], version=4))
        mapping[sid] = new
    for sid, new in mapping.items():
        short, nshort = sid.replace("-", ""), new.replace("-", "")
        for f in names:
            if f.startswith(short):
                os.rename(os.path.join(path, f), os.path.join(path, nshort + f[len(short):]))
        meta_txt = meta_txt.replace(sid, new)
    open(meta_p, "w", encoding="utf-8").write(meta_txt)
    man_p = os.path.join(path, ".managed.json")
    if os.path.exists(man_p):
        man = json.load(open(man_p))
        for sid, new in mapping.items():
            man = [m.replace(sid.replace("-", ""), new.replace("-", "")) for m in man]
        open(man_p, "w").write(json.dumps(sorted(man)))
    else:  # a directory assembled without one: list what is there, as tantivy would
        open(man_p, "w").write(json.dumps(sorted(f for f in os.listdir(path) if not f.startswith("."))))
    for f in (".tantivy-meta.lock", ".tantivy-writer.lock"):
        if os.path.exists(os.path.join(path, f)) and os.path.getsize(os.path.join(path, f)) == 0:
            os.remove(os.path.join(path, f))
    print(json.dumps({"dir": path, "segments": list(mapping.values())}))


def t5_reference(path):
    """release_v1.py::tv_normalize copied verbatim (minus the release.json sidecar), for the
    equivalence test: `canon.py ref DIR` on a copy whose meta.json had its payload removed by the
    same regex must give the same bytes as `canon.py canon DIR` on another copy."""
    meta_p = os.path.join(path, "meta.json")
    meta_txt = open(meta_p, encoding="utf-8").read()
    segs = [s_["segment_id"] for s_ in json.loads(meta_txt).get("segments", [])]
    names = os.listdir(path)
    mapping = {}
    for sid in segs:
        short = sid.replace("-", "")
        h = hashlib.sha256()
        for f in sorted(n for n in names if n.startswith(short)):
            h.update(f[len(short):].encode())
            h.update(bytes.fromhex(sha256_file(os.path.join(path, f))))
        new = str(uuid.UUID(bytes=h.digest()[:16], version=4))
        mapping[sid] = new
    for sid, new in mapping.items():
        short, nshort = sid.replace("-", ""), new.replace("-", "")
        for f in names:
            if f.startswith(short):
                os.rename(os.path.join(path, f), os.path.join(path, nshort + f[len(short):]))
        meta_txt = meta_txt.replace(sid, new)
    open(meta_p, "w", encoding="utf-8").write(meta_txt)
    man_p = os.path.join(path, ".managed.json")
    if os.path.exists(man_p):
        man = json.load(open(man_p))
        for sid, new in mapping.items():
            man = [m.replace(sid.replace("-", ""), new.replace("-", "")) for m in man]
        open(man_p, "w").write(json.dumps(sorted(man)))
    for f in (".tantivy-meta.lock", ".tantivy-writer.lock"):
        if os.path.exists(os.path.join(path, f)) and os.path.getsize(os.path.join(path, f)) == 0:
            os.remove(os.path.join(path, f))


def strip_payload(path):
    p = os.path.join(path, "meta.json")
    t = open(p, encoding="utf-8").read()
    open(p, "w", encoding="utf-8").write(re.sub(r',\s*"payload":\s*"[^"]*"', "", t))


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("check", "canon", "ref", "strip"):
        raise SystemExit(__doc__)
    if sys.argv[1] == "check":
        sys.exit(0 if check(sys.argv[2]) else 1)
    if sys.argv[1] == "ref":
        t5_reference(sys.argv[2])
    elif sys.argv[1] == "strip":
        strip_payload(sys.argv[2])
    else:
        if not check(sys.argv[2]):
            raise SystemExit("a segment file contains its own id: renaming would break it")
        canon(sys.argv[2])
