# search-health / search-refresh. notify.sh is lib/notify.sh, staged into the build
# context by `deploy.sh health-up` (not kept in git twice).
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "psycopg[binary]==3.3.6" "tzdata==2025.2"
WORKDIR /app
COPY freshness.py sources.py health.py scheduler.py notify.sh ./
USER 1000:1000
CMD ["python", "/app/health.py"]
