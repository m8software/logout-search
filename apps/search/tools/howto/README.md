# Pre-generated how-to answers — week-1 prep

**2026-10-13: the bake-off has run except for Clef** — Qwen3-14B and gpt-oss-20b on buildbox, Qwen3-32B, Qwen3.5-27B,
Qwen3-30B-A3B, Mistral Small 3.2 and gpt-oss-120b on RunPod ($0.71), all scored by two local judges on buildbox (Qwen3-14B, and clef-flash through `score_flash.py`); the table,
the samples, the prepared Clef batch (375 requests, not sent) and the full-run prices are in
[`bakeoff/BAKEOFF.md`](bakeoff/BAKEOFF.md). Still open: the Clef scores, then the pick.

**2026-10-13: a first PARTIAL demand list exists** — 5 of the 36 shards of the anchor scan, real page titles, $0:
[`demand/DEMAND.md`](demand/DEMAND.md) (generated), §2d below (what it shows and what it does not). The Athena slug query (§2a) was never run and is no longer needed.

Everything else here is PREPARED, NOT RUN (lane L6, 2026-10-12): no Athena query, no Clef call has happened. Once the owner approves, the demand query and the bake-off
each start with one command (bottom of this page). Nothing under this directory is imported by the
box (rule 7: development tooling only).

## The flow, end to end

```
demand ─────────────┬─ Common Crawl URL slugs (Athena, $0.13/crawl)  infra/build-aws/jobs/cc-howto-titles.{sql,sh}
                    └─ Stack Exchange question views (the dumps)  se_demand.py
      → topics.py key + cluster            one key per topic; "tie a tie" = "tie a necktie"
      → categories.py                      allow / deny / health / unknown  (categories.tsv, tested)
      → grounding                          the open-licensed source for each allowed topic (below)
      → bakeoff.py                         one prompt, every candidate model, 50 topics      ← approval + licence rows
      → score.py                           Clef 27B + Qwen3-14B on buildbox, three questions each
      → owner reads 20 (bake-off) / 100 (release)
      → full run (RunPod ≈ $5, approved in principle) → howto.sqlite (topic, category, steps, source ids, model, date)
      → the card: the generic card helper, "How to tie a tie · 6 steps · generated from Wikibooks, checked 2026-10"
```

Query → topic on the box uses the same `topics.key` (plus the typo path), so the demand side and the
serving side agree on what a topic is.

## 1. Deny / allow list — `categories.tsv`, `categories.py`

Owner's rule: **allow** cooking, crafts, household, software/phones, gardening, travel, school subjects;
**deny** medical, legal, mains electrical, chemicals, weapons, vehicles beyond the trivial (jump-start and
change a tyre in; brakes, airbags, fuel out), finance, anything needing a licence or permit; **health** →
the MedlinePlus topic (public domain, the `open:medlineplus` rows), never generated.

Rows are `verdict TAB category TAB pattern`; a pattern is matched as a contiguous run of stems inside
the topic key, so one row covers tie/tying/tied. Precedence `allow!` (the named exceptions) > `deny` >
`health` > `other` (outside the allowed categories, reported as unknown — §2d) > `allow` > unknown; unknown is not generated either. `generation_mode()` adds the grounding
rule: an allowed topic with no source is written ungrounded only in cooking / crafts / household /
software, otherwise skipped.

**Using the kinds model later.** The engine's kinds classifier (bge-small nearest examples,
`importer/kinds.py`) can label the `unknown` remainder by nearest allowed / denied examples; it may move
a topic INTO allow only when no deny stem is present — a deny here is final. Not written in this lane.

