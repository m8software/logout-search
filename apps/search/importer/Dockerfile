FROM python:3.12-slim
WORKDIR /app
# 7z streams Posts.xml out of the Stack Exchange archives (stackexchange.py); osmium (osmplaces.py) needs libexpat.
RUN apt-get update && apt-get install -y --no-install-recommends p7zip-full libexpat1 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && python -c "import duckdb; c = duckdb.connect(); c.execute('INSTALL httpfs'); c.execute('INSTALL aws')"
COPY schema.sql search_import.py ccscan.py ccfetch.py docs_policy.py wikiparse.py wikicats.py kinds.py kinds_open.py junk.py stocks.py irlinks.py wikisites.py stackexchange.py osmplaces.py vectors.py wikidata_sites.py ccpages.py openpages.py gutenberg.py quality.py subreddits.py youtube.py wikilinks.py wikipop.py wayback.py pageviews.py anchors.py wdc.py entities.py clickstream.py querylist.py facts.py d2q.py card_images.py ./
ENTRYPOINT ["python", "/app/search_import.py"]
