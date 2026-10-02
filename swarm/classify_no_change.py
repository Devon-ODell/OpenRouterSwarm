#!/usr/bin/env python3
"""A1.1: classify why `no_change` attempts produced no diff.

The swarm code's own taxonomy (swarmd.py count_edits docstring): a model that
called edit_file yet changed nothing "got it wrong"; a model that never reached
an edit "ran out of the rounds this harness gave it". Only the first is the
model's failure.

Primary evidence is the retained `response-<n>-<role>.txt` file(s) in each
attempt dir (the model's actual output). Buckets:

  attempted_edit   response contains edit_file/tool_call/diff markers but the
                   diff still came back empty -> most expensive failure, model
                   or tool-applier got it wrong (A1.3 missing diff contract)
  no_model_output  no response file retained -> harness/model never returned
                   text (exhaustion, timeout, or infra)
  prose            response is explanation only, no patch markers (A1.3)
  refused          response contains refusal / incapability language
  reported_no_diff response explicitly said NO_DIFF...
  technical        attempt.json shows infra/model_error/PermissionError
  unresolved       could not be classified from retained files

Usage:
    python3 swarm/classify_no_change.py <attempts_dir> [journal.jsonl]
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

REFUSED_RE = re.compile(
    r"\b(cannot|can't|unable|unwilling|not able to|refus|won't|"
    r"outside my (scope|abilities|bounded)|not appropriate|not (in )?my (role|job)|"
    r"can't comply)\b", re.I)
EDIT_RE = re.compile(
    r"<function=edit_file>|<tool_call>|```diff|^diff --git|^--- a/|^\+{3} b/|"
    r"<edit|apply_patch|patch|old_str|new_str", re.I)
NO_DIFF_RE = re.compile(r"NO_DIFF\s*:?\s*(\S.{0,120})", re.I)


def classify_dir(d: Path):
    aid = d.name
    try:
        att = json.loads((d / "attempt.json").read_text())
    except Exception as e:
        return (aid, "unreadable", str(e))

    # technical: explicit infra / model error markers in the attempt
    try:
        phases = [str(ev.get("phase")) for ev in att.get("events", [])]
    except Exception:
        phases = []
    if any(p in phases for p in ("gate_error", "infra", "sandbox", "model_error")):
        return (aid, "technical", "infra/model_error phase in events")

    resp = ""
    resp_paths = sorted(d.glob("response-*.txt"))
    if resp_paths:
        try:
            resp = "\n".join(p.read_text(errors="replace") for p in resp_paths)
        except Exception:
            resp = ""

    if not resp_paths:
        return (aid, "no_model_output",
                f"no response file retained; events={phases[-2:]}")
    if NO_DIFF_RE.search(resp):
        return (aid, "reported_no_diff", NO_DIFF_RE.search(resp).group(1)[:110])
    if EDIT_RE.search(resp):
        return (aid, "attempted_edit",
                excerpt(resp, EDIT_RE))
    if REFUSED_RE.search(resp):
        return (aid, "refused", excerpt(resp, REFUSED_RE))
    # prose-only fallback: it returned words but no edits
    return (aid, "prose", (resp[:140].replace("\n", " ") or "(empty response)"))


def excerpt(text, regex):
    m = regex.search(text)
    if not m:
        return ""
    s = max(0, m.start() - 40)
    return text[s:m.end() + 60].replace("\n", " ")[:140]


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    attempts_dir = Path(argv[0])
    journal_path = argv[1] if len(argv) > 1 else None

    wanted = None
    if journal_path:
        wanted = set()
        for line in open(journal_path):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("event") == "attempt" and r.get("stage") == "no_change":
                wanted.add(r["id"])

    results = []
    for d in sorted(attempts_dir.iterdir()):
        if not d.is_dir() or not d.name.startswith("t"):
            continue
        if wanted is not None and d.name.split("-")[0] not in wanted:
            continue
        try:
            att = json.loads((d / "attempt.json").read_text())
        except Exception:
            continue
        if att.get("phase") != "no_change":
            continue
        results.append((d.name, *classify_dir(d)[1:]))

    c = Counter(b for _, b, _ in results)
    print(f"classified {len(results)} `no_change` attempt dirs "
          f"(tasks: {len(wanted) if wanted else 'all'})\n")
    total = len(results)
    for b, n in c.most_common():
        print(f"  {b:<18} {n:>4}  {100.0*n/total:5.1f}%")
    if results:
        print()
        print("examples:")
        shown = set()
        for name, bucket, ev in results:
            if bucket in shown:
                continue
            shown.add(bucket)
            print(f"  {name}: {bucket} | {ev[:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))