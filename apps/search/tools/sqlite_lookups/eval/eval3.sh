#!/usr/bin/env bash
# eval3.sh — the final-form release build (stamps, compressed cards) from the saved staging, T5's
# sqlguard on every SQLite file, per-table sizes, then eval arm D2 on the same queries as eval.sh.
set -uo pipefail   # -e off on purpose: a failed step is logged and the rest still runs
cd ~/t4 || exit 1
say() { echo "== $(date -u +%T) $*"; }
say "build rel3 from stage-rel1.sqlite (standalone: registry + signals stand-ins)"
rm -rf rel3 && mkdir -p rel3
# shellcheck disable=SC2024  # the log is ours on purpose
sudo -n docker run --rm --label "dbtest=safe to remove" --name t4-build3 --cpus 2 --memory 4g \
  -v ~/t4:/w -w /w --entrypoint python t4-lookups:1 \
  build.py /w/rel3 --release-id t4-rel3 --stage /w/stage-rel1.sqlite > build-rel3.log 2>&1
tail -2 build-rel3.log
sudo -n docker run --rm --label "dbtest=safe to remove" -v ~/t4:/w --entrypoint sh t4-lookups:1 -c "chmod -R a+rX /w/rel3"
du -sb rel3/* rel3/text/* | sort -k2
say "sqlguard (T5's release_v1.py)"
sudo -n docker run --rm --label "dbtest=safe to remove" -e PHRASES_SQL=/w/t5/phrases.sql -v ~/t4:/w -w /w/t5 --entrypoint python t4-lookups:1 \
  release_v1.py sqlguard /w/rel3/lookup.sqlite /w/rel3/places-osm.sqlite
say "arm D2 (release rel3)"
./arm2.sh D2 release rel3 || exit 1
python3 run.py search search-queries.txt res-D2-search.json
python3 run.py instant instant-queries.txt res-D2-instant.json
sudo -n docker logs t4-api 2>&1 | grep -oE "ms=[0-9]+ stages=.*" > stages-D2.txt
sudo -n docker logs t4-api 2>&1 | grep -E "Traceback|Error|failed" | sort | uniq -c | head -20 > errors-D2.txt
# shellcheck disable=SC2024
sudo -n docker stats --no-stream --format "{{.MemUsage}}" t4-api > mem-D2.txt
sudo -n docker rm -f t4-api >/dev/null
say EVAL3-DONE
