//! Release-format Tantivy builder and bench (SEARCH-RELEASE-FORMAT.md rules 3, 5, 7).
//!
//!   tantivy-structure build OUT --stop english.stop [--kind wiki|sites|pages|anchors] [--schema none|fastonly] [--chunk N]
//!                           [--heap-mb M] [--jobs J] [--split-at N --cold-out COLD]  < export.tsv
//!   (all commands: --analyzer en_pg|en_pg2, default en_pg; build also --groups G --keep-parts;
//!    regroup PARTS|INDEX OUT --groups G --stop F re-merges a --keep-parts build, or the segments of an index in meta.json order, into G segments)
//!   tantivy-structure bench --index DIR [--cold COLD] --stop F --queries F --out OUT.tsv
//!                           [--mode full|hotcold] [--stats global|local] [--rule always|hot|alpha:X]
//!                           [--repeats R] [--no-evict] [--dump DUMP.jsonl] [--py-like]
//!   --py-like: collect through Count + TopDocs in one MultiCollector, as tantivy-py's search() does (no WAND)
//!   tantivy-structure dfs   --index DIR --stop F --queries F                 term -> doc_freq
//!   tantivy-structure space --index DIR                                    bytes per field
//!   tantivy-structure merge --index DIR [--below N]                        one writer merge of the segments (deletes applied)
//!
//! build reads `COPY wiki_docs (page_id, title, aliases, infobox, headings, lead)` text lines
//! on stdin, already in the order the index should have (pop_order.py), and writes:
//!   - doc_id: u64, fast (+ indexed unless --schema fastonly, so monthly deletes work by term);
//!   - title: text with positions, not stored;  body: title+aliases+infobox+headings+lead, freqs.
//! --kind picks the index and its input columns (tab-separated, COPY text escapes, doc_id first):
//!   wiki (default)  doc_id title aliases infobox headings lead   -> title (positions) + body (all joined)
//!   sites           doc_id head body        -> head (freqs) + body (freqs): what a site is called / all its text
//!   pages           doc_id body             -> body (freqs)
//!   anchors         doc_id body             -> body (freqs): what other sites call the site (doc_id = the site's)
//! Every kind has the same u64 fast `doc_id` and stores nothing else.
//! Nothing displayable is stored (rule 3). The input is cut into --chunk-doc chunks; chunk c goes
//! to worker c % J, and each worker indexes into a private index with ONE indexing thread, no
//! merge policy and a commit per chunk, so every segment is a fixed slice of the input. Then ONE
//! merge of all segments in input order: doc ids follow the input order (rule 5; tantivy 0.26 has
//! no index sorting) and the bytes depend only on the input and --chunk/--heap-mb, not on J or
//! thread timing (rule 7), except the random segment UUID, which canon.py replaces.
//!
//! bench evicts the index files from the page cache (posix_fadvise DONTNEED) before each
//! repeat, opens the index, runs every query once ("first") and once more ("warm").
//! hotcold searches the hot index, then the cold one unless the rule says hot is enough:
//!   always   search cold every time;  hot   never search cold;
//!   alpha:X  skip cold when hot has 10 hits and hot's 10th score >= X * (sum over the query's
//!            terms of the best block-max score that term reaches in cold). X = 1 is exact: cold
//!            cannot place a document above hot's 10th.
//! --stats global scores both halves with the statistics of hot+cold together (identical scores
//! to one index); local scores each half with its own statistics (all tantivy-py can do).
use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::os::unix::io::AsRawFd;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{sync_channel, Receiver};
use std::time::Instant;

use tantivy::collector::TopDocs;
use tantivy::index::SegmentId;
use tantivy::indexer::NoMergePolicy;
use tantivy::query::{
    Bm25StatisticsProvider, Bm25Weight, BooleanQuery, Occur, Query, TermQuery,
};
use tantivy::schema::{
    Field, IndexRecordOption, NumericOptions, Schema, TextFieldIndexing, TextOptions,
};
use tantivy::tokenizer::{
    Language, LowerCaser, SimpleTokenizer, Stemmer, StopWordFilter, TextAnalyzer,
};
use tantivy::{
    DocAddress, Index, IndexSettings, Score, Searcher, TantivyDocument, Term,
};

/// en_pg: simple tokenizer. en_pg2: keeps dotted / @ tokens whole (golf.nl, fable5.1); the same
/// regex as search_api.PG_TOKEN_RE. The name is stored in the index schema, so a reader that
/// opens it must register the same analyzer under the same name.
const PG_TOKEN_RE: &str = r"[^\W_]+(?:[.@][^\W_]+)+|[^\W_]+";
static ANALYZER_NAME: std::sync::OnceLock<String> = std::sync::OnceLock::new();
fn an_name() -> &'static str {
    ANALYZER_NAME.get().map(|s| s.as_str()).unwrap_or("en_pg")
}

static NOIDS: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
static MULTI: std::sync::OnceLock<bool> = std::sync::OnceLock::new();

fn die(msg: impl AsRef<str>) -> ! {
    eprintln!("tantivy-structure: {}", msg.as_ref());
    std::process::exit(2)
}

