# apps/search/release — data release export (PROTOTYPE, gate T5)

Builds one data release of the downloadable search engine (ADR-063,
`docs/internal/SEARCH-HOME-ENGINE-PLAN.md` §2/§5) from the Postgres database on indexhost.
Dev tooling: nothing here runs on a box or a user's machine.

```
<release>/text/wiki/                 Tantivy 0.26.2, Wikipedia (T1 parity recipe)          CC BY-SA 4.0
<release>/text/sites/{sites,pages,anchors}/  Tantivy (docs, page_docs, anchor_docs)       CC0 layer + third-party titles; pages CC BY-SA (open dumps)
<release>/vectors/raw/wiki_bit.*      raw nomic 1-bit vectors — T3 makes usearch indexes     CC BY-SA 4.0 (derived)
<release>/lookup-wiki.sqlite          aliases, official sites, links, facts, next, leads (1,000 chars), YouTube, stocks
<release>/lookup-sites.sqlite         suggest tables, navigation, ranks, anchors, site display rows   CC0 layer
<release>/lookup-places-osm.sqlite    OpenStreetMap places only                              ODbL 1.0
<release>/lookup-phrases.sqlite       AmazonQAC phrase completions                           CDLA-Permissive-2.0
<release>/lookup-unsafe-ut1.sqlite    UT1 unsafe lists (domain, category)                    CC BY-SA 4.0
<release>/card-images.sqlite          Commons card pictures, licence per row
<release>/english.stop                the analyzer's stop list (the engine needs the identical file)
<release>/gov-states.json             which state a government site belongs to (gov_states.py build, CISA .gov registry)   public domain
<release>/manifest.json(.sig)         files, sizes, sha256, licence per file, engine compatibility; ed25519
```

## Run

The chain of these commands for a whole release is `apps/search/build/release.toml`, run by
`apps/search/build/pipeline.py` (cached stages, bridges around what still reads Postgres): see
`docs/internal/SEARCH-BUILD-PIPELINE.md`. What follows is each tool on its own.

```bash
apps/search/release/run-proto.sh start proto-20261001     # from the workstation (homeserver jump)
apps/search/release/run-proto.sh status
STAGES="sites vectors" apps/search/release/run-proto.sh start proto-20261001   # some stages only
```

`run-proto.sh` never reads the live database. It snapshots `nas/logout`, clones it to
`nas/logout-release-proto` with `primarycache=metadata` (the export's reads and writes stay
out of the ARC that live search depends on), runs a throwaway Postgres (`release-proto-pg`,
autovacuum off, 2 GB / 2 CPUs, shared_buffers 256 MB) on the clone and one capped container per stage
(`logout/search-api:1` + tantivy from `pip --target`, 4 GB / 4 CPUs). Between stages it times
the live API on `tools/latency-queries.txt` so any cost to live search is on record. The
clone and the release stay; the Postgres container is removed.

Each `export.py` stage prints one JSON line (rows, seconds, bytes, peak RSS, cgroup peak) to
`results-<release>.jsonl` next to the release.

## Rules kept (hard checks — the export refuses, it does not warn)

- **Source guard** (`export.py guard`, run by every DB stage): `serve/no-page-text-check.sql`
  must return 0 (ADR-059: page bodies are `page_words()`), and `history_visits` must be 0 in
  every table that has it, in `serve` and in every table read from `search`.
  `release-profile.sql` makes the clone pass, on the throwaway clone only.
- **Text indexes never store page text** (`check_no_page_text()`).
- **`check_export_sql()` refuses:**
  - any SQL naming a refused table: build-only (`site_meta`, `site_inlinks`, `kw_country`,
    `wiki_pop`), `subreddits`, and everything Jev produced or filtered (ADR-065):
    `kind_docs`, `kind_members`, `kind_keys`, `kind_vecs`, `kind_site_vec`, `site_kinds`,
    `kinds_*`, `d2q_docs`;
  - any selection of `history_visits`/`hist` or a Jev score (`site_quality.useful/parked/spam/
    unsafe/junk/source`), in any form — no bands;
  - SQL missing a required filter: no subreddit suggestions, no WDC places
    (`osm NOT LIKE 'wdc:%'`), only UT1 rows of `site_quality`, and anchor text through
    `serve/phrases.sql`'s word list (G3 D9).
- **One file per licence** (G3 D5) and a licence tag on every manifest entry; an untagged file
  refuses to sign. Our own layer is tagged CC0-1.0 (D7). WDC listings and anything derived only
  from them never leave indexhost (D3).
