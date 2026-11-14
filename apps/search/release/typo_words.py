#!/usr/bin/env python3
"""typo-words.txt: the words of English as a file of the release, for the typo path.

    typo_words.py build OEWN.xml.gz OUT.txt     from Open English WordNet's LMF file
    typo_words.py check OUT.txt                 the shape the engine reads; the problems, exit 1

Source: Open English WordNet 2025 (https://en-word.net/, CC BY 4.0, built on Princeton WordNet 3.1). Only the spelling of
each lemma and listed form is kept, one lowercase word of letters per line, sorted. Never a definition, an example or
a synset (ADR-059). A lemma of several words gives each word ("ice cream" -> ice, cream). Words of one letter are left
out: they would make every stray letter a word.
"""
import gzip
import os
import re
import sys
import xml.etree.ElementTree as ET

WORD = re.compile(r"[a-z]{2,}\Z")
PART = re.compile(r"[^\W\d_]+")
MIN_WORDS, MAX_WORDS = 50_000, 500_000     # OEWN 2025 gives about 105,000; far from that is the wrong file


def words_of(lemma: str) -> set:
    return {w for w in PART.findall(lemma.lower()) if WORD.match(w)}


def build(src: str, dst: str) -> int:
    out = set()
    with gzip.open(src, "rb") as f:
        for _ev, el in ET.iterparse(f):
            if el.tag in ("Lemma", "Form"):
                out |= words_of(el.get("writtenForm") or "")
            elif el.tag == "LexicalEntry":
                el.clear()
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(w + "\n" for w in sorted(out)))
    errs = check(dst)
    if errs:
        raise SystemExit(f"{dst}: " + "; ".join(errs))
    return len(out)


def check(path: str) -> list:
    if not os.path.isfile(path):
        return ["no such file"]
    with open(path, encoding="utf-8") as f:
        words = f.read().split("\n")
    if words[-1] != "":
        return ["no newline at the end"]
    words = words[:-1]
    errs = []
    if not MIN_WORDS <= len(words) <= MAX_WORDS:
        errs.append(f"{len(words)} words, expected {MIN_WORDS} to {MAX_WORDS}")
    if words != sorted(set(words)):
        errs.append("not sorted and unique")
    bad = [w for w in words if not WORD.match(w)]
    if bad:
        errs.append(f"{len(bad)} lines are not one lowercase word, first {bad[0]!r}")
    return errs


def main(argv) -> int:
    if len(argv) == 4 and argv[1] == "build":
        print(f"{argv[3]}: {build(argv[2], argv[3])} words")
        return 0
    if len(argv) == 3 and argv[1] == "check":
        errs = check(argv[2])
        for e in errs:
            print(f"{argv[2]}: {e}", file=sys.stderr)
        return 1 if errs else 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
