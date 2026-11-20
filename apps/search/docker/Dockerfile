# syntax=docker/dockerfile:1
# Logout Search in a container: the same program a desktop install runs (installer/logout_search.py), the engine it
# starts, and the AI-app connector over HTTP. No database. Build from apps/search:
#     docker build -f docker/Dockerfile -t logout-search .
# The search data is not in the image: the first start downloads the signed release into /data and checks every file.

# the vector search's inner loop in C; the final image keeps no compiler (the engine falls back to numpy without it)
FROM python:3.12-slim-trixie AS kern
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY serve/api/ivf_kern.c /src/
RUN gcc -O3 -Wall -Wextra -Werror -shared -fPIC -o /src/libivf_kern.so /src/ivf_kern.c

FROM python:3.12-slim-trixie
# onnxruntime telemetry off before the library loads; one malloc arena pair, not one per thread (as the engine image)
ENV ORT_DISABLE_TELEMETRY=1 MALLOC_ARENA_MAX=2 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY serve/api/requirements.txt /app/serve/api/requirements.txt
COPY docker/requirements.txt /app/docker/requirements.txt
RUN pip install --no-cache-dir -r /app/docker/requirements.txt
COPY LICENSE THIRD-PARTY-LICENSES.md /app/
COPY release/ /app/release/
COPY news/ /app/news/
COPY serve/api/ /app/serve/api/
COPY installer/ /app/installer/
COPY docker/entrypoint.py /app/docker/entrypoint.py
COPY --from=kern /src/libivf_kern.so /app/serve/api/libivf_kern.so
# the build fails here when a library is missing or this system's SQLite is too old for the release's place index
RUN python /app/installer/logout_search.py engine --check-imports \
    && install -d -o 1000 -g 1000 /data
USER 1000:1000
# a named volume takes /data's owner from the image; a folder of your own must be writable by uid 1000
VOLUME /data
# 8091: the AI-app connector (http://HOST:8091/mcp). The engine's own port stays inside the container.
EXPOSE 8091
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "/app/docker/entrypoint.py", "--health"]
ENTRYPOINT ["python", "/app/docker/entrypoint.py"]
