"""The registry's identity rules, stdlib only — ONE definition for everyone.

registry.sqlite gives every thing the engine can point at one doc_id. A thing's *identity* (its
`ident`, the string a builder knows it by) is rebuilt from the registry row itself, so nothing is
stored twice. This module owns that rebuild, used by:

  * release_v1.py registry --previous   (survivors keep their doc_id; the rule must be exact);
  * the lookup builder (T4, apps/search/tools/sqlite_lookups/build.py): (type, ident) -> doc_id;
  * `release_v1.py idhash`              (per-type count + sha256 of the sorted idents: compare a
                                         registry with a builder's staged universe across machines).

doc_id = type << 28 | seq (TYPE_SHIFT). Types and ident rules (docs/internal/SEARCH-RELEASE-FORMAT.md):

  type  name     ident                                   registry row
  1     wiki     str(page_id)                            ext = page_id, title
  2     site     the host string                         path = host, title (docs), alt = url if not
                                                         'https://' || host || '/'
  3     page     url                                     host_id -> hosts.origin, path; alt = url when it
                                                         has no scheme://host form; title
  4     place    the id of the dated place files:        path = that id (title NULL: names stay in the place
                 "node/123" | "way/123" (OSM, ODbL) or    files, one per licence). The prefixes cannot
                 "ov:<GERS uuid>" (Overture, CDLA); never collide, so one type holds both. A GERS id
                 "wdc:..."                               that changes between Overture releases is a NEW
                                                         place: the old id is tombstoned, a new doc minted
  5     account  platform TAB value TAB qid              host_id -> platforms.id, path = value,
                                                         ext = qid number ('Q123' -> 123); alt = url if
                                                         not prefix || value || suffix; title NULL (the
                                                         name is the entity's: lookup entities.name)
  6     youtube  channel_id                              path = channel_id, title = name
  7     stock    ticker                                  path = ticker, title = coalesce(label, name)
  8     link     url                                     host_id -> hosts.origin, path (or alt); title

An account's qid is "Q" + digits or empty (ext NULL).
"""
import sqlite3

# doc_id = type << 28 | seq: 4 bits of type (15 types; 8 used), 28 bits of seq (268 M per type; the
# biggest type, accounts, is 3.7% of that). The whole id fits 32 bits, so a u32 consumer works, and it is
# far inside SQLite's 64-bit INTEGER and a JavaScript number. signals.bin keeps ONE array per type, sized
# by that type's own highest seq, so a wide id space costs nothing there.
TYPE_SHIFT = 28
SEQ_MASK = (1 << TYPE_SHIFT) - 1
MAX_TYPE = (1 << (32 - TYPE_SHIFT)) - 1
TYPES = {"wiki": 1, "site": 2, "page": 3, "place": 4, "account": 5, "youtube": 6, "stock": 7, "link": 8}
TYPE_NAMES = {v: k for k, v in TYPES.items()}
assert max(TYPES.values()) <= MAX_TYPE
# the lookup builder's stand-in numbering (build.py before the registry existed) -> this registry's
T4_TO_V1 = {1: 1, 2: 2, 3: 5, 4: 6, 5: 7, 6: 4, 7: 8}
LAYOUT = "3"                  # meta.layout of registry.sqlite: bump when a type's row layout changes
WIKI_BASE = "https://en.wikipedia.org/wiki/"


def ident_select(reg: str = "registry", hosts: str = "hosts", platforms: str = "platforms",
                 types: tuple = ()) -> str:
    """SELECT doc_id, type, ident over a registry (the tables may be schema-qualified)."""
    where = f"WHERE r.doc_id >> {TYPE_SHIFT} IN ({', '.join(str(t) for t in types)})" if types else ""
    return f"""SELECT r.doc_id AS doc_id, r.doc_id >> {TYPE_SHIFT} AS type,
        CASE r.doc_id >> {TYPE_SHIFT}
          WHEN 1 THEN CAST(r.ext AS TEXT)
          WHEN 2 THEN r.path
          WHEN 3 THEN coalesce(h.origin || r.path, r.alt)
          WHEN 4 THEN r.path
          WHEN 5 THEN p.name || char(9) || r.path || char(9) || coalesce('Q' || r.ext, '')
          WHEN 6 THEN r.path
          WHEN 7 THEN r.path
          WHEN 8 THEN coalesce(h.origin || r.path, r.alt)
        END AS ident
      FROM {reg} r
      LEFT JOIN {hosts} h ON h.id = r.host_id AND r.doc_id >> {TYPE_SHIFT} IN (3, 8)
      LEFT JOIN {platforms} p ON p.id = r.host_id AND r.doc_id >> {TYPE_SHIFT} = 5
      {where}"""


