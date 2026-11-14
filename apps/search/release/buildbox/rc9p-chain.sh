#!/bin/bash
# rc9p (2026-10-23): rc9 corrected for publishing (audit 2026-10-22 G1-G6 + Stack Exchange credits), --previous rc9. Same inputs as rc9; places release 2026-10-23-us (rc9's, re-read with Overture's provider names).
# runs steps 2, 3, 4, 5 one after another (one heavy job at a time); each step logs to its own file
set -eo pipefail
for n in 2 3 4 5; do bash /mnt/c/logout-wsltest/rc9p-step$n.sh >> /data/release/rc9p-step$n.log 2>&1 || { echo "step $n FAILED"; exit 1; }; echo "step $n ok"; done
echo CHAIN-DONE
