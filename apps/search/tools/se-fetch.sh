#!/usr/bin/env bash
# =============================================================================
# se-fetch.sh: the extra Stack Exchange sites for the how-to index (2026-11-10)
# =============================================================================
# Official archive.org item `stackexchange` only (owner decision 2026-10-17, docs/legal/SEARCH-DATA-SOURCES.md), one
# connection, 2 MB/s, resumable (curl -C -), a generic user agent. Sizes were read from archive.org/metadata/stackexchange
# before the first byte: 15 sites, about 307 MB. Run on indexhost; files land in $DEST (default the job folder's se/).
#   se-fetch.sh [site ...]      default list below; writes $DEST/DONE when every file has its full size
set -euo pipefail
DEST="${DEST:-/mnt/nas/logout/search/wikiext-job/se}"
META="${META:-/mnt/nas/logout/search/wikiext-job/out3/se-meta.json}"
UA="Mozilla/5.0 (compatible; logout-search-import/1)"
SITES=("$@")
[ ${#SITES[@]} -gt 0 ] || SITES=(crafts sustainability lifehacks expatriates fitness parenting arduino coffee beer vegetarianism martialarts sound avp elementaryos hardwarerecs)
mkdir -p "$DEST"
rm -f "$DEST/DONE"
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$DEST/fetch.log"; }
for s in "${SITES[@]}"; do
    f="${s}.stackexchange.com.7z"
    want=$(python3 -c "import json,sys; m=json.load(open('$META')); print(next(int(x['size']) for x in m['files'] if x['name']=='$f'))")
    for try in 1 2 3 4 5; do
        have=$(stat -c %s "$DEST/$f" 2>/dev/null || echo 0)
        [ "$have" = "$want" ] && break
        log "$f: $have of $want bytes, try $try"
        curl -sS -L --limit-rate 2M -C - --retry 3 -A "$UA" -o "$DEST/$f" "https://archive.org/download/stackexchange/$f" || sleep 20
    done
    have=$(stat -c %s "$DEST/$f" 2>/dev/null || echo 0)
    [ "$have" = "$want" ] || { log "$f: incomplete ($have of $want)"; exit 1; }
    log "$f: done ($want bytes)"
done
touch "$DEST/DONE"
log "all done"
