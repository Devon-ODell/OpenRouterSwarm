"""Verification helpers: weakened-test detection and failure output reading.

Extracted from swarmd (P2), partial — the gate runners themselves (run_gate,
baseline cache, provisioning hooks) stay in swarmd until P7. Pure functions of
their parameters; no module reads swarmd globals.
"""
import re
from pathlib import Path

ASSERT_CALL = re.compile(
    r"\b(assert\w*(?:\.\w+)*|expect|require|should\w*|t\.(?:Error|Fatal)f?)\b")
# Directory names too common to identify anything: "games" is not what "games/reversi" means.
GENERIC_DIR = frozenset({"", ".", "/", "src", "lib", "app", "test", "tests", "spec", "specs",
                         "games", "site", "docs", "scripts", "static", "public", "assets"})


def assert_counts(lines):
    """How many times each assertion function is called across these lines."""
    counts = {}
    for line in lines:
        for m in ASSERT_CALL.finditer(line):
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts


def identifiers(path):
    """The names that stand for this path in code: the path, its directory, and their names."""
    p = Path(path)
    out = {str(path), p.name, p.stem}
    parent = p.parent
    if str(parent) not in GENERIC_DIR:
        out |= {str(parent), parent.name}
    return {x for x in out if x and x not in GENERIC_DIR}


def deleted_paths(diff):
    """Every name that identifies something this diff deletes."""
    out, pending = set(), None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            pending = line.split(" b/", 1)[-1].strip()
        elif line.startswith("deleted file mode") and pending:
            out |= identifiers(pending)
            pending = None
    return out


def test_hunks(diff):
    """[(path, deleted, [hunk lines])] for the test files this diff touches."""
    files, path, deleted, is_test, hunks = [], None, False, False, None

    def close():
        if is_test and path:
            files.append((path, deleted, hunks))
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            close()
            path = line.split(" b/", 1)[-1].strip()
            low = path.lower()
            is_test = any(m in low for m in ("test", "spec_", "_spec"))
            deleted, hunks = False, []
        elif line.startswith("deleted file mode"):
            deleted = True
        elif line.startswith("@@") and is_test:
            hunks.append([])
        elif is_test and hunks and line[:1] in ("+", "-", " "):
            hunks[-1].append(line)
    close()
    return files


def names_deleted(line, gone):
    """True when this line refers to something the diff deletes.

    A test that loads games/star-catcher/game.js writes it as
    path.join(__dirname, '..', 'games', 'star-catcher', 'game.js'), so the slug is what can be
    matched, not the joined path."""
    return any(re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", line) for name in gone)


def names_its_source(test_path, gone):
    """True when a deleted test file is named after something else the diff deletes.

    tests/test_reversi.py deleted alongside games/reversi/ is one removal, not a weakening;
    tests/test_core.py deleted while the code stays is the clearest weakening there is."""
    stem = Path(test_path).stem.lower()
    return any(len(name) >= 3 and name.lower() in stem for name in gone)


def weakened_tests(diff, allow_test_changes=False):
    """Assertions this diff took out of test files that are still meant to exist.

    The old check counted every removed line containing "assert" under any path containing
    "test". All four hits across 189 studio attempts were legitimate, and this check carries the
    heaviest penalty in the ledger (weight 6) and hangs the model on the rafters, so a false
    positive punishes a model for doing what the owner asked:

      - two attempts deleted games/reversi/ and games/star-catcher/ and the assertions that
        loaded them, which is what "remove star catcher and reversi" means;
      - one was the owner's own removal request, again;
      - one changed `assert.equal(b[112], 3)` to `assert.equal(b[80], 3)` — an assertion edited
        to match an 8x15 board, not an assertion removed.

    So: count per assertion function, treat a removal paired with an addition of the same
    function in the same hunk as an edit, skip hunks that remove code for files the diff deletes,
    and skip a test file deleted alongside the source it tested.
    """
    if allow_test_changes:
        return 0                       # the owner authorised this; the adversary is told so
    total, all_gone = 0, deleted_paths(diff)
    for path, deleted, hunks in test_hunks(diff):
        # A file cannot be its own deleted source, or every deleted test file would excuse itself.
        gone = all_gone - identifiers(path)
        if deleted:
            if names_its_source(path, gone):
                continue               # the test went with the code it tested
            # A test file deleted while the code it tests stays is the plainest weakening there
            # is, so every assertion it took with it counts.
            for lines in hunks:
                total += sum(assert_counts([l[1:] for l in lines if l.startswith("-")]).values())
            continue
        for lines in hunks:
            minus = [l[1:] for l in lines if l.startswith("-")]
            plus = [l[1:] for l in lines if l.startswith("+")]
            if gone and any(names_deleted(l, gone) for l in minus):
                continue               # this hunk removes the use of something that is gone
            back = assert_counts(plus)
            for name, n in assert_counts(minus).items():
                total += max(0, n - back.get(name, 0))
    return total


def last_error(output):
    lines = [l.strip() for l in output.splitlines() if l.strip()]
    for l in reversed(lines):
        if re.search(r"error|fail|assert|exception|panic", l, re.I):
            return l[:200]
    return lines[-1][:200] if lines else "no output"


