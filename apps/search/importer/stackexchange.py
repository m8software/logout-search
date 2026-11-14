"""Stack Exchange Q&A (2026-09-24) — the how-to answers local search lacked.

Source (decision 2026-10-17, docs/legal/SEARCH-DATA-SOURCES.md): the official archive.org item
`stackexchange`, uploaded by Stack Exchange, Inc. (April 2024 files, one 7z per site, CC BY-SA 4.0,
no download agreement). NOT the community re-upload `stackexchange_20260630` and NOT the per-site
download on the Stack Exchange site, whose click-through asks for "own use" and "no LLM training".
Attribution = the link back to each question, which every result carries, and its authors (below). A curated
list of English sites; Stack Overflow itself (69 GB) is a separate, later job.

Per site, Posts.xml is streamed out of the 7z (never unpacked to disk) and reduced to
one row per answered question with a positive score: title, tags, the start of the
question and the start of its best answer (the accepted one, else the highest-scored).

Attribution (owner decision 2026-10-22: the item's terms, followed exactly): every result shows who asked, and who
wrote the answer whose words the page is indexed by, each linked to the author's profile. `se-authors` reads the
owners out of the same archives (Posts.xml OwnerUserId / OwnerDisplayName, Users.xml DisplayName) into se_authors,
without touching se_docs; the release carries them in registry.sqlite's `authors` table.
"""

from __future__ import annotations

import html
import os
import re
import subprocess
import time
import urllib.request
import xml.etree.ElementTree as ET

DUMP = os.environ.get("SE_DUMP", "stackexchange")           # the archive.org item name
DUMP_TAG = os.environ.get("SE_DUMP_TAG", "20240408")          # goes into page_docs.crawl ('open:stackexchange:<tag>')
DIR = os.environ.get("SE_DIR", "/stackexchange")                # the archives (compose mounts ./stackexchange here)
USER_AGENT = "logout-search-import/1 (personal search index)"
SITES = """superuser.com askubuntu.com serverfault.com unix.stackexchange.com diy.stackexchange.com
cooking.stackexchange.com apple.stackexchange.com android.stackexchange.com gaming.stackexchange.com
travel.stackexchange.com money.stackexchange.com mechanics.stackexchange.com gardening.stackexchange.com
bicycles.stackexchange.com softwareengineering.stackexchange.com security.stackexchange.com
webapps.stackexchange.com photo.stackexchange.com electronics.stackexchange.com
raspberrypi.stackexchange.com dba.stackexchange.com english.stackexchange.com law.stackexchange.com
health.stackexchange.com pets.stackexchange.com outdoors.stackexchange.com homebrew.stackexchange.com
woodworking.stackexchange.com 3dprinting.stackexchange.com networkengineering.stackexchange.com""".split()

Q_CHARS, A_CHARS = 800, 1500
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def log(msg: str) -> None:
    print(f"[stackexchange] {msg}", flush=True)


def text_of(body: str, n: int) -> str:
    """Post HTML -> plain text, first n chars (code blocks kept as text)."""
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", body or ""))).strip()[:n]


def tags_of(raw: str) -> str:
    """'<linux><bash>' (old) or '|linux|bash|' (2024+) -> 'linux bash'."""
    return " ".join(t for t in re.split(r"[<>|]+", raw or "") if t)


def best_key(accepted_id, a: dict) -> tuple[int, int]:
    """How a question's answers are ranked: the accepted one first, then by score (the first on a tie)."""
    return (1 if accepted_id == a.get("Id") else 0, int(a.get("Score", 0)))


def reduce_posts(rows) -> list[tuple]:
    """Iterable of Posts.xml row attribute dicts -> (qid, title, tags, score, question, answer).

    Questions come before their answers in the file (ids ascend). Keep answered questions
    with score >= 1; their best answer is the accepted one, else the highest-scored.
    """
    qs: dict[int, list] = {}
    best: dict[int, tuple[int, int, str]] = {}   # qid -> (is_accepted, score, text)
    for a in rows:
        kind = a.get("PostTypeId")
        if kind == "1":
            if int(a.get("Score", 0)) >= 1 and int(a.get("AnswerCount", 0)) > 0:
                qs[int(a["Id"])] = [a.get("Title", ""), tags_of(a.get("Tags", "")), int(a["Score"]),
                                    text_of(a.get("Body", ""), Q_CHARS), a.get("AcceptedAnswerId")]
        elif kind == "2":
            qid = int(a.get("ParentId", 0))
            q = qs.get(qid)
            if q is None:
                continue
            key = best_key(q[4], a)
            if qid not in best or key > best[qid][:2]:
                best[qid] = (*key, a.get("Body", ""))
    out = []
    for qid, (title, tags, score, qtext, _acc) in qs.items():
        b = best.get(qid)
        if b and b[1] >= 0:
            out.append((qid, title, tags, score, qtext, text_of(b[2], A_CHARS)))
    return out


