FROM python:3.12-slim
WORKDIR /app
# feedparser (BSD): RSS 0.9x-2.0, RSS 1.0/RDF, Atom, JSON Feed. defusedxml (PSF): news
# sitemaps with entity expansion off (billion laughs / external entities).
RUN pip install --no-cache-dir "psycopg[binary]==3.3.6" "feedparser==6.0.12" "defusedxml==0.7.1"
COPY poller.py newsdb.py pg_to_sqlite.py schema.sql schema-sqlite.sql feeds.tsv feeds-more.tsv feeds-public.tsv sports.py sports_personal.p[y] weather.py wxall.py fx.py ./
USER 1000:1000
CMD ["python", "/app/poller.py"]