struct Args(Vec<String>);
impl Args {
    fn get(&self, k: &str) -> Option<String> {
        self.0.iter().position(|a| a == k).and_then(|i| self.0.get(i + 1).cloned())
    }
    fn req(&self, k: &str) -> String {
        self.get(k).unwrap_or_else(|| die(format!("missing {k}")))
    }
    fn has(&self, k: &str) -> bool {
        self.0.iter().any(|a| a == k)
    }
}

fn analyzer(stopfile: &str) -> TextAnalyzer {
    let words: Vec<String> = std::fs::read_to_string(stopfile)
        .unwrap_or_else(|e| die(format!("read {stopfile}: {e}")))
        .lines()
        .map(|l| l.trim().to_string())
        .filter(|l| !l.is_empty())
        .collect();
    if an_name() == "en_pg2" {
        let tok = tantivy::tokenizer::RegexTokenizer::new(PG_TOKEN_RE).unwrap_or_else(|e| die(format!("regex: {e}")));
        return TextAnalyzer::builder(tok)
            .filter(LowerCaser)
            .filter(StopWordFilter::remove(words))
            .filter(Stemmer::new(Language::English))
            .build();
    }
    TextAnalyzer::builder(SimpleTokenizer::default())
        .filter(LowerCaser)
        .filter(StopWordFilter::remove(words))
        .filter(Stemmer::new(Language::English))
        .build()
}

/// The text fields of an index kind, in input-column order after doc_id: (name, positions?).
/// wiki is special-cased by its builder (six input columns, body is the join of five).
fn text_fields(web: &str) -> Vec<(&'static str, bool)> {
    match web {
        "wiki" => vec![("title", true), ("body", false)],
        "sites" => vec![("head", false), ("body", false)],
        "pages" | "anchors" => vec![("body", false)],
        _ => die(format!("--kind is wiki, sites, pages or anchors, not {web}")),
    }
}

fn schema(kind: &str, web: &str) -> Schema {
    let mut sb = Schema::builder();
    let id = NumericOptions::default().set_fast();
    let id = if kind == "fastonly" { id } else { id.set_indexed() };
    sb.add_u64_field("doc_id", id);
    for (name, positions) in text_fields(web) {
        let opt = TextOptions::default().set_indexing_options(
            TextFieldIndexing::default()
                .set_tokenizer(an_name())
                .set_index_option(if positions { IndexRecordOption::WithFreqsAndPositions } else { IndexRecordOption::WithFreqs }),
        );
        sb.add_text_field(name, opt);
    }
    sb.build()
}

/// One COPY text-format field: \N is NULL (empty), backslash escapes decoded in one pass.
fn copy_unescape(f: &str) -> String {
    if f == "\\N" {
        return String::new();
    }
    if !f.contains('\\') {
        return f.to_string();
    }
    let mut out = String::with_capacity(f.len());
    let mut it = f.chars();
    while let Some(c) = it.next() {
        if c != '\\' {
            out.push(c);
            continue;
        }
        match it.next() {
            Some('t') => out.push('\t'),
            Some('n') => out.push('\n'),
            Some('r') => out.push('\r'),
            Some('b') => out.push('\u{8}'),
            Some('f') => out.push('\u{c}'),
            Some('v') => out.push('\u{b}'),
            Some(o) => out.push(o),
            None => out.push('\\'),
        }
    }
    out
}

/// What a worker receives: a document for part `part`, or "chunk done, commit".
enum Msg {
    Doc(usize, TantivyDocument),
    Commit(usize),
}

fn new_index(dir: &Path, sch: &Schema, stop: &str) -> Index {
    std::fs::create_dir_all(dir).unwrap_or_else(|e| die(format!("mkdir {}: {e}", dir.display())));
    let index = Index::builder()
        .schema(sch.clone())
        .settings(settings())
        .create_in_dir(dir)
        .unwrap_or_else(|e| die(format!("create {}: {e}", dir.display())));
    index.tokenizers().register(an_name(), analyzer(stop));
    index
}

fn settings() -> IndexSettings {
    IndexSettings { docstore_compress_dedicated_thread: false, ..IndexSettings::default() }
}

/// One worker: a private index per part, one indexing thread, no merges, a commit per chunk.
/// Each segment it writes holds one chunk (or a deterministic slice of one, if the heap fills).
fn worker(tmp: PathBuf, sch: Schema, stop: String, heap: usize, rx: Receiver<Msg>) -> Vec<PathBuf> {
    let mut writers: HashMap<usize, (PathBuf, tantivy::IndexWriter)> = HashMap::new();
    let get = |part: usize| -> PathBuf { tmp.join(format!("part{part}")) };
    for m in rx {
        let part = match &m {
            Msg::Doc(p, _) | Msg::Commit(p) => *p,
        };
        let (_, w) = writers.entry(part).or_insert_with(|| {
            let dir = get(part);
            let idx = new_index(&dir, &sch, &stop);
            let w: tantivy::IndexWriter = idx
                .writer_with_num_threads(1, heap)
                .unwrap_or_else(|e| die(format!("writer: {e}")));
            w.set_merge_policy(Box::new(NoMergePolicy));
            (dir, w)
        });
        match m {
            Msg::Doc(_, d) => {
                w.add_document(d).unwrap();
            }
            Msg::Commit(_) => {
                w.commit().unwrap_or_else(|e| die(format!("commit: {e}")));
            }
        }
    }
    let mut dirs = Vec::new();
    for (_, (dir, mut w)) in writers {
        w.commit().unwrap_or_else(|e| die(format!("commit: {e}")));
        w.wait_merging_threads().unwrap();
        dirs.push(dir);
    }
    dirs
}

