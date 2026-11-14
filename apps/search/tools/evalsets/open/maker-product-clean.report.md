# maker-product-clean: built from maker-product.tsv (400 rows) and release rc12-release

rows out: 169 (dev 90, test 79); the old set: dev 211, test 189

| what | rows |
|---|---|
| not the dominant meaning of its key (products hold under 50% of the key's article views) | 120 |
| article not found under the query's key in the release | 92 |
| gold site is web.archive.org | 11 |
| discontinued (dissolved / end-time date in the release's facts) | 5 |
| (not dropped) rows merged into another row of the same query | 3 |

Dominance share of each surviving query group (products' pageviews / all articles under the key):
- groups: 279; at or over 0.3: 185; 0.5: 169; 0.7: 151; 0.9: 131
