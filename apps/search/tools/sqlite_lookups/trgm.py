"""pg_trgm-compatible trigrams in Python (T4, SEARCH-HOME-ENGINE-PLAN.md).

pg_trgm (contrib/pg_trgm/trgm_op.c generate_trgm): lowercase the string, split it into
words of alphanumeric characters, pad each word with two spaces in front and one behind,
take every 3-character window, and keep the distinct ones. similarity(a, b) =
shared / (|A| + |B| - shared), computed in float4.

Postgres hashes a trigram with a multibyte character into 3 bytes (compact_trigram, CRC32);
here the trigram is kept as characters, so equality is the same except for a CRC collision.
validate.py checks this file against the live Postgres similarity().
"""
import struct
import unicodedata

# the separator between padded words in an FTS5 trigram index: it never appears in a
# word, so no indexed trigram spans two words
SEP = "\x01"


def pg_lower(s: str) -> str:
    """lower() one character at a time, like glibc towlower: "İ" -> "i", not "i" + a dot."""
    return "".join(ch.lower()[:1] if ch.isupper() else ch for ch in s)


def words(s: str) -> list[str]:
    out, cur = [], []
    for ch in pg_lower(s):
        # glibc's iswalnum (what pg_trgm asks) also counts combining marks (Thai, Indic
        # vowel signs) as letters; str.isalnum does not
        if ch.isalnum() or (ch > "\x7f" and unicodedata.category(ch)[0] == "M"):
            cur.append(ch)
        elif cur:
            out.append("".join(cur)); cur = []
    if cur:
        out.append("".join(cur))
    return out


def trigrams(s: str) -> set[str]:
    out = set()
    for w in words(s):
        p = "  " + w + " "
        for i in range(len(p) - 2):
            out.add(p[i:i + 3])
    return out


def f32(x: float) -> float:
    return struct.unpack("f", struct.pack("f", x))[0]


def sim_counts(shared: int, na: int, nb: int) -> float:
    """similarity() from the counts, rounded through float4 like pg_trgm."""
    if na + nb - shared <= 0:
        return 0.0
    return f32(f32(shared) / f32(na + nb - shared))


def similarity(a: str, b: str) -> float:
    ta, tb = trigrams(a), trigrams(b)
    return sim_counts(len(ta & tb), len(ta), len(tb))


def padded(s: str) -> str:
    """The text an FTS5 trigram index stores for s: every padded word, SEP between them,
    so the index holds exactly pg_trgm's trigrams (plus a few that contain SEP)."""
    return SEP.join("  " + w + " " for w in words(s))


def fts_phrase(t: str) -> str:
    return '"' + t.replace('"', '""') + '"'
