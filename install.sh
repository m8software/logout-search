#!/bin/sh
# logout search: install on Linux in one line.
#
#   curl -fsSL https://logout.cloud/install.sh | sh
#
# What it does, in order, all inside your home folder (no root, nothing system-wide):
#   1. downloads the program (the Linux bundle of the latest release on GitHub) and checks its SHA-256
#   2. unpacks it to ~/.local/share/logout-search/app and links ~/.local/bin/logout-search to it
#   3. runs the program's own setup (`logout-search setup`): it downloads the search data (about 11 GB, 18 GB on
#      disk) from the Hugging Face dataset, every file checked against a signed list, a stopped download resumes;
#      starts the engine now and at logon (a systemd user unit), answering only on 127.0.0.1:8090; connects the
#      logout browser extension where a browser is found; and lets your phone pair at https://search.logout.cloud
#
# To undo: logout-search autostart off --dest ~/.local/share/logout-search/data
#          logout-search host unregister --dest ~/.local/share/logout-search/data
#          rm -rf ~/.local/share/logout-search ~/.local/bin/logout-search
#
# Settings (environment): LOGOUT_SEARCH_HOME (where it all goes), LOGOUT_SEARCH_NO_DATA=1 (stop after step 2).
set -eu

REPO="m8software/logout-search"
BASE="${LOGOUT_SEARCH_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/logout-search}"
APP="$BASE/app"
BIN="$HOME/.local/bin"
URL="https://github.com/$REPO/releases/latest/download"

say() { printf '%s\n' "$*"; }
die() { printf 'install: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Linux" ] || die "this script is for Linux; Windows and macOS downloads are at https://logout.cloud/download"
case "$(uname -m)" in
    x86_64|amd64) ARCH=x86_64 ;;
    *) die "no bundle for $(uname -m) yet (x86_64 only); run it from source: https://github.com/$REPO" ;;
esac
for tool in curl tar sha256sum; do
    command -v "$tool" >/dev/null 2>&1 || die "'$tool' is needed and was not found"
done

NAME="logout-search-linux-$ARCH"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

say "downloading the program ($NAME.tar.gz)"
curl -fL --retry 3 --progress-bar -o "$TMP/$NAME.tar.gz" "$URL/$NAME.tar.gz" || die "could not download $URL/$NAME.tar.gz"
curl -fsSL --retry 3 -o "$TMP/SHA256SUMS" "$URL/SHA256SUMS" || die "could not download the checksums"
want="$(grep " $NAME.tar.gz\$" "$TMP/SHA256SUMS" | cut -d' ' -f1)"
have="$(sha256sum "$TMP/$NAME.tar.gz" | cut -d' ' -f1)"
[ -n "$want" ] && [ "$want" = "$have" ] || die "the download's SHA-256 ($have) is not the published one (${want:-none listed})"

say "unpacking to $APP"
mkdir -p "$BASE" "$BIN"
rm -rf "$APP.new"
mkdir "$APP.new"
tar -xzf "$TMP/$NAME.tar.gz" -C "$APP.new" --strip-components=1
[ -x "$APP.new/logout-search" ] || die "the bundle holds no logout-search program"
rm -rf "$APP.old"
[ -d "$APP" ] && mv "$APP" "$APP.old"
mv "$APP.new" "$APP"
rm -rf "$APP.old"
ln -sf "$APP/logout-search" "$BIN/logout-search"
"$APP/logout-search" version >/dev/null 2>&1 || die "the program does not start on this system (it needs glibc 2.35 or newer: Ubuntu 22.04, Debian 12, Fedora 36 or later). Run it from source instead: https://github.com/$REPO"

if [ "${LOGOUT_SEARCH_NO_DATA:-0}" = "1" ]; then
    say "done: the program is at $BIN/logout-search (no data installed, as asked)"
    exit 0
fi

case ":$PATH:" in
    *":$BIN:"*) ;;
    *) say "note: $BIN is not on your PATH; the program is $BIN/logout-search" ;;
esac
# the rest is the program's own setup, the same one a double-click runs on Windows and macOS:
# the data (checked against the signed list), start now and at logon, the browser extension, pairing for other devices
exec "$APP/logout-search" setup --dest "$BASE"
