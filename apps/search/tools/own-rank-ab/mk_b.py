"""Build arm-B signals.bin from the rc2-acc one: own rank in place of the Tranco fields (eval only)."""
import sys, json, struct, sqlite3, numpy as np
sys.path.insert(0, ".")
from own_rank import official_key
from urllib.parse import urlsplit
src, reg_path, own_tsv, wsites_tsv, out = sys.argv[1:6]
b = open(src, 'rb').read(12); n = struct.unpack('<I', b[8:12])[0]
head = json.loads(open(src, 'rb').read(12 + n)[12:]); base = 12 + n
arr = {f['name']: np.fromfile(src, dtype=f['dtype'], count=f['count'], offset=base + f['offset']) for f in head['fields']}
own = {}
for l in open(own_tsv):
    h, d, r, cc, bs = l.rstrip('\n').split('\t'); own[h] = int(r)
db = sqlite3.connect(f"file:{reg_path}?mode=ro", uri=True)
mask = (1 << 28) - 1
tr = np.zeros_like(arr['site.tranco_rank']); nav = np.zeros_like(arr['site.nav_rank'])
for did, host in db.execute("SELECT doc_id, path FROM registry WHERE doc_id >> 28 = 2"):
    r = own.get(host, 0)
    tr[did & mask] = r; nav[did & mask] = r
page = {}
V3 = len(open(wsites_tsv).readline().rstrip('\n').split('\t')) == 4      # page_id domain box url (recipe v3), else page_id domain tranco
for l in open(wsites_tsv):
    p = l.rstrip('\n').split('\t'); page[int(p[0])] = (p[1], p[3]) if V3 else (p[1], None)
sr = np.zeros_like(arr['wiki.site_rank'])
for did, ext in db.execute("SELECT doc_id, ext FROM registry WHERE doc_id >> 28 = 1"):
    d = page.get(ext)
    if d:
        sr[did & mask] = own.get(official_key(d[1], d[0]), 0) if V3 else own.get(d[0], 0)
ln = np.zeros_like(arr['link.nav_rank'])
for did, url in db.execute("SELECT r.doc_id, coalesce(h.origin || r.path, r.alt) FROM registry r LEFT JOIN hosts h ON h.id = r.host_id WHERE r.doc_id >> 28 = 8"):
    ln[did & mask] = own.get((urlsplit(url).hostname or ''), 0)
SRC = "own_rank.rank (0 = none): reciprocal-rank fusion (K=60) of the Common Crawl web-graph rank, the pageviews and sitelinks of the article whose official site it is, and the sites linking to it (importer/own_rank.py)"
LIC = "Common Crawl web graph (CC ToU) + CC0 (Wikimedia pageviews, Wikidata); M8 layer (CC0)"
fields, arrays, off = [], [], 0
for f in head['fields']:
    name = f['name']; a = arr[name]
    if name == 'site.tranco_rank': name, a, f = 'site.own_rank', tr, dict(f, source=SRC, licence=LIC, note='')
    elif name == 'site.nav_rank': a, f = nav, dict(f, source='nav_sites_own.rank, min per host (0 = none)', licence=LIC, note='')
    elif name == 'wiki.site_rank': a, f = sr, dict(f, source="own_rank of the article's official site (wiki_sites.domain; 0 = none)", licence=LIC, note='')
    elif name == 'link.nav_rank': a, f = ln, dict(f, source="host_nav_own.rank (own_rank of the url's host), min per url (0 = none)", licence=LIC, note='')
    f = dict(f, name=name, offset=off); fields.append(f); arrays.append(a); off += a.nbytes
head2 = dict(head, fields=fields)
h = json.dumps(head2, sort_keys=True).encode()
with open(out, 'wb') as o:
    o.write(b'LOSIGNL1' + struct.pack('<I', len(h)) + h)
    for a in arrays: o.write(a.tobytes())
for k, a in (('site.own_rank', tr), ('site.nav_rank', nav), ('wiki.site_rank', sr), ('link.nav_rank', ln)):
    print(k, 'nonzero', int((a > 0).sum()), 'of', len(a))
