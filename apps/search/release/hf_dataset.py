#!/usr/bin/env python3
"""The public Hugging Face dataset a home engine installs and updates from (SEARCH-HOME-ENGINE-PLAN.md §5; the licensing
audit, docs/internal/audits/2026-10-09-silo-licensing-audit.md: a dataset card, and a licence notice for every file).

    hf_dataset.py counts RELEASE_DIR > counts.json          what the release holds, counted from its files (for the card)
    hf_dataset.py stage PUBLISH_DIR OUT --repo ORG/NAME --sign KEY.pem [--update PKG_DIR ...] [--counts counts.json] [--addons DIR] [--test-key]
    hf_dataset.py card PUBLISH_DIR --repo ORG/NAME --pubkey HEX [--latest OUT/latest.json] [--counts counts.json] [--addons DIR] > README.md
    hf_dataset.py check OUT --pubkey KEY
    hf_dataset.py upload OUT --repo ORG/NAME --token-file F [--public] [--dry-run]

What the dataset holds on `main` (= the newest release), one git tag per release (the tag is the release id):

    manifest.json  manifest.json.sig  files/<path>[.xz|.zst]   the release exactly as `assemble.py build --publish` wrote it
    updates/<from release>/   update_pkg.py's package from that release to this one, + package.json(.sig): every file, size, sha256
    LICENSES/                 the licence texts and NOTICE.txt, readable (the copies under files/ are compressed)
    LICENSE                   which licence covers which file: the per-file notice Hugging Face's terms leave in force
    README.md                 the dataset card
    latest.json(.sig)         {format, release, revision, manifest_sha256, updates}: what installers and updaters read first

`stage` only writes OUT (hard links where it can). `upload` sends everything but latest.json, removes what the new release no
longer has, and sends latest.json LAST: an installer that reads `main` meanwhile still gets the old release. The tag goes on that
last commit, so the release's tag holds its own latest.json (tagged before it, the tag held the previous release's).
`stage` needs `cryptography` or the `openssl` command to sign; `check` is standard library; `upload` needs huggingface_hub.
The card's numbers (articles, sites, places...) are never typed in: `counts` reads them from the release's own files, and
`card` re-renders the card alone, so a wording change needs no new release.
"""
import argparse
import hashlib
import json
import lzma
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "installer"))
import install as inst  # noqa: E402  (stdlib: ed25519 verify, safe paths, the latest.json reader)

LATEST_FORMAT = inst.LATEST_FORMAT
PACKAGE_FORMAT = 1
REMOVAL = "help@logout.cloud"


class DatasetError(Exception):
    pass


def sha256_file(p: str) -> str:
    return inst.file_digest(p)[0]


