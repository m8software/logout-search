#!/bin/bash
# rc9 (2026-10-20): the rc8 script of the same name with rc8 -> rc9 and rc7 -> rc8 (rc8 is --previous). Inputs stay in rc5in; places release 2026-10-10-us as rc8. rc9 = rc8 code (branch rc9-build) on the refreshed build tables: Gutenberg links, open dumps + Stack Exchange pages (profiled: words only), full-crawl anchors, YouTube followers.
U=rc9-chain; systemctl reset-failed $U 2>/dev/null || true
systemd-run --unit $U --collect --property=Nice=10 --property=StandardOutput=append:/data/release/$U.log --property=StandardError=append:/data/release/$U.log bash /mnt/c/logout-wsltest/rc9-chain.sh
