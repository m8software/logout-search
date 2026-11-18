"""Wikipedia meaning search from the release files, no database (SEARCH_SEM_BACKEND=ivf).

The home engine's low-memory form, chosen by the T3b measurements (docs/internal/audits/
2026-10-01-usearch-spike.md section 6): nomic-embed-text-v1.5 1-bit codes (768 bits = 96 bytes per
article) grouped into 2,600 lists (IVF). A search compares the query's code with the 2,600 list
centres, then compares it exactly (Hamming distance) with every code in the nearest `probes` lists.
On testbox that found 0.935 of the exact top 10 at 200 probes; it slows gradually, not suddenly, when
memory is short, because the codes are read from a memory-mapped file.

numpy (bitwise_count, numpy >= 2.0) picks the lists; the per-list scoring is ivf_kern.c when the
image built it (libivf_kern.so), else numpy, with the same answer. The build side
(apps/search/release/ivf_build.py) writes the folder:

    centroids.u8   L x 96   majority bit of each list
    offsets.i64    L + 1    list l is rows offsets[l] .. offsets[l+1]-1
    codes.u8       N x 96   codes, grouped by list
    ids.i32        N        Wikipedia page_id of each row
    ivf.json       format, counts, the release id

The query encoder is nomic's own fp32 ONNX on the CPU with the rule the stored codes were made with
(search_import.py bit-load; the sem-nomic branch's nomic_pool/bit_code): mean of the real tokens'
vectors, unit length, bit i = component i >= 0, packed first component first.
"""

from __future__ import annotations

import json
import os
import threading

import numpy as np

FORMAT = "logout-sem-ivf/1"
DIM_BYTES = 96
WORDS = DIM_BYTES // 8
NOMIC_PREFIX = "search_query: "
NOMIC_TOKENS = 32


def _load_kern():
    """libivf_kern.so (ivf_kern.c, built in the engine image, 2026-11-04): the per-list scoring in
    one compiled pass, ~5x faster than the numpy loop, same answer. Absent (the installer bundle, a
    dev checkout) or SEARCH_IVF_KERN=0 = the numpy path below."""
    if os.environ.get("SEARCH_IVF_KERN", "1") == "0":
        return None
    import ctypes
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.environ.get("SEARCH_IVF_KERN_LIB", ""), os.path.join(here, "libivf_kern.so")):
        if p and os.path.exists(p):
            try:
                lib = ctypes.CDLL(p)
            except OSError:
                continue
            f = lib.ivf_scan
            P = ctypes.c_void_p
            f.argtypes = [P, P, P, ctypes.c_int, P, ctypes.c_int, ctypes.c_int, P, P]
            f.restype = ctypes.c_int
            return f
    return None


_kern = _load_kern()


