"""Context building: named paths, outlines, context packs, prior-attempt summaries.

Extracted from swarmd (P2). No module-level global reads: the state directory,
the queue's done-file reader and the git subprocess all arrive as parameters
(attempts_dir, read_done, git_run). Cross-module policy (failure_class,
first_failing_line) comes from task_rules, which has no repository imports.
"""
import datetime as dt
import json
import re
import subprocess
from pathlib import Path


def _jsonl_rows(path):
    """Rows of a JSONL file, as dicts (default reader for kin_text)."""
    try:
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    except (OSError, json.JSONDecodeError):
        return []


def _default_git_run(args, cwd):
    """Invoke git exactly as the daemon git() does (no hooks, no fsmonitor), returning (rc, out)."""
    cmd = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args]
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=180)
    return r.returncode, r.stdout.strip()


# ------------------------------------------------------------------ earlier attempts

# The one line of a failure that says what to do differently.
FAILING = re.compile(r"^(?:FAIL|ERROR):.*|^E\s{2,}.*|^\s*(?:Assertion|Type|Value|Name|Attribute|"
                     r"Index|Key|Import|Module|Syntax|Runtime|OS|IO)Error\b.*|^\s*assert\s.*",
                     re.M)


from task_rules import first_failing_line as _first_failing_line  # noqa: E402
from task_rules import failure_class as _failure_class  # noqa: E402


def attempt_lines(task, attempts_dir, limit=3, max_chars=1_200):
    """What earlier attempts at this task actually hit, one line each.

    This was `json.dumps(sorted(prior)[-3:])[-6000:]`: a JSON dump cut off mid-string, made
    mostly of absolute paths the model cannot open, at the point in the prompt where the model
    is most likely to be reading."""
    relevant = {task["id"], task.get("parent"), *(task.get("depends_on") or [])}
    rows = []
    for path in (attempts_dir / "attempts").glob("*/attempt.json"):
        try:
            row = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if row.get("task", {}).get("id") in relevant and row.get("finished"):
            rows.append(row)
    out = []
    for row in sorted(rows, key=lambda r: r["finished"])[-limit:]:
        when = dt.datetime.fromtimestamp(row["finished"]).strftime("%m-%d %H:%M")
        stage = row.get("phase") or "unknown"
        model = row.get("implementer") or row.get("active_model") or "?"
        note = re.sub(r"\s+", " ", str(row.get("note") or "")).strip()
        # Paths and evidence directories are noise here; the failing line is the signal.
        note = re.sub(r"(?:; )?evidence: \S+", "", note).strip(" ;")
        failing = _first_failing_line(row.get("note") or "")
        line = f"- {when} {stage} on {model}: {_failure_class(stage, note)}"
        if note and note != stage:
            line += f" — {note[:160]}"
        if failing:
            line += f" ({failing})"
        out.append(line)
    text = "\n".join(out)
    return text[:max_chars]


# ------------------------------------------------------------------ context pack

# Paths a task names in its own words. Anything outside this set is a guess about what matters,
# and a guess is what filled these prompts with a lecture on parking-garage real options.
CODE_PATH = re.compile(r"[\w][\w./-]*\.(?:py|js|cjs|mjs|jsx|ts|tsx|json|md|html|css|rs|toml)\b")
GAME_DIR = re.compile(r"\b(games/([\w.-]+))/")
# Definitions worth an outline when a file is too long to show: Python and JavaScript
# functions, classes, and the consts a game's module keeps its tables in.
DEFINITION = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function)\s+\w+"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+\w+\s*="
    r"|^\s*\w+\s*[:=]\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>)")


def numbered(text, first=1):
    """Lines in read_file's exact format, so an edit_file old_str can be copied out of them."""
    return "\n".join(f"{i:>5}\t{line}" for i, line in enumerate(text.splitlines(), first))


def outline(text):
    """Definitions with their line numbers, for a file too long to show whole.

    A signature is what locates the code; the rest of a 200-character line is body, and an
    outline that repeats it is as big as the file it stands in for."""
    return "\n".join(f"{i:>5}\t{line.rstrip()[:120]}"
                     for i, line in enumerate(text.splitlines(), 1) if DEFINITION.match(line))


def task_text(task):
    rows = task.get("acceptance") or []
    return " ".join([str(task.get("title") or ""), str(task.get("detail") or "")]
                    + [r if isinstance(r, str) else str(r.get("text", "")) for r in rows])


def named_paths(task, also=""):
    """The file paths the task names, in the order it names them, deduplicated."""
    text = task_text(task) + " " + (also or "")
    return list(dict.fromkeys([*(task.get("allowed_paths") or []),
                               *(m.group(0).rstrip(".") for m in CODE_PATH.finditer(text))]))