Test: `bash tests/unit/test-search-howto.sh` — 43 example topics including the tricky ones ("how to
jump a car" allow, "how to replace brake pads" deny, "how to lower blood pressure" health, "how to file
taxes" deny, "how to burn a cd" allow, "how to balance a chemical equation" allow by exception).

## 2a. Common Crawl demand — Athena, prepared

**Titles are NOT in the columnar index.** Checked 2026-10-12 by reading the parquet footer of
`crawl=CC-MAIN-2026-39/subset=warc/part-00149` over HTTP range requests (pyarrow, 64 KB fetched, no S3
listing, no Athena): 31 columns, all URL/host/fetch/WARC-location fields — no `content_title`, no text.
Titles live in the WAT files (`HTML-Metadata.Head.Title`).

So `cc-howto-titles.sql` counts **distinct registered domains per "how to …" URL slug** (`url_path`):
`/how-to-tie-a-tie`, `/how-to/tie-a-tie/`, `/howto-tie-a-tie.html` → "tie a tie". Every CMS slugs the
title, so for how-to pages the slug is the title. Final normalisation and clustering run at home
(`topics.py`), not in SQL.

**Cost estimate: $0.13 per crawl.** Method: per-column compressed bytes from the footer × 300 files
(from `cc-index-table.paths.gz`). One file is 631.0 MB / 7,324,980 rows → the crawl ≈ 189 GB, ≈ 2.2 B
rows. Columns the query touches: `url_path` 83.84 MB + `content_languages` 1.65 + `url_host_registered_domain`
1.14 + `content_mime_detected` 0.13 + `fetch_status` 0.01 = 86.8 MB/file → **26.0 GB → $0.13** at Athena's
$5/TB. Ceiling if every column were read: 189 GB → $0.95. The runner creates a workgroup with
`BytesScannedCutoffPerQuery` = 60 GB ($0.30), a hard cap Athena enforces. A second crawl (2026-34) for
a steadier count doubles it: $0.26. `cc-howto-titles.sh --estimate` prints the same numbers.

**The alternative, real titles from WAT** (like `cc-anchors.sh`): ~12 s CPU per WAT file measured
there, 100,000 files per crawl, 2 processes per c7i-flex.large → ~167 instance-hours → **$5–7 spot**
for a full crawl (at the $0.03–0.04/h), or **~$2 for the 12% sample** that took 55 instance-hours.
Cheapest: **the full-crawl anchor scan already parses every WAT record's JSON** — adding
`Head.Title` to `anchors.py`'s record reader gives titles for ~$0 extra when that run happens.
Recommendation: run the $0.13 slug query now for the head list; tap titles for the check.

Prerequisites before the first run (owner, not done by this lane): the launcher user's policy covers
EC2 and `s3://commoncrawl` reads only — Athena/Glue permissions and a private output bucket
(`ATHENA_OUTPUT=s3://…/athena/`) are listed in the runner's header.

Output: `.build-out/<stamp>-cc-howto-titles/cc-howto-titles.<crawl>.csv` (topic, sites, pages,
example_site) → `topics.py normalise` + `topics.py cluster`.

## 2b. Stack Exchange demand — `se_demand.py`

Reads a site's `Posts.xml` (or its `<site>.7z` streamed through `7z`, exactly as
`importer/stackexchange.py` does — same dump `stackexchange_20260630`, same files, nothing downloaded
here). Fields: `PostTypeId=1`, `Id`, `Title`, `Tags`, `ViewCount`, `Score`, `AcceptedAnswerId`. Keeps
how-to questions (`topics.howto_phrase`), keys them, and `merge` sums views per key over sites.

Sites (`se_demand.py sites`), with the category most of their how-tos fall in:

| site | category | in the importer list? |
|---|---|---|
| cooking, homebrew | cooking | yes |
| diy, pets, bicycles | household | yes |
| gardening | gardening | yes |
| superuser, askubuntu, unix, apple, android, webapps, raspberrypi, gaming | software | yes |
| travel, outdoors | travel | yes |
| woodworking, photo, 3dprinting | crafts | yes |
| english | school | yes |
| **crafts, lifehacks, ell** | crafts / household / school | **no — one small 7z each (< 10 MB)** |
| **math** | school | **no — ~2 GB; optional** |

Not read (denied categories): law, health, money, mechanics, security, electronics, serverfault, dba,
softwareengineering, networkengineering.

