#!/bin/sh
# logout search: install on Linux in one line.
#
#   curl -fsSL https://logout.cloud/install.sh | sh
#
# What it does, in order, all inside your home folder (no root, nothing system-wide):
#   1. downloads the program (the Linux bundle of the latest release on GitHub) and checks its SHA-256
#   2. unpacks it to ~/.local/share/logout-search/app and links ~/.local/bin/logout-search to it
#   3. downloads the search data (about 11 GB, 18 GB on disk) from the Hugging Face dataset; every file is
#      checked against a list signed with the key below before it is used; a stopped download resumes
#   4. starts the engine at logon and now (a systemd user unit), answering only on 127.0.0.1:8090
#   5. connects the logout browser extension, if a supported browser is found
#
# To undo: logout-search autostart off --dest ~/.local/share/logout-search/data
#          logout-search host unregister --dest ~/.local/share/logout-search/data
#          rm -rf ~/.local/share/logout-search ~/.local/bin/logout-search
#
# Settings (environment): LOGOUT_SEARCH_HOME (where it all goes), LOGOUT_SEARCH_NO_DATA=1 (stop after step 2).
set -eu

REPO="m8software/logout-search"
SOURCE="hf:logoutsearch/logout-search"
PUBKEY="ed25519:40502f6841e8644b8f193c10af27e5b148f74297bf6da7067115a60792d138bc"
BASE="${LOGOUT_SEARCH_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/logout-search}"
APP="$BASE/app"
DATA="$BASE/data"
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

say "1/5  downloading the program ($NAME.tar.gz)"
curl -fL --retry 3 --progress-bar -o "$TMP/$NAME.tar.gz" "$URL/$NAME.tar.gz" || die "could not download $URL/$NAME.tar.gz"
curl -fsSL --retry 3 -o "$TMP/SHA256SUMS" "$URL/SHA256SUMS" || die "could not download the checksums"
want="$(grep " $NAME.tar.gz\$" "$TMP/SHA256SUMS" | cut -d' ' -f1)"
have="$(sha256sum "$TMP/$NAME.tar.gz" | cut -d' ' -f1)"
[ -n "$want" ] && [ "$want" = "$have" ] || die "the download's SHA-256 ($have) is not the published one (${want:-none listed})"

say "2/5  unpacking to $APP"
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
"$APP/logout-search" version >/dev/null || die "the program does not run on this system"

if [ "${LOGOUT_SEARCH_NO_DATA:-0}" = "1" ]; then
    say "done: the program is at $BIN/logout-search (no data installed, as asked)"
    exit 0
fi

say "3/5  downloading the search data to $DATA (about 11 GB; safe to stop and run this again)"
"$APP/logout-search" install --source "$SOURCE" --pubkey "$PUBKEY" --dest "$DATA"

say "4/5  starting the engine now and at logon"
if "$APP/logout-search" autostart on --dest "$DATA"; then
    :
else
    say "     could not set it to start by itself (no systemd user session?). Start it by hand:"
    say "       logout-search start --dest $DATA"
fi

say "5/5  connecting the browser extension"
"$APP/logout-search" host register --dest "$DATA" || say "     no supported browser found; run 'logout-search host register --dest $DATA' after installing one"

say ""
say "Installed. The engine answers on this computer at http://127.0.0.1:8090 (the first searches take a few seconds)."
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) say "Note: $BIN is not on your PATH; the program is $BIN/logout-search" ;;
esac
