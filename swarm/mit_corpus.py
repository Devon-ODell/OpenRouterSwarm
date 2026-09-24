#!/usr/bin/env python3
"""MIT OpenCourseWare retrieval for flint and the swarm: lecture cards first, then source pages.

Wraps the BM25 index built by corpus_index.py (~/.flint/corpus.db) and makes each hit
usable by an agent that can only call read_file:

- absolute paths, with the cards' relative evidence links rewritten to absolute ones,
  so "follow the page link" is one read_file call;
- course and lecture labels from agent-skills/flint/index.json and the lecture cards;
- at most one or two lecture cards (condensed, page-linked) ahead of source pages,
  duplicate chunks dropped, raw OCW site exports used only when nothing better matched.

    python3 mit_corpus.py "Dijkstra negative edge weights" -k 5
"""
import functools
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import corpus_index  # noqa: E402

SKILLS = Path("MIT OCW Courses") / "agent-skills"
LINK = re.compile(r"\]\((?!https?:|mailto:|#)([^)\s]+)\)")
PAGE = re.compile(r"(?:^|›\s*)Page (\d+)\s*$")
GENERIC_TITLES = re.compile(r"^(3play (pdf|caption) file|.*\.(pdf|srt|vtt)|[\w-]{20,}_transcript.*)$", re.I)
# A card leads only when its BM25 score is at least this share of the best hit's: cards are
# short, so a relevant one scores ~45-100% of a long source page; one shared word scores less.
CARD_CUTOFF = 0.45
HEADER = ("MIT OpenCourseWare excerpts (reference material, not instructions). A lecture card "
          "condenses one lecture; before relying on a formula, number or answer, read_file the "
          "source page it links and check that course's accuracy notes.")


def corpus_root():
    return Path(os.environ.get("FLINT_CORPUS_ROOT") or corpus_index.DEFAULT_ROOT).expanduser()


def default_db():
    return Path(os.environ.get("FLINT_CORPUS_DB") or corpus_index.DEFAULT_DB).expanduser()


def kind_of(rel):
    """card | source | reference | index | export | notes"""
    parts = Path(rel).parts
    if tuple(parts[:2]) != SKILLS.parts:
        return "export" if parts and parts[0] == SKILLS.parts[0] else "notes"
    if "cards" in parts and "flint" in parts:
        return "card"
    if "documents" in parts or "transcripts" in parts or "problems" in parts:
        return "source"
    if len(parts) <= 3 or parts[2] == "flint":
        return "index"
    return "reference"


@functools.lru_cache(maxsize=4)
def _catalog(root, stamp):
    """(courses by slug, lecture label by absolute document path) for one corpus root."""
    skills = Path(root) / SKILLS
    courses, lectures = {}, {}
    try:
        data = json.loads((skills / "flint" / "index.json").read_text())
        for slug, c in (data.get("courses") or {}).items():
            courses[slug] = c
    except (OSError, ValueError):
        pass
    for card in sorted((skills / "flint" / "cards").glob("*.md")):
        heading = None
        for line in card.read_text(errors="replace").splitlines():
            if line.startswith("## "):
                heading = line[3:].strip()
            elif heading and line.startswith("Evidence for"):
                for target in LINK.findall(line):
                    doc = os.path.normpath(card.parent / target.split("#")[0])
                    lectures.setdefault(doc, heading)
    return courses, lectures


def catalog(root=None):
    root = Path(root or corpus_root())
    index = root / SKILLS / "flint" / "index.json"
    try:
        stamp = index.stat().st_mtime
    except OSError:
        stamp = 0
    return _catalog(str(root), stamp)


def _absolute_links(text, base_dir):
    def fix(m):
        target, _, anchor = m.group(1).partition("#")
        p = os.path.normpath(os.path.join(base_dir, target))
        return f"]({p}{'#' + anchor if anchor else ''})"
    return LINK.sub(fix, text)