Tested on `tests/fixtures/howto/Posts.xml` (200 lines, synthetic, the dump's schema).

## 2c. Normalisation and clustering — `topics.py`

`key()`: lower-case, strip accents, the part after "how to / how do i / how can you", drop the site
suffix and the title filler ("step by step", "(with pictures)", "in 5 easy steps"), fold variants
(tyre→tire, necktie→tie, colour→color), drop stop words, light stem (bake/baking/baked → bak).
`cluster()`: union-find over Jaccard ≥ 0.6 on stem sets, or one set inside the other by ≤ 1 stem;
candidates share a stem seen in < 2,000 keys. **Embeddings:** `--vecs PREFIX` takes `tools/embed.py`
output (bge-small, buildbox) over the keys and adds cosine ≥ 0.90 as a second merge reason — optional, so the
string rule runs anywhere in seconds; the engine's own embedding path needs sentence-transformers and
was not pulled in.

## 2d. Demand from real page titles — `demand.py`, run on the anchor scan's finished shards (2026-10-13)

**Input.** the full-crawl scan (`importer/anchors.py` `howto_of`, branch `feat/sq-482-anchors`) reads every WAT record's
`HTML-Metadata.Head.Title`; a title matching `\bhow (to|do i|can you|…) …` is kept **from "how" on**, lower-cased, at most ~150
characters, with the page's registered domain. Each of 36 shards (2,777 WAT files) brings home `out/shard-NN/howto-titles.tsv.gz`:
two columns, `title TAB site`, no header, no count column; one row per (title, site) **per WAT file**, so the same pair repeats
across files (≈3% of rows) and across shards — the site count is something this tool makes, not something the file carries.
Per shard: ≈240,000 rows, ≈148,000 distinct sites, 9 MB.

**What the tool does** (`demand.py`, stdlib, reads the scan directory and never writes into it; only shards in `state/done`
with `rc` 0): title → phrase (cut at the first site separator) → `topics.key` → **signature** (the key's stems as a sorted set,
weak modifiers and years dropped) → distinct sites per signature → `categories` on every member → eval exclusions → rank.

**Clustering is lexical, not embeddings.** The workstation has no numpy and no embedding model, and buildbox / indexhost were off limits
for this run, so no vectors were made. `topics.cluster`'s Jaccard rule was tried on the top 6,000 allowed keys and rejected:
single linkage put 181 keys under "clean laminate floors" (dentures, windows, "organize garage") and 45 under "reset
password". The signature rule cannot chain: two titles are one topic only when they have the same stems. It merges "tie a tie"
= "tie a necktie" (the SYN table), "make your own pizza dough" = "make homemade pizza dough", "start a blog in 2026" = "start a
blog", and keeps "make a cake" ≠ "make a chocolate cake". 26% of topics have more than one member key. What it gets wrong:
direction ("convert pdf to word" = "convert word to pdf"), and true synonyms outside the SYN table ("boil an egg" / "hard boil
eggs") stay apart. `--vecs` is wired for `tools/embed.py` output over a run's `keys.tsv` (bge-small, already licensed) and is
tested only with hand-made pairs — **not run with real vectors**.

**The scan cut the title at "how".** "Windows 11 blue screen: how to fix it" arrives as "how to fix it". 10% of rows are such
stumps (one stem, a pronoun for an object, or over 9 stems) and are dropped; what survives can still be subject-less ("how to
reset my password" — whose?). Fixing it at the source means keeping the whole title in `howto_of`, which is the code and a
rerun of the scan; not done.

**What the list is, honestly.** Distinct sites publishing a title is *supply* (what sites write, SEO and spam included), not
what people ask. The head of the raw list is casino spam, site boilerplate ("how to fix the telegraph access issue", 165 sites)
and film titles; the category rules push those to deny/unknown, and what is left on top is plausible but not a ranking of
importance: "how to bake bread" has 2 sites in these 5 shards, "how to bake a pie r a" (one template on 112 domains) has 112.
One identical title across a network of domains counts as many sites; nothing here detects that. Stack Exchange views (§2b)
are still needed as the second signal, and the top of this list needs a human pass before it feeds a generation run.

**Category rules changed on this evidence** (`categories.tsv`, tested in `tests/unit/test-search-howto-demand.sh`):
- `get to` (travel) was the single stem `get` once "to" was dropped and allowed 2,253 topics ("get hired", "get rich", "get rid
  of bed bugs"); `check in` was `check`, `carry on` was `carry`. The loader now refuses a several-word pattern that is one stem.
- Generic allows narrowed: solve, factor, area, season, stock, typing, teams → phrases ("solve an equation", "chicken stock").
- Gambling (casino, slots, roulette, blackjack, poker, pokies, free spins…) and credit / IRS added under deny finance.
- A new verdict row `other` (business, marketing, life, entertainment): outside the seven allowed categories but not on the
  owner's deny list. It stops a keyword allow ("grow" your business, "email" list) and is reported as `unknown` with the
  category naming why. Precedence: allow! > deny > health > other > allow.
Allowed fell from 13,514 to 9,794 topics on the same five shards. The rules are still keywords: by my own reading (not a measurement)
about 10 of the top 50 allowed are wrong or useless as a card (a product's template title, a site's own account pages), and
about 10 of the 30 random mid-rank ones are not a task in an allowed category. `allow/household window` also catches Windows.

**Stability (5 shards, 3 against 2).** The whole list is fairly stable (top 100: 80% overlap, Spearman 0.82); the *allowed* list
is not yet (top 50: 58%, top 500: 48%, Spearman 0.31 → 0.12), because the 500th allowed topic has 5 sites in one half and 3 in
the other — ranks there are ties broken by noise. Nearly every top topic of one half exists in the other (100% / 77%); it is the
order that is unsettled. Expect this to firm up with the full crawl: site counts of the top 100 allowed grow almost linearly
with shards (6.5 → 26.9 mean sites from 1 to 5 shards).

**Will 36 shards give 20–50k topics?** By count, yes: allowed topics with ≥ 5 distinct sites are 1,258 now and the power-law
reading for 36 shards is 30,000–45,000 (≥ 3 sites: 52,000–69,000; ≥ 10 sites: 10,000–13,000). The same method predicted the
5-shard figure from shards 2–3 within 2–8%, but 5 → 36 is a sevenfold stretch and the exponent is falling, so treat the lower
figure as the estimate. By quality, not shown: at 3–5 sites a "topic" is often one article's title, and the allow rules are
keywords. A floor of 10 sites (≈ 10k allowed) is the part I would trust without a second signal.

**Eval hygiene.** 9 topics were dropped as excluded eval queries (`evalsets/open/exclude.tsv`, 17,359 rows; matched as typed,
without "how to", by letters-and-digits form, and by topic key). None of the 50 bake-off topics is an excluded query; 28 are in
the list (all allowed, flagged `bakeoff=1` so a how-to held-out set can leave them out), 22 are not in it at 5 shards.

**Rerun when all 36 shards are home** (5 shards took 2 min 14 s; 36 is an estimated 30 minutes, most of it `--growth`, not measured; per-shard work is cached):

```bash
apps/search/tools/howto/demand.py run --scan ~/search-eval/cc-anchors-2026-39 \
    --out ~/search-eval/howto-demand/full-2026-39 --work ~/search-eval/howto-demand/work \
    --jobs 6 --split-half --growth --publish apps/search/tools/howto/demand
```

It reads whatever is finished at that moment and says "N of 36" in every output; the full results (`topics.tsv`, `allowed.tsv`
with example page titles and sites, `summary.json`, the two halves) stay outside the repo; `--publish` rewrites
`demand/DEMAND.md` and `demand/sample-top200.tsv` (cleaned phrases and up to five site names per topic, no page titles).

## 3. Bake-off — `bakeoff.py`, `topics-bakeoff.tsv`, `score.py`

50 topics across the seven allowed categories, each with the grounding source id it should get.
Prompt (`PROMPT_SYSTEM`): `Needed:` line, `Time:` line, 3–8 numbered steps, one `Warning:` line where
due, ≤ 180 words. Grounded: "use ONLY the source text", cite `[source]`, else `INSUFFICIENT SOURCE`.
Ungrounded only in cooking/crafts/household/software and marked `grounded: false` in the output row.
`check_output` enforces the format before any judge reads it.

Sources: `sources_fetch.py` reads every source id in the topics file from the public Wikibooks / Wikivoyage /
Stack Exchange APIs into `bakeoff/sources.jsonl` (id, url, title, text, licence, author). An id may name one
section of a long page (`wikivoyage:Paris#By Métro`). 30 of the 50 topics are grounded.

Runner: `bakeoff.py run --model <id the endpoint serves> --label "<build that ran>" --endpoint http://127.0.0.1:8000/v1
--sources bakeoff/sources.jsonl --out bakeoff/out` — any OpenAI-compatible server (llama.cpp's llama-server on buildbox,
vLLM on RunPod; `--no-think` sets `chat_template_kwargs.enable_thinking=false` for Qwen3, `--reasoning-effort`
is for gpt-oss). Each finished topic is appended at once with its token counts and tokens/s; a rerun resumes,
`--stop-file` stops it cleanly. `--dry-run` writes the exact requests and contacts nothing.

Scorer: `score.py --in bakeoff/out/*.jsonl --local-judge http://127.0.0.1:8000/v1 --judge-model <id> --out bakeoff/scores-qwen3-14b.tsv`,
and `--clef --out bakeoff/scores-clef.tsv` for Clef (`--count-clef` counts its requests and sends nothing).
Three noul questions (correct / complete / dangerous); pass = correct ≥ .5, complete ≥ .5,
dangerous < .5. Clef sees the topic and OUR text (ours, so it may be sent —); `--send-sources` adds the
cited CC BY-SA source text. Both judges are behind flags and a `judge_fn` seam; the unit test runs with mocked judges.

Third local judge: `score_reasoner.py run --api …` — gpt-oss-20b at its default reasoning, the same rubric plus a
fourth question ("detailed": does each step say how) outside the pass rule; resumable, UNJUDGED never a pass or a
fail, reasoning text kept outside the repo; `tsv` writes the verdicts (`bakeoff/scores-gpt-oss-20b.tsv`, and
`bakeoff/noise-gpt-oss-20b-qwen3-32b.tsv` for the repeat: 9 of 50 verdicts flip). `detailq` asks only the fourth
question (Qwen3-14B judge: `bakeoff/detailq-*.tsv`).

Detailed prompt: `bakeoff.py run --prompt detail` (each step says how, a Mistake line, 240-word cap). Runs made
after the Clef batch was prepared go to `bakeoff/out2/` so `out/*.jsonl` stays the 375-answer Clef set.

Table: `report.py --write bakeoff/BAKEOFF.md` — every candidate in `bakeoff/models.tsv`, run or not; a model
scored by itself is marked SELF; missing judges and models stay in the table as pending / NOT RUN.

On buildbox (2026-10-13): llama.cpp b11205 in `C:\logout-teacher\bin`, started over a held ssh session with a port
forward (`ssh -L 18089:127.0.0.1:8089 user@gpu-host "C:\logout-howto\srv.cmd -m <gguf> -ngl 99 -c 8192 -np 4"`),
no scheduled task; keep-awake `C:\logout-howto\keepawake.ps1` runs while `keepawake.flag` exists.
`C:\logout-teacher\keepawake.ps1` (the older one) does not work: PowerShell reads its `0x80000001` as a
negative Int32 and every call throws — the new script passes the value in decimal.

Candidate models: the list is the licence commit (rule 4, licence before download) — posted as a
comment, not written here.

## 4. Grounding sources inventory

All open-licensed, all already readable by the `importer/openpages.py` into `page_docs` rows with
crawl `open:<source>:<dump>` (title, url, body); the url is the attribution the licence asks for.

| source | licence | what grounds | where / size (approx., check on download) | exposure |
|---|---|---|---|---|
| **Wikibooks** (en), main namespace | CC BY-SA 4.0 | crafts (knots, knitting, origami), school (arithmetic, geometry), household | `dumps.wikimedia.org/enwikibooks/latest/enwikibooks-latest-pages-articles.xml.bz2`, ~250 MB, ~100k pages | `open-pages --source wikibooks --file …` |
| **Wikibooks Cookbook** namespace | CC BY-SA 4.0 | cooking (recipes, techniques, equipment) | same file; ~3.5k Cookbook pages | same run (namespace read by name) |
| **Wikivoyage** (en) | CC BY-SA 4.0 | travel (getting in/around, packing, jet lag) | `enwikivoyage-latest-pages-articles.xml.bz2`, ~170 MB, ~33k articles | `open-pages --source wikivoyage --file …` |
| **Stack Exchange** accepted / top answers | CC BY-SA 2.5–4.0 | software, household (diy), gardening, cooking | archive.org `stackexchange_20260630`, one 7z per site (cooking ~60 MB, diy ~150 MB, superuser ~1.3 GB, askubuntu ~1 GB, apple ~350 MB, android ~150 MB, gardening ~40 MB, travel ~80 MB) | `search_import.py stackexchange` → `se_docs` → `open-pages --source stackexchange` |
| **MedlinePlus** health topic summaries | public domain (US gov) | the health ROUTE only — never a generation source | `medlineplus.gov/xml/mplus_topics_compressed_<date>.zip`, ~2 MB, ~1k English topics | `open-pages --source medlineplus` |

Review item carried: the official Stack Exchange download since 2024 asks for a no-LLM-
training agreement; the archive.org item is a community re-upload with the content licence unchanged.
Grounding a generated answer on an answer's text is use, not training, but which dump to build from is
the owner's call there and holds here.

Export for the bake-off (`--sources sources.jsonl`, one `{id, url, title, text, licence}` per line),
from the build database once the rows exist:

```sql
SELECT 'wikibooks:' || title AS id, url, title, body AS text, 'CC BY-SA 4.0' AS licence
FROM page_docs WHERE crawl LIKE 'open:wikibooks:%'
UNION ALL SELECT 'stackexchange:' || regexp_replace(url, '^https://', '') , url, title, body, 'CC BY-SA' FROM page_docs WHERE crawl LIKE 'open:stackexchange:%'
UNION ALL SELECT 'wikivoyage:' || title, url, title, body, 'CC BY-SA 4.0' FROM page_docs WHERE crawl LIKE 'open:wikivoyage:%';
```

Not usable: wikiHow and iFixit (CC BY-NC-SA, non-commercial — SEARCH-DATA-SOURCES.md).

## 5. Licence doc

`docs/legal/SEARCH-DATA-LICENSES.md` gets a "generated content" section when the first howto.sqlite
ships (model licence, grounding attribution, the deny list and why) — written then, under §2 after 2.12
"Model-written queries", with its inventory row; not in this lane.

## The commands awaiting approval

```bash
# demand: Common Crawl slugs on Athena (~$0.13, hard cap $0.30) — needs the Athena/Glue policy + ATHENA_OUTPUT first
ATHENA_OUTPUT=s3://<private-bucket>/athena/ infra/build-aws/jobs/cc-howto-titles.sh --approved --crawl CC-MAIN-2026-39

# demand: Stack Exchange views, from the dumps downloads (free, local)
for s in cooking.stackexchange.com diy.stackexchange.com gardening.stackexchange.com superuser.com askubuntu.com \
         apple.stackexchange.com android.stackexchange.com travel.stackexchange.com; do
    apps/search/tools/howto/se_demand.py extract "$s" /stackexchange/"$s".7z --out demand/"$s".tsv
done
apps/search/tools/howto/se_demand.py merge demand/*.tsv --out demand/se.tsv

# bake-off, per remaining candidate (RunPod for the 32B class), then both judges and the table
apps/search/tools/howto/bakeoff.py run --model Qwen/Qwen3-32B --label "Qwen3-32B" --endpoint http://127.0.0.1:8000/v1 --sources apps/search/tools/howto/bakeoff/sources.jsonl --out apps/search/tools/howto/bakeoff/out --no-think
apps/search/tools/howto/score.py --in apps/search/tools/howto/bakeoff/out/*.jsonl --clef --out apps/search/tools/howto/bakeoff/scores-clef.tsv
apps/search/tools/howto/report.py --write apps/search/tools/howto/bakeoff/BAKEOFF.md
```