# --- authors (se-authors) ---------------------------------------------------------------------------
ASKED, ANSWERED = 1, 2          # se_authors.role: the question's owner; the owner of the answer se_docs kept
_INT = re.compile(r"^-?[0-9]+$")


def owner_of(a: dict) -> tuple:
    """A post's (OwnerUserId as int or None, OwnerDisplayName or None). The dump gives OwnerDisplayName only
    for a post whose account is gone (then OwnerUserId is absent)."""
    uid = a.get("OwnerUserId") or ""
    name = (a.get("OwnerDisplayName") or "").replace("\x00", "").strip()
    return (int(uid) if _INT.match(uid) else None, name or None)


def reduce_owners(rows, keep) -> dict:
    """Posts.xml rows -> {qid: (question owner, answer owner or None)} for the question ids in `keep` (se_docs'
    questions of the site). The answer is the one reduce_posts() kept for the page text: best_key(), the first
    on a tie, and none when the best has a negative score."""
    qs: dict[int, tuple] = {}                    # qid -> (AcceptedAnswerId, owner)
    best: dict[int, tuple] = {}                  # qid -> (best_key, owner)
    for a in rows:
        kind = a.get("PostTypeId")
        if kind == "1":
            qid = int(a["Id"])
            if qid in keep:
                qs[qid] = (a.get("AcceptedAnswerId"), owner_of(a))
        elif kind == "2":
            qid = int(a.get("ParentId", 0))
            q = qs.get(qid)
            if q is None:
                continue
            key = best_key(q[0], a)
            if qid not in best or key > best[qid][0]:
                best[qid] = (key, owner_of(a))
    out = {}
    for qid, (_acc, owner) in qs.items():
        b = best.get(qid)
        out[qid] = (owner, b[1] if b and b[0][1] >= 0 else None)
    return out


def user_names(rows, want) -> dict:
    """Users.xml rows -> {Id: DisplayName} for the ids in `want`."""
    out = {}
    for a in rows:
        uid = a.get("Id") or ""
        if _INT.match(uid) and int(uid) in want:
            name = (a.get("DisplayName") or "").replace("\x00", "").strip()
            if name:
                out[int(uid)] = name
    return out


def credit(owner: tuple, names: dict) -> tuple:
    """(user_id, display_name) to show. An account that is in Users.xml: its id (the profile link) and its name.
    Otherwise no id (the profile is gone) and the post's own OwnerDisplayName, or None ("a deleted user")."""
    uid, shown = owner
    if uid is not None and uid in names:
        return uid, names[uid]
    return None, shown


def author_rows(site: str, owners: dict, names: dict):
    """-> (site, qid, role, user_id, display_name) rows, sorted by (qid, role)."""
    for qid in sorted(owners):
        q, a = owners[qid]
        yield (site, qid, ASKED, *credit(q, names))
        if a is not None:
            yield (site, qid, ANSWERED, *credit(a, names))


