#!/bin/bash
# rc9 (2026-10-20): refresh the eight build tables in buildbox's Postgres copy from indexhost's PROFILED clone dumps
# (words-only bodies; guard = 0 on indexhost before the dump). Served by indexhost over plain HTTP on the home network
# (python3 -m http.server, unit rc9-http). Non-constraint indexes are dropped for the COPY and recreated after.
set -eo pipefail
. /mnt/c/logout-wsltest/rc9-common.sh
SRC=http://index-host:8766; L=$R/rc9-load; mkdir -p $L
grep -qx buildbox-pg <<<"$(docker ps --format '{{.Names}}')" || pg_up
Q(){ docker exec buildbox-pg psql -U search -d search -h 127.0.0.1 -Atc "$1"; }
curl -sf $SRC/dump.counts -o $L/dump.counts; curl -sf $SRC/SHA256SUMS -o $L/SHA256SUMS; curl -sf $SRC/guard.txt -o $L/guard.txt
grep -q '^guard=0$' $L/guard.txt || { echo "indexhost guard is not 0: $(cat $L/guard.txt)"; exit 1; }
cols(){ case $1 in
 wiki_links) echo "platform,qid,value,url,name,description,wiki_title,sitelinks,key,hkey,wkey,skey";;
 page_docs) echo "url,domain,crawl,fetched_at,http_status,title,description,h1,lang,body,error";;
 anchors) echo "domain,anchor,sources";; anchor_docs) echo "domain,body,sources";;
 site_meta) echo "site,kind,value,files";; site_inlinks) echo "domain,sources";;
 docs) echo "domain,crawl,url,fetched_at,http_status,title,description,h1,lang,body,error,robots";;
 youtube_channels) echo "channel_id,handle,name,description,wiki_title,followers,sitelinks,key,hkey,wkey,skey";; esac; }
for t in wiki_links page_docs anchors anchor_docs site_meta site_inlinks docs youtube_channels; do
  want=$(awk -v t=$t '$1==t{print $2}' $L/dump.counts); [ -n "$want" ] || { echo "no dump count for $t"; exit 1; }
  log "load $t: before $(Q "select count(*) from $t"), want $want"
  Q "select indexdef from pg_indexes where tablename='$t' and indexname not in (select conname from pg_constraint where conrelid='$t'::regclass)" > $L/$t.indexes
  while read -r d; do n=$(echo "$d" | sed -E 's/^CREATE (UNIQUE )?INDEX ([^ ]+) .*/\2/'); Q "DROP INDEX IF EXISTS $n"; done < $L/$t.indexes
  Q "TRUNCATE $t"
  curl -sf $SRC/$t.tsv.zst | zstd -dc | docker exec -i buildbox-pg psql -U search -d search -h 127.0.0.1 -q -c "COPY $t ($(cols $t)) FROM STDIN"
  got=$(Q "select count(*) from $t"); echo "$t loaded=$got want=$want" | tee -a $L/loaded.counts; [ "$got" = "$want" ] || { echo "COUNT MISMATCH $t"; exit 1; }
  while read -r d; do [ -n "$d" ] && Q "$d"; done < $L/$t.indexes
  Q "ANALYZE $t"
done
log "LOAD-DONE"
