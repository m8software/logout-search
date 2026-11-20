# logout search

A web search engine that runs on your own computer. The index is a file you download; a search
is answered from that file, on your machine, and is sent nowhere.

This repository is the engine and everything used to build its data. It is published from our
private working repository by copying the current files, without history.

**State, 2026-11-08:** the engine runs on Linux, Windows and macOS, and in a Docker container (`apps/search/docker`, no database). The data release
`proto-20261108-rc15` is signed and published as the Hugging Face dataset
[logoutsearch/logout-search](https://huggingface.co/datasets/logoutsearch/logout-search), which also
holds the public key and the install command. One program, `logout-search`, installs the data, starts the engine,
keeps it updated and connects the browser extension; the bundles for Linux, Windows and macOS (Apple silicon) are
built by `.github/workflows/bundle.yml` and attached to each release. They are not signed or notarised yet.


## What you need

Minimums:

- **Memory:** 4 GB
- **Processor:** 2 cores, 2015 or newer (Intel Core 6th generation, AMD Ryzen or Apple silicon)
- **Storage:** 50 GB free, on an SSD

## Other devices and pairing

A phone or another computer uses your engine through the page at search.logout.cloud. On the same network the page
finds the engine; tap **Pair** once and that device reaches it from any network.

- The site only introduces the page to your engine (a rendezvous). Searches do not go to the site.
- The page and the engine then talk over a WebRTC data channel, encrypted from one end to the other. A paired device
  and the engine each prove they hold the pairing secret, so the rendezvous cannot put itself in the middle.
- They connect directly when they can. When no direct path exists (the device is away from home, or the engine's
  computer does not accept incoming connections, as with Windows Firewall's defaults), the encrypted stream passes
  through a relay on our server. The relay carries it and cannot read it.

The engine's side of this is `apps/search/serve/api/rtc_peer.py`.

## Web search for an AI app on the same computer

An AI app that accepts MCP servers (a desktop chat app, an editor, a local-model front end) can search your engine
instead of a paid search API. The app starts `logout-search mcp` itself; that program asks the engine on `127.0.0.1`
and hands back titles, addresses and excerpts.

```
logout-search mcp --print-config     # prints the block to paste into the app's MCP settings
```

- The question goes from the app to the engine on your machine and nowhere else. If the engine is not running, the
  tool says so. It never asks the public site instead.
- Nothing is written down: neither this program nor the engine writes a search to disk.
- It returns what the index holds (an excerpt of about 300 characters per page, plus a direct answer when there is
  one). It does not fetch pages.

An app that takes an address instead (one in a container, or in the browser) connects over HTTP:
`logout-search mcp --http 8091` serves `http://127.0.0.1:8091/mcp`, on this computer only, and refuses requests sent
by web pages. For questions that need a whole page, give the app a page-reading tool as well (the wiki says why).

Both generations of the protocol are served (2026-07-28, and the `initialize` handshake of 2025-11-25 and earlier).
The code is `apps/search/installer/mcp_server.py`; the wiki page **AI apps** has the steps.

## Docker

```
cd apps/search/docker && docker compose up -d
```

One container (the published image `ghcr.io/m8software/logout-search`, x86-64; `docker-compose.build.yml` builds it
from this source instead): the engine and the AI-app connector at `http://127.0.0.1:8091/mcp`. No database. The first start
downloads the signed search data (about 11 GB, 18 GB once unpacked) into a volume. The wiki page **Docker** has the
details.

## Quick start (from source)

```bash
# 1. Install the signed data release from Hugging Face (~11 GB download, 18 GB on disk)
python3 apps/search/installer/install.py install \
  --source hf:logoutsearch/logout-search \
  --pubkey ed25519:40502f6841e8644b8f193c10af27e5b148f74297bf6da7067115a60792d138bc \
  --dest ~/logout-search

# 2. Install the engine's Python runtime dependencies and start on 127.0.0.1:8090
pip install -r apps/search/serve/api/requirements.txt
python3 apps/search/installer/install.py start --dest ~/logout-search
```

The first few searches after a start take 2-5 s while the indexes come into memory; after that they
take the times above.

**Optional add-ons** (same dataset, same key, each with its own licence; install after the release, then
start again):

```bash
K=ed25519:40502f6841e8644b8f193c10af27e5b148f74297bf6da7067115a60792d138bc
# Stack Exchange how-to index: answer excerpts with credit on how-to searches (178 MB download, 481 MB on disk, CC BY-SA)
python3 apps/search/installer/install.py addon install se-howto --source hf:logoutsearch/logout-search --pubkey $K --dest ~/logout-search
# Wikipedia's article links and citations: more rows under the card (1.5 GB download, 5.6 GB on disk, CC BY-SA 4.0)
python3 apps/search/installer/install.py addon install wikilinks --source hf:logoutsearch/logout-search --pubkey $K --dest ~/logout-search
python3 apps/search/installer/install.py start --dest ~/logout-search
```

`addon list` shows what is installed and `addon remove <name>` takes one away. The measurements above were
taken on our public server, which runs both add-ons.

## What is here

| Path | What it is |
|---|---|
| `apps/search/serve/api/` | The engine: `search_api.py` and what it loads. `Dockerfile` builds it; `Dockerfile.rtc` adds the peer that lets other devices on your network reach it |
| `apps/search/docker/` | The Docker option: `Dockerfile`, `docker-compose.yml` (the published image; `docker-compose.build.yml` builds it here) and the entrypoint that installs the data and starts the engine and the AI-app connector |
| `apps/search/installer/` | `logout_search.py`: the one program on a home computer (install, start, update, start at logon, browser host); `install.py`: downloads a signed data release, checks every file, makes it current (standard library only); `bundle/build.py`: the PyInstaller bundle |
| `apps/search/extension/`, `extension-default/` | Browser extensions that connect a search page to the engine on the same computer |
| `apps/search/release/` | How a data release is assembled, checked, signed and updated; `release/buildbox/rc9-*` and `rc9p-*` are the scripts that built the latest one; `release/hf_dataset.py` publishes it |
| `apps/search/importer/`, `refresh/`, `news/` | How the data is built from its sources |
| `apps/search/tools/` | Measurement: evaluation sets built from open data (`tools/evalsets/open/`), A/B scripts, benchmarks |
| `tests/unit/` | Unit tests that run in this copy (`ci/run-unit-tests.sh`) |

## Reading the code

- **Placeholders.** `testbox`, `buildbox`, `indexhost` and `homeserver` stand for the machines this was
  built and measured on; `user@jump-host`, `user@index-host` and `10.0.0.x` for their addresses.
  Scripts that mention them record how something was built or measured and will not run
  elsewhere unchanged.
- **Not copied:** deployment scripts tied to our servers, build scripts of earlier releases,
  the public website (search.logout.cloud), the eval sets of real places, and tests that need those.

## Licence

- **The code** is under the GNU Affero General Public License v3.0: [LICENSE](LICENSE).
  The name and logo are not covered.
- **Software it runs on** is listed, with licences and what each bundles, in
  [apps/search/THIRD-PARTY-LICENSES.md](apps/search/THIRD-PARTY-LICENSES.md). None of it is in
  this repository; it is fetched when an image is built.
- **Models** shipped with the engine: [apps/search/serve/api/NOTICE](apps/search/serve/api/NOTICE).
- **The data** is not in this repository and is not under the AGPL. Each file of a release
  carries its own licence (Wikipedia text CC BY-SA 4.0, OpenStreetMap ODbL, and others); a
  release holds the texts and credits in `LICENSES/`. See
  [apps/search/release/hf/README.md](apps/search/release/hf/README.md).

## Removing a result, a picture, an excerpt or a page

If you run a site, are named in a result, or hold rights in something a result shows, write to
**help@logout.cloud**. Say which result, picture, excerpt or page, the search that shows it, and
what you want removed. A site owner writes from an address at the site's domain.

- **What we check:** that you can speak for the site or the work, and that the item is in the current
  release. A person reads every rights claim and every claim about a person.
- **How it is applied:** the engine answers from a data release and does not fetch the live web, so a
  removal is made when a release is built. A computer that already installed a release gets it when it
  runs `logout-search update`. Until then it keeps showing the old data. Stored searches are served only
  for the release they were made on and for at most one day.
- **What exists today:** a list of sites whose description is not shown
  (`apps/search/release/optout-domains.txt`); a site's own `noindex`, `nosnippet`, `max-snippet:0` and
  text-and-data-mining statements, which the build already honours
  (`apps/search/importer/docs_policy.py`); and block lists that remove whole sites from a build
  (`apps/search/tools/sqlite_lookups/build.py`, `stage_blocked`).
- **Gaps:** there is no tracked list for removing a whole site, a card picture or a Stack Exchange or
  Wikipedia excerpt on request. Those are removed by hand when a release is built, or leave when they
  leave their source. There is no promised response time yet.

## Tests

    pip install -r ci/requirements-test.txt      # and the zstd command
    bash ci/run-unit-tests.sh

A test that lacks a package it needs says `SKIP` and passes; the script lists which ones
skipped. Reporting a security problem: help@logout.cloud.