/// Every worker segment of one part, in input order (the input position of each segment's doc 0).
fn ordered_segments(dirs: &[PathBuf], seq_of: &HashMap<u64, u64>, stop: &str) -> Vec<(PathBuf, SegmentId)> {
    let mut segs: Vec<(u64, PathBuf, SegmentId)> = Vec::new();
    for d in dirs {
        let idx = Index::open_in_dir(d).unwrap();
        idx.tokenizers().register(an_name(), analyzer(stop));
        let searcher = idx.reader().unwrap().searcher();
        for r in searcher.segment_readers() {
            let id0 = r.fast_fields().u64("doc_id").unwrap().first(0).unwrap();
            segs.push((seq_of[&id0], d.clone(), r.segment_id()));
        }
    }
    segs.sort_by_key(|x| x.0);
    segs.into_iter().map(|x| (x.1, x.2)).collect()
}

/// Merge consecutive input-ordered segments into `groups` output segments (1 = one segment).
/// merge_filtered_segments writes opstamp 0 and a payload naming the source segments; canon.py
/// drops the payload. With groups > 1 the group segments are assembled into one index at `out`.
fn merge_groups(order: &[(PathBuf, SegmentId)], out: &str, groups: usize, stop: &str) -> serde_json::Value {
    let t = Instant::now();
    let mut opened: HashMap<PathBuf, Index> = HashMap::new();
    for (d, _) in order {
        opened.entry(d.clone()).or_insert_with(|| {
            let idx = Index::open_in_dir(d).unwrap();
            idx.tokenizers().register(an_name(), analyzer(stop));
            idx
        });
    }
    let n = order.len();
    let groups = groups.clamp(1, n);
    let mut outs: Vec<String> = Vec::new();
    let mut docs = 0u32;
    for g in 0..groups {
        let (lo, hi) = (g * n / groups, (g + 1) * n / groups);
        let segments: Vec<tantivy::Segment> = order[lo..hi]
            .iter()
            .map(|(d, id)| opened[d].searchable_segments().unwrap().into_iter().find(|s| s.id() == *id).unwrap())
            .collect();
        let o = if groups == 1 { out.to_string() } else { format!("{out}.g{g}") };
        std::fs::create_dir_all(&o).unwrap();
        let dir = tantivy::directory::MmapDirectory::open(&o).unwrap();
        let merged = tantivy::indexer::merge_filtered_segments(&segments, settings(), vec![None; segments.len()], dir)
            .unwrap_or_else(|e| die(format!("merge into {o}: {e}")));
        docs += merged.searchable_segment_metas().unwrap().iter().map(|m| m.max_doc()).sum::<u32>();
        outs.push(o);
    }
    if groups > 1 {
        assemble(&outs, out);
    }
    serde_json::json!({"dir": out, "docs": docs, "segments_before_merge": n, "segments": groups,
                       "merge_s": (t.elapsed().as_secs_f64() * 10.0).round() / 10.0})
}

/// One index directory from the single segments of several: copy the files, concatenate the
/// segment lists (in the order given) into a meta.json.
fn assemble(parts: &[String], out: &str) {
    std::fs::create_dir_all(out).unwrap();
    let mut meta: Option<serde_json::Value> = None;
    for p in parts {
        let m: serde_json::Value = serde_json::from_reader(File::open(Path::new(p).join("meta.json")).unwrap()).unwrap();
        for e in std::fs::read_dir(p).unwrap() {
            let e = e.unwrap();
            let name = e.file_name();
            if name.to_string_lossy() != "meta.json" && !name.to_string_lossy().starts_with('.') {
                std::fs::copy(e.path(), Path::new(out).join(&name)).unwrap();
            }
        }
        match meta.as_mut() {
            None => meta = Some(m),
            Some(acc) => {
                let segs = m["segments"].as_array().unwrap().clone();
                acc["segments"].as_array_mut().unwrap().extend(segs);
            }
        }
    }
    let mut meta = meta.unwrap();
    meta.as_object_mut().unwrap().remove("payload");
    std::fs::write(Path::new(out).join("meta.json"), serde_json::to_string_pretty(&meta).unwrap()).unwrap();
    for p in parts {
        std::fs::remove_dir_all(p).unwrap();
    }
}

