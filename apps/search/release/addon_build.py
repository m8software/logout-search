#!/usr/bin/env python3
"""Build one signed add-on for `install.py addon install` (the optional data files that are not part of a release).

    addon_build.py build --name wikilinks|se-howto --file FILE --out OUT --version V --fits RELEASE_ID [--fits ...]
                         --key KEY.pem [--compress none|xz] [--title T] [--licence-name N] [--licence-url U] [--notice-file F]
    addon_build.py check OUT --name NAME --pubkey KEY.pub|HEX      (what the installer will accept: signature, size, sha256, rules)

`build` writes OUT/addons/<name>/manifest.json, manifest.json.sig and files/<file>[.xz]. OUT is laid out so that copying it over
the root of the Hugging Face dataset (next to latest.json and the release) puts the add-on where `install.py addon install` reads
it. It uploads nothing. The signature is the release's: a raw ed25519 signature over manifest.json bytes, made by
hf_dataset.sign (the `cryptography` package, else `openssl pkeyutl -rawin`). The key file is given on the command line; this tool
never looks for one.

--fits takes a release id or a pattern (`rc15`, `rc15*`). --compress xz stores the file as .xz (the installer verifies the stored
bytes and the decoded file, as for a release). Default none: the SQLite files barely shrink.

Standard library only (plus openssl or cryptography to sign).
"""
import argparse
import hashlib
import json
import lzma
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "installer"))
sys.path.insert(0, HERE)
import install as inst  # noqa: E402
import hf_dataset  # noqa: E402  (sign())

CC_BY_SA = ("CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/")
DEFAULTS = {
    "wikilinks": {
        "title": "Wikipedia links and citations (wikilinks.sqlite)",
        "description": "The links English Wikipedia articles carry (external links, further reading, cited pages) with the sentence each citation supports. "
                       "Used by the search card and the W / C breadth rows. Built by importer/wikiext.py.",
        "licence": CC_BY_SA,
        "notice": ("Contains data derived from English Wikipedia (https://en.wikipedia.org): the addresses its editors link from each article, "
                   "the section and the sentence a citation is attached to, and the titles of cited pages. Wikipedia text (c) Wikipedia "
                   "contributors, licensed under the Creative Commons Attribution-ShareAlike 4.0 License "
                   "(https://creativecommons.org/licenses/by-sa/4.0/). The history page of each article lists its authors. "
                   "Changes: reduced to link, section, sentence and domain-count tables by Logout (importer/wikiext.py). "
                   "This file is shared under the same licence, CC BY-SA 4.0."),
    },
    "se-howto": {
        "title": "Stack Exchange how-to index (se-howto.sqlite)",
        "description": "Questions from 45 Stack Exchange sites with a short excerpt of the best answer, used to answer how-to searches. Built by tools/se-howto.py.",
        "licence": CC_BY_SA,
        "notice": ("Contains content from Stack Exchange network sites (the official data dump, uploaded by Stack Exchange, Inc.), licensed under "
                   "Creative Commons Attribution-ShareAlike: CC BY-SA 2.5, 3.0 or 4.0 by the date of each answer "
                   "(https://creativecommons.org/licenses/by-sa/4.0/). Each result that shows an excerpt links the question, names the asker "
                   "and the answer's author with a link to each profile, and marks the licence version. Excerpts are cut and have markup and "
                   "code blocks removed. Changes: titles, tags and excerpts reduced and re-indexed by Logout (tools/se-howto.py). "
                   "This file is shared under CC BY-SA 4.0."),
    },
}


def stored(src: str, dst_dir: str, rel: str, compress: str):
    """Put the file in dst_dir/files/ (hard link or copy; or xz), -> (stored path, transport entry or None)."""
    os.makedirs(os.path.join(dst_dir, "files"), exist_ok=True)
    if compress == "none":
        dst = os.path.join(dst_dir, "files", rel)
        if os.path.exists(dst):
            os.remove(dst)
        try:
            os.link(src, dst)
        except OSError:
            shutil.copyfile(src, dst)
        return dst, None
    dst = os.path.join(dst_dir, "files", rel + ".xz")
    with open(src, "rb") as fi, lzma.open(dst, "wb", preset=6) as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)
    sha, n = inst.file_digest(dst)
    return dst, {"encoding": "xz", "bytes": n, "sha256": sha}


