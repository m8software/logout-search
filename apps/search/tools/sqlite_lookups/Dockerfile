# T4 bench image: the search-api image (master's search_api.py helpers, psycopg, sqlite 3.46)
# plus tantivy (typo-correction variant) and sqldiff (changeset size). Throwaway, testbox only.
FROM logout/search-api:nomic2
USER root
RUN apt-get update -qq && apt-get install -y -qq zstd sqlite3-tools >/dev/null \
    && pip install -q tantivy==0.26.2