fn merge_part(dirs: &[PathBuf], out: &str, seq_of: &HashMap<u64, u64>, stop: &str, groups: usize, order_file: Option<&Path>) -> serde_json::Value {
    let order = ordered_segments(dirs, seq_of, stop);
    if let Some(f) = order_file {
        let v: Vec<serde_json::Value> = order
            .iter()
            .map(|(d, id)| serde_json::json!({"dir": d.to_string_lossy(), "segment": id.uuid_string()}))
            .collect();
        std::fs::write(f, serde_json::to_string(&v).unwrap()).unwrap();
    }
    merge_groups(&order, out, groups, stop)
}

/// regroup PARTS OUT --groups G: merge a kept build's segments (PARTS/order.json) differently.
fn regroup(a: &Args, parts: &str, out: &str) {
    let stop = a.req("--stop");
    let groups: usize = a.req("--groups").parse().unwrap();
    let order: Vec<(PathBuf, SegmentId)> = if Path::new(parts).join("order.json").exists() {
        let v: Vec<serde_json::Value> = serde_json::from_reader(File::open(Path::new(parts).join("order.json")).unwrap()).unwrap();
        v.iter()
            .map(|e| (PathBuf::from(e["dir"].as_str().unwrap()), SegmentId::from_uuid_string(e["segment"].as_str().unwrap()).unwrap()))
            .collect()
    } else {
        // an index directory: its segments in meta.json order (the build's input order)
        let m: serde_json::Value = serde_json::from_reader(File::open(Path::new(parts).join("meta.json")).unwrap()).unwrap();
        m["segments"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| (PathBuf::from(parts), SegmentId::from_uuid_string(e["segment_id"].as_str().unwrap()).unwrap()))
            .collect()
    };
    println!("{}", merge_groups(&order, out, groups, &stop));
}

fn build(a: &Args, out: &str) {
    let stop = a.req("--stop");
    let kind = a.get("--schema").unwrap_or_else(|| "none".into());
    let chunk: u64 = a.get("--chunk").map(|s| s.parse().unwrap()).unwrap_or(1_000_000);
    let heap: usize = a.get("--heap-mb").map(|s| s.parse().unwrap()).unwrap_or(1500) * 1_000_000;
    let jobs: usize = a.get("--jobs").map(|s| s.parse().unwrap()).unwrap_or(1);
    let split: u64 = a.get("--split-at").map(|s| s.parse().unwrap()).unwrap_or(u64::MAX);
    let cold_out = a.get("--cold-out");
    let groups: usize = a.get("--groups").map(|s| s.parse().unwrap()).unwrap_or(1);
    let keep = a.has("--keep-parts");
    if split != u64::MAX && cold_out.is_none() {
        die("--split-at needs --cold-out");
    }
    let outs: Vec<String> = std::iter::once(out.to_string()).chain(cold_out.clone()).collect();
    for o in &outs {
        if Path::new(o).exists() {
            die(format!("{o} exists; build writes a fresh directory"));
        }
    }
    let web = a.get("--kind").unwrap_or_else(|| "wiki".into());
    let sch = schema(&kind, &web);
    let f_id = sch.get_field("doc_id").unwrap();
    let (f_title, f_body) = (sch.get_field("title").ok(), sch.get_field("body").unwrap());
    let cols: Vec<Field> = text_fields(&web).iter().map(|(n, _)| sch.get_field(n).unwrap()).collect();
    let ncols = if web == "wiki" { 6 } else { 1 + cols.len() };
    let tmp = PathBuf::from(format!("{out}.parts"));
    let t0 = Instant::now();
    let mut txs = Vec::new();
    let mut handles = Vec::new();
    for j in 0..jobs {
        // a queue as long as a chunk, so the reader can fill worker j+1 while worker j indexes
        let (tx, rx) = sync_channel::<Msg>(chunk as usize + 1);
        let (wtmp, wsch, wstop) = (tmp.join(format!("w{j}")), sch.clone(), stop.clone());
        handles.push(std::thread::spawn(move || worker(wtmp, wsch, wstop, heap, rx)));
        txs.push(tx);
    }
    let mut seq_of: Vec<HashMap<u64, u64>> = vec![HashMap::new(); outs.len()];
    let mut local = vec![0u64; outs.len()];
    let stdin = std::io::stdin();
    let mut r = BufReader::with_capacity(1 << 20, stdin.lock());
    let mut buf = Vec::new();
    let (mut seq, mut bad) = (0u64, 0u64);
    loop {
        buf.clear();
        if r.read_until(b'\n', &mut buf).unwrap() == 0 {
            break;
        }
        let line = String::from_utf8_lossy(&buf);
        let line = line.trim_end_matches('\n');
        let parts: Vec<&str> = line.split('\t').collect();
        let pid: Option<u64> = if parts.len() == ncols { parts[0].parse().ok() } else { None };
        let Some(pid) = pid else {
            bad += 1;
            continue;
        };
        let f: Vec<String> = parts[1..].iter().map(|s| copy_unescape(s)).collect();
        let mut d = TantivyDocument::default();
        d.add_u64(f_id, pid);
        if web == "wiki" {
            let body = f.iter().filter(|s| !s.is_empty()).cloned().collect::<Vec<_>>().join(" ");
            d.add_text(f_title.unwrap(), &f[0]);
            d.add_text(f_body, &body);
        } else {
            for (fld, text) in cols.iter().zip(f.iter()) {
                d.add_text(*fld, text);
            }
        }
        let part = usize::from(seq >= split);
        if seq_of[part].insert(pid, seq).is_some() {
            die(format!("doc_id {pid} appears twice in the input"));
        }
        let c = local[part] / chunk;
        let w = &txs[(c as usize) % jobs];
        w.send(Msg::Doc(part, d)).unwrap();
        local[part] += 1;
        if local[part] % chunk == 0 {
            w.send(Msg::Commit(part)).unwrap();
        }
        seq += 1;
    }
    drop(txs);
    let mut dirs: Vec<PathBuf> = Vec::new();
    for h in handles {
        dirs.extend(h.join().unwrap());
    }
    let index_s = t0.elapsed().as_secs();
    let mut res = Vec::new();
    if seq == 0 {
        // No rows at all (a release whose registry has no pages: type 3 is empty): the release layout still
        // needs the folder, so write a valid index with zero segments (meta.json, schema, no data files).
        // `bad` lines are counted in the report; a build that read only bad lines is empty too.
        for o in &outs {
            drop(new_index(Path::new(o), &sch, &stop));
            res.push(serde_json::json!({"dir": o, "docs": 0, "segments": 0, "empty": true}));
        }
    }
    for (part, o) in outs.iter().enumerate() {
        let pd: Vec<PathBuf> = dirs.iter().filter(|d| d.ends_with(format!("part{part}"))).cloned().collect();
        if !pd.is_empty() {
            let of = if keep && part == 0 { Some(tmp.join("order.json")) } else { None };
            res.push(merge_part(&pd, o, &seq_of[part], &stop, groups, of.as_deref()));
        }
    }
    if !keep {
        std::fs::remove_dir_all(&tmp).ok();
    }
    println!(
        "{}",
        serde_json::json!({"rows": seq, "bad_lines": bad, "kind": web, "schema": kind, "chunk": chunk, "jobs": jobs,
                           "heap_mb_per_job": heap / 1_000_000, "index_s": index_s,
                           "build_s": t0.elapsed().as_secs(), "parts": res})
    );
}

