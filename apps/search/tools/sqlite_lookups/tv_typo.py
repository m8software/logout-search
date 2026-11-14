"""The typo-correction keys as a Tantivy index, for the fuzzy-term variant (T4).

  python tv_typo.py DB OUTDIR      one document per distinct suggest key (typo_keys), raw tokenizer
"""
import os, shutil, sqlite3, sys, time
import tantivy

db, out = sys.argv[1], sys.argv[2]
t0 = time.time()
shutil.rmtree(out, ignore_errors=True)
os.makedirs(out)
sb = tantivy.SchemaBuilder()
sb.add_text_field("key", stored=True, tokenizer_name="raw", index_option="basic")
schema = sb.build()
idx = tantivy.Index(schema, path=out)
w = idx.writer(heap_size=300_000_000, num_threads=1)
n = 0
for (key,) in sqlite3.connect(db).execute("SELECT key FROM typo_keys ORDER BY kid"):
    w.add_document(tantivy.Document(key=key))
    n += 1
w.commit()
w.wait_merging_threads()
size = sum(os.path.getsize(os.path.join(out, f)) for f in os.listdir(out))
print(f"tantivy typo index: {n} keys, {size / 1e6:.1f} MB, {time.time() - t0:.0f} s")
