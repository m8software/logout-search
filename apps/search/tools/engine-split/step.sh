#!/usr/bin/env bash
# step.sh MODULE — move one stage module's private code, update the copy lists, run the static checks. Run in the worktree.
set -euo pipefail
M=$1; R=${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)}; A=$R/apps/search/serve/api; E=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd $R
python3 $E/split_tool.py $A $M | tail -4
# copy lists
sed -i "s/ stage_version\.py\b/ stage_version.py $M.py/" $A/Dockerfile $A/Dockerfile.rtc
sed -i "s/ stage_version\.py\b/ stage_version.py $M.py/" apps/search/tools/dev-engine.sh
sed -i "s/\"stage_version.py\"/\"stage_version.py\", \"$M.py\"/" apps/search/installer/bundle/build.py
git diff --stat -- $A/Dockerfile $A/Dockerfile.rtc apps/search/tools/dev-engine.sh apps/search/installer/bundle/build.py | tail -1
python3 -m py_compile $A/search_api.py $A/$M.py && echo compiled
python3 $E/undef.py $A | tail -3
python3 $E/part-versions.py $A > $E/step-versions.json && diff -q $E/base-versions.json $E/step-versions.json && echo "versions IDENTICAL"
python3 $E/dircmp.py $A > $E/names-step.txt && diff -q $E/names-base.txt $E/names-step.txt && echo "namespace IDENTICAL"
python3 $E/scrubcheck.py && echo "scrub rules still match"
# what left search_api.py is exactly what the stage module holds
git diff -U0 -- $A/search_api.py | grep -E '^-[^-]' | sed 's/^-//' | grep -v '^\s*$' | sort -u > /tmp/removed.$$ || true
cat $A/stage_*.py | sed 's/\s*$//' | sort -u > /tmp/staged.$$
sed 's/\s*$//' /tmp/removed.$$ | sort -u | comm -23 - /tmp/staged.$$ | head -5 > /tmp/missing.$$ ; test ! -s /tmp/missing.$$ && echo "every removed line is in a stage module" || { echo "REMOVED LINES NOT IN STAGE MODULES:"; cat /tmp/missing.$$; }
git diff -U0 -- $A/search_api.py | grep -E '^\+[^+]' | grep -vE '^\+(from stage_|    [A-Za-z_0-9]+,$|\)$|# |$|if __name__|    sys.modules)' | head -3
rm -f /tmp/removed.$$ /tmp/staged.$$ /tmp/missing.$$
