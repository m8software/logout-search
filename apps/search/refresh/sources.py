"""Every data source of the search build database on indexhost, with its freshness budget and its
refresh job. health.py checks these; scheduler.py runs the `job` entries.

Fields
  tables     tables in the build database `search` that hold it (the first is the one probed for
             last_write; rows/bytes are summed).
  as_of      how to read the data's own vintage, if it carries one ("wiki_dump"); else
             the budget is measured from last_write (see freshness.py).
  budget_d   older than this = stale. Monthly sources: 45 d (a month + two weeks to notice and fix).
             Quarterly: 120 d. Hand-made sources that have no refresh yet: 180 d, so they show
             in the status JSON without paging anyone before a refresh exists.
  min_rows   below this the source counts as dropped (about half of the 2026-10-01 count).
  origin     where the data comes from; cadence = how often the origin publishes.
  unattended whether today's loader could run with nobody watching, and why not.
  job        what scheduler.py would run (importer argv lists; {prev_month} = last month, YYYY-MM).
             ready=False: not wired yet (multi-step, downloads, GPU); approval: the user has to
             approve it separately because it costs money or risks a block of the home IP.
"""

D = 86400

# {prev_month} is filled by scheduler.py.
SOURCES = [
    # --- Wikipedia and Wikidata (monthly) ----------------------------------------------------
    {"name": "wikipedia", "tables": ["wiki_docs"], "as_of": "wiki_dump", "budget_d": 50, "min_rows": 6_000_000,
     "origin": "dumps.wikimedia.org enwiki pages-articles (1st and 20th of each month)", "cadence": "monthly",
     "unattended": "no: 27 GB download (wikipedia/wikidl.py), `wiki` parse, `wiki-load` (BM25 build needs the 20g container)",
     "served": True,
     "job": {"cadence_d": 30, "heavy": True, "ready": False, "approval": None, "steps": []}},
    {"name": "wiki_vectors", "tables": ["wiki_vec"], "budget_d": 60, "min_rows": 6_000_000,
     "origin": "tools/embed.py over the Wikipedia articles (GPU)", "cadence": "after each Wikipedia load",
     "unattended": "no: GPU embedding run, then `vec-load` (HNSW build, 12g shm)", "served": True,
     "job": {"cadence_d": 30, "heavy": True, "ready": False, "approval": None, "steps": []}},
    {"name": "wiki_sites", "tables": ["wiki_sites"], "budget_d": 50, "min_rows": 500_000,
     "origin": "official websites from the Wikipedia dump + Wikidata (QLever)", "cadence": "monthly",
     "unattended": "after the dump download only (`wiki-sites --file <dump>`, then `wikidata-sites`)", "served": True,
     "job": {"cadence_d": 30, "heavy": True, "ready": False, "approval": None, "steps": []}},
    {"name": "wiki_pop", "tables": ["wiki_pop"], "budget_d": 45, "min_rows": 3_000_000,
     "origin": "Wikidata sitelinks (qlever.dev) + dumps.wikimedia.org pageview_complete monthly", "cadence": "monthly",
     "unattended": "yes (two downloads, new-table-then-swap)", "served": True,
     "job": {"cadence_d": 30, "heavy": True, "ready": True, "approval": None,
             "steps": [["wiki-pop"], ["pageviews", "--month", "{prev_month}"]]}},
    {"name": "clickstream", "tables": ["wiki_search_refs", "wiki_next"], "budget_d": 45, "min_rows": 2_000_000,
     "origin": "dumps.wikimedia.org clickstream-enwiki (published early the next month)", "cadence": "monthly",
     "unattended": "yes", "served": True,
     "job": {"cadence_d": 30, "heavy": True, "ready": True, "approval": None,
             "steps": [["clickstream", "--month", "{prev_month}"]]}},
    {"name": "entities", "tables": ["entities"], "budget_d": 45, "min_rows": 300_000,
     "origin": "Wikidata via qlever.dev (429/500 retried per category)", "cadence": "monthly",
     "unattended": "yes, but a refused category exits 0 with FAILED in the log (the ledger marks it partial)",
     "served": False,
     "job": {"cadence_d": 30, "heavy": True, "ready": True, "approval": None, "steps": [["entities"]]}},
    {"name": "facts", "tables": ["wiki_facts"], "budget_d": 45, "min_rows": 4_000_000,
     "origin": "Wikidata via qlever.dev", "cadence": "monthly",
     "unattended": "yes, same partial-failure caveat as entities", "served": True,
     "job": {"cadence_d": 30, "heavy": True, "ready": True, "approval": None, "steps": [["facts"]]}},
    {"name": "wiki_links", "tables": ["wiki_links"], "budget_d": 45, "min_rows": 5_000_000,
     "origin": "Wikidata via qlever.dev (accounts, official sites, public-office IDs); Project Gutenberg's offline "
               "catalog feed for books Wikidata has no ebook ID for", "cadence": "monthly",
     "unattended": "yes", "served": True,
     "job": {"cadence_d": 30, "heavy": True, "ready": True, "approval": None,
             "steps": [["wiki-links"], ["gutenberg-catalog"]]}},
    {"name": "youtube", "tables": ["youtube_channels"], "budget_d": 45, "min_rows": 70_000,
     "origin": "Wikidata P2397 via qlever.dev", "cadence": "monthly",
     "unattended": "yes", "served": True,
     "job": {"cadence_d": 30, "heavy": True, "ready": True, "approval": None, "steps": [["youtube"]]}},
    # --- sites ---------------------------------------------------------------------------------
    {"name": "blocklists", "tables": ["sites"], "budget_d": 45, "min_rows": 1,   # floor: not empty (the old 500,000 was the Tranco list)
     "origin": "hagezi/nocoin/WindowsSpy blocklists (sites that are never indexed)", "cadence": "monthly",
     "unattended": "yes", "served": True,
     "job": {"cadence_d": 30, "heavy": True, "ready": True, "approval": None,
             "steps": [["blocklists"]]}},
    {"name": "query_list", "tables": ["query_list"], "budget_d": 45, "min_rows": 4_000_000,
     "origin": "derived: entities, clickstream, AmazonQAC (import/amazonqac-terms.tsv.gz)", "cadence": "after its inputs",
     "unattended": "yes (local inputs only)", "served": False,
     "job": {"cadence_d": 30, "heavy": True, "ready": True, "approval": None,
             "steps": [["query-list", "--qac", "/import/amazonqac-terms.tsv.gz"]]}},
    {"name": "stocks", "tables": ["stock_tickers", "stock_keys"], "budget_d": 45, "min_rows": 20_000,
     "origin": "SEC ticker list (sec-cik-mapper mappings.csv, downloaded by hand) + Wikidata", "cadence": "monthly",
     "unattended": "no: the mappings file is a manual download", "served": True,
     "job": {"cadence_d": 30, "heavy": False, "ready": False, "approval": None, "steps": []}},
    # --- Common Crawl and AWS (quarterly) ----------------------------------------------------
    {"name": "homepages", "tables": ["docs", "serve_sites"], "budget_d": 120, "min_rows": 150_000,
     "origin": "Common Crawl URL index scan + range fetch (ccscan/ccfetch)", "cadence": "quarterly",
     "unattended": "no: ~67 GB per crawl from the home IP; Common Crawl asks < 10 req/s and has 403-blocked us",
     "served": True,
     "job": {"cadence_d": 90, "heavy": True, "ready": False,
             "approval": "home-IP traffic to Common Crawl (rate limits, a 403 block once)", "steps": []}},
    {"name": "pages", "tables": ["page_docs"], "budget_d": 120, "min_rows": 150_000,
     "origin": "Common Crawl pages inside the top sites (ccpages + ccfetch --pages)", "cadence": "quarterly",
     "unattended": "no: as homepages", "served": True,
     "job": {"cadence_d": 90, "heavy": True, "ready": False,
             "approval": "home-IP traffic to Common Crawl", "steps": []}},
    {"name": "anchors", "tables": ["anchors", "anchor_docs", "site_meta", "site_inlinks"], "budget_d": 120,
     "min_rows": 1_000_000,
     "origin": "AWS job infra/build-aws/jobs/cc-anchors.sh over Common Crawl WAT files", "cadence": "quarterly",
     "unattended": "no: AWS instances cost money (~$2.50 for the 12% sample)", "served": True,
     "job": {"cadence_d": 90, "heavy": True, "ready": False, "approval": "AWS spend", "steps": []}},
    {"name": "places", "tables": ["places"], "budget_d": 120, "min_rows": 2_000_000,
     "origin": "Geofabrik OSM extracts ($OSM_EXTRACT, daily) + Web Data Commons (AWS job, yearly)", "cadence": "quarterly",
     "unattended": "OSM part yes (`places` keeps the WDC rows); WDC part needs AWS",
     "served": True,
     "job": {"cadence_d": 90, "heavy": True, "ready": True, "approval": None,
             "steps": [["places"]]}},
    {"name": "subreddits", "tables": ["subreddits"], "budget_d": 180, "min_rows": 300_000,
     "origin": "AWS job infra/build-aws/jobs/reddit-subs.sh", "cadence": "yearly",
     "unattended": "no: AWS spend; personal box only (never shipped)", "served": True,
     "job": {"cadence_d": 365, "heavy": False, "ready": False, "approval": "AWS spend", "steps": []}},
    # --- legacy curated sources: RETIRED — no job may ever run them, and they never alert. ------------------
    {"name": "kinds", "tables": ["kind_docs", "kind_members", "kind_keys", "kind_vecs", "kind_site_vec"],
     "budget_d": 180, "min_rows": 200_000,
     "origin": "Wikidata/Wikipedia categories/DMOZ/awesome lists (rounds by hand)", "cadence": "by hand",
     "unattended": "no: retired (ADR-065)", "served": True, "retired": "ADR-065: legacy source, not built any more",
     "job": {"cadence_d": 180, "heavy": True, "ready": False, "approval": "model spend", "steps": []}},
    {"name": "site_quality", "tables": ["site_quality"], "budget_d": 180, "min_rows": 100_000,
     "origin": "legacy homepage scores + UT1 lists", "cadence": "by hand",
     "unattended": "no: retired (ADR-065)", "served": True, "retired": "ADR-065: legacy source, not built any more",
     "job": {"cadence_d": 180, "heavy": False, "ready": False, "approval": "model spend", "steps": []}},
    {"name": "d2q", "tables": ["d2q_docs"], "budget_d": 180, "min_rows": 5_000,
     "origin": "model-written queries per site (d2q.json)", "cadence": "by hand",
     "unattended": "no: retired (ADR-065)", "served": True, "retired": "ADR-065: legacy source, not built any more",
     "job": {"cadence_d": 180, "heavy": False, "ready": False, "approval": "model spend", "steps": []}},
    {"name": "card_images", "tables": ["card_images"], "budget_d": 180, "min_rows": 100_000,
     "origin": "Wikidata/Commons pictures, free licences only (rate-limited fetch)", "cadence": "by hand",
     "unattended": "no: long Commons fetch from the home IP", "served": True,
     "job": {"cadence_d": 180, "heavy": False, "ready": False,
             "approval": "home-IP traffic to Wikimedia Commons", "steps": []}},
    # --- built by serve-refresh ----------------------------------------------------------------
    {"name": "suggest", "tables": ["suggest_items", "suggest_keys", "suggest_top", "suggest_phrases"],
     "budget_d": 45, "min_rows": 1_000_000,
     "origin": "serve/suggest.sql + phrases.sql (inside deploy.sh serve-refresh)", "cadence": "with every serve-refresh",
     "unattended": "yes once staged (`deploy.sh refresh-stage` writes refresh/jobs/serve-refresh.sh); swaps the LIVE serve",
     "served": True,
     # Runs only when served build data is newer than serve (status.json serve.behind_build).
     "job": {"cadence_d": 1, "heavy": True, "ready": True, "approval": None, "when": "inputs_changed",
             "steps": [["@serve-refresh"]]}},
]

# The live pollers (news database) — their budgets are in minutes, see health.py.
LIVE = ["news", "sports", "weather", "wxall"]

# Containers whose presence means a heavy job is running (the scheduler refuses to start then).
# Other sessions' work does not follow the search-job-* naming (search-g1build, search-bitbench …).
# A matching container counts only while it uses CPU (freshness.busy_from_stats).
HEAVY_CONTAINER_PATTERNS = (r"^search-job-", r"build", r"bench", r"^search-boxbuild", r"merge", r"^wiki")

for _s in SOURCES:
    _s["budget_s"] = _s["budget_d"] * D
