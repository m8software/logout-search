#!/usr/bin/env bash
# =============================================================================
# data-update-publish.sh — a new data release (or a daily one) as a small signed update for installs (phase 4, 2026-11-15)
# =============================================================================
# Owner 2026-11-15: "we need up to date auto updating data asap". An install on release OLD reaches release NEW without a
# full download: one signed package per release (update_pkg.py), kept in latest.json's `chain` (hf_dataset.py stage --prev),
# which the install walks hop by hop (release/update_fetch.py; installer/autoupdate.py runs it every day).
#
#   data-update-publish.sh OLD_RELEASE NEW_RELEASE NEW_PUBLISH PREV_STAGE OUT [--store FILE --old-id ID]
#       OLD_RELEASE   the previous release, uncompressed (what installs are on)
#       NEW_RELEASE   the new release, uncompressed (assemble.py build --out)
#       NEW_PUBLISH   the same release as assemble.py's publish folder (files/<path>.zst + manifest.json(.sig))
#       PREV_STAGE    the folder `stage` made for OLD (its latest.json chain is kept); "" for the first release of a chain
#       OUT           a NEW folder: OUT/pkg (the package), OUT/stage (what upload sends), OUT/delta (release_delta), OUT/affected.bin
#       --store/--old-id   the finished-answers store holding OLD's answers (SEARCH_STORE) and OLD's release id: the names the update
#                     may change go into the package as hashes, and an install keeps its stored lists and answers for every other
#                     name (installer/install.py lists_follow). Without them the installs re-take their lists, as before.
#   Environment:
#       KEY     PEM ed25519 private key = the RELEASE key (never printed, never copied: read in place; on indexhost it is
#               ${SEARCH_ROOT}/curated/lists-publish/release-key.pem, root only)
#       REPO    the dataset, ORG/NAME (default logoutsearch/logout-search)
#       PY      python with numpy + tantivy + cryptography; zstd must be on PATH (the host that ran assemble.py has both)
#       IGNORE  release paths the engine does not read, space separated (default "models/rerank-turbo": SEARCH_CE_DIR points elsewhere)
#       UPLOAD=1 sends it (needs TOKEN_FILE, a Hugging Face write token); anything else stops after `check` and `upload --dry-run`
#
# Everything before UPLOAD=1 is local. The order matters to installs: `upload` sends latest.json LAST (hf_dataset.py), so an
# install that reads main meanwhile still sees the old release, and the package it needs is already there when it sees the new.
# =============================================================================
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PY:-python3}"; REPO="${REPO:-logoutsearch/logout-search}"; IGNORE="${IGNORE:-models/rerank-turbo}"
log() { printf '%s data-update: %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
die() { log "$*"; exit 1; }

[[ $# -ge 5 ]] || { sed -n '2,29p' "${BASH_SOURCE[0]}"; exit 2; }
OLD="$1"; NEW="$2"; PUB="$3"; PREV="$4"; OUT="$5"; shift 5
STORE=""; OLDID=""
while [[ $# -gt 0 ]]; do case "$1" in --store) STORE="$2"; shift 2 ;; --old-id) OLDID="$2"; shift 2 ;; *) die "unknown option $1" ;; esac; done
[[ -f "${KEY:-}" ]] || die "KEY must name the release key's PEM file (it is not printed)"
[[ -d "$OLD" && -d "$NEW" && -d "$PUB" ]] || die "OLD_RELEASE, NEW_RELEASE and NEW_PUBLISH must be folders"
[[ -z "$PREV" || -f "$PREV/latest.json" ]] || die "PREV_STAGE has no latest.json"
[[ ! -e "$OUT" ]] || die "$OUT exists: a run never builds on top of another"
command -v zstd >/dev/null || die "no zstd command"
mkdir -p "$OUT"
ign=(); for p in $IGNORE; do ign+=(--ignore "$p"); done

log "1/4 what changed (release_delta): $OLD -> $NEW"
"$PY" "$HERE/../twin/release_delta.py" delta "$OLD" "$NEW" "$OUT/delta" "${ign[@]}" > "$OUT/delta.json" || die "release_delta failed"
"$PY" - "$OUT/delta.json" <<'PYEOF' >&2
import json, sys
r = json.load(open(sys.argv[1]))
print(f"  changed tokens {r['tokens']}, global {r['global_changed']}, index {r['index_changed']}, files unchanged {r['files_same']}")
for p, i in r["sqlite"].items():
    print(f"  {p}: {i.get('kind')} {i.get('tables', '')}")
PYEOF
aff=()
if [[ -n "$STORE" ]]; then
    [[ -n "$OLDID" ]] || die "--store needs --old-id"
    log "2/4 the answers it may change (names, as hashes only): $STORE"
    "$PY" "$HERE/../twin/release_delta.py" affected --store "$STORE" --old-release "$OLDID" --delta "$OUT/delta" --hashes-out "$OUT/affected.bin" >&2 \
        || die "affected failed"
    aff=(--affected "$OUT/affected.bin")
else
    log "2/4 no --store: the package carries no affected.bin; installs re-take their lists"
fi
log "3/4 the package (narrow diff, signed with KEY)"
"$PY" "$HERE/update_pkg.py" build "$OLD" "$NEW" "$OUT/pkg" --sign "$KEY" "${aff[@]}" >&2 || die "update_pkg failed (the package is NOT published)"
log "    package: $(du -sh "$OUT/pkg" | cut -f1) in $(find "$OUT/pkg" -type f | wc -l) files"
log "4/4 stage (chain kept from ${PREV:-nothing}), check"
prev=(); [[ -n "$PREV" ]] && prev=(--prev "$PREV")
"$PY" "$HERE/hf_dataset.py" stage "$PUB" "$OUT/stage" --repo "$REPO" --sign "$KEY" --update "$OUT/pkg" "${prev[@]}" >&2 || die "stage failed"
PUBHEX=$("$PY" -c "import sys; sys.path.insert(0, sys.argv[1]); import hf_dataset; print(hf_dataset.public_hex(sys.argv[2]))" "$HERE" "$KEY")   # the PUBLIC half only
"$PY" "$HERE/hf_dataset.py" check "$OUT/stage" --pubkey "$PUBHEX" >&2 || die "check failed: nothing to upload"
"$PY" "$HERE/hf_dataset.py" upload "$OUT/stage" --repo "$REPO" --dry-run
if [[ "${UPLOAD:-}" == 1 ]]; then
    [[ -s "${TOKEN_FILE:-}" ]] || die "UPLOAD=1 needs TOKEN_FILE"
    "$PY" "$HERE/hf_dataset.py" upload "$OUT/stage" --repo "$REPO" --token-file "$TOKEN_FILE"
    log "published"
else
    log "stopped before upload (UPLOAD=1 TOKEN_FILE=... to send). Next run's PREV_STAGE: $OUT/stage"
fi
