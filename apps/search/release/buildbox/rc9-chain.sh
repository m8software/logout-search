#!/bin/bash
# rc9 (2026-10-20): the rc8 script of the same name with rc8 -> rc9 and rc7 -> rc8 (rc8 is --previous). Inputs stay in rc5in; places release 2026-10-10-us as rc8. rc9 = rc8 code (branch rc9-build) on the refreshed build tables: Gutenberg links, open dumps + Stack Exchange pages (profiled: words only), full-crawl anchors, YouTube followers.
# runs steps 2, 3, 4, 5 one after another (one heavy job at a time); each step logs to its own file
set -eo pipefail
for n in 2 3 4 5; do bash /mnt/c/logout-wsltest/rc9-step$n.sh >> /data/release/rc9-step$n.log 2>&1 || { echo "step $n FAILED"; exit 1; }; echo "step $n ok"; done
echo CHAIN-DONE
