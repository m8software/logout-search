# indexhost's nomic embedder for the Wikipedia refresh (container names wiki-embed-*), 2026-11-16: the scorer image's torch 2.4.1 (CUDA 12.1)
# plus sentence-transformers. Measured on the RTX 3060 Ti: gate vs the live rc15 codes 2,951 of 2,954 identical (max 1 differing bit);
# 1,942,585 rows in 3,935 s = 494 rows/s (the daily job's number); against the RunPod A4000 (594 rows/s) 98.8 % of rows bit-identical,
# mean 0.0137 differing bits, max 5.
#   docker build -t wiki-embed:1 apps/search/release/embed-image
#   docker run --rm --name wiki-embed-NAME --gpus all -e HF_HOME=/hf -v <wiki_embed_pod.py>:/w/wiki_embed_pod.py:ro \
#       -v IN:/in:ro -v OUT:/out -v HF:/hf wiki-embed:1 /in /out        (IN holds texts.tsv.gz, sample-texts.tsv.gz, sample-codes.json)
FROM logout/scorer:1
USER root
RUN pip install --no-cache-dir "sentence-transformers==5.1.1" einops numpy
WORKDIR /w
ENTRYPOINT ["python3", "/w/wiki_embed_pod.py"]
