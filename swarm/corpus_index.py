#!/usr/bin/env python3
"""
corpus_index.py — local retrieval index over a folder of study material.

No network, no API quota, no model download. Extracts text from md/txt/html/pdf/
captions/code, chunks it, and stores it in a SQLite FTS5 table with BM25 ranking.

    python3 corpus_index.py build   --root ~/Documents/flint-training [--prune] [--budget 150]
    python3 corpus_index.py search  "optimal stopping under transaction costs" -k 6
    python3 corpus_index.py stats

Rebuilds are incremental: unchanged files (same mtime+size) are skipped.
"""
import argparse, html, json, os, re, sqlite3, subprocess, sys, time
from pathlib import Path

DEFAULT_ROOT = Path.home() / "Documents" / "flint-training"
DEFAULT_DB = Path(os.environ.get("FLINT_CORPUS_DB", Path.home() / ".flint" / "corpus.db"))

TEXT_EXT = {".md", ".txt", ".markdown", ".rst"}
CODE_EXT = {".py", ".r", ".js", ".go", ".sql", ".sh"}
HTML_EXT = {".html", ".htm", ".xml"}
PDF_EXT = {".pdf"}
CAPTION_EXT = {".vtt", ".webvtt", ".srt"}
ALL_EXT = TEXT_EXT | CODE_EXT | HTML_EXT | PDF_EXT | CAPTION_EXT

SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv",
             "dist", "build", ".next", "site-packages",
             # OCW offline exports: site JS bundles and MathJax would drown BM25 results.
             "static_shared", "mathjax"}
MAX_BYTES = 2_000_000   # larger text/code files are generated bundles, not study material

CHUNK_CHARS = 1400
OVERLAP_PARAS = 1
MIN_CHUNK = 120

# ---------------------------------------------------------------- extraction

def _strip_html(raw):
    raw = re.sub(r"(?is)<(script|style|nav|footer|svg)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<!--.*?-->", " ", raw)
    raw = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6])[^>]*>", "\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return html.unescape(raw)


def _strip_captions(raw):
    out, last = [], None
    for line in raw.splitlines():
        s = line.strip()
        if not s or s == "WEBVTT" or s.isdigit():
            continue
        if "-->" in s or re.fullmatch(r"[\d:.,\s>-]+", s):
            continue
        s = re.sub(r"</?[civ][^>]*>", "", s)
        if s != last:                      # captions repeat lines constantly
            out.append(s)
            last = s
    return "\n".join(out)


def extract(path):
    ext = path.suffix.lower()
    try:
        if ext in PDF_EXT:
            r = subprocess.run(["pdftotext", "-q", "-nopgbrk", str(path), "-"],
                               capture_output=True, timeout=60)
            return r.stdout.decode("utf-8", "replace")
        raw = path.read_text("utf-8", "replace")
        if ext in HTML_EXT:
            return _strip_html(raw)
        if ext in CAPTION_EXT:
            return _strip_captions(raw)
        return raw
    except Exception:
        return ""


# ---------------------------------------------------------------- chunking

HEADING_RE = re.compile(r"^\s{0,3}(#{1,4})\s+(.+?)\s*#*\s*$")


def chunk(text):
    """Yield (heading, body). Heading is the nearest preceding markdown header."""
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    heading, buf, size = "", [], 0
    for p in paras:
        m = HEADING_RE.match(p.splitlines()[0]) if p else None
        if m:
            # A heading starts a new section: flush what belongs to the previous heading
            # so no chunk is labelled with (or overlaps into) a section it is not part of.
            if buf:
                body = "\n\n".join(buf)
                if len(body) >= MIN_CHUNK:
                    yield heading, body
            heading, buf, size = m.group(2)[:200], [], 0
        if size + len(p) > CHUNK_CHARS and buf:
            body = "\n\n".join(buf)
            if len(body) >= MIN_CHUNK:
                yield heading, body
            buf = buf[-OVERLAP_PARAS:] if OVERLAP_PARAS else []
            size = sum(len(x) for x in buf)
        buf.append(p)
        size += len(p)
    if buf:
        body = "\n\n".join(buf)
        if len(body) >= MIN_CHUNK:
            yield heading, body