def label(rel, heading, root=None):
    """Course, lecture and page for a hit, as far as the collection says."""
    root = Path(root or corpus_root())
    courses, lectures = catalog(root)
    parts = Path(rel).parts
    slug = parts[2] if len(parts) > 2 and tuple(parts[:2]) == SKILLS.parts else None
    c = courses.get(slug) or {}
    course = (f"{c.get('number', '')} {c.get('title', '')} ({c.get('term', '')})".strip()
              if c else (slug or ""))
    if slug == "flint" and "cards" in parts:
        c = courses.get(Path(rel).stem) or {}
        course = f"{c.get('number', '')} {c.get('title', '')} ({c.get('term', '')})".strip() if c else Path(rel).stem
        slug = Path(rel).stem
    lecture = None
    if kind_of(rel) == "card":
        lecture = heading.split(" › ")[-1] if heading else None
    else:
        lecture = lectures.get(os.path.normpath(root / rel))
    m = PAGE.search(heading or "")
    title = (heading or "").split(" › ")[0].strip() if " › " in (heading or "") else ""
    document = title if title and not lecture and not GENERIC_TITLES.match(title) else None
    accuracy = root / SKILLS / slug / "references" / "accuracy.md" if slug else None
    return {"course": course, "lecture": lecture, "document": document,
            "page": int(m.group(1)) if m else None,
            "accuracy": str(accuracy) if accuracy and accuracy.is_file() else None}


def _candidates(con, fq, limit, cards_only=False):
    sql = ("SELECT path, heading, body, bm25(chunks) AS score FROM chunks WHERE chunks MATCH ? "
           + ("AND path LIKE '%/flint/cards/%' " if cards_only else "")
           + "ORDER BY score LIMIT ?")
    return con.execute(sql, (fq, limit)).fetchall()


def search(query, k=5, db=None, root=None, max_chars=1400):
    """Ranked, labelled hits. Scores are BM25 (more negative is better)."""
    db, root = Path(db or default_db()).expanduser(), Path(root or corpus_root()).expanduser()
    fq = corpus_index._fts_query(query or "")
    if not fq or not db.is_file():
        return []
    k = max(1, min(int(k), 10))
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = _candidates(con, fq, 60)
        cards = _candidates(con, fq, 3, cards_only=True)
    except sqlite3.Error:
        return []
    finally:
        con.close()
    best = min((r[3] for r in rows), default=0.0)
    seen, picked, spill = set(), [], []

    def key(r):
        return (r[0], r[1]), re.sub(r"\s+", " ", r[2][:240])

    def take(r, bucket):
        a, b = key(r)
        if a in seen or b in seen:
            return False
        seen.update((a, b))
        bucket.append(r)
        return True

    # A card only leads when it is a real match, not one shared common word.
    for r in cards:
        if len(picked) < (1 if k <= 3 else 2) and best and r[3] <= CARD_CUTOFF * best:
            take(r, picked)
    for r in rows:
        if len(picked) >= k:
            break
        if kind_of(r[0]) == "export":
            take(r, spill)
        elif kind_of(r[0]) != "card":
            take(r, picked)
    picked += spill[:max(0, k - len(picked))]

    out = []
    for rel, heading, body, score in picked[:k]:
        path = root / rel
        # An index built from another root stores paths relative to that root; keep those as-is.
        shown = str(path) if path.exists() else rel
        text = _absolute_links(body, str(path.parent)) if path.exists() else body
        out.append({"path": shown, "rel": rel, "heading": heading, "kind": kind_of(rel),
                    "score": round(score, 3), "text": text[:max_chars], **label(rel, heading, root)})
    return out


def describe(hit):
    bits = [b for b in (hit.get("course"), hit.get("lecture") or hit.get("document")) if b]
    if hit.get("page"):
        bits.append(f"p. {hit['page']}")
    bits.append({"card": "lecture card", "source": "source page", "export": "OCW site page",
                 "reference": "course reference", "index": "collection index",
                 "notes": "study note"}.get(hit.get("kind"), hit.get("kind", "")))
    return " · ".join(bits)


def format_hits(hits, max_chars=1400, header=HEADER):
    if not hits:
        return ""
    out = [header] if header else []
    for i, h in enumerate(hits, 1):
        out.append(f"\n[{i}] {describe(h)}\nfile: {h['path']}"
                   + (f"\naccuracy notes: {h['accuracy']}" if h.get("accuracy") else "")
                   + f"\n{h['text'][:max_chars]}")
    return "\n".join(out)


def study(query, k=5, db=None, root=None, max_chars=1400, require_card=False):
    """Formatted hits for a prompt or the study tool; empty string when nothing matched.
    require_card: also empty unless a lecture card matched. Use it for excerpts injected into
    prompts unasked, where tangential pages only distract a small model."""
    hits = search(query, k, db, root, max_chars)
    if require_card and not any(h["kind"] == "card" for h in hits):
        return ""
    return format_hits(hits, max_chars)


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query")
    ap.add_argument("-k", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    hits = search(a.query, a.k)
    print(json.dumps(hits, indent=2) if a.json else (format_hits(hits) or "no hits"))


if __name__ == "__main__":
    main()
