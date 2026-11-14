"""Check trgm.py against the live Postgres: similarity() and lower() (T4).

  python validate.py N      N random (a, b) pairs from wiki titles, place names and
                            suggest keys, each against itself mutated and against a
                            neighbour; prints the disagreements.
Runs read-only against PGHOST.
"""
import random, sys
import psycopg
import trgm

N = int(sys.argv[1]) if len(sys.argv) > 1 else 10000
rnd = random.Random(7)
c = psycopg.connect()
src = []
for sql in ("SELECT title FROM wiki_docs TABLESAMPLE SYSTEM (0.2) LIMIT %s",
            "SELECT name FROM places TABLESAMPLE SYSTEM (0.5) WHERE name <> '' LIMIT %s",
            "SELECT key FROM suggest_keys TABLESAMPLE SYSTEM (0.5) LIMIT %s",
            "SELECT key FROM suggest_keys WHERE key ~ '[^a-z0-9]' LIMIT %s"):
    src += [r[0] for r in c.execute(sql, (N // 4,))]
rnd.shuffle(src)


def mutate(s):
    if len(s) < 2:
        return s + "x"
    i = rnd.randrange(len(s))
    op = rnd.randrange(3)
    if op == 0:
        return s[:i] + s[i + 1:]
    if op == 1:
        return s[:i] + rnd.choice("aeiourstn") + s[i + 1:]
    return s[:i] + s[i + 1:i + 2] + s[i:i + 1] + s[i + 2:]


pairs = []
for i, s in enumerate(src):
    pairs.append((s, mutate(s)))
    pairs.append((s, src[i - 1]))
bad_sim = bad_low = 0
ex = []
for k in range(0, len(pairs), 2000):
    chunk = pairs[k:k + 2000]
    rows = c.execute("SELECT similarity(a, b), lower(a) FROM unnest(%s::text[], %s::text[]) AS t(a, b)",
                     ([a for a, _ in chunk], [b for _, b in chunk])).fetchall()
    for (a, b), (ps, pl) in zip(chunk, rows):
        mine = trgm.similarity(a, b)
        if abs(mine - ps) > 1e-6:
            bad_sim += 1
            if len(ex) < 15:
                ex.append((a, b, ps, mine))
        if pl != trgm.pg_lower(a):
            bad_low += 1
print(f"pairs={len(pairs)} similarity_mismatch={bad_sim} lower_mismatch={bad_low} "
      f"non_ascii_pairs={sum(1 for a, b in pairs if not (a + b).isascii())}")
for e in ex:
    print("  ", repr(e))
