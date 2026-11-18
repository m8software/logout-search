#!/usr/bin/env bash
# =============================================================================
# dev-engine.sh — the owner's own "local engine" dev setup on indexhost, for search.logout.cloud's
# local / network / paired modes. Run from the workstation.
# =============================================================================
# indexhost already runs the search stack (search-postgres + search-api, the build/fallback
# box). This adds, beside it and without touching it:
#
#   search-rtc-dev          WebRTC engine peer (apps/search/serve/api/rtc_peer.py), host
#                           network, image logout/search-rtc:dev = indexhost's search-api image
#                           + this branch's search_api.py/native_host.py/rtc_peer.py + aiortc.
#                           Answers pages on the home network ("network") and paired devices
#                           anywhere ("paired"). State (room, device secrets, settings.json,
#                           sports-status.json) in $JOBS/rtc/state (uid 1000, 0700).
#   search-news-mine-dev    news poller (apps/search/news/poller.py) — ONLY the feeds the user
#                           added in ⚙ (NEWS_FEEDS=""), into state/news-mine.sqlite (ADR-063)
#   search-sports-mine-dev  sports poller — ONLY the user's personal sources (SPORTS_BUILTIN=0)
#   $JOBS/nh-dev.sh         the native-messaging host for the browser extension (desktop
#                           "local" mode): a throwaway container of the rtc image, same state
#                           and indexhost's news. The workstation's host wrapper ssh-es to it.
#
# indexhost's own search-news / search-sports keep the approved feeds and the built-in leagues in indexhost's news
# file ($NEWS_STORE/news.sqlite). The dev engines READ that file (the full approved list, 565 feeds — owner
# 2026-10-01: "give me the full list") AND state/news-mine.sqlite beside it (SEARCH_NEWS_SQLITE_EXTRA):
# a ⚙ feed outside the approved list, or a personal sports source, is stored only there. The two "mine"
# pollers fetch ONLY what ⚙ lists (NEWS_FEEDS="", SPORTS_BUILTIN=0) — never the approved list a second time.
#
#   apps/search/tools/dev-engine.sh deploy        build images from this checkout, (re)start all
#   apps/search/tools/dev-engine.sh status        containers, pairing devices, settings
#   apps/search/tools/dev-engine.sh logs [N]      last N log lines of each (default 20)
#   apps/search/tools/dev-engine.sh stats [DAYS]  what the engine was asked (this dev engine keeps a
#                                                 query log in its state dir — unlike the public site
#                                                 and the shipped app), how long it took, and what
#                                                 came back empty or unsure (default 7 days)
#   apps/search/tools/dev-engine.sh pair NAME     print a pairing link (+ QR) for a device
#   apps/search/tools/dev-engine.sh devices       list paired devices
#   apps/search/tools/dev-engine.sh revoke ID     revoke a device
#   apps/search/tools/dev-engine.sh host          (re)install the extension's native host on
#                                                 this workstation, pointing at indexhost, and copy the
#                                                 extension to ~/.local/share/logout-search/extension
#   apps/search/tools/dev-engine.sh down          stop and remove the dev containers (state kept)
#
# Iterating: edit search_api.py / native_host.py / rtc_peer.py / news/*.py here, then
# `deploy` (about a minute; rebuilds only what changed). Page changes go through
# infra/search-public/push.sh instead. Extension changes: reload it in brave://extensions.
# =============================================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
JUMP="${INDEXHOST_JUMP:-user@jump-host}"            # indexhost is reached through homeserver
INDEXHOST="${INDEXHOST:-user@index-host}"
INDEXHOST_IP="${INDEXHOST_IP:-index-host}"
JOBS=/mnt/nas/logout/search/bench/jobs
NEWS_STORE=/mnt/nas/logout/search/news-store     # ADR-063: indexhost's news file (deploy.sh news-up), read by the dev engines
TWIN_SHARED=/mnt/nas/logout/search/twin/shared   # ship.sh's second-tier list (apps/search/twin), computed by the home engine too

remote() {  # remote < script : run a bash script on indexhost as root
    ssh -o BatchMode=yes "$JUMP" "ssh -o BatchMode=yes ${INDEXHOST} 'sudo -n bash -s'"
}
exec_rtc() {  # exec_rtc ARGS... : rtc_peer.py CLI inside the running engine
    printf 'docker exec search-rtc-dev python /app/rtc_peer.py %s\n' "$(printf '%q ' "$@")" | remote
}