def _prefetch_ranges(mm: "np.memmap", starts: list, ends: list) -> None:
    """Hint the OS to read mm[start:end] for each pair (byte offsets into the mapped file)."""
    import sys
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class RANGE(ctypes.Structure):
            _fields_ = [("VirtualAddress", ctypes.c_void_p), ("NumberOfBytes", ctypes.c_size_t)]
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = getattr(k32, "PrefetchVirtualMemory", None)        # Windows 8 and later; absent = no hint
        if fn is None:
            raise OSError("PrefetchVirtualMemory not available")
        fn.argtypes = [wintypes.HANDLE, ctypes.c_size_t, ctypes.POINTER(RANGE), wintypes.ULONG]
        fn.restype = wintypes.BOOL
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        base = mm.ctypes.data
        arr = (RANGE * len(starts))(*[RANGE(base + a, b - a) for a, b in zip(starts, ends)])
        fn(k32.GetCurrentProcess(), len(starts), arr, 0)
        return
    import mmap
    inner = getattr(mm, "_mmap", None)
    if inner is None or not hasattr(inner, "madvise"):
        raise OSError("no madvise on this mapping")
    page = mmap.PAGESIZE
    for a, b in zip(starts, ends):
        a0 = (a // page) * page
        inner.madvise(mmap.MADV_WILLNEED, a0, min(b, len(inner)) - a0)


def _ort_lean(opts) -> None:
    """Memory, measured on testbox with rc3 (2026-10-05): no arena / no memory pattern (the arena grows to
    the largest input and never shrinks; ~60 MB less idle, no slower); SEARCH_ORT_PREPACK=0 also skips
    the repacked weight copies, 827 -> 530 MB for nomic fp32 at 53 -> 76 ms an embedding (a small box)."""
    opts.enable_cpu_mem_arena = False
    opts.enable_mem_pattern = False
    if os.environ.get("SEARCH_ORT_PREPACK", "1") == "0":
        opts.add_session_config_entry("session.disable_prepacking", "1")
    if os.environ.get("SEARCH_ORT_SPIN", "0") != "1":   # as search_api._ort_spin (2026-11-03)
        opts.add_session_config_entry("session.intra_op.allow_spinning", "0")


def code_of(v) -> np.ndarray:
    """768 floats -> the 96-byte code: bit i set when component i >= 0, first component = MSB of byte 0."""
    return np.packbits((np.asarray(v, dtype=np.float32) >= 0).astype(np.uint8))


def pool(h, mask) -> np.ndarray:
    """Mean of the real tokens' vectors (padding masked out), scaled to unit length: how
    nomic-embed-text-v1.5 pools."""
    v = (h * mask[:, None]).sum(0) / max(float(mask.sum()), 1.0)
    return v / np.linalg.norm(v)


class NomicCPU:
    """nomic-embed-text-v1.5 fp32 ONNX on the CPU (the int8 file flips 1 box in 12: not used)."""

    def __init__(self, model_dir: str, threads: int = 2):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        ort.disable_telemetry_events()
        self.tok = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
        self.tok.enable_truncation(max_length=NOMIC_TOKENS)
        opts = ort.SessionOptions()
        _ort_lean(opts)
        opts.intra_op_num_threads = threads
        self.sess = ort.InferenceSession(os.path.join(model_dir, "model.onnx"), opts,
                                         providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}

    def __call__(self, text: str) -> np.ndarray:
        e = self.tok.encode(NOMIC_PREFIX + text)
        ids = np.array([e.ids], dtype=np.int64)
        am = np.array([e.attention_mask], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": am}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        return pool(self.sess.run(None, feed)[0][0], am[0])


class IvfIndex:
    """The release's IVF folder, memory-mapped. Thread-safe: searches only read."""

    def __init__(self, path: str, release: str | None = None):
        meta = json.load(open(os.path.join(path, "ivf.json"), encoding="utf-8"))
        if meta.get("format") != FORMAT:
            raise ValueError(f"{path}: format {meta.get('format')!r}, this engine reads {FORMAT!r}")
        if release and meta.get("release") != release:
            raise ValueError(f"{path}: built for release {meta.get('release')!r}, engine expects {release!r}")
        self.meta = meta
        self.cent = np.fromfile(os.path.join(path, "centroids.u8"), np.uint8).reshape(-1, DIM_BYTES).view(np.uint64)
        self.offs = np.fromfile(os.path.join(path, "offsets.i64"), np.int64)
        n = int(self.offs[-1])
        if len(self.offs) != len(self.cent) + 1:
            raise ValueError(f"{path}: {len(self.cent)} centroids but {len(self.offs) - 1} lists")
        self._codes_map = np.memmap(os.path.join(path, "codes.u8"), np.uint8, "r", shape=(n, DIM_BYTES))
        self.codes = self._codes_map.view(np.uint64)
        self.ids = np.memmap(os.path.join(path, "ids.i32"), np.int32, "r", shape=(n,))
        if n != meta.get("rows"):
            raise ValueError(f"{path}: {n} codes but ivf.json says {meta.get('rows')}")

    def __len__(self) -> int:
        return len(self.ids)

    def prefetch(self, lists: np.ndarray) -> None:
        """Ask the OS to read the code runs of the lists about to be scanned, in a few large merged requests, before the scan faults them in
        4 KB at a time: 200 lists are 50 MB of codes in runs of about 250 KB. Linux: madvise(MADV_WILLNEED) on the mapping; Windows:
        PrefetchVirtualMemory (no read-ahead for mapped pages there, each fault a synchronous read through the antivirus filter). A failure
        turns it off for the process: it is only a hint."""
        if self._prefetch_off or len(lists) == 0:
            return
        try:
            a = self.offs[lists] * DIM_BYTES
            b = self.offs[lists + 1] * DIM_BYTES
            o = np.argsort(a)
            a, b = a[o], b[o]
            keep = a < b
            a, b = a[keep], b[keep]
            if not len(a):
                return
            gap = np.concatenate(([True], a[1:] > np.maximum.accumulate(b)[:-1] + (64 << 10)))      # a run that starts within 64 KB of the last one joins it
            starts = a[gap]
            ends = np.maximum.reduceat(b, np.flatnonzero(gap))
            _prefetch_ranges(self._codes_map, starts.tolist(), ends.tolist())
        except Exception as e:  # noqa: BLE001 - a hint, never an error
            self._prefetch_off = True
            print(f"ivf prefetch off: {type(e).__name__}: {e}", flush=True)

    _prefetch_off = False

    def search(self, code: np.ndarray, k: int = 5, probes: int = 200) -> list[tuple[int, int]]:
        """(page_id, Hamming distance) of the k nearest codes in the `probes` nearest lists, nearest
        first; ties broken by row (the build order), so a search is repeatable."""
        q = np.ascontiguousarray(code, dtype=np.uint8).view(np.uint64)
        dc = np.bitwise_count(self.cent ^ q).sum(1, dtype=np.int32)
        probes = min(probes, len(dc))
        lists = np.argpartition(dc, probes - 1)[:probes] if probes < len(dc) else np.arange(len(dc))
        if _kern is not None and 0 < k <= 4096:
            lists = np.ascontiguousarray(lists, dtype=np.int64)
            self.prefetch(lists)
            out_d = np.empty(k, np.int32)
            out_r = np.empty(k, np.int64)
            n = _kern(self.codes.ctypes.data, self.offs.ctypes.data, lists.ctypes.data, len(lists),
                      q.ctypes.data, WORDS, k, out_d.ctypes.data, out_r.ctypes.data)
            if n >= 0:
                return [(int(self.ids[out_r[i]]), int(out_d[i])) for i in range(n)]
        best_d = np.empty(0, np.int32)
        best_r = np.empty(0, np.int64)
        # contiguous runs of rows, one per list; a run is scored in one vector op
        for li in lists:
            a, b = int(self.offs[li]), int(self.offs[li + 1])
            if a == b:
                continue
            d = np.bitwise_count(self.codes[a:b] ^ q).sum(1, dtype=np.int32)
            if len(d) > k:
                kth = np.partition(d, k - 1)[k - 1]
                part = np.flatnonzero(d <= kth)        # every tie at the cut, so "lowest row wins" holds
            else:
                part = np.arange(len(d))
            best_d = np.concatenate((best_d, d[part]))
            best_r = np.concatenate((best_r, part.astype(np.int64) + a))
            if len(best_d) > 4 * k:
                keep = np.lexsort((best_r, best_d))[:k]
                best_d, best_r = best_d[keep], best_r[keep]
        order = np.lexsort((best_r, best_d))[:k]
        return [(int(self.ids[best_r[i]]), int(best_d[i])) for i in order]


_lock = threading.Lock()
_state: dict = {}


def load(index_dir: str, model_dir: str, release: str | None = None) -> tuple[IvfIndex, NomicCPU]:
    """The index and the encoder, loaded once. Raises when either is missing or mismatched: a box
    configured for IVF must not quietly answer without meaning search."""
    with _lock:
        if "ix" not in _state:
            ix = IvfIndex(index_dir, release)
            enc = NomicCPU(model_dir)
            _state["ix"], _state["enc"] = ix, enc
        return _state["ix"], _state["enc"]


def load_pages(index_dir: str, release: str | None = None) -> IvfIndex:
    """The pages' IVF folder (vectors/pages-ivf*), loaded once; the encoder is load()'s."""
    with _lock:
        if "pages" not in _state:
            _state["pages"] = IvfIndex(index_dir, release)
        return _state["pages"]
