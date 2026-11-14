#!/usr/bin/env bash
# Load a pages dump from a build instance (infra/build-aws/jobs/cc-gov-pages.sh: pg_dump -Fc --data-only of cc_pages +
# page_docs) into the build database WITHOUT touching rows that exist: the dump is restored into a scratch schema,
# then INSERT ... ON CONFLICT (url) DO NOTHING into the real tables, and the scratch schema is dropped.
#   load-pages-dump.sh DUMP [CONTAINER]      run on the build host; CONTAINER default search-postgres
# Prints the counts before and after. Read-only on everything but cc_pages and page_docs (new rows only).
set -euo pipefail
DUMP="${1:?usage: load-pages-dump.sh DUMP [CONTAINER]}"; C="${2:-search-postgres}"; D="${DOCKER:-docker}"
[[ -s "$DUMP" ]] || { echo "no such dump: $DUMP" >&2; exit 2; }
S=inbox_$(date -u +%Y%m%d%H%M%S)
P() { $D exec -i "$C" psql -U search -d search -v ON_ERROR_STOP=1 -Atq "$@"; }
echo "before: cc_pages $(P -c 'SELECT count(*) FROM cc_pages') page_docs $(P -c 'SELECT count(*) FROM page_docs')"
P -c "CREATE SCHEMA $S; CREATE TABLE $S.cc_pages (LIKE public.cc_pages INCLUDING DEFAULTS); CREATE TABLE $S.page_docs (LIKE public.page_docs INCLUDING DEFAULTS INCLUDING GENERATED);"
# the dump's COPY statements name public.<table>; retarget them to the scratch schema on the text stream
$D exec -i "$C" pg_restore -f - --data-only < "$DUMP" | sed -E "s/^COPY public\.(cc_pages|page_docs) /COPY $S.\1 /" | P -q
echo "in dump: cc_pages $(P -c "SELECT count(*) FROM $S.cc_pages") page_docs $(P -c "SELECT count(*) FROM $S.page_docs")"
P -c "INSERT INTO public.cc_pages SELECT * FROM $S.cc_pages ON CONFLICT (url) DO NOTHING"
P -c "INSERT INTO public.page_docs (url, domain, crawl, fetched_at, http_status, title, description, h1, lang, body, error)
      SELECT url, domain, crawl, fetched_at, http_status, title, description, h1, lang, body, error FROM $S.page_docs ON CONFLICT (url) DO NOTHING"
P -c "DROP SCHEMA $S CASCADE"
echo "after:  cc_pages $(P -c 'SELECT count(*) FROM cc_pages') page_docs $(P -c 'SELECT count(*) FROM page_docs') gov pages $(P -c "SELECT count(*) FROM cc_pages WHERE kind = 'gov'")"