# ---------------------------------------------------------------- db

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY, mtime REAL, size INTEGER, nchunks INTEGER, indexed_at REAL
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    path UNINDEXED, heading, body, tokenize='porter unicode61'
);
"""


def connect(db):
    db = Path(db).expanduser()
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    return con


def walk(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() not in ALL_EXT or fn.startswith(".") or ".min." in fn:
                continue
            try:
                if p.suffix.lower() not in PDF_EXT and p.stat().st_size > MAX_BYTES:
                    continue
            except OSError:
                continue
            yield p


def prune(root, db, verbose=True):
    """Drop index entries whose file no longer exists under root. Only use with the root the
    index was built from: entries are stored relative to it."""
    root = Path(root).expanduser()
    con = connect(db)
    gone = [r[0] for r in con.execute("SELECT path FROM files") if not (root / r[0]).is_file()]
    for rel in gone:
        con.execute("DELETE FROM chunks WHERE path = ?", (rel,))
        con.execute("DELETE FROM files WHERE path = ?", (rel,))
    con.commit()
    con.close()
    if verbose:
        print(f"pruned {len(gone)} missing files")
    return len(gone)


def build(root, db, budget=None, verbose=True):
    root = Path(root).expanduser()
    con = connect(db)
    known = {r[0]: (r[1], r[2]) for r in con.execute("SELECT path, mtime, size FROM files")}
    t0, new, skipped, chunks_added = time.time(), 0, 0, 0

    for p in walk(root):
        rel = str(p.relative_to(root))
        try:
            st = p.stat()
        except OSError:
            continue
        if known.get(rel) == (st.st_mtime, st.st_size):
            skipped += 1
            continue
        if budget and time.time() - t0 > budget:
            con.commit()
            if verbose:
                print(f"[budget] stopping early — rerun to continue")
            break

        text = extract(p)
        con.execute("DELETE FROM chunks WHERE path = ?", (rel,))
        n = 0
        for heading, body in chunk(text):
            con.execute("INSERT INTO chunks (path, heading, body) VALUES (?,?,?)",
                        (rel, heading, body))
            n += 1
        con.execute(
            "INSERT OR REPLACE INTO files (path, mtime, size, nchunks, indexed_at) VALUES (?,?,?,?,?)",
            (rel, st.st_mtime, st.st_size, n, time.time()))
        new += 1
        chunks_added += n
        if verbose and new % 100 == 0:
            print(f"  {new} files, {chunks_added} chunks, {time.time()-t0:.0f}s", flush=True)
        if new % 200 == 0:
            con.commit()

    con.commit()
    total = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if verbose:
        print(f"indexed {new} new/changed files ({chunks_added} chunks), "
              f"skipped {skipped} unchanged, {total} chunks total, {time.time()-t0:.0f}s")
    indexed = {r[0] for r in con.execute("SELECT path FROM files")}
    remaining = sum(1 for p in walk(root) if str(p.relative_to(root)) not in indexed)
    con.close()
    return remaining


# ---------------------------------------------------------------- search

FTS_SAFE = re.compile(r"[^A-Za-z0-9_]+")
STOP = {"the", "a", "an", "of", "to", "and", "or", "in", "for", "on", "is", "it",
        "how", "what", "why", "do", "i", "my", "with", "that", "this", "be"}


def _fts_query(q):
    terms = [t for t in FTS_SAFE.split(q) if len(t) > 2 and t.lower() not in STOP]
    if not terms:
        terms = [t for t in FTS_SAFE.split(q) if t]
    return " OR ".join(f'"{t}"' for t in terms[:24])


def search(q, k=6, db=DEFAULT_DB, max_chars=1800):
    fq = _fts_query(q)
    if not fq:
        return []
    con = connect(db)
    rows = con.execute(
        "SELECT path, heading, body, bm25(chunks) AS score FROM chunks "
        "WHERE chunks MATCH ? ORDER BY score LIMIT ?", (fq, k)).fetchall()
    con.close()
    return [{"path": p, "heading": h, "text": b[:max_chars], "score": round(s, 3)}
            for p, h, b, s in rows]


def stats(db=DEFAULT_DB):
    con = connect(db)
    f = con.execute("SELECT COUNT(*), SUM(nchunks) FROM files").fetchone()
    c = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    top = con.execute(
        "SELECT substr(path, 1, instr(path||'/', '/')-1) d, COUNT(*) n "
        "FROM files GROUP BY d ORDER BY n DESC LIMIT 12").fetchall()
    con.close()
    return {"files": f[0] or 0, "chunks": c, "by_dir": dict(top)}


# ---------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="index (or re-index changed files)")
    b.add_argument("--root", default=str(DEFAULT_ROOT))
    b.add_argument("--db", default=str(DEFAULT_DB))
    b.add_argument("--budget", type=float, default=None,
                   help="stop after N seconds; rerun to continue")
    b.add_argument("--prune", action="store_true",
                   help="first drop entries for files that no longer exist under --root")

    s = sub.add_parser("search", help="query the index")
    s.add_argument("query")
    s.add_argument("-k", type=int, default=6)
    s.add_argument("--db", default=str(DEFAULT_DB))
    s.add_argument("--json", action="store_true")

    st = sub.add_parser("stats", help="index size and composition")
    st.add_argument("--db", default=str(DEFAULT_DB))

    a = ap.parse_args()
    if a.cmd == "build":
        if a.prune:
            prune(a.root, a.db)
        left = build(a.root, a.db, budget=a.budget)
        print(f"unindexed files remaining: {left}")
    elif a.cmd == "search":
        hits = search(a.query, a.k, a.db)
        if a.json:
            print(json.dumps(hits, indent=2))
        else:
            for h in hits:
                print(f"\n--- {h['path']}  [{h['heading']}]  bm25={h['score']}")
                print(h["text"][:700])
        if not hits:
            print("no hits", file=sys.stderr)
    else:
        print(json.dumps(stats(a.db), indent=2))


if __name__ == "__main__":
    main()