def idmap(conn: sqlite3.Connection, registry_path: str, name: str = "idmap", types: tuple = ()) -> int:
    """Create TEMP table <name>(type, ident, doc_id), PRIMARY KEY (type, ident) WITHOUT ROWID, for
    every doc of the registry (or only `types`). Returns the row count. A builder translates with
    `JOIN idmap m ON m.type = ? AND m.ident = <its key>`; a (type, ident) it needs and the registry
    lacks is the builder's error to report."""
    conn.execute("ATTACH ? AS regsrc", (registry_path,))
    meta = dict(conn.execute("SELECT key, value FROM regsrc.meta"))
    if meta.get("layout") != LAYOUT:
        conn.execute("DETACH regsrc")
        raise SystemExit(f"{registry_path}: registry layout {meta.get('layout')!r}, this code reads {LAYOUT!r}")
    conn.execute(f"DROP TABLE IF EXISTS temp.{name}")
    conn.execute(f"CREATE TEMP TABLE {name} (type INTEGER, ident TEXT, doc_id INTEGER, "
                 f"PRIMARY KEY (type, ident)) WITHOUT ROWID")
    # sorted insert: the primary key is appended to, never searched
    conn.execute(f"""INSERT INTO temp.{name} SELECT type, ident, doc_id FROM (
        {ident_select('regsrc.registry', 'regsrc.hosts', 'regsrc.platforms', types)})
        WHERE ident IS NOT NULL ORDER BY type, ident""")
    n = conn.execute(f"SELECT count(*) FROM temp.{name}").fetchone()[0]
    conn.commit()
    conn.execute("DETACH regsrc")
    return n


def url_of(conn: sqlite3.Connection, doc_id: int, schema: str = "main"):
    """The URL a registry row stands for (None for types without one). For readers and tests."""
    r = conn.execute(f"""SELECT r.doc_id >> {TYPE_SHIFT}, r.ext, r.path, r.title, r.alt, h.origin, p.prefix, p.suffix
                         FROM {schema}.registry r LEFT JOIN {schema}.hosts h ON h.id = r.host_id
                         LEFT JOIN {schema}.platforms p ON p.id = r.host_id WHERE r.doc_id = ?""", (doc_id,)).fetchone()
    if r is None:
        return None
    t, ext, path, title, alt, origin, prefix, suffix = r
    if t == 1:
        return WIKI_BASE + (title or "").replace(" ", "_")
    if t == 2:
        return alt if alt is not None else f"https://{path}/"
    if t in (3, 8):
        return origin + path if origin is not None else alt
    if t == 5:
        return alt if alt is not None else (prefix or "") + path + (suffix or "")
    if t == 6:
        return f"https://www.youtube.com/channel/{path}"
    return None


def idhash(path: str) -> dict:
    """Per type: how many idents and the sha256 of the sorted idents ("ident\\n" each). On a registry it
    rebuilds them by the shared rule; on a lookup builder's staged file it reads its `things` table
    (types mapped with T4_TO_V1). Two machines compare these lines instead of copying a gigabyte:
    `python3 registry_idents.py idhash FILE`."""
    import hashlib
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.execute("PRAGMA cache_size = -400000")
    db.execute("PRAGMA temp_store = FILE")
    tabs = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "things" in tabs:
        rows = ((T4_TO_V1[t], k) for t, k in db.execute("SELECT type, key FROM things ORDER BY type, key"))
    else:
        rows = db.execute(f"SELECT type, ident FROM ({ident_select()}) WHERE ident IS NOT NULL ORDER BY type, ident")
    out, cur, h, n = {}, None, None, 0
    for t, ident in rows:
        if t != cur:
            if cur is not None:
                out[TYPE_NAMES[cur]] = {"n": n, "sha256": h.hexdigest()}
            cur, h, n = t, hashlib.sha256(), 0
        h.update(ident.encode("utf-8") + b"\n")
        n += 1
    if cur is not None:
        out[TYPE_NAMES[cur]] = {"n": n, "sha256": h.hexdigest()}
    return dict(sorted(out.items()))