def build(a) -> dict:
    d = DEFAULTS[a.name]
    spec = inst.ADDONS[a.name]
    if os.path.basename(a.file) != spec["file"]:
        raise SystemExit(f"{a.file}: the {a.name} add-on is the file {spec['file']}")
    if not os.path.isfile(a.file):
        raise SystemExit(f"{a.file}: no such file")
    sha, n = inst.file_digest(a.file)
    out = os.path.join(a.out, "addons", a.name)
    os.makedirs(out, exist_ok=True)
    _p, t = stored(a.file, out, spec["file"], a.compress)
    notice = open(a.notice_file, encoding="utf-8").read() if a.notice_file else d["notice"]
    m = {"format": inst.ADDON_FORMAT, "kind": "addon", "name": a.name, "version": a.version,
         "title": a.title or d["title"], "description": d["description"], "fits_releases": a.fits,
         "licence": {"name": a.licence_name or d["licence"][0], "url": a.licence_url or d["licence"][1]},
         "notice": notice, "total_bytes": n, "files": [{"path": spec["file"], "bytes": n, "sha256": sha}]}
    if t:
        m["transport"] = {"files": {spec["file"]: t}}
    body = (json.dumps(m, indent=1, sort_keys=True) + "\n").encode()
    inst.check_addon_manifest(body, a.name)                       # what we write must be what the installer accepts
    sig = hf_dataset.sign(a.key, body)
    with open(os.path.join(out, "manifest.json"), "wb") as f:
        f.write(body)
    with open(os.path.join(out, "manifest.json.sig"), "wb") as f:
        f.write(sig)
    return {"addon": a.name, "version": a.version, "dir": out, "bytes": n, "sha256": sha, "stored_bytes": (t or {"bytes": n})["bytes"]}


def check(a) -> dict:
    pub = inst.load_pubkey(a.pubkey)
    base = os.path.join(a.out, "addons", a.name)
    body = open(os.path.join(base, "manifest.json"), "rb").read()
    if not inst.ed25519_verify(pub, body, open(os.path.join(base, "manifest.json.sig"), "rb").read()):
        raise inst.InstallError(f"{base}/manifest.json.sig does not verify with the given key")
    m = inst.check_addon_manifest(body, a.name)
    f = m["files"][0]
    t = (m.get("transport") or {}).get("files", {}).get(f["path"])
    p = os.path.join(base, "files", f["path"] + (".xz" if t else ""))
    sha, n = inst.file_digest(p)
    want = t or f
    if (n, sha) != (want["bytes"], want["sha256"]):
        raise inst.InstallError(f"{p}: {n} bytes / {sha[:16]}..., the manifest says {want['bytes']} / {want['sha256'][:16]}...")
    return {"addon": a.name, "ok": True, "bytes": f["bytes"]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    b = sp.add_parser("build")
    b.add_argument("--name", required=True, choices=sorted(inst.ADDONS))
    b.add_argument("--file", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--version", required=True)
    b.add_argument("--fits", action="append", required=True, help="a release id or pattern the add-on works with (repeatable)")
    b.add_argument("--key", required=True, help="PEM private key (the release key for a real add-on)")
    b.add_argument("--compress", choices=["none", "xz"], default="none")
    b.add_argument("--title")
    b.add_argument("--licence-name")
    b.add_argument("--licence-url")
    b.add_argument("--notice-file", help="replace the built-in NOTICE text")
    c = sp.add_parser("check")
    c.add_argument("out")
    c.add_argument("--name", required=True, choices=sorted(inst.ADDONS))
    c.add_argument("--pubkey", required=True)
    a = ap.parse_args()
    try:
        r = build(a) if a.cmd == "build" else check(a)
    except (inst.InstallError, hf_dataset.DatasetError) as e:
        print(f"addon_build: REFUSED: {e}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(r, sort_keys=True))


if __name__ == "__main__":
    main()