// ------------------------------------------------------------------------------------- bench

fn evict(dir: &str) -> u64 {
    let mut n = 0;
    for e in std::fs::read_dir(dir).unwrap() {
        let p = e.unwrap().path();
        if p.is_file() {
            if let Ok(f) = File::open(&p) {
                unsafe { libc::posix_fadvise(f.as_raw_fd(), 0, 0, libc::POSIX_FADV_DONTNEED) };
                n += f.metadata().map(|m| m.len()).unwrap_or(0);
            }
        }
    }
    n
}

fn open(dir: &str, stop: &str) -> Searcher {
    let index = Index::open_in_dir(dir).unwrap_or_else(|e| die(format!("open {dir}: {e}")));
    index.tokenizers().register(an_name(), analyzer(stop));
    let reader = index.reader().unwrap();
    reader.searcher()
}

fn query_terms(an: &mut TextAnalyzer, q: &str) -> Vec<String> {
    let mut seen: Vec<String> = Vec::new();
    let mut ts = an.token_stream(q);
    while ts.advance() {
        let t = ts.token().text.clone();
        if !seen.contains(&t) {
            seen.push(t);
        }
    }
    seen.truncate(8);
    seen
}

struct Global<'a>(Vec<&'a Searcher>);
impl Bm25StatisticsProvider for Global<'_> {
    fn total_num_tokens(&self, field: Field) -> tantivy::Result<u64> {
        let mut s = 0;
        for x in &self.0 {
            s += x.total_num_tokens(field)?;
        }
        Ok(s)
    }
    fn total_num_docs(&self) -> tantivy::Result<u64> {
        let mut s = 0;
        for x in &self.0 {
            s += x.total_num_docs()?;
        }
        Ok(s)
    }
    fn doc_freq(&self, term: &Term) -> tantivy::Result<u64> {
        let mut s = 0;
        for x in &self.0 {
            s += x.doc_freq(term)?;
        }
        Ok(s)
    }
}

/// The doc id of a hit from its fast field: u64 (this tool) or i64 (tantivy-py add_integer_field).
/// The columns are opened once per segment, when the searcher is opened (as tantivy-py's
/// fast_field_values does per call); opening one per hit cost ~1.4 ms on a 6.8M-document segment
/// and made the first bench rows read layout effects that were only this.
enum IdCol {
    U(tantivy::columnar::Column<u64>),
    I(tantivy::columnar::Column<i64>),
}
struct IdReader(Vec<IdCol>);
impl IdReader {
    fn new(s: &Searcher, field: &str) -> IdReader {
        IdReader(
            s.segment_readers()
                .iter()
                .map(|r| {
                    let ff = r.fast_fields();
                    match ff.u64(field) {
                        Ok(c) => IdCol::U(c),
                        Err(_) => IdCol::I(ff.i64(field).unwrap_or_else(|e| die(format!("fast field {field}: {e}")))),
                    }
                })
                .collect(),
        )
    }
    fn get(&self, a: DocAddress) -> u64 {
        match &self.0[a.segment_ord as usize] {
            IdCol::U(c) => c.first(a.doc_id).unwrap_or(0),
            IdCol::I(c) => c.first(a.doc_id).unwrap_or(0) as u64,
        }
    }
}

