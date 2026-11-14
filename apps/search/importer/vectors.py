"""Load embeddings made on a GPU box (tools/embed.py) into pgvector.

    search_import.py vec-load --table wiki_vec --ids /import/wiki.ids.npy --vecs /import/wiki.vecs.npy

-> TABLE (id bigint PRIMARY KEY, v halfvec(384)) + an HNSW cosine index, built into
TABLE_new and swapped in. halfvec: half the size of vector, the same recall for
normalised 384-d embeddings.
"""

from __future__ import annotations

import os
import re
import time

import numpy as np

DIM = 384
_NAME = re.compile(r"^[a-z_]+_vec$")


def log(msg: str) -> None:
    print(f"[vectors] {msg}", flush=True)


def check(table: str, ids, vecs) -> None:
    if not _NAME.match(table):
        raise SystemExit(f"table must look like <name>_vec, not {table!r}")
    if vecs.ndim != 2 or vecs.shape[1] != DIM:
        raise SystemExit(f"vectors must be N x {DIM}, got {vecs.shape}")
    if len(ids) != len(vecs):
        raise SystemExit(f"{len(ids)} ids for {len(vecs)} vectors")
    if len(np.unique(ids)) != len(ids):
        raise SystemExit("duplicate ids")


def cmd_vec_load(conn, table: str, ids_path: str, vecs_path: str) -> None:
    from pgvector import HalfVector              # here, not at import: check() is testable without psycopg
    from pgvector.psycopg import register_vector
    t0 = time.time()
    ids = np.load(ids_path)
    vecs = np.load(vecs_path, mmap_mode="r")
    check(table, ids, vecs)
    register_vector(conn)
    new = table + "_new"
    conn.execute(f"DROP TABLE IF EXISTS {new}")
    conn.execute(f"CREATE TABLE {new} (id bigint PRIMARY KEY, v halfvec({DIM}))")
    with conn.transaction(), conn.cursor().copy(f"COPY {new} (id, v) FROM STDIN WITH (FORMAT BINARY)") as cp:
        cp.set_types(["int8", "halfvec"])
        for n, (i, v) in enumerate(zip(ids, vecs)):
            cp.write_row((int(i), HalfVector(np.asarray(v, dtype=np.float32))))
            if n and n % 1_000_000 == 0:
                log(f"{n:,} rows")
    log(f"{len(ids):,} vectors loaded in {(time.time() - t0) / 60:.1f} min; building HNSW")
    # The graph must fit in maintenance_work_mem or pgvector finishes it on disk, which
    # crawled at ~80 rows/s past 4.5M of 6.79M Wikipedia vectors with 6GB (2026-09-24).
    # 6.79M x halfvec(384) needs ~8GB; the DSM segment of a parallel build is this size,
    # so search-postgres needs shm_size and mem_limit above it (docker-compose.yml).
    conn.execute(f"SET maintenance_work_mem = '{os.environ.get('VEC_BUILD_MEM', '10GB')}'")
    conn.execute(f"SET max_parallel_maintenance_workers = {int(os.environ.get('VEC_BUILD_WORKERS', '6'))}")
    conn.execute(f"CREATE INDEX {new}_hnsw ON {new} USING hnsw (v halfvec_cosine_ops) WITH (m = 16, ef_construction = 64)")
    with conn.transaction():
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute(f"ALTER TABLE {new} RENAME TO {table}")
        conn.execute(f"ALTER INDEX {new}_hnsw RENAME TO {table}_hnsw")
        conn.execute(f"ALTER INDEX {new}_pkey RENAME TO {table}_pkey")
    log(f"{table} ready: {len(ids):,} vectors in {(time.time() - t0) / 60:.1f} min")