def _posts(archive: str, member: str = "Posts.xml"):
    """Stream one XML file (Posts.xml, Users.xml) out of the 7z and yield each row's attributes."""
    p = subprocess.Popen(["7z", "x", "-so", archive, member], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    try:
        for _ev, el in ET.iterparse(p.stdout, events=("end",)):
            if el.tag == "row":
                yield dict(el.attrib)
            el.clear()
    finally:
        p.stdout.close()
        p.wait()


def url_for(site: str, dump: str | None = None) -> str:
    """The official item keeps its archives at the item's root; a community mirror keeps them in a folder named
    after the item (404 without it)."""
    dump = dump or DUMP
    if dump == "stackexchange":
        return f"https://archive.org/download/{dump}/{site}.7z"
    return f"https://archive.org/download/{dump}/{dump}/{site}.7z"


def download(site: str) -> str:
    """One site's 7z from archive.org, once (a partial download is resumed)."""
    os.makedirs(DIR, exist_ok=True)
    dest = os.path.join(DIR, f"{site}.7z")
    if os.path.exists(dest):
        return dest
    # The mirror keeps the archives in a folder named after the item (404 without it).
    url = url_for(site)
    tmp = dest + ".part"
    have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Range": f"bytes={have}-"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "ab") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    os.replace(tmp, dest)
    return dest


def cmd_stackexchange(conn, sites: list[str] | None = None) -> None:
    sites = sites or SITES
    t0 = time.time()
    conn.execute("DROP TABLE IF EXISTS se_docs_new")
    conn.execute("CREATE TABLE se_docs_new (site text, qid integer, title text, tags text, score integer, "
                 "question text, answer text, PRIMARY KEY (site, qid))")
    total = 0
    for site in sites:
        s0 = time.time()
        archive = download(site)                      # one archive at a time: polite to archive.org
        rows = reduce_posts(_posts(archive))
        with conn.transaction(), conn.cursor().copy(
                "COPY se_docs_new (site, qid, title, tags, score, question, answer) FROM STDIN") as cp:
            for r in rows:
                cp.write_row((site, *(v.replace("\x00", "") if isinstance(v, str) else v for v in r)))
        total += len(rows)
        log(f"{site}: {len(rows):,} answered questions in {time.time() - s0:.0f}s (total {total:,})")
    with conn.transaction():
        conn.execute("ALTER TABLE se_docs_new ADD COLUMN search_text text GENERATED ALWAYS AS ("
                     "title || ' ' || tags || ' ' || coalesce(question, '') || ' ' || coalesce(answer, '')) STORED")
        conn.execute("DROP TABLE IF EXISTS se_docs")
        conn.execute("ALTER TABLE se_docs_new RENAME TO se_docs")
        conn.execute("ALTER INDEX IF EXISTS se_docs_new_pkey RENAME TO se_docs_pkey")
        conn.execute("CREATE INDEX se_docs_bm25 ON se_docs USING bm25 (search_text) WITH (text_config = 'english')")
        conn.execute("CREATE INDEX se_docs_title_bm25 ON se_docs USING bm25 (title) WITH (text_config = 'english')")
    log(f"se_docs ready: {total:,} questions from {len(sites)} sites in {(time.time() - t0) / 60:.1f} min")


def cmd_se_authors(conn, sites: list[str] | None = None) -> None:
    """se_authors(site, qid, role, user_id, display_name) for every question in se_docs: who asked (role 1) and who
    wrote the answer se_docs kept (role 2). Reads Posts.xml and Users.xml out of the archives se_docs was built
    from (DIR, one 7z per site, never downloaded here: another dump could rank another answer first). se_docs is
    only read."""
    if conn.execute("SELECT to_regclass('se_docs')").fetchone()[0] is None:
        raise SystemExit("no se_docs table — run `search_import.py stackexchange` first")
    have = [r[0] for r in conn.execute("SELECT DISTINCT site FROM se_docs ORDER BY site").fetchall()]
    sites = sorted(sites) if sites else have
    missing = [s for s in sites if not os.path.exists(os.path.join(DIR, f"{s}.7z"))]
    if missing:
        raise SystemExit(f"{DIR}: no archive for {missing} — mount the folder se_docs was imported from "
                         f"(the official item {DUMP!r}, files dated {DUMP_TAG})")
    t0 = time.time()
    conn.execute("DROP TABLE IF EXISTS se_authors_new")
    conn.execute("CREATE TABLE se_authors_new (site text, qid integer, role smallint, user_id integer, "
                 "display_name text, PRIMARY KEY (site, qid, role))")
    total = 0
    for site in sites:
        s0 = time.time()
        keep = {r[0] for r in conn.execute("SELECT qid FROM se_docs WHERE site = %s", (site,)).fetchall()}
        archive = os.path.join(DIR, f"{site}.7z")
        owners = reduce_owners(_posts(archive), keep)
        want = {o[0] for pair in owners.values() for o in pair if o is not None and o[0] is not None}
        names = user_names(_posts(archive, "Users.xml"), want)
        rows = list(author_rows(site, owners, names))
        with conn.transaction(), conn.cursor().copy(
                "COPY se_authors_new (site, qid, role, user_id, display_name) FROM STDIN") as cp:
            for r in rows:
                cp.write_row(r)
        total += len(rows)
        log(f"{site}: {len(owners):,} of {len(keep):,} questions found, {len(rows):,} credits, "
            f"{sum(1 for r in rows if r[3] is None):,} without a profile, in {time.time() - s0:.0f}s (total {total:,})")
    with conn.transaction():
        conn.execute("DROP TABLE IF EXISTS se_authors")
        conn.execute("ALTER TABLE se_authors_new RENAME TO se_authors")
        conn.execute("ALTER INDEX IF EXISTS se_authors_new_pkey RENAME TO se_authors_pkey")
    log(f"se_authors ready: {total:,} credits from {len(sites)} sites in {(time.time() - t0) / 60:.1f} min")
