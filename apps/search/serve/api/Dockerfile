# The vector search's inner loop in C (ivf_kern.c, 2026-11-04): compiled here, copied in below;
# the final image keeps no compiler. sem_ivf falls back to numpy when the library is missing.
FROM python:3.12-slim AS kern
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY ivf_kern.c /src/
RUN gcc -O3 -Wall -Wextra -Werror -shared -fPIC -o /src/libivf_kern.so /src/ivf_kern.c

FROM python:3.12-slim
WORKDIR /app
# Privacy: onnxruntime telemetry off before the library loads (it otherwise builds a
# device id at import, 2026-09-24); disable_telemetry_events() in search_api.py too.
ENV ORT_DISABLE_TELEMETRY=1
RUN pip install --no-cache-dir "psycopg[binary]==3.3.6" "onnxruntime==1.30.0" "tokenizers==0.23.2" "numpy==2.5.3"
# The query side of the semantic tier: the same model tools/embed.py used for the documents
# (BAAI/bge-small-en-v1.5, MIT), as ONNX, baked in so search-api never downloads at run time.
ADD https://huggingface.co/BAAI/bge-small-en-v1.5/resolve/5c38ec7c405ec4b44b94cc5a9bb96e735b38267a/onnx/model.onnx /models/bge-small-en-v1.5/model.onnx
ADD https://huggingface.co/BAAI/bge-small-en-v1.5/resolve/5c38ec7c405ec4b44b94cc5a9bb96e735b38267a/tokenizer.json /models/bge-small-en-v1.5/tokenizer.json
# Cross-encoder rerankers, Jev-free (2026-10-02; replace jina-turbo-jev — Jev's terms changed
# 10-01). Trained on constructed answers + two-judge agreement (Clef 27B, Qwen3-14B):
#   rerank-turbo-jevfree    jina-reranker-v1-turbo-en (Apache-2.0), int8 ONNX — the CPU
#   rerank-ettin68m-jevfree Ettin-68M (MIT), OpenVINO fp16 IR (static 8x96) for an Intel iGPU,
#                           + its int8 ONNX for the iGPU-vs-CPU agreement check at start
# The files come from Gitea's package registry (login needed), so ./deploy.sh fetch-model puts
# them in the build context first, sha256-checked: models/<name>/. Licences: /app/NOTICE.
COPY models/rerank-turbo-jevfree/ /models/rerank-turbo-jevfree/
COPY models/rerank-ettin68m-jevfree/ /models/rerank-ettin68m-jevfree/
# No web-intent classifier since 2026-10-02: intent-jev was Jev-trained, and the hand rules
# (_SHOP/_FRESH/_TASK) now catch what it caught (docs/internal/search-jev/13).
# Intel iGPU runtime for OpenVINO. Box hardware is undecided, so BOTH driver lines:
#   current NEO 26.35 + IGC 2.41.5  -> Gen12 and newer (Alder Lake-N / N100 class)
#   legacy1 NEO 24.35 + IGC 1.0.17537 -> Gen8-Gen11 (Skylake HD 530 class — testbox)
# Debian's own intel-opencl-icd 22.43 segfaults in libLLVM-14 on Gen9 (2026-09-26).
# Every .deb is pinned by the sha256 Intel publishes (verified 2026-09-27). No /dev/dri at
# run time = these sit unused and search-api reranks on the CPU.
RUN set -eu; apt-get update; apt-get install -y --no-install-recommends ca-certificates curl ocl-icd-libopencl1; \
    cd /tmp; \
    for u in \
      https://github.com/intel/intel-graphics-compiler/releases/download/v2.41.5/intel-igc-core-2_2.41.5+22716_amd64.deb \
      https://github.com/intel/intel-graphics-compiler/releases/download/v2.41.5/intel-igc-opencl-2_2.41.5+22716_amd64.deb \
      https://github.com/intel/compute-runtime/releases/download/26.35.39758.10/intel-opencl-icd_26.35.39758.10-0_amd64.deb \
      https://github.com/intel/compute-runtime/releases/download/26.35.39758.10/libigdgmm12_22.10.0_amd64.deb \
      https://github.com/intel/intel-graphics-compiler/releases/download/igc-1.0.17537.24/intel-igc-core_1.0.17537.24_amd64.deb \
      https://github.com/intel/intel-graphics-compiler/releases/download/igc-1.0.17537.24/intel-igc-opencl_1.0.17537.24_amd64.deb \
      https://github.com/intel/compute-runtime/releases/download/24.35.30872.36/intel-opencl-icd-legacy1_24.35.30872.36_amd64.deb; \
    do curl -fsSLO "$u"; done; \
    printf '%s\n' \
      '0a6e64a663ae65a0fa02d6912ae3b6b37cf85b90c21cc423fd9fef70aaf4f628  intel-igc-core-2_2.41.5+22716_amd64.deb' \
      '779e1b9e88098eb25711e9a8f67c2752665bad22f134aa40ed5649f6e1b87058  intel-igc-opencl-2_2.41.5+22716_amd64.deb' \
      '61712caaddeba3d38e4f79e2a0fb23fea25596ca2d72c3144c6eea2331ec4301  intel-opencl-icd_26.35.39758.10-0_amd64.deb' \
      '6031a63d6e8a12ce61c14efc15f2c8e727061286e3820b8594e6d00615e04d54  libigdgmm12_22.10.0_amd64.deb' \
      'c1e1ecdfe2064c047c552651cfdcdafc504f2033afafba65654338b880048b67  intel-igc-core_1.0.17537.24_amd64.deb' \
      'dd016400f87fa2b6a9fa9fbcca7eb4a2629174a29de679709f9bec5cede88b0e  intel-igc-opencl_1.0.17537.24_amd64.deb' \
      'bbe71e4f414259e06a10cde72c29a2bd78d41b2bb2f6f8463b1806797fe66e85  intel-opencl-icd-legacy1_24.35.30872.36_amd64.deb' \
      | sha256sum -c; \
    dpkg -i ./*.deb; rm -f ./*.deb; apt-get purge -y curl; apt-get autoremove -y; rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "openvino==2026.4.0"
# Tantivy (MIT) for the Wikipedia text list when SEARCH_WIKI_BACKEND=tantivy (ADR-063, phase 5),
# and for typo correction when SEARCH_LOOKUP_BACKEND=sqlite (T4). Imported only then.
RUN pip install --no-cache-dir "tantivy==0.26.2"
# ADD --chmod would give the folder 644 too (no x: unreadable) — set modes afterwards.
RUN chmod -R a+rX /models
# one malloc arena pair, not one per thread: anonymous memory after 3,060 queries 2.67 -> 2.12 GB (rc3, testbox 2026-10-05)
ENV MALLOC_ARENA_MAX=2
COPY search_api.py site_pages.py lookups_sqlite.py lookups_release.py kinds_release.py tv_open.py release_root.py sem_ivf.py gov_state.py state_anchor.py result_store.py compute_cache.py provenance.py parts.py stage_version.py stage_assembly.py stage_local.py stage_verdict.py stage_place.py stage_list.py stage_card.py stage_base.py stage_parts.py site_slots.py facts.py news_sqlite.py instant_answers.py product_table.py ranker.json shop_stores.json warm.py native_host.py NOTICE ./
COPY licenses/ /app/licenses/
COPY --from=kern /src/libivf_kern.so /app/libivf_kern.so
USER 1000:1000
EXPOSE 8090
CMD ["python", "/app/search_api.py"]