- **What a site said about being shown** (G3 D4c; `importer/docs_policy.py` is the rule, `tools/sqlite_lookups/build.py
  stage_capture_policy` and `release_v1.py`'s docs statements apply it):
  - a capture from the Wayback Machine, and a capture whose robots statements say `noindex` / `none`, is not used at
    all (no title, address, description or index words from it; an older capture never stands in for it); nor are
    that site's inner pages (`PAGE_SQL`, `WEB_INPUT_SQL["pages"]`, the stage's `s_pages`; the stages report the count);
  - a description is not shown for `nosnippet`, `max-snippet:0` or a TDM reservation, is cut to `max-snippet:N`, and
    otherwise to 160 characters at a word;
  - a capture that was never read for its robots statements (`docs.robots IS NULL`) shows **no** description.
    `search_import.py ccrobots` reads those captures again from Common Crawl and fills the column;
    `DESCR_UNCHECKED=keep` keeps them for a build that is not published, and `lookup.sqlite`'s meta then says so;
  - `release_v1.py check --tranco-free` (the publish gate) refuses a lookup file whose `descr_policy` is not the
    publish policy;
  - removal on request: `OPTOUT_FILE` (one domain per line; the tracked list is `optout-domains.txt`) blanks the
    shown description of those sites. Requests come in at help@logout.cloud.
- **Wikipedia leads** are cut to `LEAD_CHARS` (1,000) in `lookup-wiki.sqlite`; the text index
  keeps titles, aliases, infobox and headings in full.
- `export.py removed` writes what all of this keeps out, with sizes.
- `suggest_*` still come from `serve`, built before any history clearing: a real release must
  rebuild them from a box-profiled database (open question in the audit).
- **Test key only:** the manifest is signed with a key generated on indexhost under
  `release-proto/testkey/`. Never the production manifest key (`utility/publish-manifest.sh`).

## Hooks for the other gates

- **T2** owns ranking parity: the field layout here mirrors `search_api.py`'s text
  expressions (`HEAD_EXPR`, `TEXT_FORMS`) but is not validated against Postgres.
  Sites/pages/anchors are three indexes because Tantivy's BM25 statistics are per index.
- **T3** replaces `vectors/raw/` (`wiki_bit.ids.u64` + `wiki_bit.bits`, N x 96 bytes,
  MSB-first, = Postgres `bit(768)`) with usearch indexes plus a delta index.
- **T4** replaces the `lookup-*.sqlite` files (data only, no secondary indexes, arrays as JSON
  text).

Results and open questions: `docs/internal/audits/2026-10-01-release-export-proto.md`.

## Release format v1 — the T5 parts (`release_v1.py`)

Target: `docs/internal/SEARCH-RELEASE-FORMAT.md`. T4 owns `lookup.sqlite` (incl. `names`, `cards`);
Tantivy-structure owns the index schema and writer; T3b owns vectors. This file builds the rest.

| File | What | Keyed by |
|---|---|---|
| `registry.sqlite` | every document once: `registry(doc_id, ext, host_id, path, title, alt)`, `hosts(id, origin)` (page and link URLs), `platforms(id, name, prefix, suffix)`, `tombstones(doc_id, removed_release)`, `authors(doc_id, role, user_id, name)` (Stack Exchange credits, from `se_authors`) | `doc_id = type << 28 \| seq`: 1 wiki, 2 site, 3 page, 4 place, 5 account, 6 youtube, 7 stock, 8 link |
| `signals.bin` | monthly-changing numbers, one fixed-width little-endian array per field per type; `LOSIGNL1` + JSON header (fields, dtypes, sources, licences) | `doc_id & 0xFFFFFF` within the field's type |
| `pictures.sqlite` | card pictures (optional download) | `doc_id` |

The idents (what a builder calls a thing), what each row holds and the field list of `signals.bin` are
in `docs/internal/SEARCH-RELEASE-FORMAT.md`; the code that owns them is `registry_idents.py` (stdlib
only: `idmap`, `url_of`, `idhash`, a selftest). The lookup builder translates `(type, ident) -> doc_id`
with the same rules; `python3 registry_idents.py idhash FILE` prints per-type counts and a sha256 of the
sorted idents, for a registry or for a builder's staged file, so two machines can compare them.

`registry --from-stage STAGE.sqlite` builds the same universe from the lookup builder's staged file
(`apps/search/tools/sqlite_lookups/build.py`, tables `s_*`) on a machine without Postgres; it has no
`page` rows and cannot make `signals.bin` (wiki_pop is not staged).

- **Stable ids.** `registry --previous LAST.sqlite`: survivors keep their id, new documents are
  appended after the type's highest id, removed ids go to `tombstones` and are never reused.
- **URLs are rebuilt, not stored:** wiki = `wiki_base` + title; site/page = `hosts.origin` +
  `path`; account = `platforms.prefix` + handle + `suffix`. `alt` holds a URL only when it does
  not follow its pattern. Place rows carry only the OSM id (names stay in the ODbL file).
- **Signals from build-only tables:** only `wiki.pageviews` and `wiki.sitelinks` are read from
  `wiki_pop`; the table itself stays refused (G3 D10).
  The site rank is our own (`site.own_rank` and the ranks derived from it, built from `own_rank.tsv` by `importer/own_rank.py`, G3 D2).
  Still Tranco-derived and tagged in the header: `site.suggest_pop/demand` and `link.suggest_pop/demand`, until suggest_items is
  rebuilt from own_rank. `release_v1.py check REL --tranco-free` is the publish gate; the default check accepts both kinds of release.

### Version stamps (rule 8)

