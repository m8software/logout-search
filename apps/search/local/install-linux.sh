#!/usr/bin/env bash
# =============================================================================
# logout search — local engine for search.logout.cloud (Linux, v0)
# =============================================================================
# Registers the native-messaging host "cloud.logout.search" with every browser found
# (Chrome, Chromium, Brave, Edge, Vivaldi, Firefox — non-snap/flatpak builds) and, with
# --engine docker, starts the local engine (search-postgres + search-api, no ports).
# The browser extension (apps/search/extension) then answers search.logout.cloud from
# this computer; nothing goes to the site.
#
#   ./install-linux.sh                    # engine in Docker (this repo's images)
#   ./install-linux.sh --engine-cmd CMD   # host = CMD (e.g. a remote dev engine over ssh)
#   ./install-linux.sh --uninstall
#
# The engine needs a data directory at ~/.local/share/logout-search/pg (a box snapshot's,
# copied from a built instance — see README.md, Data).
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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

engine=docker; cmd=""; name="$(hostname)"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)       name="${2:?--name needs a value}"; shift 2 ;;
        --engine-cmd) engine=cmd; cmd="${2:?--engine-cmd needs a command}"; shift 2 ;;
        --uninstall)  engine=uninstall; shift ;;
        *) echo "usage: $0 [--name FRIENDLY] [--engine-cmd CMD | --uninstall]" >&2; exit 1 ;;
    esac
done

if [[ "$engine" == uninstall ]]; then
    for d in "${CHROMIUM_DIRS[@]}"; do rm -f "$d/NativeMessagingHosts/${NAME}.json"; done
    rm -f "$FIREFOX_DIR/native-messaging-hosts/${NAME}.json" "$HOST"
    docker compose -f "$HERE/docker-compose.yml" -p logout-search-local down 2>/dev/null || true
    echo "removed (data kept at ${SHARE}/pg)"; exit 0
fi

mkdir -p "$SHARE"
if [[ "$engine" == docker ]]; then
    command -v docker >/dev/null || { echo "Docker is needed for the local engine (v0)" >&2; exit 1; }
    [[ -d "${SHARE}/pg" ]] || { echo "no data at ${SHARE}/pg — see README.md (Data)" >&2; exit 1; }
    install -m 700 -d "${SHARE}/rtc"          # pairing state: room + device secrets
    ( umask 077; [[ -f "${SHARE}/.env" ]] || printf 'POSTGRES_PASSWORD=%s\nDATA=%s\n' "$(openssl rand -hex 24)" "${SHARE}" > "${SHARE}/.env" )
    # the badge on search.logout.cloud names the engine: friendly name + this machine's LAN address
    # (from inside the container the host would only see the container's own address)
    ip=$(ip -4 route get 192.0.2.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')
    sed -i '/^LOGOUT_ENGINE_/d' "${SHARE}/.env"
    printf 'LOGOUT_ENGINE_NAME=%s\nLOGOUT_ENGINE_IP=%s\n' "$name" "${ip:-}" >> "${SHARE}/.env"
    docker compose -f "$HERE/docker-compose.yml" -p logout-search-local --env-file "${SHARE}/.env" up -d --build
    cmd="docker exec -i logout-search-local-api python /app/native_host.py"
fi
printf '#!/bin/sh\n# logout search native host (written by install-linux.sh)\nexec %s\n' "$cmd" > "$HOST"
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
echo "host: $HOST — now load the extension (README.md) and open https://search.logout.cloud"
