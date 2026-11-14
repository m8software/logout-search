#!/usr/bin/env bash
# =============================================================================
# wiki-links-arms.sh: measure the W / C breadth letters on a PRIVATE copy of the engine (2026-11-09)
# =============================================================================
# Runs on indexhost (root or docker group) from a job folder J (default /mnt/nas/logout/search/wikiext-job):
#   J/api/       search_api.py + result_store.py under test (copied from this checkout)
#   J/tools/     list-judge.py + evalsets/ (copied from this checkout)
#   J/wikilinks.sqlite   the engine file (importer/wikiext.py sqlite)
#   J/lists/     <arm>.lists.jsonl out (then copied to the Clef queue folder by hand)
# The engine is the twin's image and settings (twin.env: rc14, JDL, no Tranco) with the live reranker st3 at /ce, NO
# result store (so curated lists, which bypass breadth, are out of the measure), no news fill and --network none:
# nothing reaches the live engines or the internet; searches are asked from inside the container.
#
#   wiki-links-arms.sh up                      start wikiext-eng (3 CPUs low shares, 4.5 GB)
#   wiki-links-arms.sh fetch ARM FLAGS [QUERIES.tsv ...]   list-judge fetch with &bl=FLAGS (default queries: mix-300)
#   wiki-links-arms.sh down
# =============================================================================
set -euo pipefail
J="${J:-/mnt/nas/logout/search/wikiext-job}"
S=/mnt/nas/logout/search
IMG="${IMG:-logout/search-api:public-twin}"
NAME=wikiext-eng
D=(sudo -n docker)
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

case "${1:-}" in
up)
    "${D[@]}" rm -f "$NAME" >/dev/null 2>&1 || true
    grep -v -e '^SEARCH_STORE' -e '^SEARCH_HOME' -e '^SEARCH_CE_THREADS' "$S/twin/twin.env" > "$J/eng.env"
    echo "SEARCH_HOME=40.592,-105.871" >> "$J/eng.env"
    mkdir -p "$J/news" "$J/lists"
    if [ ! -s "$J/news/news.sqlite" ]; then            # a WAL file mounted read-only fails silently: copy, then fold the WAL in
        cp "$S/twin/news/news.sqlite" "$S/twin/news/news.sqlite-wal" "$S/twin/news/news.sqlite-shm" "$J/news/" 2>/dev/null || true
        "${D[@]}" run --rm --network none --user 950:950 -v "$J/news:/n" --entrypoint python "$IMG" -c \
            "import sqlite3; c=sqlite3.connect('/n/news.sqlite'); c.execute('pragma wal_checkpoint(truncate)'); c.execute('pragma journal_mode=delete'); c.close()"
    fi
    "${D[@]}" run -d --name "$NAME" --network none --cpus 3 --cpu-shares 256 -m 4608m --env-file "$J/eng.env" \
        -e SEARCH_CE_THREADS=3 -e SEARCH_CE_DIR=/ce -e SEARCH_CE_GOV_DIR=/ce -e SEARCH_NEWS_FILL_EVERY_S=0 \
        -e SEARCH_WIKILINKS=/wl/wikilinks.sqlite -e "SEARCH_CARDLINKS=/cardlinks/cardlinks.sqlite" \
        -v "$J/api/search_api.py:/app/search_api.py:ro" -v "$J/api/result_store.py:/app/result_store.py:ro" \
        -v "$S/release-data/rc14:/rel:ro" -v "$S/ce-st3:/ce:ro" -v "$S/sitenav:/sitenav:ro" -v "$S/cardlinks:/cardlinks:ro" \
        -v "$J/news:/news:ro" -v "$J:/wl:ro" -v "$J/tools:/repo/apps/search/tools:ro" -v "$J/lists:/lists" "$IMG" >/dev/null
    for _ in $(seq 1 120); do
        "${D[@]}" exec "$NAME" python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/readyz', timeout=5).status==200 else 1)" 2>/dev/null && { log "up"; exit 0; }
        sleep 5
    done
    "${D[@]}" logs "$NAME" 2>&1 | tail -20 >&2; log "not ready"; exit 1 ;;
fetch)
    arm="$2"; flags="$3"; shift 3
    qs=("$@"); [ ${#qs[@]} -gt 0 ] || qs=(/repo/apps/search/tools/evalsets/mix-300.tsv)
    s=$(date +%s)
    "${D[@]}" exec -e "LIST_JUDGE_EXTRA=&bl=${flags}" "$NAME" nice -n 10 python /repo/apps/search/tools/list-judge.py fetch http://127.0.0.1:8090 "$arm" /lists "${qs[@]}"
    log "$arm (bl=${flags}) fetched in $(( $(date +%s) - s )) s: $(wc -l < "$J/lists/$arm.lists.jsonl") searches" ;;
down)
    "${D[@]}" rm -f "$NAME" >/dev/null 2>&1 || true; log "down" ;;
*) sed -n 2,19p "$0"; exit 2 ;;
esac