def _selftest() -> None:
    """python3 registry_idents.py — every type's ident and url rebuild from a hand-made registry."""
    import os
    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "r.sqlite")
    c = sqlite3.connect(p)
    c.executescript(f"""
      CREATE TABLE registry (doc_id INTEGER PRIMARY KEY, ext INTEGER, host_id INTEGER, path TEXT, title TEXT, alt TEXT);
      CREATE TABLE hosts (id INTEGER PRIMARY KEY, origin TEXT UNIQUE NOT NULL);
      CREATE TABLE platforms (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, prefix TEXT, suffix TEXT);
      CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
      INSERT INTO meta VALUES ('layout', '{LAYOUT}');
      INSERT INTO hosts VALUES (1, 'https://a.example');
      INSERT INTO platforms VALUES (1, 'instagram', 'https://www.instagram.com/', '/');
    """)
    rows = [
        (1 << TYPE_SHIFT, 4242, None, None, "Foo Bar", None),
        (2 << TYPE_SHIFT, None, None, "x.example", "X", None),
        ((2 << TYPE_SHIFT) | 1, None, None, "y.example", None, "http://www.y.example/"),
        (3 << TYPE_SHIFT, None, 1, "/p?q=1", "P", None),
        (4 << TYPE_SHIFT, None, None, "node/7", None, None),
        ((4 << TYPE_SHIFT) | 1, None, None, "ov:af02d818-3255-412e-924b-93d0c4e2f784", None, None),
        (5 << TYPE_SHIFT, 100, 1, "nasa", None, None),
        ((5 << TYPE_SHIFT) | 1, None, 1, "bare", None, "https://other.example/bare"),
        (6 << TYPE_SHIFT, None, None, "UC123", "Chan", None),
        (7 << TYPE_SHIFT, None, None, "TICK", "Tick Corp", None),
        (8 << TYPE_SHIFT, None, 1, "/l", "L", None),
        ((8 << TYPE_SHIFT) | 1, None, None, None, "N", "weird-url"),
    ]
    c.executemany("INSERT INTO registry VALUES (?,?,?,?,?,?)", rows)
    c.commit()
    want = {(1, "4242"), (2, "x.example"), (2, "y.example"), (3, "https://a.example/p?q=1"), (4, "node/7"), (4, "ov:af02d818-3255-412e-924b-93d0c4e2f784"),
            (5, "instagram\tnasa\tQ100"), (5, "instagram\tbare\t"), (6, "UC123"), (7, "TICK"),
            (8, "https://a.example/l"), (8, "weird-url")}
    m = sqlite3.connect(":memory:")
    n = idmap(m, p)
    got = {(t, i) for t, i in m.execute("SELECT type, ident FROM idmap")}
    assert n == len(rows) and got == want, (got ^ want)
    urls = {d: url_of(c, d) for d, *_ in rows}
    assert urls[1 << TYPE_SHIFT] == WIKI_BASE + "Foo_Bar"
    assert urls[2 << TYPE_SHIFT] == "https://x.example/" and urls[(2 << TYPE_SHIFT) | 1] == "http://www.y.example/"
    assert urls[3 << TYPE_SHIFT] == "https://a.example/p?q=1"
    assert urls[5 << TYPE_SHIFT] == "https://www.instagram.com/nasa/" and urls[(5 << TYPE_SHIFT) | 1] == "https://other.example/bare"
    assert urls[6 << TYPE_SHIFT] == "https://www.youtube.com/channel/UC123" and urls[7 << TYPE_SHIFT] is None
    assert urls[8 << TYPE_SHIFT] == "https://a.example/l" and urls[(8 << TYPE_SHIFT) | 1] == "weird-url"
    assert not any(i.startswith("wdc:") for _t, i in got), "a wdc: ident in a registry"
    print("registry_idents selftest ok:", n, "docs,", len(want), "idents")


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "idhash":
        print(json.dumps({"stage": "idhash", "path": sys.argv[2], "types": idhash(sys.argv[2])}))
    else:
        _selftest()
