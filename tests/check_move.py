#!/usr/bin/env python3
"""Check that a refactor moved code verbatim, and that line-count targets were not gamed.

Used on every slice of a "move this code into a new module" packet (DECOMPOSE-SWARMD.md),
because a green suite does not prove a move was faithful: a P9c slice once split the last line of
one method onto the end of the next and every test still passed.

    # functions moved out of swarm/swarmd.py at HEAD into swarm/reporting.py, where the
    # globals STATE and _read became the injected parameters state and read:
    .venv/bin/python tests/check_move.py swarm/swarmd.py swarm/reporting.py \\
        write_now run_status --rename STATE=state --rename _read=read

    # methods of class Worker moved into class AttemptRunner, `self` -> `w`, globals -> `d.x`:
    .venv/bin/python tests/check_move.py swarm/swarmd.py swarm/attempt_runner.py \\
        judge hang --old-class Worker --new-class AttemptRunner --self w --deps log,git

Bodies are compared without docstrings and with whitespace collapsed, after applying the declared
renames outside string literals (inside f-string braces too). Keyword-argument names on the left
of `=` are never renamed. Any remaining difference is printed: either a slip, or an undeclared
rename to account for. Exit status 1 on any difference or formatting finding.

`--ref` (default HEAD) is the git revision the original file is read from.
"""
import argparse
import ast
import difflib
import re
import subprocess
import sys
from pathlib import Path

STRING_OR_COMMENT = re.compile(r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|#.*$)")


def node(src, name, cls=None):
    tree = ast.parse(src)
    scope = tree.body
    if cls:
        found = [n for n in scope if isinstance(n, ast.ClassDef) and n.name == cls]
        if not found:
            return None
        scope = found[0].body
    for n in scope:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name:
            return n
        if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == name for t in n.targets):
            return n
    return None


def body_lines(src, n):
    lines = src.split("\n")          # not splitlines(): ast counts only \n as a line break
    if isinstance(n, ast.Assign):
        start = n.lineno - 1
    else:
        first = n.body[0]
        doc = isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
            and isinstance(first.value.value, str)
        start = first.end_lineno if doc else first.lineno - 1
    return [re.sub(r"\s+", " ", l).strip() for l in lines[start:n.end_lineno] if l.strip()]


def renamer(renames, self_name):
    if self_name:
        renames = {**renames, "self": self_name}
    if not renames:
        return lambda line: line
    names = "|".join(map(re.escape, sorted(renames, key=len, reverse=True)))
    bare = re.compile(rf"(?<![\w.])({names})\b(?!\s*=(?!=))")

    def code(part):
        return bare.sub(lambda m: renames[m.group(1)], part)

    def line(text):
        out = []
        for part in STRING_OR_COMMENT.split(text):
            if part.startswith("#"):
                out.append(part)
            elif part.startswith(("\"", "'")):
                out.append(re.sub(r"\{([^{}]*)\}", lambda m: "{" + code(m.group(1)) + "}", part))
            else:
                out.append(code(part))
        return "".join(out)
    return line


def formatting_findings(path):
    """Statements packed onto one line, and top-level definitions with no blank line before."""
    src = Path(path).read_text()
    tree = ast.parse(src)
    out = []
    seen = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.stmt) and hasattr(n, "lineno"):
            for body in ("body", "orelse", "finalbody"):
                stmts = getattr(n, body, None)
                if isinstance(stmts, list):
                    for a, b in zip(stmts, stmts[1:]):
                        if isinstance(a, ast.stmt) and isinstance(b, ast.stmt) and a.end_lineno == b.lineno:
                            seen[b.lineno] = f"{path}:{b.lineno}: two statements on one line"
    for a, b in zip(tree.body, tree.body[1:]):
        if a.end_lineno == b.lineno:
            seen[b.lineno] = f"{path}:{b.lineno}: two statements on one line"
    out.extend(seen.values())
    lines = src.split("\n")
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            first = min([n.lineno] + [d.lineno for d in n.decorator_list])
            if first > 2 and lines[first - 2].strip() and not lines[first - 2].lstrip().startswith("#"):
                out.append(f"{path}:{first}: top-level `{n.name}` has no blank line before it")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("old_file", help="where the code lived, read at --ref")
    ap.add_argument("new_file", help="where the code lives now (working tree)")
    ap.add_argument("names", nargs="+")
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--old-class")
    ap.add_argument("--new-class")
    ap.add_argument("--self", dest="self_name", help="what `self` became in the moved body, e.g. w")
    ap.add_argument("--rename", action="append", default=[], metavar="OLD=NEW")
    ap.add_argument("--deps", default="", help="comma list of globals that became d.<name>")
    a = ap.parse_args(argv)

    old_src = subprocess.run(["git", "show", f"{a.ref}:{a.old_file}"], capture_output=True,
                             text=True, check=True).stdout
    new_src = Path(a.new_file).read_text()
    renames = dict(r.split("=", 1) for r in a.rename)
    renames.update({d: f"d.{d}" for d in a.deps.split(",") if d})
    fix = renamer(renames, a.self_name)
    bad = 0
    for name in a.names:
        o, n = node(old_src, name, a.old_class), node(new_src, name, a.new_class)
        if o is None or n is None:
            print(f"== {name}: MISSING in {'old' if o is None else 'new'} file")
            bad += 1
            continue
        old = [fix(l) for l in body_lines(old_src, o)]
        new = [l for l in body_lines(new_src, n) if l != f"{a.self_name}, d = self.{a.self_name}, self.d"]
        diff = list(difflib.unified_diff(old, new, lineterm="", n=0))[2:]
        print(f"== {name}: {'identical' if not diff else 'DIFFERS'}")
        for line in diff:
            print("   " + line)
        bad += bool(diff)
    for finding in formatting_findings(a.new_file):
        print(finding)
        bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
