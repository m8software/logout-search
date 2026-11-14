#!/usr/bin/env bash
# rc9 (2026-10-20): snapshot+clone the search dataset, throwaway Postgres on the clone, release-profile.sql on the
# `search` database (ADR-059: bodies -> sorted words), guard, then COPY the eight refreshed tables for lr.
set -euo pipefail
REL=rc9-20261020; SRC=nas/logout; DS=nas/logout-release-proto; SNAP=$SRC@release-$REL; MNT=/mnt/$DS
OUT=/mnt/nas/logout/search/backups/rc9; CODE=$OUT/code; PG=release-proto-pg; NET=release-proto-net
IMAGE_PG=logout/search-postgres:18.6-pgts1.4.0
log(){ echo "$(date -u +%FT%TZ) $*"; }
echo "{\"stage\":\"start\",\"at\":\"$(date -u +%FT%TZ)\"}" >> $OUT/clone.jsonl
docker rm -f $PG >/dev/null 2>&1 || true
if zfs list -H $DS >/dev/null 2>&1; then
  ORIG=$(zfs get -H -o value origin $DS); log "destroying old clone $DS (origin $ORIG)"
  zfs destroy -r $DS; [ "$ORIG" != "-" ] && zfs destroy "$ORIG" || true
fi
zfs snapshot $SNAP; zfs clone -o primarycache=metadata -o secondarycache=none -o atime=off $SNAP $DS
[ "$(zfs get -H -o value primarycache $DS)" = metadata ] || { log "clone primarycache not metadata"; exit 1; }
log "cloned $SNAP -> $DS"
rm -f $MNT/search/pgdata/18/docker/postmaster.pid
docker network inspect $NET >/dev/null 2>&1 || docker network create $NET >/dev/null
docker run -d --name $PG --network $NET --cpus 2 --memory 3g --shm-size 1g -v $MNT/search/pgdata:/var/lib/postgresql $IMAGE_PG \
  postgres -c shared_preload_libraries=pg_textsearch -c shared_buffers=512MB -c autovacuum=off -c max_wal_size=8GB -c work_mem=128MB -c max_parallel_workers_per_gather=0 -c maintenance_work_mem=512MB >/dev/null
t0=$(date +%s)
until docker exec $PG pg_isready -U search -d search -h 127.0.0.1 >/dev/null 2>&1; do sleep 5; [ "$(docker inspect -f '{{.State.Running}}' $PG)" = true ] || { docker logs --tail 30 $PG; exit 1; }; [ $(( $(date +%s) - t0 )) -lt 3600 ] || exit 1; done
log "PG up in $(( $(date +%s) - t0 ))s"
Q(){ docker exec $PG psql -U search -d search -Atc "$1"; }
log "counts on clone before profile"; for t in wiki_links page_docs anchors anchor_docs site_meta site_inlinks docs youtube_channels; do echo "$t $(Q "select count(*) from $t")"; done | tee $OUT/counts.clone
log "profile (release-profile.sql on the search database)"
docker exec -i $PG psql -U search -d search -v ON_ERROR_STOP=1 -q < $CODE/release-profile.sql
log "guard"; G=$(docker exec -i $PG psql -U search -d search -Atq < $CODE/no-page-text-check.sql); echo "guard=$G" | tee $OUT/guard.txt; [ "$G" = 0 ] || { log "GUARD FAILED ($G readable bodies): STOP"; exit 1; }
cols(){ case $1 in
 wiki_links) echo "platform,qid,value,url,name,description,wiki_title,sitelinks,key,hkey,wkey,skey";;
 page_docs) echo "url,domain,crawl,fetched_at,http_status,title,description,h1,lang,body,error";;
 anchors) echo "domain,anchor,sources";; anchor_docs) echo "domain,body,sources";;
 site_meta) echo "site,kind,value,files";; site_inlinks) echo "domain,sources";;
 docs) echo "domain,crawl,url,fetched_at,http_status,title,description,h1,lang,body,error,robots";;
 youtube_channels) echo "channel_id,handle,name,description,wiki_title,followers,sitelinks,key,hkey,wkey,skey";; esac; }
for t in wiki_links page_docs anchors anchor_docs site_meta site_inlinks docs youtube_channels; do
  log "dump $t"; docker exec $PG psql -U search -d search -Atc "COPY (SELECT $(cols $t) FROM $t) TO STDOUT" | nice -n 10 zstd -q -T2 -3 -o $OUT/$t.tsv.zst -f
  echo "$t $(zstd -dc $OUT/$t.tsv.zst | wc -l) $(stat -c %s $OUT/$t.tsv.zst)" | tee -a $OUT/dump.counts
done
sha256sum $OUT/*.tsv.zst > $OUT/SHA256SUMS
log "stopping throwaway PG (clone kept until buildbox has loaded)"; docker stop -t 60 $PG >/dev/null; docker rm -f $PG >/dev/null
log "DUMP-DONE"; echo "{\"stage\":\"dump-done\",\"at\":\"$(date -u +%FT%TZ)\"}" >> $OUT/clone.jsonl
