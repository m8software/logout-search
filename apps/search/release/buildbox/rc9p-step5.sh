#!/bin/bash
# rc9p (2026-10-23): rc9 corrected for publishing (audit 2026-10-22 G1-G6 + Stack Exchange credits), --previous rc9. Same inputs as rc9; places release 2026-10-23-us (rc9's, re-read with Overture's provider names).
#   5.1 kinds.sqlite from the open-tier dump, kind-name vectors from bge-small-en-v1.5, built twice
#   5.2 the three web text indexes (sites, pages, anchors) from step 3's web-input rows, analyzer en_pg2
# Needs steps 1 and 3 (registry, web-input). Reads no Postgres. Output: rc9p-web/kinds.sqlite, rc9p-web/text/sites/{sites,pages,anchors}.
set -eo pipefail
. /mnt/c/logout-wsltest/rc9p-common.sh
DUMP=$R/kindsin/judged-tables.sql.gz
test -s $DUMP || { echo "no $DUMP (the judged open-tier kinds dump)"; exit 1; }
test -s $OP/registry.sqlite || { echo "no registry: run step 1"; exit 1; }
for k in sites pages anchors; do test -e $OPW/webinput/$k.rows.tsv.zst || { echo "no web-input for $k: run step 3"; exit 1; }; done
log "5.1 kinds --embed (twice: same bytes?)"
BGE=828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35      # Hugging Face's sha256 of BAAI/bge-small-en-v1.5 onnx/model.onnx
got=$(docker run --rm --entrypoint sha256sum logout/search-api:1 /models/bge-small-en-v1.5/model.onnx | cut -d' ' -f1)
[ "$got" = "$BGE" ] || { echo "the image's bge-small model is not upstream's ($got): STOP"; exit 1; }
for o in kinds.sqlite kinds.rerun.sqlite; do
  eval $PY -v $R/kindsin:/kindsin:ro --memory 5g -e RELEASE_ID=$RELP logout/search-api:1 /code/release/release_v1.py kinds /w/$REL2/rc9p/registry.sqlite /w/$REL2/rc9p-web/$o $RELP /kindsin/judged-tables.sql.gz --embed /models/bge-small-en-v1.5 | tail -c 1200; echo
done
sha256sum $OPW/kinds.sqlite $OPW/kinds.rerun.sqlite; cmp $OPW/kinds.sqlite $OPW/kinds.rerun.sqlite && rm -f $OPW/kinds.rerun.sqlite
eval $PY --memory 2g logout/search-api:1 /code/release/release_v1.py sqlguard /w/$REL2/rc9p-web/kinds.sqlite | tail -c 600; echo
log "5.2 the index builder image"
TS=$C/tools/tantivy-structure
docker image inspect logout-tantivy-build:4 >/dev/null 2>&1 || docker build -q -t logout-tantivy-build:4 $TS
TW=$W/tvweb7; rm -rf $TW; mkdir -p $TW/in $TW/out
cp $C/release/english.stop $TW/in/; ln $OP/registry.sqlite $TW/in/registry.sqlite 2>/dev/null || cp $OP/registry.sqlite $TW/in/
cut -f1,3 $R/rc5in/own_rank_G.tsv > $TW/in/hostranks.tsv          # host TAB our rank: the popularity order of the index
for k in sites pages anchors; do
  ln $OPW/webinput/$k.rows.tsv.zst $TW/in/ 2>/dev/null || cp $OPW/webinput/$k.rows.tsv.zst $TW/in/
  log "5.2 $k"
  KIND=$k ANALYZER=en_pg2 DOCKER_OPTS="--memory 5g" bash $TS/local-build.sh $TW 2>&1 | tail -15
  python3 /mnt/c/logout-wsltest/rc9-tvstamp.py $TW/out/$k $RELP $k $C/release/english.stop
done
rm -rf $OPW/text; mkdir -p $OPW/text/sites; mv $TW/out/sites $TW/out/pages $TW/out/anchors $OPW/text/sites/
for k in sites pages anchors; do echo "$k docs=$(python3 -c "import json,sys; print(sum(s['max_doc'] for s in json.load(open(sys.argv[1]))['segments']))" $OPW/text/sites/$k/meta.json) bytes=$(du -sb $OPW/text/sites/$k | cut -f1)"; done | tee $R/rc9p-step5.sizes
log "STEP5-DONE"
