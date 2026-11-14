#!/bin/bash
# rc9 (2026-10-20): the rc8 script of the same name with rc8 -> rc9 and rc7 -> rc8 (rc8 is --previous). Inputs stay in rc5in; places release 2026-10-10-us as rc8. rc9 = rc8 code (branch rc9-build) on the refreshed build tables: Gutenberg links, open dumps + Stack Exchange pages (profiled: words only), full-crawl anchors, YouTube followers.
cd /data/release/w/proto-20261002-testbox || exit 1
find rc9/registry.sqlite rc9/signals.bin rc9/pictures.sqlite rc9/lookup/lookup.sqlite rc9/lookup/places-osm.sqlite rc9/lookup/places-overture.sqlite rc9/lookup/hours-web.sqlite rc9/lookup/text/typo rc9-web/anchor-phrases.sqlite rc9-web/kinds.sqlite rc9-web/text -type f | LC_ALL=C sort | xargs sha256sum
