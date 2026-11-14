# logout search — local engine (desktop)

search.logout.cloud answers from **this computer** when the logout extension and the local
engine are installed; nothing is sent to the site (no searches, no events). Option A of the
design (2026-09-27): the page ↔ extension ↔ **native messaging** ↔ engine. No network port —
the browser starts the host itself and talks over stdin/stdout, so no website can probe it.

    page (search.logout.cloud)  --postMessage-->  content.js  --runtime msg-->  background.js
         <html data-logout-local="pending|1">                          |  connectNative("cloud.logout.search")
                                                                       v
                                  native_host.py (search_api's /instant code, in-process) --> Postgres

| Piece | Where |
|---|---|
| Extension (MV3: Chrome, Brave, Edge, Vivaldi, Firefox 121+) | `apps/search/extension/` — ID `dgnaanjmpoeoecakbcfjmcnoliapkebo` (fixed by `key`; private key `.credentials/search-extension.pem`), Firefox ID `search@logout.cloud` |
| Native host | `apps/search/serve/api/native_host.py` (baked into the search-api image) |
| Linux installer (v0) | `install-linux.sh` — registers the host with every browser, starts the engine in Docker |
| Tests | `tests/unit/test-search-native-host.sh` (protocol), `tests/unit/test-search-page-own-engine.sh` (extension + page helpers, gjs), `tests/unit/test-search-public-gateway.sh` (site) |

## Other devices on the same network (no extension) —

The engine also checks in with search.logout.cloud's **rendezvous** (`wss://search.logout.cloud/rv`,
`infra/search-public/rv/rv.py`). A page on any device behind the same public IP (or IPv6 /64)
is introduced to it and then talks to it **directly** over a WebRTC data channel (encrypted
end to end; same protocol as the native host). Badge: **network**. The rendezvous sees one
offer/answer, never a search; STUN runs on the VPS (`stun:search.logout.cloud:3478`).
Engine side: `apps/search/serve/api/rtc_peer.py` (service `rtc` in docker-compose.yml).
**Away from home (VPN, cellular) — pairing.** On the home network the page offers
**Pair this device** and asks for a PIN that only the engine's owner can get:
No command is needed: the first device on the engine's own LAN pairs with one tap, and every later one gets a code or
QR from a paired device's menu (Add a device), where devices are also listed and removed. By hand, still:
`docker exec logout-search-local-rtc python /app/rtc_peer.py --pin` (six digits, ten minutes, one
pairing, thrown away after five wrong guesses). Or print a link/QR on the engine:
`docker exec logout-search-local-rtc python /app/rtc_peer.py --pair "Phone"` (`--devices`,
`--revoke ID`). A paired device finds the engine by its room at the rendezvous from any
network; offer and answer are signed with the device's secret (HMAC over the DTLS
fingerprints — the rendezvous cannot man-in-the-middle). Over a VPN the engine's own LAN
address is reachable directly; otherwise a TURN relay on the VPS forwards the encrypted
stream (credentials only for paired devices). Badge: **paired** (tooltip: direct or relayed).
Each connection may send 20 requests a second (burst 40; `RTC_RATE_PER_S`, `RTC_RATE_BURST`).
**At home, after pairing.** A paired device signs its same-network offers too and takes
only an answer signed with its secret from an engine whose name it has paired with: another
engine on the home network (or the rendezvous) cannot answer in its place. An engine from before
 does not sign same-network answers; the page then reaches it by its room (badge **paired**).
A device whose pairing was revoked no longer connects at home until it forgets the engine ("Your engines" → forget) or is paired again.
Not covered: an engine listed under a name the device never paired with is still taken on trust.
The badge holds only the kind of connection (local / network / paired, direct or relayed). The
engine's name, LAN address and index size are sent when the person clicks the badge
(`{details: true}`), not on connecting, and the extension writes only `via` into the page.

## Try it (Linux)

1. Engine: `./install-linux.sh` (Docker; needs data, below) — or, for development against
   indexhost's engine: `./install-linux.sh --engine-cmd "ssh ... 'sudo -n bash .../nh-dev.sh'"`.
2. Extension: `brave://extensions` (or `chrome://extensions`) → **Developer mode** →
   **Load unpacked** → `apps/search/extension`. The ID must read `dgnaanjmpoeoecakbcfjmcnoliapkebo`.
   Firefox: `about:debugging` → This Firefox → Load Temporary Add-on → `manifest.json`.
3. Open https://search.logout.cloud — a green **local** badge appears next to the logo.

Snap Firefox/Chromium cannot start native hosts without extra setup; use .deb/tarball builds.

## Data

The engine needs a box profile's **data directory** (indexes built — a `pg_dump` restore
rebuilds every index: hours). Lossless is 51 GB, small ~30 GB. Today it is copied from
indexhost's bench instance (only the files `pg_class` references — see
`infra/search-public/server.sh adopt`); publishing data-directory archives per platform is
part of the plan below.

## Windows, macOS and Linux without Docker (2026-10-24)