def sign(key: str, body: bytes) -> bytes:
    """ed25519 over `body` with a PEM private key: the `cryptography` package, else `openssl pkeyutl -rawin`."""
    try:
        from cryptography.hazmat.primitives import serialization
        return serialization.load_pem_private_key(open(key, "rb").read(), password=None).sign(body)
    except ImportError:
        pass
    with tempfile.NamedTemporaryFile() as f:          # ed25519 signs in one shot: openssl needs a file, not a pipe
        f.write(body)
        f.flush()
        r = subprocess.run(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", key, "-in", f.name], capture_output=True)
    if r.returncode or len(r.stdout) != 64:
        raise DatasetError(f"cannot sign with {key}: {r.stderr.decode(errors='replace')[:200]}")
    return r.stdout


def public_hex(key: str) -> str:
    """The 32-byte public half of a PEM private key, as hex (what the card prints and installers pin)."""
    try:
        from cryptography.hazmat.primitives import serialization
        k = serialization.load_pem_private_key(open(key, "rb").read(), password=None).public_key()
        return k.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    except ImportError:
        r = subprocess.run(["openssl", "pkey", "-in", key, "-pubout", "-outform", "DER"], capture_output=True)
        if r.returncode or len(r.stdout) != 44:
            raise DatasetError(f"cannot read the public key of {key}")
        return r.stdout[12:].hex()


def place(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def stored_path(m: dict, path: str) -> str:
    t = ((m.get("transport") or {}).get("files") or {}).get(path)
    return "files/" + path + ({"xz": ".xz", "zstd": ".zst"}[t["encoding"]] if t else "")


def walk(root: str) -> list:
    out = []
    for d, dirs, fs in os.walk(root):
        dirs.sort()
        for f in sorted(fs):
            out.append(os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/"))
    return out


def read_plain(pub: str, m: dict, path: str) -> bytes:
    raw = open(os.path.join(pub, *stored_path(m, path).split("/")), "rb").read()
    enc = (((m.get("transport") or {}).get("files") or {}).get(path) or {}).get("encoding")
    if enc == "xz":
        return lzma.decompress(raw)
    if enc == "zstd":                                      # zstd is the publisher's default since 2026-11-14
        try:
            import zstandard
            return zstandard.ZstdDecompressor().decompressobj().decompress(raw)
        except ImportError:
            pass
        import shutil
        import subprocess
        if shutil.which("zstd") is None:
            raise DatasetError(f"{path} is stored as zstd: install the zstandard package or the zstd command to read it")
        r = subprocess.run(["zstd", "-dc"], input=raw, capture_output=True)
        if r.returncode:
            raise DatasetError(f"{path}: zstd could not decode it ({r.stderr.decode(errors='replace')[:200]})")
        return r.stdout
    return raw


# ------------------------------------------------------------------------------------------------ card and LICENSE
def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return str(n)


def licence_file(m: dict, extra: list) -> str:
    lines = [
        "Licences of the files in this dataset",
        "",
        "There is no single licence. Every file below is under the licence named beside it, and only that; the",
        "texts are in LICENSES/, the attributions in LICENSES/NOTICE.txt. A file stored compressed (files/...xz) is",
        "under the licence of the file it decodes to. Nothing here is offered under any other terms.",
        "",
        f"Release {m['release']}. Removal requests: {REMOVAL}.",
        "",
    ]
    for f in sorted(m["files"], key=lambda f: f["path"]):
        lines.append(f"{stored_path(m, f['path'])}\n    {f['licence']}")
    for p, lic in extra:
        lines.append(f"{p}\n    {lic}")
    return "\n".join(lines) + "\n"


def count(n: int) -> str:
    """6,790,408 -> "6.8 million"; 311,687 -> "311,687"; 25,763 -> "25,763"."""
    return (f"{n / 1e6:.1f}".rstrip("0").rstrip(".") + " million") if n >= 1_000_000 else f"{n:,}"


# registry doc types when a registry's meta does not say (registry layout 3 has them in meta.types)
TYPES = {"wiki": 1, "site": 2, "page": 3, "place": 4, "account": 5, "youtube": 6, "stock": 7, "link": 8}


def counts(rel: str) -> dict:
    """What the release holds, counted from its SQLite files (the uncompressed release, not the publish folder). A file the
    release lacks is skipped: the card then has no number for it."""
    rel = os.path.abspath(rel)
    out = {}

    def q(name, sql):
        p = os.path.join(rel, name + ".sqlite")
        if not os.path.isfile(p):
            return None
        db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            return db.execute(sql).fetchone()[0]
        finally:
            db.close()

    p = os.path.join(rel, "registry.sqlite")
    if os.path.isfile(p):
        db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        meta = dict(db.execute("SELECT key, value FROM meta"))
        types = json.loads(meta["types"]) if "types" in meta else TYPES
        shift = int(meta.get("type_shift", 28))
        by = dict(db.execute(f"SELECT doc_id >> {shift}, count(*) FROM registry GROUP BY 1").fetchall())
        for t, code in types.items():
            out[t] = by.get(code, 0)
        out["documents"] = sum(by.values())
        if db.execute("SELECT count(*) FROM sqlite_master WHERE name = 'platforms'").fetchone()[0]:
            out["platforms"] = db.execute("SELECT count(*) FROM platforms").fetchone()[0]
        db.close()
    for key, name, sql in (
        ("facts", "facts", "SELECT count(*) FROM facts"),
        ("facts_items", "facts", "SELECT count(DISTINCT doc_id) FROM facts"),
        ("pictures", "pictures", "SELECT count(*) FROM pictures"),
        ("places_osm", "places-osm", "SELECT count(*) FROM places"),
        ("places_overture", "places-overture", "SELECT count(*) FROM places"),
        ("hours", "hours-web", "SELECT count(*) FROM hours_web"),
        ("anchors", "anchor-phrases", "SELECT count(*) FROM anchor_phrase"),
        ("kinds", "kinds", "SELECT count(*) FROM kind_keys"),
        ("kind_sites", "kinds", "SELECT count(*) FROM kind_docs"),
    ):
        n = q(name, sql)
        if n is not None:
            out[key] = n
    return out


def parts(m: dict, c: dict) -> str:
    """The "What is in it" table: part, count (when counts are given), source, licence. Rows name only what the release has."""
    paths = {f["path"] for f in m["files"]}
    has = lambda *ps: any(any(p == x or x.startswith(p.rstrip("/") + "/") for x in paths) for p in ps)  # noqa: E731
    g = lambda k: count(c[k]) if c and k in c else ""  # noqa: E731
    rows = []
    if has("text/wiki/"):
        rows.append(("Wikipedia articles: title, lead, word index, meaning vector", g("wiki"), "English Wikipedia", "CC BY-SA 4.0"))
    if has("facts.sqlite"):
        rows.append(("Facts about articles", g("facts"), "Wikidata", "CC0 1.0"))
    if has("pictures.sqlite"):
        rows.append(("Pictures, as thumbnails", g("pictures"), "Wikimedia Commons", "per picture, in its row"))
    if has("places-osm.sqlite", "places-overture.sqlite"):
        n = count(c["places_osm"] + c.get("places_overture", 0)) if c and "places_osm" in c else ""
        rows.append(("Places in the United States", n, "OpenStreetMap, Overture Maps", "ODbL 1.0; Overture CDLA-Permissive-2.0"))
    if has("hours-web.sqlite"):
        rows.append(("Opening hours", g("hours"), "businesses' own pages, All the Places", "facts; All the Places CC0 1.0"))
    if has("text/sites/sites/"):
        rows.append(("Websites: address, title, description, word index", g("site"), "Common Crawl", "the sites' own words; our compilation CC0 1.0"))
    if has("kinds.sqlite"):
        rows.append(("Site categories", g("kinds"), "Wikidata, Wikipedia, UT1, Open Directory Project and others", "CC BY-SA 4.0"))
    if has("text/sites/pages/"):
        rows.append(("Questions and guide pages", g("page"), "Stack Exchange, Wikibooks, Wikivoyage, MedlinePlus", "CC BY-SA; MedlinePlus public domain"))
    if c and c.get("account"):
        rows.append(("Official profiles of article subjects", g("account"), "Wikidata, SEC", "CC0 1.0"))
    if has("signals.bin", "ranker.json"):
        rows.append(("Ranking signals and weights", "", "Wikimedia, Common Crawl, our own", "CC0 1.0"))
    if has("models/"):
        rows.append(("Models (ONNX)", "3", "nomic-embed-text-v1.5, st3 (our fine-tune of jina-reranker-v1-turbo-en), bge-small-en-v1.5", "Apache-2.0, Apache-2.0, MIT"))
    cols = ["Part", "Count", "Source", "Licence"] if c else ["Part", "Source", "Licence"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        r = list(r) if c else [r[0]] + list(r[2:])
        lines.append("| " + " | ".join(x.replace("|", "/") for x in r) + " |")
    return "\n".join(lines)


def size_category(n: int) -> str:
    for top, name in ((1e3, "n<1K"), (1e4, "1K<n<10K"), (1e5, "10K<n<100K"), (1e6, "100K<n<1M"), (1e7, "1M<n<10M"),
                      (1e8, "10M<n<100M"), (1e9, "100M<n<1B")):
        if n < top:
            return name
    return "n>1B"


def addons_section(addons_dir: str, repo: str, pubhex: str) -> str:
    """The card's Add-ons section, read from each add-on's own manifest (OUT/addons/<name>/manifest.json, addon_build.py):
    sizes and the releases it fits are never typed in. A folder with no manifest is not an add-on and is skipped."""
    rows, names = [], []
    for name in sorted(os.listdir(addons_dir)):
        mf = os.path.join(addons_dir, name, "manifest.json")
        if not os.path.isfile(mf):
            continue
        a = json.load(open(mf))
        tr = (a.get("transport") or {}).get("files") or {}
        down = sum(tr.get(f["path"], f)["bytes"] for f in a["files"])
        rows.append(f"| `{a['name']}` | {a['description']} | {human(down)} | {human(a['total_bytes'])} | "
                    f"{', '.join(a['fits_releases'])} | {a['licence']['name']} |")
        names.append(a["name"])
    if not rows:
        return ""
    cmds = "\n".join(f"python install.py addon install {n} --source hf:{repo} --pubkey ed25519:{pubhex} --dest <folder>" for n in names)
    return f"""
## Add-ons

Optional data published beside the releases under `addons/<name>/`, each with its own signed manifest (the same key) and its
own licence. They are not part of a release: install the ones you want after the release, then restart the engine.

| Add-on | What | Download | Installed | Fits | Licence |
|---|---|---|---|---|---|
""" + "\n".join(rows) + f"""

```
{cmds}
python install.py start --dest <folder>
```

`install.py addon list --dest <folder>` shows what is installed; `install.py addon remove <name> --dest <folder>` takes one away.
An add-on is used only with the releases it fits; after an update it waits for a matching one.
"""


def card(m: dict, repo: str, pubhex: str, test_key: bool, updates: dict, c: dict = None, addons: str = "") -> str:
    total = m["total_bytes"]
    stored = sum(((m.get("transport") or {}).get("files") or {}).get(f["path"], f)["bytes"] for f in m["files"])
    warn = ("\n> **Signed with a TEST key.** This copy is for checking the dataset layout; do not install it. The release key\n"
            "> replaces this one before the first public release.\n") if test_key else ""
    ups = "".join(f"- `updates/{k}/`: from `{k}`\n" for k in sorted(updates)) or "- none (this is the first release)\n"
    tpl = os.path.join(HERE, "hf", "README.md")            # the hand-written card: what each part is, for people
    t = open(tpl).read()
    if not t.startswith("---\n") or "\n---\n" not in t[4:]:
        raise DatasetError(f"{tpl} has no front matter")
    front, body = t[4:].split("\n---\n", 1)
    if "license: other" not in front or "license_link: LICENSE" not in front:
        raise DatasetError(f"{tpl}: the front matter must say license: other and license_link: LICENSE")
    if c and c.get("documents"):
        front += f"\nsize_categories:\n- {size_category(c['documents'])}"
    lead = (f"This release: {count(c['wiki'])} Wikipedia articles, {count(c['site'])} websites, "
            f"{count(c.get('places_osm', 0) + c.get('places_overture', 0))} places and {count(c['pictures'])} pictures. "
            f"{human(stored)} to download, {human(total)} installed.") if c and {"wiki", "site", "pictures"} <= set(c) else \
           f"This release: {len(m['files'])} files, {human(stored)} to download, {human(total)} installed."
    body = body.replace("<!-- lead -->", lead).replace("<!-- parts -->", parts(m, c))
    head = "---\n" + front + "\n---\n" + body.rstrip("\n") + "\n"
    addons = addons_section(addons, repo, pubhex) if addons else ""
    return head + f"""
## Install

```
python install.py install --source hf:{repo} --pubkey ed25519:{pubhex} --dest <folder>
```

The installer checks every file against the signed `manifest.json` before switching over. Public key:
`{pubhex}`; it also signs `latest.json` and each update's `package.json`.
{warn}{addons}
## Releases

This is **`{m['release']}`**. Each release is a tag of this repository; `latest.json` on `main` names the newest, and
`hf:{repo}@<release>` installs one release. Updates let an installed engine download only what changed:
{ups}
## Contact

M8 Software LLC, **{REMOVAL}**.
"""


# ------------------------------------------------------------------------------------------------ stage
def load_publish(pub: str) -> tuple:
    try:
        raw = open(os.path.join(pub, "manifest.json"), "rb").read()
        m = json.loads(raw)
    except (OSError, ValueError) as e:
        raise DatasetError(f"{pub}/manifest.json unreadable: {e}")
    if m.get("from_release") is not None:
        raise DatasetError(f"{pub} is an update, not a full release")
    if not os.path.isfile(os.path.join(pub, "manifest.json.sig")):
        raise DatasetError(f"{pub} has no manifest.json.sig")
    for f in m["files"]:
        inst.safe_rel(f["path"])
        if not f.get("licence"):
            raise DatasetError(f"{f['path']} has no licence tag")
        p = os.path.join(pub, *stored_path(m, f["path"]).split("/"))
        want = (((m.get("transport") or {}).get("files") or {}).get(f["path"]) or f)["bytes"]
        if not os.path.isfile(p) or os.path.getsize(p) != want:
            raise DatasetError(f"{stored_path(m, f['path'])} missing or not {want} bytes: not a complete publish folder")
    return m, raw


def load_counts(path: str) -> dict:
    if not path:
        return None
    try:
        c = json.load(open(path))
    except (OSError, ValueError) as e:
        raise DatasetError(f"--counts {path}: {e}")
    if not isinstance(c, dict) or not all(isinstance(v, int) for v in c.values()):
        raise DatasetError(f"--counts {path}: not a JSON object of integers (hf_dataset.py counts RELEASE_DIR writes one)")
    return c


CHAIN_MAX = 40          # hops kept in latest.json's chain: an install further behind than this installs the release itself


def stage(pub: str, out: str, repo: str, key: str, updates: list, test_key: bool, counts_file: str = None, addons: str = "",
          prev: str = None, chain_max: int = CHAIN_MAX) -> dict:
    pub, out = os.path.abspath(pub), os.path.abspath(out)
    if not inst.HF_RE.match("hf:" + repo):
        raise DatasetError(f"--repo {repo!r} is not ORG/NAME")
    if os.path.exists(out):
        raise DatasetError(f"{out} exists: stage into a new folder")
    m, raw = load_publish(pub)
    rid = m["release"]
    os.makedirs(out + ".partial")
    tmp = out + ".partial"
    for name in ("manifest.json", "manifest.json.sig"):
        place(os.path.join(pub, name), os.path.join(tmp, name))
    for f in m["files"]:
        sp = stored_path(m, f["path"])
        place(os.path.join(pub, *sp.split("/")), os.path.join(tmp, *sp.split("/")))
    extra = []
    for f in m["files"]:
        if f["path"].startswith("LICENSES/"):
            dst = os.path.join(tmp, *f["path"].split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            data = read_plain(pub, m, f["path"])
            if hashlib.sha256(data).hexdigest() != f["sha256"]:
                raise DatasetError(f"{f['path']} does not decode to the bytes the manifest lists")
            open(dst, "wb").write(data)
            extra.append((f["path"], f["licence"] + " (readable copy)"))
    if not any(p == "LICENSES/NOTICE.txt" for p, _ in extra):
        raise DatasetError("the release has no LICENSES/NOTICE.txt: the attributions must travel with the data")
    ups = {}
    for pkg in updates or []:
        pkg = os.path.abspath(pkg)
        try:
            upd = json.load(open(os.path.join(pkg, "update.json")))
        except (OSError, ValueError) as e:
            raise DatasetError(f"{pkg}/update.json unreadable: {e}")
        frm = upd.get("from_release")
        if upd.get("to_release") != rid:
            raise DatasetError(f"{pkg} updates to {upd.get('to_release')!r}, this release is {rid!r}")
        if upd.get("to_manifest_sha256") != hashlib.sha256(raw).hexdigest():
            raise DatasetError(f"{pkg} was built for another manifest of {rid}")
        if not isinstance(frm, str) or not inst.ID_RE.match(frm) or frm in ups:
            raise DatasetError(f"{pkg}: from_release {frm!r} is not a plain name, or two packages start from it")
        if not os.path.isfile(os.path.join(pkg, "update.json.sig")):
            raise DatasetError(f"{pkg} is not signed (update_pkg.py build --sign)")
        dest = f"updates/{frm}"
        listed = []
        for rel in walk(pkg):
            if rel in ("package.json", "package.json.sig"):
                continue
            inst.safe_rel(rel)
            place(os.path.join(pkg, *rel.split("/")), os.path.join(tmp, dest, *rel.split("/")))
            p = os.path.join(pkg, *rel.split("/"))
            listed.append({"path": rel, "bytes": os.path.getsize(p), "sha256": sha256_file(p)})
        body = json.dumps({"format": PACKAGE_FORMAT, "from_release": frm, "to_release": rid, "files": listed},
                          indent=1, sort_keys=True).encode()
        open(os.path.join(tmp, dest, "package.json"), "wb").write(body)
        open(os.path.join(tmp, dest, "package.json.sig"), "wb").write(sign(key, body))
        extra.append((f"{dest}/", "the files of release " + rid + " they rebuild, as listed above"))
        ups[frm] = dest
    # the chain (daily cadence): the hops already published (the previous staged folder, `prev`) + this release's own hops.
    # Each hop is its own folder updates/<from> (a release id is unique, so no two hops share a name) and its own signed
    # package.json; latest.json (signed) lists them in order, oldest first, ending at this release.
    chain = []
    if prev:
        prev = os.path.abspath(prev)
        old_latest = json.load(open(os.path.join(prev, "latest.json")))
        for h in old_latest.get("chain") or []:
            if h["to"] == rid or h["from"] == rid:
                continue                                  # a re-stage of this release: its own hops are rebuilt above
            chain.append(h)
        keep = max(0, chain_max - len(ups))
        chain = chain[-keep:] if keep else []
        for h in chain:
            inst.safe_rel(h["package"])
            for rel in walk(os.path.join(prev, *h["package"].split("/"))):
                place(os.path.join(prev, *h["package"].split("/"), *rel.split("/")), os.path.join(tmp, *h["package"].split("/"), *rel.split("/")))
            extra.append((h["package"] + "/", "an earlier daily update, still published for installs that are behind"))
    for frm, dest in sorted(ups.items()):
        chain.append({"from": frm, "to": rid, "package": dest,
                      "bytes": sum(os.path.getsize(os.path.join(tmp, dest, *r.split("/"))) for r in walk(os.path.join(tmp, dest)))})
    pubhex = public_hex(key)
    open(os.path.join(tmp, "LICENSE"), "w").write(licence_file(m, extra))
    open(os.path.join(tmp, "README.md"), "w").write(card(m, repo, pubhex, test_key, ups, load_counts(counts_file), addons))
    latest = json.dumps({"format": LATEST_FORMAT, "release": rid, "revision": rid, "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                         "updates": ups, "chain": chain}, indent=1, sort_keys=True).encode()
    open(os.path.join(tmp, "latest.json"), "wb").write(latest)
    open(os.path.join(tmp, "latest.json.sig"), "wb").write(sign(key, latest))
    os.rename(tmp, out)
    return {"release": rid, "files": len(walk(out)), "bytes": sum(os.path.getsize(os.path.join(out, p)) for p in walk(out)),
            "updates": sorted(ups), "public_key": pubhex}


# ------------------------------------------------------------------------------------------------ check
def check(out: str, pubkey: str) -> dict:
    """Everything an installer and an updater will verify, done here first: signatures, every listed file's size and sha256,
    a licence line for every file, nothing unlisted."""
    pub = inst.load_pubkey(pubkey)
    src = inst.Source(out)
    latest = inst.read_latest(src, pub)
    raw = open(os.path.join(out, "manifest.json"), "rb").read()
    if not inst.ed25519_verify(pub, raw, open(os.path.join(out, "manifest.json.sig"), "rb").read()):
        raise DatasetError("manifest.json.sig does not verify")
    m = inst.check_manifest(raw)
    if hashlib.sha256(raw).hexdigest() != latest["manifest_sha256"] or m["release"] != latest["release"]:
        raise DatasetError("latest.json names another manifest")
    seen = {"manifest.json", "manifest.json.sig", "latest.json", "latest.json.sig", "README.md", "LICENSE"}
    transport = (m.get("transport") or {}).get("files") or {}
    for f in m["files"]:
        sp = stored_path(m, f["path"])
        t = transport.get(f["path"], f)
        p = os.path.join(out, *sp.split("/"))
        if not os.path.isfile(p) or os.path.getsize(p) != t["bytes"] or sha256_file(p) != t["sha256"]:
            raise DatasetError(f"{sp}: missing, wrong size or wrong sha256")
        seen.add(sp)
        if f["path"].startswith("LICENSES/"):
            seen.add(f["path"])
            if sha256_file(os.path.join(out, *f["path"].split("/"))) != f["sha256"]:
                raise DatasetError(f"readable {f['path']} differs from the release's")
    for frm, dest in latest["updates"].items():
        body = open(os.path.join(out, dest, "package.json"), "rb").read()
        if not inst.ed25519_verify(pub, body, open(os.path.join(out, dest, "package.json.sig"), "rb").read()):
            raise DatasetError(f"{dest}/package.json.sig does not verify")
        pk = json.loads(body)
        if pk.get("from_release") != frm or pk.get("to_release") != m["release"]:
            raise DatasetError(f"{dest}/package.json is not the update {frm} -> {m['release']}")
        seen.update({f"{dest}/package.json", f"{dest}/package.json.sig"})
        for f in pk["files"]:
            p = os.path.join(out, dest, *inst.safe_rel(f["path"]).split("/"))
            if not os.path.isfile(p) or os.path.getsize(p) != f["bytes"] or sha256_file(p) != f["sha256"]:
                raise DatasetError(f"{dest}/{f['path']}: missing, wrong size or wrong sha256")
            seen.add(f"{dest}/{f['path']}")
    hops = latest.get("chain") or []
    for h in hops:
        if h["to"] == m["release"]:
            continue                                      # this release's own hops were checked above (they are in `updates`)
        dest = h["package"]
        body = open(os.path.join(out, dest, "package.json"), "rb").read()
        if not inst.ed25519_verify(pub, body, open(os.path.join(out, dest, "package.json.sig"), "rb").read()):
            raise DatasetError(f"{dest}/package.json.sig does not verify")
        pk = json.loads(body)
        if pk.get("from_release") != h["from"] or pk.get("to_release") != h["to"]:
            raise DatasetError(f"{dest}/package.json is not the update {h['from']} -> {h['to']}")
        seen.update({f"{dest}/package.json", f"{dest}/package.json.sig"})
        for f in pk["files"]:
            p = os.path.join(out, dest, *inst.safe_rel(f["path"]).split("/"))
            if not os.path.isfile(p) or os.path.getsize(p) != f["bytes"] or sha256_file(p) != f["sha256"]:
                raise DatasetError(f"{dest}/{f['path']}: missing, wrong size or wrong sha256")
            seen.add(f"{dest}/{f['path']}")
    nodes = [h["from"] for h in hops] + ([hops[-1]["to"]] if hops else [])
    if hops and (nodes[-1] != m["release"] or any(hops[i]["to"] != hops[i + 1]["from"] for i in range(len(hops) - 1))):
        raise DatasetError("latest.json's chain is not one unbroken line of releases ending at this one")
    unlisted = sorted(set(walk(out)) - seen)
    if unlisted:
        raise DatasetError(f"files nothing lists (an installer would never check them): {unlisted[:5]}")
    lic = open(os.path.join(out, "LICENSE")).read()
    missing = [stored_path(m, f["path"]) for f in m["files"] if f"{stored_path(m, f['path'])}\n    {f['licence']}" not in lic]
    if missing:
        raise DatasetError(f"LICENSE has no line for {missing[:5]}")
    readme = open(os.path.join(out, "README.md")).read()
    front = readme[4:].split("\n---\n", 1)[0] if readme.startswith("---\n") else ""
    if "license: other" not in front or "license_link: LICENSE" not in front:
        raise DatasetError("README.md has no dataset-card header pointing at LICENSE")
    # what only the dataset says (the card, LICENSE, latest.json, the update packages and their folder names) under the
    # release check's "nothing internal" rule; the release's own files had it at release_v1.py check --tranco-free
    sys.path.insert(0, HERE)
    import release_check  # noqa: E402  (stdlib)
    said = ["README.md", "LICENSE", "latest.json"] + [f"{d}/{n}" for d in sorted({h["package"] for h in hops} | set(latest["updates"].values())) for n in ("package.json", "update.json")]
    for p in said:
        hit = release_check.INTERNAL_WORDS.search(p + "\n" + open(os.path.join(out, p), encoding="utf-8").read())
        if hit:
            raise DatasetError(f"{p} says {hit.group(0)!r}: internal words do not ship")
    return {"release": m["release"], "files": len(seen), "updates": sorted(latest["updates"]), "ok": True}


# ------------------------------------------------------------------------------------------------ upload
LAST = ("latest.json", "latest.json.sig")
BETA_LAST = ("latest-beta.json", "latest-beta.json.sig")   # the beta channel's pointer: installs on `beta` follow it, the others never read it
# published beside the releases, each signed on its own, never a release's stale file: the add-ons (install.py ADDONS) and the
# daily curated lists (install.py `lists update`, sent by data-update-publish.sh)
KEEP_DIRS = ("addons/", "lists/")


def pointer_ops(out: str, channel: str, beta_exists: bool) -> list:
    """(path in the repository, local file) of the pointer files sent last. A beta publish moves only the beta pointer (the
    staged latest.json goes up as latest-beta.json: the signature covers the same bytes); a stable publish moves latest.json
    and, when a beta pointer exists, the beta pointer with it: beta is never behind stable."""
    ops = []
    if channel == "beta":
        ops += [(b, l) for b, l in zip(BETA_LAST, LAST)]
    else:
        ops += [(n, n) for n in LAST]
        if beta_exists:
            ops += [(b, l) for b, l in zip(BETA_LAST, LAST)]
    return [(repo_path, os.path.join(out, local_name)) for repo_path, local_name in ops]


def upload(out: str, repo: str, token_file: str, public: bool, dry_run: bool, channel: str = "stable") -> dict:
    latest = json.load(open(os.path.join(out, "latest.json")))
    rid = latest["release"]
    local = [p for p in walk(out) if p not in LAST]
    plan = {"repo": repo, "release": rid, "channel": channel, "files": len(local),
            "bytes": sum(os.path.getsize(os.path.join(out, p)) for p in local),
            "then": ["remove files the release no longer has", ("latest-beta.json" if channel == "beta" else "latest.json") + " last", f"tag {rid} on that commit"]}
    if dry_run:
        return dict(plan, dry_run=True)
    from huggingface_hub import CommitOperationAdd, HfApi     # 2.x: upload_folder uploads and deletes in ONE commit
    token = open(token_file).read().strip()
    api = HfApi(token=token)
    api.create_repo(repo, repo_type="dataset", private=not public, exist_ok=True)
    remote = set(api.list_repo_files(repo, repo_type="dataset"))
    # what the new release no longer has, by exact name (glob characters never occur in release paths: refused below)
    stale = sorted(p for p in remote - set(local) - set(LAST) - set(BETA_LAST) - {".gitattributes"} if not p.startswith(KEEP_DIRS))
    if any(c in p for p in stale for c in "*?[]"):
        raise DatasetError(f"a remote path holds a glob character, it cannot be removed by name: {stale[:3]}")
    # a re-run after a failure re-sends only what the Xet store does not already hold
    api.upload_folder(repo_id=repo, folder_path=out, repo_type="dataset", ignore_patterns=list(LAST),
                      delete_patterns=stale or None, commit_message=f"release {rid}")
    done = api.create_commit(repo, repo_type="dataset", commit_message=f"latest: {rid}",
                             operations=[CommitOperationAdd(path_in_repo=rp, path_or_fileobj=lp)
                                         for rp, lp in pointer_ops(out, channel, BETA_LAST[0] in remote)])
    # a re-upload of the same release id moves its tag here; exist_ok alone would leave it on the earlier upload
    if rid in {t.name for t in api.list_repo_refs(repo, repo_type="dataset").tags}:
        api.delete_tag(repo, tag=rid, repo_type="dataset")
    api.create_tag(repo, tag=rid, revision=done.oid, repo_type="dataset", tag_message=f"release {rid}")
    return dict(plan, removed=len(stale), public=public)


def promote(repo: str, token_file: str, dry_run: bool) -> dict:
    """latest.json := latest-beta.json, bytes and signature as they are (the signature covers the same bytes)."""
    from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download
    api = HfApi(token=open(token_file).read().strip())
    got = {n: hf_hub_download(repo, n, repo_type="dataset", token=api.token) for n in BETA_LAST}
    doc = json.load(open(got[BETA_LAST[0]]))
    plan = {"repo": repo, "promote": doc["release"], "dry_run": dry_run}
    if dry_run:
        return plan
    api.create_commit(repo, repo_type="dataset", commit_message=f"latest: {doc['release']} (promoted from beta)",
                      operations=[CommitOperationAdd(path_in_repo=n, path_or_fileobj=got[b]) for n, b in zip(LAST, BETA_LAST)])
    return plan


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stage")
    s.add_argument("publish")
    s.add_argument("out")
    s.add_argument("--repo", required=True)
    s.add_argument("--sign", required=True, help="PEM ed25519 private key (the release key)")
    s.add_argument("--update", action="append", default=[], help="an update_pkg.py package INTO this release (repeatable)")
    s.add_argument("--prev", help="the previously staged folder: its chain of daily updates is kept and this release's hop is appended (CHAIN_MAX hops)")
    s.add_argument("--chain-max", type=int, default=CHAIN_MAX)
    s.add_argument("--test-key", action="store_true", help="the card says the copy is signed with a test key")
    s.add_argument("--counts", help="counts.json from `counts RELEASE_DIR`: the card's numbers")
    s.add_argument("--addons", help="the built add-ons (addon_build.py OUT/addons): the card keeps its Add-ons section; nothing of them is staged")
    n = sub.add_parser("counts")
    n.add_argument("release", help="the uncompressed release folder (assemble.py build --out)")
    k = sub.add_parser("card")
    k.add_argument("publish")
    k.add_argument("--repo", required=True)
    k.add_argument("--pubkey", required=True, help="the release key's public half, hex")
    k.add_argument("--latest", help="a staged latest.json: the updates the card lists")
    k.add_argument("--counts")
    k.add_argument("--test-key", action="store_true")
    k.add_argument("--addons", help="a folder of built add-ons (addon_build.py OUT/addons): the card gets an Add-ons section from their manifests")
    c = sub.add_parser("check")
    c.add_argument("out")
    c.add_argument("--pubkey", required=True)
    u = sub.add_parser("upload")
    u.add_argument("out")
    u.add_argument("--repo", required=True)
    u.add_argument("--token-file")
    u.add_argument("--public", action="store_true", help="create the repository public (default: private, made public by hand)")
    u.add_argument("--dry-run", action="store_true")
    u.add_argument("--channel", choices=("stable", "beta"), default="stable",
                   help="beta: send the release and move only latest-beta.json (the stable pointer stays); promote it later with `promote`")
    pr = sub.add_parser("promote", help="make the beta release the stable one: latest.json := latest-beta.json (one commit; nothing is uploaded)")
    pr.add_argument("--repo", required=True)
    pr.add_argument("--token-file", required=True)
    pr.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    try:
        if a.cmd == "stage":
            r = stage(a.publish, a.out, a.repo, a.sign, a.update, a.test_key, a.counts, a.addons or "", a.prev, a.chain_max)
        elif a.cmd == "counts":
            r = counts(a.release)
        elif a.cmd == "card":
            if not inst.HF_RE.match("hf:" + a.repo) or not re.fullmatch(r"[0-9a-f]{64}", a.pubkey):
                raise DatasetError("--repo is ORG/NAME and --pubkey 64 hex characters")
            m, _ = load_publish(a.publish)
            ups = json.load(open(a.latest))["updates"] if a.latest else {}
            sys.stdout.write(card(m, a.repo, a.pubkey, a.test_key, ups, load_counts(a.counts), a.addons or ""))
            return 0
        elif a.cmd == "check":
            r = check(a.out, a.pubkey)
        elif a.cmd == "promote":
            r = promote(a.repo, a.token_file, a.dry_run)
        else:
            if not a.dry_run and not a.token_file:
                raise DatasetError("--token-file is needed (a file holding a write token; it is never printed)")
            r = upload(a.out, a.repo, a.token_file, a.public, a.dry_run, a.channel)
    except (DatasetError, inst.InstallError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    print(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