cmd_deploy() {
    local api news
    # one deployer at a time: a lock on indexhost recording who/what/when; refuses a dirty or behind worktree
    # shellcheck source=../../../utility/deploy-lock.sh
    source "${REPO}/utility/deploy-lock.sh"
    lock_acquire indexhost "dev-engine.sh deploy" "$REPO"
    trap 'lock_release indexhost || true' EXIT
    api=$(tar czf - -C "${REPO}/apps/search/serve/api" native_host.py rtc_peer.py search_api.py gov_state.py site_slots.py product_table.py site_pages.py news_sqlite.py instant_answers.py ranker.json Dockerfile.rtc av_absent.py rtc_selfcheck.py lookups_release.py lookups_sqlite.py facts.py memstat.py sq_candidates.py warm.py result_store.py compute_cache.py parts.py stage_version.py stage_aliases.py start_trace.py warmplan.py stage_assembly.py stage_local.py stage_verdict.py stage_place.py stage_list.py stage_card.py stage_base.py stage_parts.py | base64 -w0)
    news=$(tar czf - -C "${REPO}/apps/search/news" Dockerfile poller.py newsdb.py pg_to_sqlite.py schema.sql schema-sqlite.sql feeds.tsv feeds-more.tsv feeds-public.tsv sports.py sports_personal.py weather.py wxall.py fx.py | base64 -w0)
    remote <<EOF
set -euo pipefail
J=${JOBS}; ST=\$J/rtc/state
mkdir -p \$ST \$J/news-dev; chown 1000:1000 \$ST; chmod 700 \$ST
cd \$J/rtc && echo ${api} | base64 -d | tar xzf -
cd \$J/news-dev && echo ${news} | base64 -d | tar xzf -
base=\$(docker inspect search-api --format '{{.Config.Image}}')
docker build -q -f \$J/rtc/Dockerfile.rtc --build-arg BASE="\$base" -t logout/search-rtc:dev \$J/rtc | tail -1
docker build -q -t logout/search-news:dev \$J/news-dev | tail -1

# search-api answers from a release folder with Postgres off since rc5 (2026-10-08): it then has no PGHOST. The
# engine gets the same release folder. The two "mine" pollers write a file (NEWS_SQLITE) and need no Postgres:
# they start either way (with no PGHOST they were skipped, so a deploy never started or renewed them).
pghost=\$(docker inspect search-api --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^PGHOST=//p')
rel=\$(docker inspect search-api --format '{{range .Mounts}}{{if eq .Destination "/rel"}}{{.Source}}{{end}}{{end}}')
pgnet=""; pgip=""
if [ -n "\$pghost" ]; then
  pgnet=\$(docker inspect "\$pghost" --format '{{range \$k, \$v := .NetworkSettings.Networks}}{{\$k}} {{end}}' | awk '{print \$1}')
  pgip=\$(docker inspect "\$pghost" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' | awk '{print \$1}')
fi
[ -n "\$pghost" ] || [ -n "\$rel" ] || { echo "search-api has neither PGHOST nor a /rel folder: nothing for the engine to answer from" >&2; exit 1; }
envf=\$(mktemp); trap 'rm -f \$envf' EXIT
docker inspect search-api --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E '^PG(USER|PASSWORD)=' > \$envf || true

for svc in news sports; do
  docker rm -f search-\$svc-mine-dev >/dev/null 2>&1 || true
  cmd=""; [ \$svc = sports ] && cmd="python /app/sports.py"
  docker run -d --name search-\$svc-mine-dev --restart unless-stopped \${pgnet:+--network "\$pgnet"} --env-file \$envf \
    \${pghost:+-e PGHOST="\$pghost" -e PGDATABASE=news_mine} -e NEWS_SQLITE=/state/news-mine.sqlite -e NEWS_FEEDS= -e NEWS_SETTINGS=/state/settings.json \
    -e SPORTS_BUILTIN=0 -e SPORTS_SETTINGS=/state/settings.json -e SPORTS_STATUS=/state/sports-status.json \
    -v \$ST:/state logout/search-news:dev \$cmd >/dev/null
done

# host network: the engine's LAN (and VPN) addresses are its WebRTC candidates; it reaches
# indexhost's Postgres at the container's bridge address
docker rm -f search-rtc-dev >/dev/null 2>&1 || true
if [ -s /mnt/nas/logout/search/result-store/results.sqlite ] && [ -s "\$ST/results.sqlite" ]; then
  sqlite3 "\$ST/results.sqlite" "ATTACH '/mnt/nas/logout/search/result-store/results.sqlite' AS s; INSERT OR REPLACE INTO main.curated SELECT * FROM s.curated;" 2>/dev/null || true
  chown 1000:1000 "\$ST/results.sqlite"
fi
docker run -d --name search-rtc-dev --network host --restart unless-stopped -v \$ST:/state -v ${NEWS_STORE}:/news \
  \${rel:+-v "\$rel:/rel:ro"} \${pgip:+-e PGHOST="\$pgip"} \
  --env-file <(docker inspect search-api --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -vE '^(PGHOST|SEARCH_STORE)=') \
  -e SEARCH_NEWS_DB=news -e SEARCH_NEWS_SQLITE_EXTRA=/state/news-mine.sqlite -e SEARCH_TYPO_NAMES=links,places -e SEARCH_ANSWER_DEAD=1 -e SEARCH_PLACE_UNDER_THING=-40 -e SEARCH_TYPO_WORD_DF=3000 -e SEARCH_LINK_SITE_MIN_SITELINKS=1 -e SEARCH_LINK_SITE_LOW_GUARD=1 -e LOGOUT_ENGINE_NAME=indexhost -e LOGOUT_ENGINE_IP=${INDEXHOST_IP} \
  -e LOGOUT_ENGINE_VIA="WebRTC (dev)" -e ENGINE_QUERY_LOG=/state/queries.jsonl -e ENGINE_QUERY_SRC=rtc \
  -e SEARCH_STORE=/state/results.sqlite -e WARM_HEAD=2000 -e WARM_REL=/rel -e WARM_NEWS_DB=/news/news.sqlite \
  -v ${TWIN_SHARED}:/twin:ro -e WARM_EXTRA_LIST=/twin/list.txt \
  -v /mnt/nas/logout/search/sitenav:/sitenav:ro -e SEARCH_SITENAV=/sitenav/sitenav.jsonl \
  -v /mnt/nas/logout/search/cardlinks:/cardlinks:ro -e SEARCH_CARDLINKS=/cardlinks/cardlinks.sqlite \
  -v /mnt/nas/logout/search/wikilinks:/wikilinks:ro \
  -v /mnt/nas/logout/search/se:/se:ro \
  -v /mnt/nas/logout/search/ce-st3:/ce:ro \
  logout/search-rtc:dev >/dev/null

# the extension's native host: one throwaway container per browser connection
cat > \$J/nh-dev.sh <<'NH'
#!/usr/bin/env bash
# dev only (written by apps/search/tools/dev-engine.sh): the native host for the extension,
# a throwaway container of the rtc image beside indexhost's search-api, same state, indexhost's news
set -euo pipefail
ip=\$(ip -4 route get 192.0.2.1 | awk '{for (i = 1; i < NF; i++) if (\$i == "src") print \$(i + 1)}')
rel=\$(docker inspect search-api --format '{{range .Mounts}}{{if eq .Destination "/rel"}}{{.Source}}{{end}}{{end}}')
exec docker run --rm -i --network container:search-api \${rel:+-v "\$rel:/rel:ro"} \
  --env-file <(docker inspect search-api --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -v '^SEARCH_STORE=') \
  -e SEARCH_STORE=/state/results.sqlite -e SEARCH_NEWS_DB=news -e SEARCH_NEWS_SQLITE_EXTRA=/state/news-mine.sqlite -e SEARCH_TYPO_NAMES=links,places -e SEARCH_ANSWER_DEAD=1 -e SEARCH_PLACE_UNDER_THING=-40 -e SEARCH_TYPO_WORD_DF=3000 -e SEARCH_LINK_SITE_MIN_SITELINKS=1 -e SEARCH_LINK_SITE_LOW_GUARD=1 -e LOGOUT_ENGINE_NAME=indexhost -e LOGOUT_ENGINE_IP="\$ip" -e LOGOUT_ENGINE_VIA="dev relay over ssh" -e ENGINE_QUERY_LOG=/state/queries.jsonl -e ENGINE_QUERY_SRC=extension \
  -v /mnt/nas/logout/search/bench/jobs/rtc/state:/state -v ${NEWS_STORE}:/news \
  -v /mnt/nas/logout/search/sitenav:/sitenav:ro -e SEARCH_SITENAV=/sitenav/sitenav.jsonl \
  -v /mnt/nas/logout/search/cardlinks:/cardlinks:ro -e SEARCH_CARDLINKS=/cardlinks/cardlinks.sqlite \
  -v /mnt/nas/logout/search/wikilinks:/wikilinks:ro \
  -v /mnt/nas/logout/search/se:/se:ro \
  -v /mnt/nas/logout/search/ce-st3:/ce:ro \
  --entrypoint python logout/search-rtc:dev /app/native_host.py
NH
chmod 755 \$J/nh-dev.sh
sleep 6
for c in search-rtc-dev search-news-mine-dev search-sports-mine-dev; do
  printf '%-24s %s\n' "\$c" "\$(docker inspect -f '{{.State.Status}} since {{.State.StartedAt}}' \$c)"
done
docker logs --tail 2 search-rtc-dev 2>&1 | grep -v 'IPv6' || true
EOF
}

cmd_status() {
    remote <<EOF
docker ps -a --format '{{.Names}}\t{{.Status}}' | grep -E 'rtc-dev|mine-dev' || echo "no dev containers"
echo "--- paired devices"; docker exec search-rtc-dev python /app/rtc_peer.py --devices 2>/dev/null || true
echo "--- settings"; cat ${JOBS}/rtc/state/settings.json 2>/dev/null || echo "(defaults)"; echo
echo "--- sources"; cat ${JOBS}/rtc/state/sports-status.json 2>/dev/null || echo "(none)"; echo
EOF
}

cmd_logs() {
    local n="${1:-20}"
    [[ "$n" =~ ^[0-9]+$ ]] || { echo "usage: $0 logs [N]" >&2; exit 1; }
    remote <<EOF
for c in search-rtc-dev search-news-mine-dev search-sports-mine-dev; do echo "== \$c"; docker logs --tail ${n} \$c 2>&1 | grep -v 'IPv6: OSError'; done
EOF
}

cmd_stats() {
    local days="${1:-7}"
    [[ "$days" =~ ^[0-9.]+$ ]] || { echo "usage: $0 stats [DAYS]" >&2; exit 1; }
    echo "cat ${JOBS}/rtc/state/queries.jsonl 2>/dev/null || true" | remote \
        | python3 "${REPO}/apps/search/tools/query-stats.py" --days "$days"
}

cmd_host() {
    "${REPO}/apps/search/local/install-linux.sh" \
        --engine-cmd "ssh -o BatchMode=yes ${JUMP} \"ssh -o BatchMode=yes ${INDEXHOST} 'sudo -n bash ${JOBS}/nh-dev.sh'\""
    # the unpacked extension lives outside git: a worktree that gets removed would take the
    # browser's copy with it. Same ID either way (manifest "key").
    local ext="${HOME}/.local/share/logout-search/extension"
    mkdir -p "$ext" && cp -r "${REPO}/apps/search/extension/." "$ext/"
    echo "extension files: ${ext} — load unpacked from there once (brave://extensions), then 'reload' after each host run"
}

cmd_down() {
    remote <<'EOF'
docker rm -f search-rtc-dev search-news-mine-dev search-sports-mine-dev 2>/dev/null || true
echo "removed (state and news-mine.sqlite kept)"
EOF
}

case "${1:-}" in
    deploy)  cmd_deploy ;;
    status)  cmd_status ;;
    logs)    shift; cmd_logs "$@" ;;
    pair)    shift; exec_rtc --pair "${*:-device}" ;;
    devices) exec_rtc --devices ;;
    revoke)  shift; [[ $# -eq 1 ]] || { echo "usage: $0 revoke ID" >&2; exit 1; }; exec_rtc --revoke "$1" ;;
    stats)   shift; cmd_stats "$@" ;;
    host)    cmd_host ;;
    down)    cmd_down ;;
    *) sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
