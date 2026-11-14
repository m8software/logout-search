---
pretty_name: logout search data
license: other
license_name: logout-search-data-mixed
license_link: LICENSE
language:
- en
tags:
- search
- information-retrieval
- offline
- wikipedia
- openstreetmap
task_categories:
- text-retrieval
viewer: false
---

# logout search data

The data for **logout search**, a web search engine that runs on your own computer. Searches are answered from these
files and sent nowhere. The engine is open source (AGPL-3.0). Site: https://search.logout.cloud/

<!-- lead -->

## What is in it

<!-- parts -->

Websites are kept as an address, a title, a description cut to 160 characters and an index of words; there is no page
text. Pages that asked not to be indexed are left out, and pages that asked for no snippet show no description. The
site categories and the reranker's training labels were made with help from Clef and Qwen3, open-weights models (Apache-2.0).
No one's searches or browsing history is in any file.

## Licences

There is no single licence. [LICENSE](LICENSE) names each file's licence; the texts and credits are in
[LICENSES/](LICENSES/). Website parts are also subject to [Common Crawl's Terms of Use](https://commoncrawl.org/terms-of-use).
Site owners: to have your description removed, write to **help@logout.cloud**.