fn top10(
    s: &Searcher,
    q: &dyn Query,
    stats: &dyn Bm25StatisticsProvider,
    idr: &IdReader,
) -> Vec<(Score, u64)> {
    let hits: Vec<(Score, DocAddress)> = if MULTI.get().copied().unwrap_or(false) {
        // what tantivy-py's Searcher.search does: Count + TopDocs in one MultiCollector, which
        // scores every matching document (no block-max WAND pruning)
        let mut mc = tantivy::collector::MultiCollector::new();
        let _count = mc.add_collector(tantivy::collector::Count);
        let top = mc.add_collector(TopDocs::with_limit(10).order_by_score());
        let mut fruit = s.search(q, &mc).unwrap();
        top.extract(&mut fruit)
    } else {
        s.search_with_statistics_provider(q, &TopDocs::with_limit(10).order_by_score(), stats)
            .unwrap()
    };
    hits.into_iter()
        .map(|(sc, a)| {
            if NOIDS.get().copied().unwrap_or(false) {
                return (sc, 0);
            }
            (sc, idr.get(a))
        })
        .collect()
}

/// Best score any cold document can reach for these terms: per term, the max of its block-max
/// scores over cold's postings (scored with `stats`), summed over the terms. tantivy 0.26 has no
/// public shallow seek, so this decodes every block: it is what a build-time sidecar of per-term
/// cold maxima would hold, not something to compute per search (bench computes it untimed).
fn cold_bound(cold: &Searcher, body: Field, terms: &[String], stats: &dyn Bm25StatisticsProvider) -> Score {
    let mut total = 0.0;
    for t in terms {
        let term = Term::from_field_text(body, t);
        let w = Bm25Weight::for_terms(stats, std::slice::from_ref(&term)).unwrap();
        let mut best: Score = 0.0;
        for r in cold.segment_readers() {
            let fnr = r.get_fieldnorms_reader(body).unwrap();
            let inv = r.inverted_index(body).unwrap();
            if let Some(mut bp) = inv.read_block_postings(&term, IndexRecordOption::WithFreqs).unwrap() {
                while bp.block_len() > 0 {
                    best = best.max(bp.block_max_score(&fnr, &w));
                    bp.advance();
                }
            }
        }
        total += best;
    }
    total
}

fn pct(v: &mut [f64], p: f64) -> f64 {
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let i = ((v.len() as f64) * p) as usize;
    (v[i.min(v.len() - 1)] * 10.0).round() / 10.0
}

