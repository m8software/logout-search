#!/usr/bin/env bash
# LEGACY, Postgres-bound (SEARCH-POSTGRES-EXIT.md phase 3): it stages from Postgres (stage-rel1.sqlite) and runs the Postgres arm. Delete at phase 7.
# eval2.sh — build the release-format lookup folder (build.py) and run arm D (release backend)
# against the same queries as eval.sh's arms (A postgres, C sqlite without Jev data).
set -uo pipefail   # -e off on purpose: a failed step is logged and the rest still runs
cd ~/t4 || exit 1
say() { echo "== $(date -u +%T) $*"; }
say "build the release folder rel1 (stage from Postgres, read-only; reuses stage-rel1.sqlite when present)"
rm -rf rel1 && mkdir -p rel1
stage=(); [ -f stage-rel1.sqlite ] && stage=(--stage /w/stage-rel1.sqlite)
# shellcheck disable=SC2024  # the log is ours on purpose
sudo -n docker run --rm --label "dbtest=safe to remove" --name t4-build2 --cpus 2 --memory 4g \
  --network search-serve_search-serve-net --env-file pg.env -v ~/t4:/w -w /w --entrypoint python t4-lookups:1 \
  build.py /w/rel1 --release-id t4-rel1 "${stage[@]}" > build-rel1.log 2>&1
tail -3 build-rel1.log
sudo -n docker run --rm --label "dbtest=safe to remove" -v ~/t4:/w --entrypoint sh t4-lookups:1 -c "chmod -R a+rX /w/rel1"
du -sb rel1/* rel1/text/* | sort -k2
say "arm D (release rel1)"
./arm2.sh D release rel1 || exit 1
python3 run.py search search-queries.txt res-D-search.json
python3 run.py instant instant-queries.txt res-D-instant.json
sudo -n docker logs t4-api 2>&1 | grep -oE "ms=[0-9]+ stages=.*" > stages-D.txt
sudo -n docker logs t4-api 2>&1 | grep -E "Traceback|Error|failed" | sort | uniq -c | head -20 > errors-D.txt
sudo -n docker rm -f t4-api >/dev/null
say EVAL2-DONE