| Artefact | Stamp |
|---|---|
| every SQLite file | `PRAGMA application_id = 0x6C6F7331` ("los1"), `PRAGMA user_version` = format, `meta(role, format, built_release, …)` |
| every Tantivy index | `release.json` beside `meta.json`: format, built_release, role, tantivy version, analyzer, stop-list sha256 |
| `signals.bin` | header: magic `LOSIGNL1`, format, built_release, type_shift, field list |
| `manifest.json` | the CURRENT release id, format, every file's size/sha256/licence, the signals field list |

Files carry the release that BUILT them, not the current one, so a file that did not change
keeps its bytes (and is not re-downloaded).

### Engine-side check (reference: `release_v1.py check REL PUBKEY`)

The engine refuses the whole release, with the message, when any of these fails:
1. `manifest.json.sig` verifies against the bundled public key;
2. manifest `format` is in the engine's supported range;
3. every listed file exists with the listed size (the updater also checks sha256 on download);
4. every SQLite file: `application_id` = "los1", `user_version` = manifest format, `meta.role` =
   the file's role, `meta.built_release` present;
5. every Tantivy index: `release.json` present, same format, Tantivy major.minor the engine
   bundles, stop-list sha256 = the release's `english.stop`;
6. `signals.bin`: magic, format, `type_shift`, and every field the engine reads present with
   the expected dtype.

### Reproducible builds (rule 7)

- Every export query is `ORDER BY`'d (lookups: `ORDER BY` the whole row) and runs with
  `synchronize_seqscans = off`; index/bitmap scans are off on the uncached clone.
- SQLite files are written in a fixed order, VACUUMed when they have free pages, and carry
  no timestamps.
- Tantivy 0.26 names segments with random UUIDs; `tvnormalize` renames each segment after
  the sha256 of its own files and rewrites `meta.json` / `.managed.json` (sorted). With one
  writer thread, two builds are then byte-identical (`tvrepro`). Multi-threaded builds split
  documents across segments differently; the writer settings belong to Tantivy-structure.

### text/wiki: the Rust builder (run-testbox.sh)

`run-testbox.sh` stage `wiki` builds `text/wiki` with `apps/search/tools/tantivy-structure` (Rust,
`tantivy =0.26.2`, built once from the committed `Cargo.lock` into `<work>/bin`): nothing
displayable is stored, `doc_id` (the registry's) is a fast + indexed field, so titles and snippets
come from SQLite by doc_id. It needs the registry first (`release_v1.py wikiinput` pairs
`wiki_docs` with it and aborts on any difference); the default stage order is registry, idhash,
wiki. `tv_normalize` then gives content-derived segment names and drops meta.json's random
`payload`, so two builds are byte-identical; `release.json` records `schema=store-nothing`, which
the manifest reports. Stage `wiki-py` is the old Python writer (stores page_id, title, snippet).
Not run at scale yet (indexhost down, testbox busy): the Tantivy-structure audit measured 3.40 -> 1.95 GB.

## Publishing: the Hugging Face dataset (`hf_dataset.py`, `update_fetch.py`)

A home engine installs from, and updates from, one public Hugging Face dataset: `main` holds the newest release in the
publish layout plus `updates/<from>/` packages into it, each release is a git tag named after its id, and a signed
`latest.json` says which. Run on the machine that holds the publish folder (testbox, `/opt/logout-release/w/<rel>-pub`):

```bash
hf_dataset.py stage <rel>-pub <stage> --repo ORG/NAME --sign <release key>.pem [--update <pkg into this release>] [--test-key]
hf_dataset.py check <stage> --pubkey <release key>.pub      # every signature, size and sha256; a licence line per file
hf_dataset.py upload <stage> --repo ORG/NAME --token-file F  # private unless --public; latest.json goes up last
```

- `stage` hard-links the publish folder and each package into `<stage>`, writes `LICENSES/` readable (the release keeps them
  xz'd), `LICENSE` (the licence of every stored file: the per-file notice the audit asks for), the dataset card and the signed
  `latest.json` / `package.json` (every package file with its size and sha256: `update.json` does not list them).
- `upload` (huggingface_hub 2.x): one commit that adds the release and removes what it no longer has, the tag, then `latest.json`.
  An installer that reads `main` meanwhile still gets the previous release; a resumed upload re-sends only what the store lacks.
- Clients: `installer/install.py install --source hf:ORG/NAME` and `update_fetch.py fetch --base BASE --source hf:ORG/NAME
  --pubkey KEY --apply`. Both read files from the release's tag, never from `main`, so an upload in progress cannot mix releases.
- Add-ons (optional data outside the release: `wikilinks`, `se-howto`): `addon_build.py build ... --key KEY.pem` writes `addons/<name>/` (manifest, signature, file)
  to copy into the dataset root; clients run `install.py addon install NAME --source hf:ORG/NAME --pubkey KEY --dest BASE`. See `docs/internal/SEARCH-INSTALLER.md`, "Add-ons".
- Before the first public upload: the production release key (today: the test key needs the same key), the
  rc9 licensing fixes (`docs/internal/audits/2026-10-22-rc9-licensing-delta.md`), and a package built against the release
  being published (an update package is tied to one manifest).

