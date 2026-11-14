#!/usr/bin/env bash
################################################################################
# Unit tests for the release's web Tantivy indexes (sites, pages, anchors; SEARCH-RELEASE-FORMAT
# rules 1, 3, 5, 7): tools/tantivy-structure/web_input.py (ident -> registry doc_id, anchor text),
# pop_order.py hostranks/sitepop/sprefix (popularity order) and `tantivy-structure build --kind`
# through local-build.sh.
#
# Part 1 (python3 + zstd): the input preparation. Part 2 (docker image logout-tantivy-build:4 and
# a python with tantivy==0.26.2: PY=...; else SKIP): the three indexes are built twice with
# different job counts and must be byte-identical, hold only doc_ids, come out in popularity order.
#
# Usage: bash tests/unit/test-search-web-index.sh     (TD_DIR=<tool dir> to test a mutated copy)
################################################################################
set -uo pipefail
SCRIPT_DIR_T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR_T/../.." && pwd)"
TD="${TD_DIR:-$REPO_ROOT/apps/search/tools/tantivy-structure}"
PY="${PY:-python3}"
IMG="${IMG:-logout-tantivy-build:4}"
export PYTHONDONTWRITEBYTECODE=1
PASS=0; FAIL=0
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; NC=$'\033[0m'
ok()  { echo "  ${GREEN}PASS${NC} $1"; PASS=$((PASS+1)); }
bad() { echo "  ${RED}FAIL${NC} $1"; shift; [[ $# -gt 0 ]] && printf '       %s\n' "$@"; FAIL=$((FAIL+1)); }
expect() { if grep -qxF -- "$1" <<<"$out"; then ok "$2"; else bad "$2" "want: $1" "got:  $(head -c 900 <<<"$out")"; fi; }

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
command -v zstd >/dev/null || { echo "  SKIP: zstd missing"; echo "Total: 0  PASS: 0  FAIL: 0 (skipped)"; exit 0; }

# ---- fixture: a registry (type shift 28), a signals.bin, rows --------------------------------
python3 - "$tmp" <<'PY'
import json, sqlite3, struct, sys
t = sys.argv[1]
S = 28
db = sqlite3.connect(t + "/registry.sqlite")
db.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT); CREATE TABLE hosts (id INTEGER PRIMARY KEY, origin TEXT UNIQUE NOT NULL);"
                 "CREATE TABLE registry (doc_id INTEGER PRIMARY KEY, ext INTEGER, host_id INTEGER, path TEXT, title TEXT, alt TEXT);")
db.execute("INSERT INTO meta VALUES ('type_shift', ?)", (str(S),))
# sites in seq order (doc_id order) that is NOT popularity order
SITES = ["slow.example", "golf.nl", "bücher.example", "tie-b.example", "tie-a.example", "hub.example", "tie-c.example", "nav.example"]
for i, h in enumerate(SITES):
    db.execute("INSERT INTO registry VALUES (?, NULL, NULL, ?, NULL, NULL)", ((2 << S) | i, h))
db.execute("INSERT INTO hosts VALUES (1, 'https://golf.nl'), (2, 'https://slow.example')")
PAGES = [(2, "/z/last"), (1, "/ranked/one"), (1, "/ranked/two"), (2, "/a/first")]
for i, (hid, p) in enumerate(PAGES):
    db.execute("INSERT INTO registry VALUES (?, NULL, ?, ?, ?, NULL)", ((3 << S) | i, hid, p, "title %d" % i))
db.execute("INSERT INTO registry VALUES (?, NULL, NULL, 'x', NULL, 'mailto:odd@odd.example')", ((3 << S) | 9,))
db.commit()
# signals: tranco (seq-indexed u4), cc_hc_pos, nav_rank over the site seq range
N = len(SITES)
tranco = [0] * N; cc = [0] * N; nav = [0] * N
tranco[1] = 5          # golf.nl
cc[2] = 3              # bücher: Common Crawl rank only
tranco[5] = 1          # hub.example: the most popular
nav[7] = 2             # nav.example: nav rank only (Tranco-derived)
cc[0] = 900            # slow.example: worse cc than bücher
fields, blob = [], b""
for name, arr in (("site.tranco_rank", tranco), ("site.cc_hc_pos", cc), ("site.nav_rank", nav)):
    raw = struct.pack("<%dI" % N, *arr)
    fields.append({"name": name, "type": "site", "dtype": "<u4", "count": N, "offset": len(blob)})
    blob += raw
head = json.dumps({"format": 1, "type_shift": S, "fields": fields}).encode()
open(t + "/signals.bin", "wb").write(b"LOSIGNL1" + struct.pack("<I", len(head)) + head + blob)
bad = dict(json.loads(head)); bad["type_shift"] = 24
hb = json.dumps(bad).encode()
open(t + "/signals-24.bin", "wb").write(b"LOSIGNL1" + struct.pack("<I", len(hb)) + hb + blob)
open(t + "/english.stop", "w").write("the\na\nof\nand\n")
# crawl rows, ident first, COPY text; every body has three words, "tie" among them, so the scores tie
# exactly and only the index's document order separates the sites
extra = ["w0", "w1", "m\u00fcnchen", "w3", "w4", "w5", "w6", "w7"]
rows = ["%s\ttie %s\ttie gadget %s" % (h, "golf.nl" if h == "golf.nl" else "xx", extra[i]) for i, h in enumerate(SITES)]
open(t + "/sites.rows.tsv", "w").write("\n".join(rows) + "\n")
open(t + "/pages.rows.tsv", "w").write("\n".join("%s\ttie page w%d" % (("https://golf.nl" if h == 1 else "https://slow.example") + p, i)
                                                 for i, (h, p) in enumerate(PAGES)) + "\nmailto:odd@odd.example\ttie odd w9\n")
open(t + "/anchors.in.tsv", "w").write("\n".join(["hub.example\tthe hub\t4", "hub.example\tbig name\t5000", "hub.example\tmid word\t5", "hub.example\tone\t1",
                                                  "golf.nl\tcasino\t8", "slow.example\tporn casino\t2", "slow.example\twatch golf\t2"]) + "\n")
PY
cat > "$tmp/phrases.sql" <<'SQL'
SELECT ARRAY['porn','casino','a1','a2','a3','a4','a5','a6','a7','a8','a9','b1','b2','b3','b4','b5','b6','b7','b8','b9','c1','c2','c3','c4','c5','c6','c7','c8','c9','d1','d2','d3','d4','d5','d6','d7','d8','d9','e1','e2','e3'], '|'
SQL

echo ""; echo "=== Part 1: input preparation (web_input.py, pop_order.py) ==="
run() { python3 "$TD/$1" "${@:2}" 2>&1; }
out=$(run pop_order.py hostranks "$tmp/registry.sqlite" "$tmp/signals.bin" "$tmp/hostranks.tsv" >/dev/null; sort -t$'\t' -k2,2n "$tmp/hostranks.tsv" | tr '\t' ' ' | paste -sd,)
expect "hub.example 1,nav.example 2,golf.nl 5,b$(printf '\xc3\xbc')cher.example 2147483651,slow.example 2147484548" "hostranks: Tranco or nav rank first (best of the two), then Common Crawl after every Tranco site, unranked left out"
out=$(run pop_order.py hostranks "$tmp/registry.sqlite" "$tmp/signals-24.bin" "$tmp/x.tsv"; echo "rc=$?")
if grep -q "not a pair" <<<"$out" && grep -q "rc=1" <<<"$out"; then ok "hostranks refuses a signals.bin whose type shift is not the registry's"; else bad "hostranks refuses a mismatched pair" "$out"; fi
run pop_order.py sitepop "$tmp/registry.sqlite" "$tmp/hostranks.tsv" "$tmp/ranks.tsv" >/dev/null
sid() { echo $(( (2 << 28) | $1 )); }; pid() { echo $(( (3 << 28) | $1 )); }
# site seqs: 0 slow, 1 golf, 2 bücher, 3 tie-b, 4 tie-a, 5 hub, 6 tie-c, 7 nav
want="$(sid 5) $(sid 7) $(sid 1) $(sid 2) $(sid 0) $(sid 3) $(sid 4) $(sid 6)"
got=$(awk -F'\t' -v lo=$((2<<28)) -v hi=$((3<<28)) '$1>=lo && $1<hi' "$tmp/ranks.tsv" | sort -t$'\t' -k2,2n | cut -f1 | paste -sd' ')
if [[ "$got" == "$want" ]]; then ok "sitepop: sites by key, the unranked last in doc_id order"; else bad "sitepop site order" "want $want" "got  $got"; fi
# pages: golf.nl pages (seq 1, 2) rank 5; slow.example pages (seq 0, 3) after; the mailto page has no host key: last
want="$(pid 1) $(pid 2) $(pid 0) $(pid 3) $(pid 9)"
got=$(awk -F'\t' -v lo=$((3<<28)) '$1>=lo' "$tmp/ranks.tsv" | sort -t$'\t' -k2,2n | cut -f1 | paste -sd' ')
if [[ "$got" == "$want" ]]; then ok "sitepop: a page takes its host's key; one site's pages stay together in doc_id order"; else bad "sitepop page order" "want $want" "got  $got"; fi
if [[ "$(awk -F'\t' '{print int($1/268435456) " " $2}' "$tmp/ranks.tsv" | sort | uniq -d | wc -l)" == 0 && "$(grep -c . "$tmp/ranks.tsv")" == 13 ]]; then ok "sitepop: ranks are unique within each type (a total order)"; else bad "sitepop ranks unique"; fi

out=$(python3 "$TD/web_input.py" map sites "$tmp/registry.sqlite" < "$tmp/sites.rows.tsv" 2>/dev/null | cut -f1 | paste -sd' ')
want=$(for i in 0 1 2 3 4 5 6 7; do sid $i; done | paste -sd' ')
if [[ "$out" == "$want"* ]]; then ok "map sites: idents become the registry's doc_ids (unicode and dotted hosts included)"; else bad "map sites doc_ids" "want $want" "got $out"; fi
out=$(printf 'b\xc3\xbccher.example\tb\xc3\xbc tie\tna\\ive\\\\x \\t M\xc3\xbcnchen\n' | python3 "$TD/web_input.py" map sites "$tmp/registry.sqlite" 2>/dev/null | cut -f2-)
expect "$(printf 'b\xc3\xbc tie\tna\\ive\\\\x \\t M\xc3\xbcnchen')" "map keeps the other columns byte for byte, escapes included"
out=$(printf 'nope.example\tx\ty\n' | python3 "$TD/web_input.py" map sites "$tmp/registry.sqlite" 2>&1; echo "rc=$?")
if grep -q "'nope.example'.*not in the registry" <<<"$out" && grep -q "rc=1" <<<"$out"; then ok "map refuses an ident the registry lacks, naming it"; else bad "map refuses an unknown ident" "$out"; fi
out=$(printf 'golf.nl\tx\ty\ngolf.nl\tx\ty\n' | python3 "$TD/web_input.py" map sites "$tmp/registry.sqlite" 2>&1 >/dev/null; echo "rc=$?")
if grep -q "appears twice" <<<"$out" && grep -q "rc=1" <<<"$out"; then ok "map refuses an ident that appears twice"; else bad "map refuses a duplicate" "$out"; fi
out=$(printf 'golf.nl\tonly one\n' | python3 "$TD/web_input.py" map sites "$tmp/registry.sqlite" 2>&1 >/dev/null; echo "rc=$?")
if grep -q "columns" <<<"$out" && grep -q "rc=1" <<<"$out"; then ok "map refuses a row with the wrong number of columns"; else bad "map refuses a short row" "$out"; fi
out=$(python3 "$TD/web_input.py" map pages "$tmp/registry.sqlite" < "$tmp/pages.rows.tsv" 2>/dev/null | cut -f1 | paste -sd' ')
want="$(pid 0) $(pid 1) $(pid 2) $(pid 3) $(pid 9)"
if [[ "$out" == "$want" ]]; then ok "map pages: url = origin + path, or the alt url of a page with no scheme://host form"; else bad "map pages doc_ids" "want $want" "got $out"; fi

out=$(python3 "$TD/web_input.py" anchors "$tmp/anchors.in.tsv" "$tmp/phrases.sql" 2>/dev/null | grep '^hub' | tr '\t' '|')
expect "hub.example|big name . big name . big name . big name . big name . big name . big name . big name .  mid word . mid word .  one .  the hub . the hub . " "anchors: each anchor repeated floor(log2 sources) times, 1 to 8, in alphabetical order"
out=$(python3 "$TD/web_input.py" anchors "$tmp/anchors.in.tsv" "$tmp/phrases.sql" 2>/dev/null | cut -f1 | paste -sd' ')
expect "hub.example slow.example" "anchors: a site left with no text after the block list is dropped"
out=$(python3 "$TD/web_input.py" anchors "$tmp/anchors.in.tsv" "$tmp/phrases.sql" 2>/dev/null | grep '^slow' | cut -f2)
if [[ "$out" == *"watch golf"* && "$out" != *porn* && "$out" != *casino* ]]; then ok "anchors: block-list words are removed from the text, the rest stays"; else bad "anchors: block-list words removed" "got: $out"; fi
out=$(printf '%s\n' "$(sid 0)	x" | python3 "$TD/pop_order.py" sprefix - "$tmp/ranks.tsv" 2>&1 | tr '\t' ' ')
expect "00000004 $(sid 0) x" "sprefix prefixes the rank (zero-padded) to the row"
out=$(printf '999\tx\n' | python3 "$TD/pop_order.py" sprefix - "$tmp/ranks.tsv" 2>&1; echo "rc=$?")
if grep -q "no rank" <<<"$out" && grep -q "rc=1" <<<"$out"; then ok "sprefix refuses a doc_id with no rank"; else bad "sprefix refuses an unranked doc_id" "$out"; fi

echo ""; echo "=== Part 1b: an input with no rows (a registry without pages; docker $IMG) ==="
if docker image inspect "$IMG" >/dev/null 2>&1; then
    w="$tmp/w-empty"; mkdir -p "$w/in"; cp "$tmp/english.stop" "$tmp/registry.sqlite" "$tmp/hostranks.tsv" "$w/in/"
    : | zstd -q -o "$w/in/pages.rows.tsv.zst"
    # the fixture registry has pages, so an empty rows file maps to nothing: this is the rc3 case (0 rows in, 0 docs)
    if KIND=pages ANALYZER=en_pg2 IMG="$IMG" bash "$TD/local-build.sh" "$w" >"$tmp/build-empty.log" 2>&1; then
        out=$(python3 -c "import json,sys; m=json.load(open(sys.argv[1])); print(len(m['segments']), sorted(f['name'] for f in m['schema']))" "$w/out/pages/meta.json" 2>&1)
        expect "0 ['body', 'doc_id']" "no rows: the build still writes a valid index folder (meta.json, zero segments, the schema), not a failed canon step"
    else bad "no rows: the build succeeds" "$(tail -4 "$tmp/build-empty.log")"; fi
else echo "  SKIP: needs docker image $IMG"; fi

echo ""; echo "=== Part 2: tantivy-structure build --kind (docker $IMG, tantivy-py) ==="
if ! "$PY" -c "import tantivy" 2>/dev/null || ! docker image inspect "$IMG" >/dev/null 2>&1; then
    echo "  SKIP: needs docker image $IMG (docker build -t $IMG apps/search/tools/tantivy-structure) and PY=<python with tantivy==0.26.2>"
    echo ""; echo "Total: $((PASS+FAIL))  PASS: $PASS  FAIL: $FAIL (part 2 skipped)"; [[ $FAIL -eq 0 ]]; exit
fi
for k in sites pages anchors; do
    w="$tmp/w-$k"; mkdir -p "$w/in"
    cp "$tmp/english.stop" "$tmp/registry.sqlite" "$tmp/hostranks.tsv" "$w/in/"
done
zstd -q "$tmp/sites.rows.tsv" -o "$tmp/w-sites/in/sites.rows.tsv.zst"
zstd -q "$tmp/pages.rows.tsv" -o "$tmp/w-pages/in/pages.rows.tsv.zst"
python3 "$TD/web_input.py" anchors "$tmp/anchors.in.tsv" "$tmp/phrases.sql" 2>/dev/null | zstd -q -o "$tmp/w-anchors/in/anchors.rows.tsv.zst"
for k in sites pages anchors; do
    for j in 1 2; do
        # the second build re-sorts from scratch with another job count
        JOBS=$j OUT="$k$j" KIND=$k ANALYZER=en_pg2 IMG="$IMG" bash "$TD/local-build.sh" "$tmp/w-$k" >"$tmp/build-$k$j.log" 2>&1 || { bad "build $k (jobs $j)" "$(tail -5 "$tmp/build-$k$j.log")"; }
        rm -f "$tmp/w-$k/sorted-$k.tsv.zst" "$tmp/w-$k/ranks-web.tsv"
    done
    if [[ -s "$tmp/w-$k/out/${k}1.sha256" ]] && cmp -s "$tmp/w-$k/out/${k}1.sha256" "$tmp/w-$k/out/${k}2.sha256"; then
        ok "$k: two builds (1 and 2 jobs, input re-sorted) are byte-identical: $(wc -l < "$tmp/w-$k/out/${k}1.sha256") files, sha256 equal"
    else bad "$k: two builds are byte-identical" "$(diff "$tmp/w-$k/out/${k}1.sha256" "$tmp/w-$k/out/${k}2.sha256" 2>&1 | head -4)"; fi
done
out=$("$PY" - "$tmp" <<'PY' 2>&1
import json, os, sys, tantivy
t = sys.argv[1]
S = 28
def ix(kind):
    p = f"{t}/w-{kind}/out/{kind}1"
    i = tantivy.Index.open(p) if os.access(p, os.W_OK) else None
    sch = i.schema
    tok = tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.regex(r"[^\W_]+(?:[.@][^\W_]+)+|[^\W_]+")).filter(tantivy.Filter.lowercase()) \
        .filter(tantivy.Filter.custom_stopword(["the", "a", "of", "and"])).filter(tantivy.Filter.stemmer("english")).build()
    i.register_tokenizer("en_pg2", tok); i.reload()
    return i
def top(i, field, term, k=20):
    s = i.searcher()
    q = tantivy.Query.term_query(i.schema, field, term)
    hs = s.search(q, k).hits
    return s, hs, s.fast_field_values("doc_id", [a for _x, a in hs]) if hs else []
sites = ix("sites")
s, hs, ids = top(sites, "body", "tie")
print("tie order:", [i & ((1 << S) - 1) for i in ids])
print("equal scores:", len({round(x, 5) for x, _a in hs}) <= 2)
s, hs, ids = top(sites, "head", "golf.nl")
print("dotted token:", [i & ((1 << S) - 1) for i in ids])
s, hs, ids = top(sites, "body", "münchen")
print("unicode:", [i & ((1 << S) - 1) for i in ids])
print("stores nothing:", all(not s.doc(a).to_dict() for _x, a in top(sites, "body", "tie")[1]))
print("docs:", sites.searcher().num_docs, "fields:", sorted(f["name"] for f in json.load(open(f"{t}/w-sites/out/sites1/meta.json"))["schema"]))
pages = ix("pages")
s, hs, ids = top(pages, "body", "tie")
print("pages order:", [i & ((1 << S) - 1) for i in ids], sorted({i >> S for i in ids}))
anch = ix("anchors")
s, hs, ids = top(anch, "body", "hub")
print("anchor doc is its site:", ids == [(2 << S) | 5])
print("segments:", [len(json.load(open(f"{t}/w-{k}/out/{k}1/meta.json"))["segments"]) for k in ("sites", "pages", "anchors")])
PY
)
expect "tie order: [5, 7, 1, 2, 0, 3, 4, 6]" "sites: documents that score the same come back in popularity order (hub, nav, golf, bücher, then the unranked by doc_id)"
expect "dotted token: [1]" "sites: en_pg2 keeps golf.nl whole, found by its own token"
expect "unicode: [2]" "sites: a non-ASCII word is found"
expect "stores nothing: True" "sites: no stored field comes back with a hit (ids only, rule 3)"
expect "docs: 8 fields: ['body', 'doc_id', 'head']" "sites: one document per site; fields are exactly doc_id, head, body"
expect "pages order: [1, 2, 0, 3, 9] [3]" "pages: popularity order (golf.nl's pages, slow.example's, then the page with no host), registry type 3"
expect "anchor doc is its site: True" "anchors: the anchor document of a site carries the site's own doc_id"
expect "segments: [1, 1, 1]" "every index is one segment (rule 7)"

echo ""; echo "Total: $((PASS+FAIL))  PASS: $PASS  FAIL: $FAIL"
[[ $FAIL -eq 0 ]]