fn bench(a: &Args) {
    let dir = a.req("--index");
    let cold_dir = a.get("--cold");
    let stop = a.req("--stop");
    let mode = a.get("--mode").unwrap_or_else(|| "full".into());
    let stats_mode = a.get("--stats").unwrap_or_else(|| "global".into());
    let rule = a.get("--rule").unwrap_or_else(|| "always".into());
    let idf = a.get("--id-field").unwrap_or_else(|| "doc_id".into());
    let repeats: usize = a.get("--repeats").map(|s| s.parse().unwrap()).unwrap_or(3);
    let do_evict = !a.has("--no-evict");
    let alpha: Option<f32> = rule.strip_prefix("alpha:").map(|x| x.parse().unwrap());
    if mode == "hotcold" && cold_dir.is_none() {
        die("--mode hotcold needs --cold");
    }
    let mut an = analyzer(&stop);
    let qs: Vec<String> = std::fs::read_to_string(a.req("--queries"))
        .unwrap()
        .lines()
        .map(|l| l.trim().to_string())
        .filter(|l| !l.is_empty() && !l.starts_with('#'))
        .collect();
    let qterms: Vec<(String, Vec<String>)> = qs
        .iter()
        .map(|q| (q.clone(), query_terms(&mut an, q)))
        .filter(|(_, t)| !t.is_empty())
        .collect();

    // cold bounds, computed before any timed pass (and before eviction) so they do not warm it
    let bounds: HashMap<String, (Score, Score)> = if mode == "hotcold" {
        let hot = open(&dir, &stop);
        let cold = open(cold_dir.as_ref().unwrap(), &stop);
        let body = hot.schema().get_field("body").unwrap();
        let both = Global(vec![&hot, &cold]);
        let b: HashMap<String, (Score, Score)> = qterms
            .iter()
            .map(|(q, t)| (q.clone(), (cold_bound(&cold, body, t, &both), cold_bound(&cold, body, t, &cold))))
            .collect();
        if let Some(p) = a.get("--dump") {
            // analysis record per query (untimed, before any timed pass): both halves' top 10
            // and cold's bound, under the chosen statistics
            let mut d = BufWriter::new(File::create(p).unwrap());
            let (hid, cid) = (IdReader::new(&hot, &idf), IdReader::new(&cold, &idf));
            for (q, t) in &qterms {
                let bq = BooleanQuery::new(
                    t.iter()
                        .map(|x| {
                            let tq: Box<dyn Query> = Box::new(TermQuery::new(
                                Term::from_field_text(body, x),
                                IndexRecordOption::WithFreqs,
                            ));
                            (Occur::Should, tq)
                        })
                        .collect(),
                );
                let (hs, cs): (&dyn Bm25StatisticsProvider, &dyn Bm25StatisticsProvider) =
                    if stats_mode == "global" { (&both, &both) } else { (&hot, &cold) };
                let bound = if stats_mode == "global" { b[q].0 } else { b[q].1 };
                writeln!(d, "{}", serde_json::json!({"q": q, "hot": top10(&hot, &bq, hs, &hid),
                    "cold": top10(&cold, &bq, cs, &cid), "bound": bound})).unwrap();
            }
        }
        b
    } else {
        HashMap::new()
    };
    let mut runs = Vec::new();
    let mut last: Vec<(String, Vec<u64>)> = Vec::new();
    let mut cold_searched = 0usize;
    for rep in 0..repeats {
        let mut evicted = 0;
        if do_evict {
            evicted += evict(&dir);
            if let Some(c) = &cold_dir {
                evicted += evict(c);
            }
        }
        let t_open = Instant::now();
        let hot = open(&dir, &stop);
        let cold = cold_dir.as_ref().map(|c| open(c, &stop));
        let open_ms = t_open.elapsed().as_secs_f64() * 1000.0;
        let body = hot.schema().get_field("body").unwrap();
        let hid = IdReader::new(&hot, &idf);
        let cid = cold.as_ref().map(|c| IdReader::new(c, &idf));
        let mut pass_stats = serde_json::Map::new();
        for pass in ["first", "warm"] {
            let mut times = Vec::new();
            let mut out = Vec::new();
            let mut nc = 0usize;
            for (q, terms) in &qterms {
                let t = Instant::now();
                let bq = BooleanQuery::new(
                    terms
                        .iter()
                        .map(|x| {
                            let tq: Box<dyn Query> = Box::new(TermQuery::new(
                                Term::from_field_text(body, x),
                                IndexRecordOption::WithFreqs,
                            ));
                            (Occur::Should, tq)
                        })
                        .collect(),
                );
                let ids: Vec<u64> = if mode == "full" {
                    top10(&hot, &bq, &hot, &hid).into_iter().map(|x| x.1).collect()
                } else {
                    let cold = cold.as_ref().unwrap();
                    let both = Global(vec![&hot, cold]);
                    let (hs, cs): (&dyn Bm25StatisticsProvider, &dyn Bm25StatisticsProvider) =
                        if stats_mode == "global" { (&both, &both) } else { (&hot, cold) };
                    let h = top10(&hot, &bq, hs, &hid);
                    let search_cold = match (rule.as_str(), alpha) {
                        ("always", _) => true,
                        ("hot", _) => false,
                        (_, Some(x)) => {
                            let b = if stats_mode == "global" { bounds[q].0 } else { bounds[q].1 };
                            h.len() < 10 || h[9].0 < x * b
                        }
                        _ => die(format!("unknown rule {rule}")),
                    };
                    let mut all = h.clone();
                    if search_cold {
                        nc += 1;
                        all.extend(top10(cold, &bq, cs, cid.as_ref().unwrap()));
                        all.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap().then(a.1.cmp(&b.1)));
                        all.truncate(10);
                    }
                    all.into_iter().map(|x| x.1).collect()
                };
                times.push(t.elapsed().as_secs_f64() * 1000.0);
                out.push((q.clone(), ids));
            }
            let n = times.len();
            pass_stats.insert(
                pass.into(),
                serde_json::json!({"n": n, "p50_ms": pct(&mut times.clone(), 0.5),
                                   "p90_ms": pct(&mut times, 0.9), "cold_searched": nc}),
            );
            cold_searched = nc;
            last = out;
        }
        runs.push(serde_json::json!({"repeat": rep, "evicted_mb": evicted >> 20,
                                     "open_ms": (open_ms * 10.0).round() / 10.0, "passes": pass_stats}));
    }
    let mut f = BufWriter::new(File::create(a.req("--out")).unwrap());
    for (q, ids) in &last {
        writeln!(f, "{}\t{}", q, ids.iter().map(|x| x.to_string()).collect::<Vec<_>>().join(",")).unwrap();
    }
    let med = |pass: &str, key: &str| -> serde_json::Value {
        let mut v: Vec<f64> = runs.iter().map(|r| r["passes"][pass][key].as_f64().unwrap()).collect();
        v.sort_by(|a, b| a.partial_cmp(b).unwrap());
        serde_json::json!({"median": v[v.len() / 2], "min": v[0], "max": v[v.len() - 1]})
    };
    println!(
        "{}",
        serde_json::json!({"index": dir, "cold": cold_dir, "mode": mode, "stats": stats_mode,
            "rule": rule, "repeats": repeats, "evict": do_evict, "queries": qterms.len(),
            "cold_searched": cold_searched,
            "first_p50": med("first", "p50_ms"), "first_p90": med("first", "p90_ms"),
            "warm_p50": med("warm", "p50_ms"), "warm_p90": med("warm", "p90_ms"), "runs": runs})
    );
}

