# T4 step 2 throwaway eval image (testbox only): the kinds12 runtime + models (the same base T2's
# wiki A/B used), this branch's search_api.py + lookups_sqlite.py. LABEL: safe to remove.
FROM logout/search-api:kinds12
USER root
RUN pip install --no-cache-dir "tantivy==0.26.2"
COPY search_api.py lookups_sqlite.py lookups_release.py tv_open.py release_root.py ranker.json warm.py native_host.py NOTICE /app/
USER 1000:1000
LABEL dbtest="safe to remove"
