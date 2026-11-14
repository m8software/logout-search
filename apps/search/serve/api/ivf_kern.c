/* The inner loop of sem_ivf.IvfIndex.search in one pass (2026-11-04).
 *
 * Scores every code in the given lists against the query (Hamming distance over `words` 64-bit
 * words) and keeps the k nearest, ordered by (distance, row) — the same answer as the numpy
 * path, ties included. The caller picks the lists. Built in the engine image's builder stage:
 *   cc -O3 -shared -fPIC -o libivf_kern.so ivf_kern.c
 * No library at run time = sem_ivf falls back to numpy.
 */
#include <stdint.h>

typedef struct { int32_t d; int64_t r; } hit;

/* a is "worse" than b: farther, or as far and a later row */
static inline int worse(hit a, hit b) { return a.d > b.d || (a.d == b.d && a.r > b.r); }

static void sift_down(hit *h, int n, int i) {          /* max-heap: the worst kept hit on top */
    for (;;) {
        int l = 2 * i + 1, r = l + 1, m = i;
        if (l < n && worse(h[l], h[m])) m = l;
        if (r < n && worse(h[r], h[m])) m = r;
        if (m == i) return;
        hit t = h[i]; h[i] = h[m]; h[m] = t; i = m;
    }
}
static void sift_up(hit *h, int i) {
    while (i > 0) {
        int p = (i - 1) / 2;
        if (!worse(h[i], h[p])) return;
        hit t = h[i]; h[i] = h[p]; h[p] = t; i = p;
    }
}

#define SCAN_BODY(POP)                                                                  \
    int n = 0;                                                                          \
    for (int li = 0; li < nlists; li++) {                                               \
        int64_t a = offs[lists[li]], b = offs[lists[li] + 1];                           \
        const uint64_t *c = codes + a * words;                                          \
        for (int64_t row = a; row < b; row++, c += words) {                             \
            int32_t d = 0;                                                              \
            for (int w = 0; w < words; w++) d += POP(c[w] ^ q[w]);                      \
            hit x = { d, row };                                                         \
            if (n < k) { heap[n] = x; sift_up(heap, n); n++; }                          \
            else if (worse(heap[0], x)) { heap[0] = x; sift_down(heap, n, 0); }         \
        }                                                                               \
    }                                                                                   \
    return n;

__attribute__((target("popcnt")))
static int scan_popcnt(const uint64_t *codes, const int64_t *offs, const int64_t *lists, int nlists,
                       const uint64_t *q, int words, int k, hit *heap) {
    SCAN_BODY(__builtin_popcountll)
}
static int scan_plain(const uint64_t *codes, const int64_t *offs, const int64_t *lists, int nlists,
                      const uint64_t *q, int words, int k, hit *heap) {
    SCAN_BODY(__builtin_popcountll)
}

/* Returns how many hits were written (<= k), nearest first; -1 on bad arguments. */
int ivf_scan(const uint64_t *codes, const int64_t *offs, const int64_t *lists, int nlists,
             const uint64_t *q, int words, int k, int32_t *out_d, int64_t *out_r) {
    if (k <= 0 || k > 4096 || words <= 0 || nlists < 0) return -1;
    hit heap[4096];
    int n = __builtin_cpu_supports("popcnt")
        ? scan_popcnt(codes, offs, lists, nlists, q, words, k, heap)
        : scan_plain(codes, offs, lists, nlists, q, words, k, heap);
    for (int end = n - 1; end > 0; end--) {             /* heap sort: worst to the back */
        hit t = heap[0]; heap[0] = heap[end]; heap[end] = t;
        sift_down(heap, end, 0);
    }
    for (int i = 0; i < n; i++) { out_d[i] = heap[i].d; out_r[i] = heap[i].r; }
    return n;
}