The native, per-user program is `apps/search/installer/logout_search.py` and the one-folder bundle
`apps/search/installer/bundle/build.py` makes of it (see docs/internal/SEARCH-INSTALLER.md, "The one
program and the bundles"): `install` the signed data release, `start` the engine on 127.0.0.1,
`autostart on` at logon, `host register` so the extension on this computer talks to it. The Docker
engine on this page stays for Linux boxes that also serve other devices (the WebRTC peer).

## Plan — Windows and macOS (history)

> **Superseded 2026-10-01** by [docs/internal/SEARCH-HOME-ENGINE-PLAN.md](../../../docs/internal/SEARCH-HOME-ENGINE-PLAN.md):
> the downloadable engine ships as Tantivy + usearch + SQLite files read by one native,
> per-user program (ADR-063): no Postgres, no VM, no Docker on the user's machine, the same
> files on Windows, Linux and macOS. The table below is kept for history.

| Step | Linux | Windows | macOS |
|---|---|---|---|
| Engine runtime | Docker (v0) → native package | **native**: Postgres 18 + pgvector + pg_textsearch built with MSVC (pg_textsearch on Windows is untested — first spike) | native: Postgres.app-style bundle; arm64 **and** x86_64 builds |
| Python host | in the search-api image | PyInstaller one-dir build (onnxruntime, tokenizers, psycopg wheels exist for win_amd64) | PyInstaller, universal2 or two builds; notarised |
| Host registration | JSON in each browser's `NativeMessagingHosts` | registry keys `HKCU\Software\{Google\Chrome,BraveSoftware\Brave-Browser,Microsoft\Edge,Mozilla}\NativeMessagingHosts\cloud.logout.search` | JSON in `~/Library/Application Support/<browser>/NativeMessagingHosts` |
| Data | data directory (x86_64 Linux) | its own data directory build (on-disk format differs by platform) | arm64 data directory (or restore on first run) |
| Background service | systemd --user (Postgres only; the host is started by the browser) | Windows service | launchd agent |
| Distribution | .deb/.rpm + tarball | signed MSI (code-signing cert — **cost, needs approval**) | notarised .pkg (Apple developer account $99/yr — **cost, needs approval**) |
| Extension store | Chrome Web Store ($5 once), AMO (free) | same | Safari needs its own app-wrapped extension (Xcode) |

Order: Linux native package → Windows spike (pg_textsearch MSVC build) → macOS. Each engine
must pass the same eval sets as the public site before it ships (accuracy first).
Licensing gate applies to distributing the data to the public.

## the owner's dev setup (indexhost) — keep and iterate

The local / network / paired modes are exercised against indexhost's engine, not a packaged
install. Everything is reproducible from this checkout with **`apps/search/tools/dev-engine.sh`**:

| Piece | Where | What |
|---|---|---|
| `search-rtc-dev` | indexhost, host network | WebRTC engine peer (`rtc_peer.py`), image `logout/search-rtc:dev` = indexhost's search-api image + this branch's `search_api.py` / `native_host.py` / `rtc_peer.py` + aiortc. Name `indexhost`, LAN index-host |
| `search-news-mine-dev`, `search-sports-mine-dev` | indexhost | pollers for **only** the feeds / personal sources set in ⚙, into `state/news-mine.sqlite` (indexhost's own `search-news` keeps the approved list in `news-store/news.sqlite`); the dev engines read both files (`SEARCH_NEWS_SQLITE_EXTRA`) |
| state | indexhost `/mnt/nas/logout/search/bench/jobs/rtc/state` (uid 1000, 0700) | `engine.json` (room + paired devices' secrets), `settings.json` (⚙), `sports-status.json` |
| extension host | indexhost `/mnt/nas/logout/search/bench/jobs/nh-dev.sh` + workstation `~/.local/share/logout-search/host` | desktop "local" mode: the extension's host ssh-es through homeserver and runs a throwaway rtc container |
| extension | Brave Origin Nightly, unpacked from `~/.local/share/logout-search/extension` (copied by `dev-engine.sh host`, outside git so a removed worktree cannot take it) | ID `dgnaanjmpoeoecakbcfjmcnoliapkebo` |

    apps/search/tools/dev-engine.sh deploy     # rebuild from this checkout + restart (≈1 min)
    apps/search/tools/dev-engine.sh status     # containers, paired devices, settings, source status
    apps/search/tools/dev-engine.sh logs 50
    apps/search/tools/dev-engine.sh pair "Sean's iPad"   # link + QR for a device off the home network
    apps/search/tools/dev-engine.sh devices | revoke ID
    apps/search/tools/dev-engine.sh host       # re-register the extension's host on this workstation
    apps/search/tools/dev-engine.sh down       # stop (state + news-mine.sqlite kept)

Loop: engine code → `dev-engine.sh deploy`; page (`infra/search-public/public`) →
`infra/search-public/push.sh status` (syncs, no rebuild); gateway / rendezvous / relay →
`infra/search-public/push.sh up`; extension → reload in `brave://extensions`.
Tests: `tests/unit/test-search-{native-host,rtc-pairing,page-own-engine,public-gateway,public-rendezvous,personal-sources}.sh`
(the aiortc/websockets/feedparser ones need a venv: `RTC_TEST_PYTHON=` / `RV_TEST_PYTHON=` / `PY=`).
Don't point a personal source at ESPN/cdn.nba.com from indexhost without deciding to — ESPN's
Akamai blocked the home IP once (a block hits the whole LAN).
