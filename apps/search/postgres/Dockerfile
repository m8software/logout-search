# Postgres 18 + pg_textsearch (BM25) + pgvector + pg_trgm — all PostgreSQL-licensed.
# pg_trgm ships in contrib; pgvector comes from the PGDG apt repo the official
# image already configures; pg_textsearch is not packaged, so it is built from
# the pinned release tarball (checksum-verified) in a throwaway stage.
ARG PG_IMAGE=postgres:18.6-trixie

FROM ${PG_IMAGE} AS build
ARG PGTS_VERSION=1.4.0
ARG PGTS_SHA256=313c231136f19ded24a7d6f4de3ca96f3840b10c55adbfc839cd0068042e2e40
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl ca-certificates \
      "postgresql-server-dev-${PG_MAJOR}" \
 && curl -fsSL -o /tmp/pgts.tgz \
      "https://github.com/timescale/pg_textsearch/releases/download/v${PGTS_VERSION}/pg_textsearch-${PGTS_VERSION}.tar.gz" \
 && echo "${PGTS_SHA256}  /tmp/pgts.tgz" | sha256sum -c - \
 && tar -xzf /tmp/pgts.tgz -C /tmp \
 && make -C "/tmp/pg_textsearch-${PGTS_VERSION}" -j"$(nproc)" \
 && make -C "/tmp/pg_textsearch-${PGTS_VERSION}" install DESTDIR=/out

FROM ${PG_IMAGE}
RUN apt-get update \
 && apt-get install -y --no-install-recommends "postgresql-${PG_MAJOR}-pgvector" \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/ /