fn dfs(a: &Args) {
    let stop = a.req("--stop");
    let s = open(&a.req("--index"), &stop);
    let body = s.schema().get_field("body").unwrap();
    let mut an = analyzer(&stop);
    let mut seen = HashSet::new();
    let mut terms: Vec<String> = Vec::new();
    for l in std::fs::read_to_string(a.req("--queries")).unwrap().lines() {
        for t in query_terms(&mut an, l.trim()) {
            if seen.insert(t.clone()) {
                terms.push(t);
            }
        }
    }
    terms.sort();
    println!("#docs\t{}", s.num_docs());
    println!("#body_tokens\t{}", s.total_num_tokens(body).unwrap());
    for t in terms {
        println!("{}\t{}", t, s.doc_freq(&Term::from_field_text(body, &t)).unwrap());
    }
}

/// Bytes per field and component, summed over segments (what each field costs on disk).
fn space(a: &Args) {
    let s = open(&a.req("--index"), &a.get("--stop").unwrap_or_else(|| "/dev/null".into()));
    let u = s.space_usage().unwrap();
    let mut m: std::collections::BTreeMap<String, u64> = std::collections::BTreeMap::new();
    for seg in u.segments() {
        for (comp, per) in [("termdict", seg.termdict()), ("postings", seg.postings()),
                            ("positions", seg.positions()), ("fast", seg.fast_fields()),
                            ("fieldnorms", seg.fieldnorms())] {
            for f in per.fields() {
                *m.entry(format!("{}.{}", f.field_name(), comp)).or_default() += f.total().get_bytes();
            }
        }
        *m.entry("store".into()).or_default() += seg.store().total().get_bytes();
    }
    m.insert("total".into(), u.total().get_bytes());
    println!("{}", serde_json::to_string(&m).unwrap());
}

/// merge --index DIR [--below N]: ONE merge, by the index writer, of every segment (or only those with fewer than N docs, at
/// least two). Unlike `regroup` this applies the segments' delete bitsets (the writer's own merge), so deleted documents leave.
/// Writes a new segment, a new meta.json and removes the merged segments' files; prints one JSON line. For the update-package
/// merge-policy measurement (docs/internal/SEARCH-UPDATE-PACKAGE.md section 6): the merged segment is what a merge month ships.
fn merge(a: &Args) {
    let dir = a.req("--index");
    let idx = Index::open_in_dir(&dir).unwrap_or_else(|e| die(format!("open {dir}: {e}")));
    idx.tokenizers().register(an_name(), analyzer(&a.get("--stop").unwrap_or_else(|| "/dev/null".into())));
    let below: u32 = a.get("--below").map(|s| s.parse().unwrap()).unwrap_or(u32::MAX);
    let ids: Vec<SegmentId> = idx.searchable_segment_metas().unwrap().iter().filter(|m| m.max_doc() < below).map(|m| m.id()).collect();
    if ids.len() < 2 {
        die("fewer than two segments to merge");
    }
    let t = Instant::now();
    let mut w: tantivy::IndexWriter = idx.writer_with_num_threads(1, 500_000_000).unwrap_or_else(|e| die(format!("writer: {e}")));
    w.set_merge_policy(Box::new(NoMergePolicy));
    let merged = w.merge(&ids).wait().unwrap_or_else(|e| die(format!("merge: {e}")));
    w.garbage_collect_files().wait().unwrap_or_else(|e| die(format!("gc: {e}")));
    println!("{}", serde_json::json!({"merged_segments": ids.len(), "docs_after": merged.map(|m| m.num_docs()), "merge_s": (t.elapsed().as_secs_f64() * 10.0).round() / 10.0}));
}

fn main() {
    let argv: Vec<String> = std::env::args().collect();
    let a = Args(argv.clone());
    if a.has("--no-ids") {
        NOIDS.set(true).ok(); // bench: time the search only (hit ids are not read back)
    }
    if a.has("--py-like") {
        MULTI.set(true).ok(); // bench, full mode: Count + TopDocs through a MultiCollector like tantivy-py
    }
    if let Some(n) = a.get("--analyzer") {
        if n != "en_pg" && n != "en_pg2" {
            die("--analyzer is en_pg or en_pg2");
        }
        ANALYZER_NAME.set(n).ok();
    }
    match argv.get(1).map(|s| s.as_str()) {
        Some("build") => {
            let out = argv.get(2).unwrap_or_else(|| die("build OUT"));
            build(&a, out)
        }
        Some("bench") => bench(&a),
        Some("regroup") => regroup(&a, argv.get(2).unwrap_or_else(|| die("regroup PARTS OUT")), argv.get(3).unwrap_or_else(|| die("regroup PARTS OUT"))),
        Some("dfs") => dfs(&a),
        Some("space") => space(&a),
        Some("merge") => merge(&a),
        _ => die("usage: build | bench | dfs (see the header of src/main.rs)"),
    }
}
