#!/bin/bash
# rc9p (2026-10-23): rc9 corrected for publishing (audit 2026-10-22 G1-G6 + Stack Exchange credits), --previous rc9. Same inputs as rc9; places release 2026-10-23-us (rc9's, re-read with Overture's provider names).
cd /data/release/w/proto-20261002-testbox || exit 1
find rc9p/registry.sqlite rc9p/signals.bin rc9p/pictures.sqlite rc9p/lookup/lookup.sqlite rc9p/lookup/places-osm.sqlite rc9p/lookup/places-overture.sqlite rc9p/lookup/hours-web.sqlite rc9p/lookup/text/typo rc9p-web/anchor-phrases.sqlite rc9p-web/kinds.sqlite rc9p-web/text -type f | LC_ALL=C sort | xargs sha256sum
