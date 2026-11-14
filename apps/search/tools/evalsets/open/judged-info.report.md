## judged-info — build `8f651b68abd0`, judged 2026-10-14, Clef 27B (`@cf/cloudflare/clef`), top-5 titles+URLs only

Engine: http://127.0.0.1:18484. Judge spend: 4984 judged | 1032 requests, 3952 cached | 274,035 input tokens | $0.0658 | 86.8s. Ledger: 2026-10-14 UTC: 1588 calls, 5593/9000 neurons used, 3407 left.

| template | in set | n judged | with results | P(top-1 right) | top-1 ≥ 0.7 | any of top-5 ≥ 0.7 | mean max | pairs judged |
|---|---|---|---|---|---|---|---|---|
| capital | 90 | 90 | 90 | 0.850 | 86.7% | 87.8% | 0.873 | 450 |
| control | 40 | 40 | 40 | 0.812 | 75.0% | 92.5% | 0.904 | 200 |
| descriptive | 258 | 258 | 258 | 0.651 | 51.9% | 86.0% | 0.818 | 1286 |
| directed | 90 | 90 | 90 | 0.905 | 96.7% | 97.8% | 0.922 | 449 |
| does | 90 | 90 | 90 | 0.836 | 81.1% | 97.8% | 0.920 | 450 |
| founded | 90 | 90 | 90 | 0.842 | 84.4% | 95.6% | 0.921 | 450 |
| gov | 70 | 70 | 70 | 0.406 | 22.9% | 30.0% | 0.533 | 350 |
| how-does | 90 | 90 | 90 | 0.892 | 90.0% | 96.7% | 0.919 | 450 |
| what-is | 90 | 90 | 90 | 0.864 | 86.7% | 91.1% | 0.907 | 449 |
| wrote | 90 | 90 | 90 | 0.838 | 85.6% | 91.1% | 0.886 | 450 |
| all | 998 | 998 | 998 | 0.773 | 73.1% | 87.4% | 0.857 | 4984 |

n judged = queries whose every top-5 result has a verdict; a query with an unjudged result is left out, so a row with n judged below in set is PARTIAL and `all` is not the whole set's number.

P(top-1 right) is the mean Clef probability of the first result (0 when the list is empty); the two percentage columns count a query when a result scores at least 0.7 (search-answer-eval's threshold).
