#!/usr/bin/env bash
# =============================================================================
# logout search — register a development engine as the browser's native host (Linux)
# =============================================================================
# Registers the native-messaging host "cloud.logout.search" with every browser found
# (Chrome, Chromium, Brave, Edge, Vivaldi, Firefox — non-snap/flatpak builds) and points
# it at CMD, e.g. a remote dev engine over ssh (what `dev-engine.sh host` passes).
# An installed engine registers itself: `logout-search host register`.
#
#   ./dev-host.sh --engine-cmd CMD
#   ./dev-host.sh --uninstall
# =============================================================================
set -euo pipefail

NAME=cloud.logout.search
CHROME_ID="dgnaanjmpoeoecakbcfjmcnoliapkebo"                     # fixed by the extension's "key" (manifest.json)
FIREFOX_ID="search@logout.cloud"
# XDG_DATA_HOME, unless it points into a snap sandbox (an installer run from a snapped
# terminal/editor would register a host path the browser cannot see)
data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
[[ "$data_home" == */snap/* ]] && data_home="$HOME/.local/share"
SHARE="${data_home}/logout-search"
HOST="${SHARE}/host"

# every channel: Brave keeps Brave-Browser, -Beta, -Nightly, Brave-Origin-Nightly... side by side,
# Chrome google-chrome, -beta, -unstable; Edge microsoft-edge, -beta, -dev
shopt -s nullglob
CHROMIUM_DIRS=(
    "$HOME"/.config/google-chrome* "$HOME/.config/chromium" "$HOME"/.config/BraveSoftware/*/
    "$HOME"/.config/microsoft-edge* "$HOME"/.config/vivaldi*
)
shopt -u nullglob
CHROMIUM_DIRS=("${CHROMIUM_DIRS[@]%/}")
FIREFOX_DIR="$HOME/.mozilla"

engine=""; cmd=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --engine-cmd) engine=cmd; cmd="${2:?--engine-cmd needs a command}"; shift 2 ;;
        --uninstall)  engine=uninstall; shift ;;
        *) echo "usage: $0 --engine-cmd CMD | --uninstall" >&2; exit 1 ;;
    esac
done

if [[ "$engine" == uninstall ]]; then
    for d in "${CHROMIUM_DIRS[@]}"; do rm -f "$d/NativeMessagingHosts/${NAME}.json"; done
    rm -f "$FIREFOX_DIR/native-messaging-hosts/${NAME}.json" "$HOST"
    echo "removed"; exit 0
fi

mkdir -p "$SHARE"
[[ "$engine" == cmd ]] || { echo "usage: $0 --engine-cmd CMD | --uninstall" >&2; exit 1; }
printf '#!/bin/sh\n# logout search native host (written by dev-host.sh)\nexec %s\n' "$cmd" > "$HOST"
chmod 755 "$HOST"

n=0
for d in "${CHROMIUM_DIRS[@]}"; do
    [[ -d "$d" ]] || continue
    mkdir -p "$d/NativeMessagingHosts"
    printf '{\n  "name": "%s",\n  "description": "logout search — local engine",\n  "path": "%s",\n  "type": "stdio",\n  "allowed_origins": ["chrome-extension://%s/"]\n}\n' \
        "$NAME" "$HOST" "$CHROME_ID" > "$d/NativeMessagingHosts/${NAME}.json"
    echo "registered: $d"; n=$((n + 1))
done
if [[ -d "$FIREFOX_DIR" ]]; then
    mkdir -p "$FIREFOX_DIR/native-messaging-hosts"
    printf '{\n  "name": "%s",\n  "description": "logout search — local engine",\n  "path": "%s",\n  "type": "stdio",\n  "allowed_extensions": ["%s"]\n}\n' \
        "$NAME" "$HOST" "$FIREFOX_ID" > "$FIREFOX_DIR/native-messaging-hosts/${NAME}.json"
    echo "registered: $FIREFOX_DIR"; n=$((n + 1))
fi
command -v snap >/dev/null && grep -qE '^(firefox|chromium) ' <<<"$(snap list 2>/dev/null)" \
    && echo "note: snap Firefox/Chromium cannot start native hosts without extra setup — use a .deb/tarball build"
[[ $n -gt 0 ]] || echo "no supported browser profile found" >&2
echo "host: $HOST — now load the extension (docs/internal/SEARCH-OWN-ENGINE.md) and open https://search.logout.cloud"
