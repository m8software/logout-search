# Open answer-known eval sets

These are labelled query sets built only from open data. They use no LLM judging and no browsing
history (ADR-064/065). Each label is a fact that an open dataset already states. The sets are public
and reproducible, so they can live in git. They serve as evaluation sets now and as training labels for
the reranker and intent models later.

To regenerate, run `tools/open-evalsets.py --host deploy`. It reads testbox's serving database read-only
and makes one QLever request, which is cached in `~/.cache/logout/p856-enwiki.tsv`. Sampling is
deterministic: md5 with seed `402`. On the same data snapshot it writes the same files. Pass
`--scale 10` (or larger) to get training-sized pools.

To score, run `tools/eval-queries.py SET.tsv --split test --direct http://127.0.0.1:18090 --host deploy`.
The tool detects the header row. It prints hit@1 and hit@5 per set and per stratum. `--details`
writes per-query rows that `tools/compare-evals.py labels A B` can pair.

**Since every arm of the search-quality epic measures with `tools/sq-eval.py` and nothing
else**: `sq-eval.py --arm NAME --engine http://127.0.0.1:18484` scores every set here in one command and
prints one table with the change against the frozen baseline (`baseline-rc8.md`) and its 95% paired-bootstrap
interval. Dev halves while working; `--test` once per arm, at the end (it logs that it did and refuses a
second run without `--force`). The engine comes from a release copy on the workstation: `tools/sq-engine.sh up
<release-dir>`. `tools/sq-candidates.py` dumps what each list retrieved for every query (the recall audit's
input). `exclude.tsv` lists every test-half query, as typed and as the engine's key: a head list, a training
pool or a precompute must not contain one of them.

**Since (2026-10-13) a row has accepted alternatives** beside its expected answer — every official
site of the thing it names, and a place's own website (`accept/`, below). `sq-eval.py` scores with them by
default and pairs against `baseline-master-20261013.md`; `--keys primary` scores the expected answer alone
against `baseline-rc8.md`, as every table before that date did. Numbers on the two key sets are never compared
with each other.

**Books (2026-10-17).** `books.tsv` asks "<title> gutenberg" and "<author> gutenberg" and expects the Gutenberg ebook or author
page. The expected answers are Wikidata's own IDs (P2034, P1938), never the catalog match (which would be checking itself). Built by
`tools/books-evalset.py` from the build database after the load (720 rows: 372 dev, 348 test; 155 ambiguous names go to
`ambiguous-books.tsv`). `judged-books-queries.tsv` holds 200 fixed-template queries people type ("read <title> online") for
`utility/search-answer-eval.py --queries` with Clef as judge; the dev half is judged first. Both halves' test queries are in
`exclude.tsv` (set `books`, source `open`; set `judged-books`, source `judged-books`). No baseline row exists for `books` (rc8 has no book data).

### Baselines

A baseline is one engine's answers to every set, frozen: the per-query rows every arm is paired against
(`sq_common.BASELINES`, one per key set; rows in `~/.local/share/logout-search/sq/<name>/`, answers in
`sq/cache/<build>/`).

| baseline | keys | code | halves | what it is |
|---|---|---|---|---|
| `baseline-master-20261013` | accept | master `f8a290d4`, build `674539304e8a` | dev **and test** | **the default for the accept keys** |
| `baseline-rc8-keys3` | accept | master `0ad83c68`, build `8f651b68abd0` | dev | the accept baseline until 2026-10-13 evening; kept as the record |
| `baseline-rc8-keys2` | accept, before the place keys | same answers | dev | kept as the record |
| `baseline-rc8` | primary | same answers | dev + test | still the default for `--keys primary` |

