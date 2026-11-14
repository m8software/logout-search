#!/usr/bin/env bash
# eval4.sh — the release builder with the dated places release (rel4: lookup + three places files + typo),
# T5's sqlguard on every SQLite file, per-file sizes.
set -uo pipefail   # -e off on purpose: a failed step is logged and the rest still runs
cd ~/t4 || exit 1
say() { echo "== $(date -u +%T) $*"; }
say "build rel4 (stage-rel1.sqlite + places-2026-10-03)"
rm -rf rel4 && mkdir -p rel4
# shellcheck disable=SC2024  # the log is ours on purpose
sudo -n docker run --rm --label "dbtest=safe to remove" --name t4-build4 --cpus 2 --memory 4g \
  -v ~/t4:/w -w /w --entrypoint python t4-lookups:1 \
  build.py /w/rel4 --release-id t4-rel4 --stage /w/stage-rel1.sqlite --places-dir /w/places-2026-10-03 > build-rel4.log 2>&1
tail -2 build-rel4.log
du -sb rel4/* rel4/text/* | sort -k2
say "sqlguard (T5's release_v1.py)"
sudo -n docker run --rm --label "dbtest=safe to remove" -e PHRASES_SQL=/w/t5/phrases.sql -v ~/t4:/w -w /w/t5 --entrypoint python t4-lookups:1 \
  release_v1.py sqlguard /w/rel4/lookup.sqlite /w/rel4/places-osm.sqlite /w/rel4/places-overture.sqlite /w/rel4/hours-web.sqlite
say EVAL4-DONE
