# Reproducible builder image for the release's Tantivy indexes (rule 7; build as logout-tantivy-build:4). Same Rust image
# digest and Cargo.lock as the audit's testbox builds, so the bytes must match.
FROM rust@sha256:3914072ca0c3b8aad871db9169a651ccfce30cf58303e5d6f2db16d1d8a7e58f AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --release --locked && cp target/release/tantivy-structure /tantivy-structure
FROM python:3.12-slim-bookworm
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends zstd >/dev/null && rm -rf /var/lib/apt/lists/*
COPY --from=build /tantivy-structure /usr/local/bin/tantivy-structure
COPY canon.py pop_order.py score.py web_input.py /usr/local/bin/