**Why it was re-frozen (2026-10-13).** `baseline-rc8-keys3` was measured on master `0ad83c68`. Master then
gained `b4a35a4c` ("a channel with 100,000 followers shows for its bare name; one under an article no longer
blocks a spelling") and the commits after it, and today's master answers a few dev queries differently:
own-title `beyoncé` (3 → 1), `måneskin` (miss → 1), `deep purple` (6 → 5), `wu-tang clan` (miss → 5), place
`karma` (3 → 4), and a YouTube channel first for `jeremy howard` / `jeeremy howard` (misses both ways) — 7 rows
by rank or first result, 117 of 5,755 by anything in the top ten. Every arm measured on current master carried
those differences as if they were its own (lanes each found it and had to run a flag-off control).
The new baseline is pure master code on the same release (`proto-20261002-testbox-rc8`), all flags at their
defaults, and it has the **test halves** too, so a `--test` run on the accept keys now has a row to pair with.

**Rule: a baseline is only valid for the serving code it was frozen on.** Whenever a merge to master changes
what the engine answers (anything under `apps/search/serve/api`, the ranker file, or the release), re-freeze:
one engine from pure master through `sq-engine.sh`, `sq-eval.py --arm <name>` on dev, register the name in
`sq_common.BASELINES`, write its `.md` here, keep the old files. Until then, pair an arm against a flag-off
control run on the same code, not against the stale file. A flag that is off by default does not need a
re-freeze when it merges: integration code with every flag off answered all 5,755 dev queries exactly as master
did (all ten results and scores; `accept-486-487.md`). The test half of a baseline is the test run of master
itself: it is spent once per re-freeze and nobody tunes against it.

## Format

TSV with a header: `query, category, expected, set, stratum, split, entity`.

| category | expected | hit when |
|---|---|---|
| wiki | Wikipedia title | a `wikipedia` result is that article |
| site | domain(s), `\|`-separated | a result's host is the domain or a subdomain of it |
| account | URL(s) without scheme/www | a result URL, normalised the same way, is one of them |
| place | `osm-id@lat,lon@name` | the OSM feature, or a same-name row within 150 m (a listing twin) |

### Accepted alternatives (`accept/<set>.accept.tsv`)

**The rule (owner decision, 2026-10-13, recorded):** for a query that names a thing, the thing's own
official site ranking above its article is a **correct** result. So a row's key accepts, beside its expected
answer:

| the row expects | also a hit |
|---|---|
| an article (`wiki`: wiki-redirect, typo-wiki) | every official site of that article's Wikidata item |
| a site (`site`: site-nav, typo-site, head-brand, own-title, maker-product) | every **other** official site of the same item: a second ending (`panam.com` beside `panam.org`), a renamed domain, a value the builder's front-page rule left out |

| a place (`place`: place, place-town, typo-place) | the place's own website, as its OpenStreetMap feature tags it (follow-up decision, same day) |

Not the reverse: a row that expects a site does **not** accept the thing's article. `account` rows name an
account, not a thing.

- **Place rows (`source` = `osm:website`).** `tools/sq-accept-places.py` reads a release copy's
  `places-osm.sqlite` (`website` = the OSM `website` tag, else `contact:website`), **ODbL 1.0, © OpenStreetMap
  contributors** — no network, no live table. Columns `query, entity, source, accept`; the tokens are made by
  the same rule as below, and counts and everything left out are in `accept/BUILD-places.json`. Not accepted
  as "its own website": a page on a social network, a review / listing / map site, an ordering or booking
  platform or a marketplace (facebook.com, instagram.com, yelp.com, tripadvisor, google.com, toasttab.com … —
  the list is `NOT_OWN` in the builder); the bare front page of a site builder (a place's own subdomain of one
  is kept); a front page that places of three or more different names share. A tag that is a page inside a
  site (a chain's page for this branch) accepts that page, never the chain's front page. Numbers
  (what the place keys changed, on the rc8 answers): `baseline-rc8-keys3.md`.

- **Source.** Wikidata official website (P856), **CC0 1.0**, through QLever — every statement of the item with
  rank normal or preferred (Florence: `comune.fi.it` preferred, `comune.firenze.it` normal; both are the
  city's). Deprecated statements are left out. No LLM, no hand edit, no engine table: `accept/BUILD.json` says
  when the data was read and how many rows of each set gained alternatives.
- **Which item.** The row's `entity`: a QID is that item; a `wiki` row is the item of the English article it
  expects; otherwise the item whose English title has that key (two articles under one key: the one with an
  official site on an expected domain; none: no alternatives).
- **Format.** `query, entity, qid, accept` — one line per row that gains something, in the set's order;
  `(query, entity)` is the key (maker-product asks 15 words twice). `accept` is `|`-joined tokens:
  `site:<host>` — the value is a front page (no path, or only a language code / `index.html` / `home`, also
  chained: `/en/home/`): a result on that host **or a subdomain of it** is a hit; `url:<host/path>` — the value
  is a page inside a host (`nasa.gov/kennedy`): a result at that URL or under it is a hit. `www.`, the scheme,
  letter case and a trailing slash never count. For a site row, tokens its expected domains already take are
  not listed.
- **The set files do not change.** `expected` stays the primary answer; strata and the dev/test split are the
  builders'. A sidecar is a separate file so each keeps one licence (CC0) and old numbers stay reproducible.
- **Regenerate:** `tools/sq-accept-keys.py` (QLever: one COUNT, two pages of 500,000 checked against it, then
  the no-article items in batches of 200; 5 s apart; 429/503 waited out; answers cached in
  `~/.cache/logout/sq-accept/`). From the cache it writes the same bytes (`--offline` refuses to ask). Run it
  after any builder rewrites a set. Match rule: `tools/eval-queries.py accept_hit()`; test:
  `tests/unit/test-search-sq-accept.sh`.
- **Limits.** Wikidata is the only source: where the engine's "official" site comes from the English infobox
  and Wikidata names another address (or none), the row stays a miss (dev: 46 site rows, 21 article rows). A
  P856 value is taken as written — a few are catalogue pages rather than the thing's site; they are matched
  as that one URL, not as a host, and a page named only by its query string (`books.google.com/books?id=…`)
  is left out.

**Split.** The split is by entity, using one key in every set: the normalised English Wikipedia title
when the entity has an article, otherwise the Wikidata QID, otherwise the OSM id. The halves are about
50/50. An entity is test in every set where it appears, so a model trained on dev never meets a test
entity under another template.

**Ambiguous names.** When one name refers to several entities, the query is not scored. It is listed
in `ambiguous-<set>.tsv` with the number of entities.

## Sets

| set | source | licence | n | dev / test |
|---|---|---|---|---|
| wiki-redirect | Wikipedia redirect → target article | CC BY-SA 4.0 | 2,400 | 1,201 / 1,199 |
| site-nav | Wikidata P856 official website → domain | CC0 | 1,505 | 750 / 755 |
| account | Wikidata X / Instagram / Facebook / TikTok / LinkedIn / YouTube ids → URL | CC0 | 1,800 | 876 / 924 |
| place | OSM named feature near testbox's home → that feature | ODbL 1.0 | 1,300 | 661 / 639 |
| place-town | the same feature asked as "<name> <town>" | ODbL 1.0 | 345 | 171 / 174 |
| typo-wiki | one generated slip in a wiki-redirect query | CC BY-SA 4.0 | 500 | 248 / 252 |
| typo-site | one generated slip in a site-nav name | CC0 | 500 | 246 / 254 |
| typo-place | one generated slip in a place name | ODbL 1.0 | 493 | 264 / 229 |
| head-brand | a well-known organisation, website or service asked by its name → its own site (Wikidata P856, ranked by Wikipedia pageviews) | CC0 | 1,000 | 504 / 496 |
| maker-product | a maker's product or service asked by its bare word → its own site (Wikidata P856 + maker properties, strata by Wikipedia pageviews) | CC0 | 400 | 211 / 189 |
| site-slot | a name from site-nav / head-brand / own-title + the words for a section ("nike returns", "login chase") → that section's page on the site's own home page | CC0 (names, Wikidata) + the sites' own links | 1,035 | 513 / 522 |
| maker-product-clean | maker-product without web.archive.org golds, discontinued products, products that are not the dominant meaning of the bare word; repeated queries merged into one row with several golds (Wikidata P856 + maker properties, release pageviews) | CC0 | 169 | 90 / 79 |
| maker-plus-product | "maker + product" as people type it ("tesla model y", "bosch dishwasher") -> the maker's own site; 22 rows carry a page-level gold (page@1 / page@5) | CC0 | 223 | 122 / 101 |
| judged-info | informational questions by fixed templates over Wikidata/Wikipedia, plus the descriptive and government lists; **no answer key — judged by Clef 27B, verdicts frozen** (section below) | CC BY-SA 4.0 | 998 | 510 / 488 |
| facts | a templated question about a named thing → the Wikidata value and its article (100 properties, strata by Wikipedia pageviews) | CC0 (values) + CC BY-SA 4.0 (titles) | 2,968 | 1,424 / 1,544 |

**Attribution.**
- Wikipedia-derived sets: text from English Wikipedia (redirect titles and article titles), CC BY-SA 4.0.
- Wikidata: CC0. The accepted alternatives in `accept/` are Wikidata P856 values and nothing else: CC0.
- Places: © OpenStreetMap contributors, available under the Open Database License
  (https://www.openstreetmap.org/copyright). `place*.tsv` and `typo-place.tsv` are derived databases
  under ODbL. Each set and its ambiguous list is a separate file, so every file carries a single licence.

## judged-info — the informational yardstick, and the judge/labeller rule

**The rule.** Clef 27B over Workers AI is the judge; it labels no training data. Training labels come from
clef-flash run locally on buildbox plus Qwen3-14B, kept where they agree (the two-judge rule). A model must never learn
the judge that grades it. The same rule is in `docs/legal/SEARCH-MODEL-LICENSES.md`. What the judge sees over
Workers AI is the query, the result's title, its URL and its kind — never a snippet or page text (ADR-059).

**Why it replaces info-v1.** info-v1 (183 queries, `utility/ce-train/yardstick_eval.py --info info-v1`) was judged
by Clef 27B, and Clef 27B also labelled 25,140 of the reranker's training pairs
so a gain on it can be the model learning the judge.
judged-info is the yardstick now (`--info judged-info`, the default); info-v1 stays for continuity, labelled contaminated.

**Unlike every set above, this one has no answer from the data**: `expected` is `-` and `category` is `judged`.
The queries are open and written by fixed templates (no LLM writes a query, no history); the verdicts on
one engine's top-5 are an LLM judge's, frozen in `judged-info.verdicts.tsv`. It is not a training-label source.

Built by `tools/judged-info-evalset.py --gov <gov-services.tsv>` (QLever + the pageview API through head-brand's
helpers; no database). 998 queries, 510 dev / 488 test:

| template | wording | source | n | dev / test |
|---|---|---|---|---|
| wrote | who wrote \<title\> | Wikidata literary works with an author (P50) | 90 | 44 / 46 |
| capital | capital of \<title\> | sovereign states with a capital (P36) | 90 | 52 / 38 |
| founded | when was \<title\> founded | organisations with an inception (P571), one md5 half | 90 | 45 / 45 |
| does | what does \<title\> do | the same organisations, the other half | 90 | 46 / 44 |
| directed | who directed \<title\> | films with a director (P57) | 90 | 36 / 54 |
| what-is | what is \<title\> | disciplines, diseases, compounds, concepts, ideologies, religions | 90 | 44 / 46 |
| how-does | how does \<title\> work | processes, phenomena, technologies, machines | 90 | 51 / 39 |
| descriptive | as written | `evalsets/descriptive-sites.txt` (hand-written, 2026-09-28) | 258 | 129 / 129 |
| control | as written | `evalsets/descriptive-controls.txt` (hand-written) | 40 | 20 / 20 |
| gov | 20 task wordings x state | `tools/gov-eval/gov-services.tsv` (branch feat/search-gov-pages), 70 by md5 | 70 | 43 / 27 |

- Head: per Wikidata template the 300 most-linked items of the class (600 for organisations and films) had their
  September 2026 pageviews fetched; the 90 most viewed whose English title has no "(...)" qualifier and at most
  six words are the rows. Stratum `views-rank` 1-30 / 31-60 / 61+ within the template.
- Split: by entity with the md5 seed 402 — the normalised article title, so an entity is on the same side as in
  every other open set; `gov:<state>-<task>` for gov rows. The two hand-written lists keep their own odd/even split.
- Licence: article titles from English Wikipedia, CC BY-SA 4.0 (Wikidata CC0; the hand-written lines and the gov
  templates are ours). One file, one licence: CC BY-SA 4.0.
- `judged-info.exclude.tsv`: every query in `exclude.tsv`'s two forms, 1,996 keys, source `judged-info`.
  No head list, training pool or precompute may contain them — both halves.
- Limits. A fixed template does not always fit its slot ("how does big mac work", "what does arsenal f.c. do",
  "what is star"): the judge scores what the engine did with it, which is still a fair same-query comparison
  between two arms, but the level is not a clean "share of good questions answered". Football clubs are a
  quarter of the organisations (pageviews). The judge sees no page text, so a right page with an uninformative
  title scores low. Verdicts cover one engine's top-5: an arm that brings a new result needs that pair judged
  (the judge's cache keys on query + title + URL, so only moved results cost).

**Verdicts and rc8 (2026-10-13 UTC).** `tools/judged-info-judge.py` asked the rc8 release copy
(`proto-20261002-testbox-rc8`, build `8f651b68abd0`, `sq-engine.sh` on the workstation, 4 GB / 4 cores) for the top 5
of every query — 4,984 pairs — and judged them with `@cf/cloudflare/clef` and search-ranker's RELEVANT question.
**The run is COMPLETE: 4,984 pairs judged, 998 of 998 queries, all by the hosted Clef 27B on the free tier.**
It took two free days: 3,952 pairs on 2026-10-13 UTC (791 queries; Workers AI then refused with HTTP 429, code
4006, "you have used up your daily free allocation of 10,000 neurons" — free plan, nothing billed), and the
other 1,032 pairs on 2026-10-14 UTC (the rest of descriptive, control and gov; 1,032 requests, 274,035 input
tokens). The second day judged the SAME engine answers: the top-5 lists cached under build `8f651b68abd0` on
2026-10-12, no engine was asked again. The `judged` column of the verdict file carries each pair's own day. The
allowance did not come back at 00:00 UTC on the 14th: it was still refused at 00:31 and answered at 00:35.
One judge throughout: a self-hosted copy of the public weights was prepared on the 14th (`CLEF_URL`,
utility/lib/clef.py) but never judged anything, so no verdict here comes from it. The hosted judge repeats
itself exactly: 50 how-to answers sent twice on 2026-10-14 came back with identical probabilities.

| template | in set | judged | P(top-1 right) | top-1 >= 0.7 | any of top-5 >= 0.7 | judged on (UTC) |
|---|---|---|---|---|---|---|
| wrote | 90 | 90 | 0.838 | 85.6% | 91.1% | 10-13 |
| capital | 90 | 90 | 0.850 | 86.7% | 87.8% | 10-13 |
| founded | 90 | 90 | 0.842 | 84.4% | 95.6% | 10-13 |
| does | 90 | 90 | 0.836 | 81.1% | 97.8% | 10-13 |
| directed | 90 | 90 | 0.905 | 96.7% | 97.8% | 10-13 |
| what-is | 90 | 90 | 0.864 | 86.7% | 91.1% | 10-13 |
| how-does | 90 | 90 | 0.892 | 90.0% | 96.7% | 10-13 |
| descriptive | 258 | 258 | 0.651 | 51.9% | 86.0% | 161 queries 10-13, 97 on 10-14 |
| control | 40 | 40 | 0.812 | 75.0% | 92.5% | 10-14 |
| gov | 70 | 70 | 0.406 | 22.9% | 30.0% | 10-14 |
| **all** | 998 | 998 | **0.773** | **73.1%** | **87.4%** | |

The partial table of 2026-10-13 said 0.816 / 79.5% / 91.9% over 791 queries; the whole set is lower because
gov (22.9% top-1) and the rest of descriptive were the missing part. Compare a later run with THIS row, not that one.

P(top-1 right) is the mean Clef probability that the first result is what the searcher wanted; the percentage
columns count a query when a result scores at least 0.7. `judged-info.report.md` is the tool's own
output. The verdict file holds query key, template, split, rank, result id (sha256 of the URL, 16 hex), URL,
kind, score, model, date and build — no title, no page text.

**The ledger over-counts.** Cloudflare's meter reached 10,000 neurons after about 3,960 calls (≈ 2.5 neurons a
call); `utility/lib/clef.py` counted 23,170 for them (0.0218 per token, input and output alike) and so shows
13,170 "paid" neurons for 2026-10-13 that were never billed. A free day is therefore about 3,900 such calls, not
1,650. The constant is left as it is (it errs on the safe side) until measures it on a second day.

## head-brand

Built by `tools/head-brand-evalset.py`, not by `open-evalsets.py`: it needs no database, only QLever (Wikidata)
and the Wikimedia pageview API. It exists because the sets above sample the long tail of official sites and
passed rc4, while own rank sent "chess" to chess.az and ranked gog.com 24,498.

- A row is an English article about an organisation, website or service (the importer's rule in
  `importer/wikidata_sites.py`: `ORG_TYPES`, exact or through subclass-of; never a person, a place, a band or an adult site) whose
  title names its own front page: "Reddit" → reddit.com, "Telegram (software)" → telegram.org, "GOG.com" →
  gog.com (query `gog`). Every front page the title names is an accepted answer (`bbc.co.uk|bbc.com`).
- Head: of 75,248 such articles, the 5,000 with the most Wikidata sitelinks had their September 2026 pageviews
  fetched; the 1,000 most viewed are the set. 768 names shared by two such articles are in
  `ambiguous-head-brand.tsv`.
- Strata: `views-rank` 1-100 (48 dev / 52 test), 101-300 (94 / 106), 301+ (362 / 338); `template` title (983)
  or label (17: the title is the host itself); `name:shared` (53 / 30) marks a query that is also the exact
  title of another article ("chess", "amazon", "telegram"): there the site is one right answer, not the only one.
  Report `name:only` and `name:shared` apart.
- Limits. The kind rule is the one the own-rank recipe credits, so the set shows whether those brands end up
  first; it cannot show a brand the type rule misses. Sites on the engine's block lists (adult sites are among
  the most viewed articles) score zero in every arm. Pageviews are one month: regenerate with `--month`.
- Not yet run against any engine.

## maker-product ("keep" showed no Google Keep)

Built by `tools/maker-product-evalset.py` (QLever + the pageview API, no database; it reads head-brand's helpers).
head-brand cannot hold these rows: "Google Keep" does not name keep.google.com.

- A row is an English article with an official website and a maker that has its own article (Wikidata developer,
  owner, parent, manufacturer, publisher, operator or creator), whose title is "<maker> <rest>" and whose site is
  named by the rest: a label of the host, a path segment, or the label after the maker (keep.google.com,
  google.com/maps, tesla.com/modely). The query is the rest: `keep`, `maps`, `model y`.
- Head: of 2,248 such articles the 400 with the most Wikidata sitelinks; September 2026 pageviews give the strata
  `views-rank` 1-100 / 101+. `site:subdomain` (76) is the Google Keep shape, `site:other` (324) a path or the
  maker's own domain. 211 dev / 189 test.
- Limits. Most of these words are a common word first ("canal", "news", "air"): the product is ONE right answer,
  so read hit@5, not hit@1. A site that is a path counts any result on the maker's host. Cars and aircraft are a
  third of the set; the software and services are at the head. Two products of one word are two rows.
- 2026-10-08, rc5 release on the workstation, all 400 rows, master -> the product tier (`search_api.maker_products`):
  hit@5 32.2% -> 50.2% (hit@10 136 -> 217 of 400); views-rank 1-100 48% -> 71%; site:subdomain 10.5% -> 50.0%; dev 69 -> 108 of 211, test 60 -> 93 of 189. One row worse ("news": Sky News 6th -> 9th).
  The rule was written looking at both halves: none of it is held out.

## facts

Built by `tools/facts-evalset.py FACTS.sqlite REGISTRY.sqlite SIGNALS.bin` from a release's own `facts.sqlite`
(`release/facts_build.py`: Wikidata through QLever), its registry and its `signals.bin`. No database, no LLM, no history.
Scored by `tools/facts-eval.py answered facts.tsv --url ENGINE` (not `eval-queries.py`: the answer is a card, not a rank).

- A row is one phrasing of the template table (`release/facts_templates.tsv`, the first alternative of every group) filled
  with one article's title without its qualifier ("Prince (musician)" asks as "prince" only when no other article shares
  the bare name; shared names are in `ambiguous-facts.tsv`, 2,011 of them). `expected` is `<article title>|<value>`: the value
  as the build stored it (ISO date, decimal amount and unit symbol, the item's English label). Category `fact`.
- **ANSWERED** = the answer box names that article (its Wikipedia url) and carries that value (dates exactly, amounts
  within 0.5%, labels case-insensitively). **CARD** = an answer box for the right article whatever the value.
- Entities: for each property the articles that have a value, ordered by the release's Wikipedia pageviews; strata
  `views-rank` 1-100 (448 dev / 552 test), 101-1000 (502 / 506), 1001+ (up to rank 20,000; 474 / 486); 12 rows per
  (property, stratum), md5-ordered (seed 402); `kind` date (185 / 175), quantity (309 / 331), item (824 / 928),
  string (106 / 110). 84 of the 100 properties: 16 are not sampled because their phrasings are not a question about one
  entity or overlap another property's ("where is X", "what does X do", "who won X", sequels, platforms, stock listing).
- Split by entity with the same key as every other set (the normalised title), so an entity is test here if it is test there.
- **Licence.** The values, units and qualifiers are Wikidata (CC0). The article titles in `query` and `expected` are English
  Wikipedia (CC BY-SA 4.0). The file carries both; attribute Wikipedia for the titles.
- **Limits.** Circular by construction: the set is built from the file the engine answers from, so ANSWERED measures the
  template matching, the name resolution (redirects, namesakes, the leading "the") and the formatting path, not whether
  Wikidata is right. The expected value is `ord` 0 of the build (preferred, then newest); a card that says a second value
  is a miss. Phrasings are the first alternative of each pattern, one per row: the other alternatives are covered only by
  the unit test. Head-heavy on purpose: two thirds of the rows are a property's 1,000 most-viewed articles.
- **2026-10-13, rc8 release copy + `facts.sqlite` on the workstation** (`sq-engine.sh`, 4 GB / 4 cpus, one engine at a time;
  `/instant?results=0`, all 2,968 rows): ANSWERED `SEARCH_FACTS=0` 248 (8.4%; dev 120 of 1,424, test 128 of 1,544: the older
  answer box, 17 question kinds over the cards' facts) -> `SEARCH_FACTS=1` **2,867 (96.6%; dev 1,363 = 95.7%, test 1,504 = 97.4%)**;
  CARD 273 -> 2,898. The 70 rows with no card are names the engine's name table does not resolve from the bare title
  ("sistas" for "Sistas (TV series)"); of the 31 with a card and another value, 25 are the older box answering first in its own form ("60 years old" for a
  birth date; "No" for "is freddie mercury dead", which is the older box's own wording bug) and 6 are the scorer (a value
  the card lists second, a calling code read as a number).
  Result lists, same build with the flag on and off (`facts-eval.py dump` twice, then `diff`): 500 open-set dev queries
  500 identical, no card on any of them; 150 facts dev queries 150 identical (145 with a card on, 15 off). The templates
  and name rules were written looking at both halves: nothing here is held out.
- **2026-10-13, the four flags fault B, dev half only (1,424 rows), same release copy, `SEARCH_FACTS=1` in every arm,
  one engine at a time:** ANSWERED flags off 1,363 (95.7%, Wilson 94.5-96.7), wrong value 23, no card 38 -> all four on with
  `SEARCH_FACTS_BARE_TITLE=1` **1,385 (97.3%, 96.3-98.0)**, 16, 23 -> with `SEARCH_FACTS_BARE_TITLE=2` **1,401 (98.4%, 97.6-98.9)**, 16, 7.
  By flag (answer stage run without an engine, same counts as the engine arms where both were run): `SEARCH_ANSWER_DEAD` +4,
  `SEARCH_FACTS_OVER_BOX` +3, `SEARCH_FACTS_UNICODE_NAMES` +6, `SEARCH_FACTS_BARE_TITLE=1` +9, `=2` +25. No row got worse. The 16
  left with another value: 11 age questions answered with the age (right, and what was asked; the set expects the birth date), 3
  scorer, 2 "how long did X last" read as running time. The 7 with no card at `=2`: 3 a phrasing whose optional last word eats
  the name ("orlen arena capacity" reads "orlen"), 3 a name with two "X (…)" articles or an article titled "The X", 1 a whole
  query that is itself an article's name. Result lists, flags on (`=2`) vs off: 500 open-set dev queries (the 11 sets) 500
  identical, no card on any; 150 facts dev queries 150 identical. Not held out, and circular like the rest of this set: a
  matching test, not real-world accuracy. Mode 2 answers past a redirect, which is Wikipedia's primary meaning of the name.
- **Template ranking.** `release/facts_rank.tsv` (from `facts_build.py rank`): per property, how many of the 100,000 most
  viewed articles carry it and their share of those articles' views. Occupation, citizenship, birth date and birthplace reach
  41-43% of head views (people), genre 27%, awards 24%, country of origin 20%; population 3%, capital 2%, CEO 0.7%.
- **Left out of v1.** Properties: anything whose value needs more than a label (P39 position with its jurisdiction, P54
  team with years, P166 award with the work), identifiers, P31/P279 ("what is X": the article lead says it better), P18 and
  other media, P856 (the site tier already answers), monolingual texts other than motto/nickname/demonym/official name,
  P1705 native label (a "X in French" phrasing needs the language), households, width, first flight, first performance,
  P1037 director/manager. Quantities in a unit the engine cannot say are stored and never shown. Lists: films, albums,
  video games and elections only, by year or decade, with an optional genre word; no "best", no country, no award lists.
  Questions that need arithmetic across entities ("which is bigger"), superlatives ("tallest building") and time-scoped
  questions ("president in 1990") are not templates.

## Sizes per stratum (dev / test)

**wiki-redirect**

- pop (`suggest_items.pop`, Wikipedia pageviews; `unranked` = not in the suggester):
  - high 316/284
  - mid 300/300
  - low 312/288
  - unranked 273/327
- len (words):
  - 1: 412/388
  - 2: 374/426
  - 3+: 415/385
- form:
  - alias 958/980
  - spelling 243/219 (a redirect within 2 edits of the title; Wikipedia's own misspelling redirects)

**site-nav**

- article:
  - yes 486/514 (P856 from QLever, item has an English article)
  - no 264/241 (`wiki_links` platform=site: no English article)
- sitelinks:
  - 1-4: 236/264
  - 5-19: 262/238
  - 20-49: 122/133
  - 50+: 130/120
- template, one per entity:
  - bare "<name>": 221/276
  - "<name> official site": 273/229
  - "<name> website": 256/250

**account**

- platform:
  - facebook 138/162
  - instagram 151/149
  - linkedin 133/167
  - tiktok 148/152
  - x ("twitter") 149/151
  - youtube 157/143
- sitelinks:
  - 1-9: 294/306
  - 10-29: 287/313
  - 30+: 295/305

**place**

- kind:
  - food 184/166
  - leisure 160/161
  - service 133/142
  - shop 178/167
  - other 6/3
- len:
  - 1: 65/67
  - 2: 291/273
  - 3+: 305/299

**place-town**

- kind:
  - food 64/68
  - leisure 21/25
  - service 33/36
  - shop 50/45
  - other 3/0

**typo-wiki** (pop):
- high 63/75
- mid 58/58
- low 76/60
- unranked 51/59

**typo-site**: article yes 160/166, no 86/88.

**typo-place**:
- food 52/45
- leisure 81/62
- service 59/60
- shop 69/60
- other 3/2

## How each set was built, and what it can and cannot tell you

- **wiki-redirect.** Comes from the redirect titles in `wiki_docs.aliases`. The following redirects
  are dropped:
  - trivial ones, where the case/punctuation-normalised key equals the title's key;
  - redirects to the title without its qualifier;
  - redirects containing `(),:/#`;
  - redirects longer than 6 words.

  A redirect is kept only when its key maps to that single article in `wiki_aliases`. It must not
  also be another title or another article's redirect.
- **site-nav.** Things with an English article take P856 from Wikidata through QLever. They do not
  use the engine's own `wiki_sites` table, which is built from infoboxes. Rules:
  - Hosts that are shared platforms (Facebook pages, blogspot, …) are dropped.
  - URLs deep inside someone else's site are dropped.
  - A name is ambiguous when another P856 item has the same key, or when the key is a title or
    redirect of a different English article.
  - `article:no` covers the much more obscure things without an article. Many of their domains are
    not in the crawled index, so this stratum mostly measures coverage. Run without `--no-present`
    to see the indexed share.
- **account.** Comes from `wiki_links`. Names are dropped when:
  - the engine's account parser would refuse them (`_ACCOUNT_NOT_A_NAME`), or
  - they are longer than 5 words, or
  - their key belongs to more than one Wikidata entity anywhere in `wiki_links`.

  YouTube answers come from `youtube_channels`, so only channels held there are included. Either the
  `@handle` URL or the `/channel/<id>` URL counts as a hit.
- **place.** Uses OSM nodes/ways within 38 km of testbox's `SEARCH_HOME` (40.592,-105.871). `/search` has
  no per-request location; it measures places from the home point within 40 km. Selection rules:
  - no brand, at most 4 words;
  - every row of that name within 40 km, Web Data Commons listings included, lies within 0.5 km of
    the others, so the name is one place.
- **place-town.** Asks for the same features as "<name> <town>". Until 2026-10-07 the engine did not
  parse a town out of a place query: the whole string went to the name match (the 10.9 % baseline
  below). `search_api.place_town` now tries the last one to three words as the town. On the
  release engine (rc4 files, own rank G), test half: hit@1 11.5 % -> 85.1 % [+66.7, +80.5 pt], hit@5
  21.8 % -> 100 %; place and typo-place unchanged on every query. The town of each query is the row's
  own `city`, so the set only holds places whose address names a town. Most remaining rank-2 answers are
  the place's own website shown above its map entry.
  Places with NO city (56 % of the OpenStreetMap file) are found by where they lie: inside the circle
  their town's addressed places draw (`tools/sqlite_lookups/towns.py`, release engine only). No open
  set labels such places, so this was measured by hiding the city of every place-town answer (and its
  listing twins) and rebuilding the circles without them — test half, hit@1 11.5 % -> 78.2 %
  [+59.2, +74.1 pt], hit@5 21.8 % -> 94.3 %; with the cities in place nothing changed on any query of
  place-town, place or typo-place. A simulation: places that carry an address may lie in denser
  parts of a town than those that do not.
- **typos.** One slip in the longest word of 5+ letters: drop, swap, double, or a neighbouring QWERTY
  key. The first letter is never touched. A typo is rejected when its key already exists as a
  Wikipedia title or redirect, a Wikidata name, or a place name. Otherwise it would be an exact hit,
  not a typo test.

**Circularity.** wiki-redirect, account, place and article:no site-nav are built from the same tables
the engine serves. They measure ranking and fusion around known lookups, and regressions such as the
2026-09-29 exact-tier unpin. They do not measure independent coverage. site-nav article:yes (P856 vs
infobox) and the typo sets are the least circular.

**Not used.** Strata use only `suggest_items.pop` and Wikidata sitelinks. History-derived columns
(`suggest_items.hist/demand`, `*history_visits`, `serve_sites`) and the legacy columns
(`site_quality`, `kind_members`) are never read.

**Not built (yet).** The optional "kind" set: Wikidata P31 classes → members, graded and lower
confidence.

## Baseline (testbox, 2026-10-02, test split)

**Setup.** The run used a throwaway container FROM testbox's `logout/search-api:nomic2` image: `--cpus 2`,
same env, `/search` endpoint, k=10, reached at 127.0.0.1 via ssh -L. The container had one deviation
from testbox: `SEARCH_STATEMENT_TIMEOUT_MS=30000`. testbox sat at load 10-29 from peer jobs, and the 2 s
production statement timeout drops whole lists under that load. The numbers therefore measure
ranking, not latency.

**Determinism.** Two runs of the same 201 queries gave identical top-1 and top-5. 0 of 201 differed.

**What the columns mean.**
- hit@k: share of queries whose expected answer is in the top k. Higher is better.
- Half-width: the 95 % paired-bootstrap half-width for a change in hit@1 that disagrees with the
  baseline on as many queries as the reference A/B did.
- MDE80: the smallest hit@1 change that set detects 80 % of the time.

**Reference A/B.** The disagreement rate comes from `--no-learned` (learned ranker off) on every third
test query (n = 1,475). That A/B moved hit@1 by -3.0 pt overall, 95 % CI [-4.0, -2.0].

| set | n test | hit@1 | hit@5 | queries that flip at rank 1 in the A/B | 95 % half-width | MDE80 |
|---|---|---|---|---|---|---|
| wiki-redirect | 1,199 | 93.7 % | 100.0 % | 2.5 % | 0.9 pt | 1.3 pt |
| site-nav | 755 | 21.3 % | 29.7 % | 7.1 % | 1.9 pt | 2.7 pt |
| account | 924 | 99.9 % | 99.9 % | 0 % | — | saturated |
| place | 639 | 55.7 % | 93.4 % | 3.3 % | 1.4 pt | 2.0 pt |
| place-town | 174 | 10.9 % | 21.8 % | 1.7 % | 1.9 pt | 2.8 pt |
| typo-wiki | 252 | 31.0 % | 42.1 % | 6.0 % | 3.0 pt | 4.3 pt |
| typo-site | 254 | 17.3 % | 29.9 % | 15.3 % | 4.8 pt | 6.9 pt |
| typo-place | 229 | 63.3 % | 86.0 % | 0 % | — | — |
| all test | 4,426 | | | 3.7 % | 0.6 pt | 0.8 pt |

**Strata.**
- **site-nav by template.**
  - bare: 43.5 % hit@1
  - "official site": 8.3 % hit@1
  - "website": 8.8 % hit@1

  The tail words cost the engine about 35 pt.
- **site-nav coverage.** Only 56 of the 1,009 site/typo-site test domains have a crawled homepage in
  `docs`. On those 56, hit@1 is 33. This set mostly measures coverage and nav-site lookup.
- **place by length.**
  - 1-word names: 35.8 % hit@1
  - 2-word names: 55.7 % hit@1
  - 3+-word names: 60.2 % hit@1
- **account.** This set is circular, since `wiki_links` is the engine's own lookup table. It
  regression-tests the account parser and tier only.

**What this says about the 0.5 pt acceptance rule.** No single set resolves ±0.5 pt for a change of
the size measured above. The pooled test split (half-width 0.6 pt) nearly does. Use `--scale 2`, or
score dev and test together for acceptance only, never for tuning.

### site-slot (2026-11-10)

"<name> <section words>" (or "<section words> <name>", 20 %), expected = the section's page. Category `account` (a result URL,
scheme and www. dropped, equals the expected URL). Generator: `tools/site-slots.py evalset SLOTS.jsonl evalsets/open/site-slot.tsv`
(seed 575, md5 order; deterministic on the same slot table). The slot table is `tools/site-slots.py build` over the 2026-11-05
home-page fetch of the 19,905 official sites (`tools/site-nav.py fetch`, plus the Common Crawl fill-ins), same registered domain only.
The names are the bare queries of site-nav (template:bare), head-brand and own-title rows whose official domain has a slot table
row; the split is that row's own (an entity is test in every set where it appears). At most two slots per entity, at most 60 rows
per slot, rarer slots first (privacy and contact pages are 55 % and 65 % of all slots and would measure nothing); only slots
people ask for (those with typed words in `serve/api/site_slots.py`) are asked. Test-half queries are in `exclude.tsv`.

**Circularity.** The key is the table the engine reads, so this set measures the parsing, the name's resolution and the row's
place in the list, not whether the table is right or complete. A table error is measured by a Clef judgement of the row, not here.
Strata: `slot:<id>`, `template:name-first|slot-first`, `from:<source set>`.
## maker-product-clean and maker-plus-product (2026-11-10)

Both are built by a tool, from the release's own files, read-only (no Tranco, no visitor searches, no network, no
engine). The old maker-product file is unchanged.

- **maker-product-clean** (`tools/maker-product-clean-evalset.py --release DIR`; its report is
  `maker-product-clean.report.md`). From the old set's 400 rows: 11 gold sites on web.archive.org, 5 discontinued
  products (a dissolved or end-time date in facts.sqlite), 92 products whose article the release does not list under
  the query's key, 120 whose products hold under half of the key's Wikipedia pageviews (signals.bin) go; 3 rows merge
  into others of the same query (one row, several golds). Dev / test assignment is the old one, per row.
- **maker-plus-product** (`tools/maker-plus-product-evalset.py`; the docstring says exactly how names were chosen). Named
  products from the release's maker facts (manufacturer, developer, publisher) with the most pageviews per category;
  typed rows ("bosch dishwasher") for categories Wikidata hardly covers, from my own maker list (SEEDS), each kept only
  with an official site in the 2026-11-05 fetched list (never one of the 251 hosts added for an eval) and an entry in the
  release's names. The set is smaller and less balanced than planned: the fetched list holds no site for most toy, tool
  and food makers (tools 7, toys 4, food 8 rows).
- **page gold** is a sidecar, `page/<set>.page.tsv` (query, entity, page); sq-eval reads it and adds a
  "<set> (page gold)" row with page@1 / page@5 beside the site-level row. 60 candidate pages from site-pages.sqlite were
  read by hand (`page/maker-plus-product.pages.checked.tsv`); 22 are the product's own page, the rest are blog posts,
  press releases, support and legal pages, and are not gold.
