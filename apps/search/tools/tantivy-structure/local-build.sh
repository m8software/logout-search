#!/usr/bin/env bash
# Build the release's Wikipedia Tantivy index (store nothing, popularity order, registry doc_ids,
# one segment, canonical bytes) on any machine with docker, zstd, python3.
#   local-build.sh WORKDIR     WORKDIR/in holds wiki_docs.tsv.zst english.stop registry.sqlite signals.bin
#   output: WORKDIR/out/wiki (canonical index), WORKDIR/out/wiki.sha256
# The web indexes (KIND=sites|pages|anchors, ANALYZER=en_pg2): WORKDIR/in holds english.stop,
# registry.sqlite, hostranks.tsv (`host TAB rank`: pop_order.py hostranks, or any list) and
# KIND.rows.tsv.zst (web_input.py's columns, ident first: its docstring says where each comes from).
# Output WORKDIR/out/KIND, KIND.sha256. Sites and anchors get the site's popularity rank, pages their
# host's; every doc_id comes from registry.sqlite.
# Same recipe as the audit's testbox runs (docs/internal/audits/2026-10-01-tantivy-structure.md section 4).
set -euo pipefail
W="${1:?usage: local-build.sh WORKDIR}"; HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMG="${IMG:-logout-tantivy-build:4}"        # :2 = :1 plus --kind sites|pages|anchors (wiki bytes identical, checked on 100k articles); :4 = an input with no rows gives a valid empty index (a registry with no pages)
KIND="${KIND:-wiki}"                  # wiki | sites | pages | anchors
OUT="${OUT:-$KIND}"                      # output folder name under WORKDIR/out
DOC_ID_ADD="${DOC_ID_ADD:-0}"          # added to every doc_id: 268435456-16777216 re-keys a (type<<24) export for a (type<<28) registry
ANALYZER="${ANALYZER:-en_pg}"          # en_pg | en_pg2 (written to logout-analyzer.txt for the engine)
DOCKER="${DOCKER:-docker}"            # e.g. DOCKER="sudo -n docker"
OPTS="${DOCKER_OPTS:-}"               # e.g. "--cpus 2 --memory 6g --label dbtest=safe-to-remove"
say() { echo "== $(date +%T) $* (load $(cut -d' ' -f1 /proc/loadavg))"; }
mkdir -p "$W/out" "$W/tmp"
if [[ "$KIND" == wiki ]]; then
[[ -s "$W/ranks.tsv" ]] || { say ranks; python3 "$HERE/pop_order.py" signals "$W/in/registry.sqlite" "$W/in/signals.bin" "$W/ranks.tsv"; }
if [[ ! -s "$W/sorted.tsv.zst" ]]; then
    say "export in popularity order"
    python3 "$HERE/pop_order.py" prefix "$W/in/wiki_docs.tsv.zst" "$W/ranks.tsv" --doc-id \
      | LC_ALL=C sort -t $'\t' -k1,1 -S "${SORT_MEM:-6G}" --compress-program=zstd -T "$W/tmp" | cut -f2- | zstd -q -3 -T"${ZSTD_T:-4}" -o "$W/sorted.tsv.zst.part"
    mv "$W/sorted.tsv.zst.part" "$W/sorted.tsv.zst"
fi
SORTED="$W/sorted.tsv.zst"
else
SORTED="$W/sorted-$KIND.tsv.zst"
[[ -s "$W/ranks-web.tsv" ]] || { say ranks; python3 "$HERE/pop_order.py" sitepop "$W/in/registry.sqlite" "$W/in/hostranks.tsv" "$W/ranks-web.tsv"; }
if [[ ! -s "$SORTED" ]]; then
    say "$KIND rows -> doc_ids -> popularity order"
    zstd -dc "$W/in/$KIND.rows.tsv.zst" | python3 "$HERE/web_input.py" map "$KIND" "$W/in/registry.sqlite" \
      | python3 "$HERE/pop_order.py" sprefix - "$W/ranks-web.tsv" \
      | LC_ALL=C sort -t $'\t' -k1,1 -S "${SORT_MEM:-2G}" --compress-program=zstd -T "$W/tmp" | cut -f2- | zstd -q -3 -T"${ZSTD_T:-4}" -o "$SORTED.part"
    mv "$SORTED.part" "$SORTED"
fi
fi
say build; rm -rf "$W/out/$OUT"
zstd -dc "$SORTED" | awk -F"\t" -v OFS="\t" -v a="$DOC_ID_ADD" 'a == 0 { print; next } { $1 += a; print }' | $DOCKER run --rm -i $OPTS -v "$W:/w" -v "$W/in:/in:ro" -w /w "$IMG" \
    tantivy-structure build /w/out/$OUT --stop /in/english.stop --chunk 150000 --heap-mb 900 --jobs "${JOBS:-4}" --analyzer "$ANALYZER" --kind "$KIND"
say canon
$DOCKER run --rm $OPTS -v "$W:/w" "$IMG" python /usr/local/bin/canon.py canon /w/out/$OUT >/dev/null
$DOCKER run --rm -v "$W:/w" alpine chown -R "$(id -u):$(id -g)" "/w/out/$OUT"   # the container wrote as root
echo "$ANALYZER" > "$W/out/$OUT/logout-analyzer.txt"        # not part of the canonical 8 files
( cd "$W/out/$OUT" && sha256sum * .managed.json ) | tee "$W/out/$OUT.sha256"
du -sh "$W/out/$OUT"; say DONE