def kin_text(task, q, read_done=None):
    """The words a task inherits from the task it was split out of.

    44 of 121 finished tasks were splits, and a decomposer writes subtasks in terms of behaviour:
    "Add parked vehicle data structure and rendering" names no file, while the task it came out
    of names games/crosstown/game.js. Without this the splits — the ones already failing — are
    exactly the ones that get no code."""
    out = []
    for tid in dict.fromkeys(x for x in (task.get("parent"), task.get("root")) if x and x != task["id"]):
        if read_done is None:
            read_done = _jsonl_rows
        row = q.get(tid) or next((r for r in read_done(q.done) if r.get("id") == tid), None)
        if row:
            out.append(task_text(row))
    return " ".join(out)


def covering_tests(wd, terms, limit=2, git_run=None):
    """Test files that mention what the task is about, first `limit` of them."""
    found = []
    for term in terms:
        if len(found) >= limit or not term:
            break
        if git_run is None:
            git_run = _default_git_run
        rc, out = git_run(["grep", "-l", "-F", "-i", "--", term, "tests"], cwd=wd)
        if rc == 0:
            for path in out.splitlines():
                if path not in found:
                    found.append(path)
    return found[:limit]


def context_pack(task, wd, limit=20_000, also="", git_run=None):
    """The code this task is about, in the prompt, in read_file's own format.

    The implementer prompt carried no code at all: 16.6 KB of goal, rules and unrelated
    excerpts, and not one line of the files the task names. A model with 12 tool rounds spent 6
    to 11 of them reading before it could edit anything, and 60 of 189 attempts ran out of
    rounds before the first edit. Everything here comes from paths the task itself names, or
    the task it was split out of, so there is nothing to guess and nothing to pad.
    """
    wd, parts, used = Path(wd), [], 0
    text = task_text(task) + " " + (also or "")
    # The last two sections are small and carry the most per character — which test module to
    # run, and what else is in the directory — so a long file cannot crowd them out.
    reserve = min(2_500, limit // 4)

    def add(header, body, ceiling=None):
        nonlocal used
        # Only blank lines come off: a line's leading spaces are its line number's padding, and
        # read_file's format is the whole point — an old_str is copied out of it byte for byte.
        body = (body or "").strip("\n")
        ceiling = limit if ceiling is None else ceiling
        if not body.strip() or used >= ceiling:
            return False
        room = ceiling - used
        if len(body) > room:
            body = body[:room] + f"\n… [{len(body) - room} chars not shown; read_file for the rest]"
        parts.append(f"{header}\n{body}")
        used += len(body) + len(header) + 2
        return True

    def show(rel, max_lines=400, head=None):
        f = wd / rel
        if not f.is_file():
            return
        try:
            body = f.read_text(errors="replace")
        except OSError:
            return
        lines, ceiling = body.splitlines(), limit - reserve
        if head:
            add(f"--- {rel} (first {min(head, len(lines))} of {len(lines)} lines) ---",
                numbered("\n".join(lines[:head])), ceiling)
            return
        whole = numbered(body)
        # A game's logic runs to 300 lines of 200 characters, so the line count alone is not a
        # size. A file that will not fit gets its definitions, which is still navigable — a
        # file cut off halfway is not.
        if len(lines) <= max_lines and len(whole) <= max(0, ceiling - used):
            add(f"--- {rel} ({len(lines)} lines) ---", whole, ceiling)
        else:
            add(f"--- {rel} ({len(lines)} lines; definitions only, read_file for the rest) ---",
                outline(body), ceiling)

    paths = named_paths(task, also)
    seen = set()
    for rel in paths:
        if (wd / rel).is_file() and rel not in seen:
            seen.add(rel)
            show(rel)
    # A game directory is a unit: what it declares, what its notes say, and its logic.
    for whole, slug in dict.fromkeys(GAME_DIR.findall(text)):
        for rel, kw in ((f"{whole}/manifest.json", {}), (f"{whole}/NOTES.md", {"head": 60}),
                        (f"{whole}/game.js", {})):
            if rel not in seen:
                seen.add(rel)
                show(rel, **kw)
    terms = [slug for _, slug in GAME_DIR.findall(text)] + [Path(rel).stem for rel in paths]
    for rel in covering_tests(wd, list(dict.fromkeys(terms)), git_run=git_run):
        if rel in seen:
            continue
        seen.add(rel)
        body = (wd / rel).read_text(errors="replace") if (wd / rel).is_file() else ""
        tests = "\n".join(f"{i:>5}\t{line.rstrip()}" for i, line in enumerate(body.splitlines(), 1)
                          if re.match(r"\s*(?:def test_|it\(|test\()", line))
        add(f"--- {rel} (the tests in it; run this module, not the whole suite) ---", tests)
    for d in dict.fromkeys(str(Path(rel).parent) for rel in paths if "/" in rel):
        if git_run is None:
            git_run = _default_git_run
        rc, out = git_run(["ls-files", "--", d], cwd=wd)
        if rc == 0 and out:
            add(f"--- files in {d} ---", "\n".join(out.splitlines()[:60]))
    if not parts:
        return ""
    return ("CODE THIS TASK TOUCHES (already read for you; line numbers match read_file, so an\n"
            "old_str can be copied straight out of it)\n\n" + "\n\n".join(parts))
