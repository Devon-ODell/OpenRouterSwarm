"""Contracts, review validation and durable evidence for recursive swarm work.

Model observations are recorded as claims. Test exits and Git tree/commit IDs
are recorded by the supervisor. Neither a review nor a reward proves correctness.
"""
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import time


STRATEGIES = {
    "regression_first": "Reproduce the problem with a focused regression test. Make the smallest fix, then run the complete configured checks.",
    "small_vertical_slice": "Implement one end-to-end slice of the acceptance criteria, verify it, and keep the diff narrowly scoped.",
    "invariants_first": "Identify boundary conditions and invariants. Add checks for them before changing the algorithm or data flow.",
}


def criteria(task):
    rows = task.get("acceptance") or [task.get("detail") or task["title"]]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 12:
        raise ValueError("acceptance must contain 1–12 concrete criteria")
    if any(not isinstance(row, str) or not row.strip() or len(row) > 2000 for row in rows):
        raise ValueError("each acceptance criterion must be nonempty text under 2000 characters")
    return [{"id": f"C{i + 1}", "text": row.strip()} for i, row in enumerate(rows)]


def contract(task, goal, base, strategy):
    return {"task": task["id"], "title": task["title"], "goal": goal,
            "base_commit": base, "acceptance": criteria(task), "strategy": strategy,
            "parent": task.get("parent"), "root": task.get("root", task["id"]),
            "depth": task.get("depth", 0), "depends_on": task.get("depends_on", [])}


def parse_review(text, tree, acceptance):
    """A malformed, stale or unsupported approval is never usable."""
    try:
        review = json.loads(text.strip())
    except (ValueError, TypeError) as exc:
        raise ValueError("review must be a JSON object without prose/fences") from exc
    if not isinstance(review, dict) or review.get("verdict") not in ("approve", "request_changes"):
        raise ValueError("review verdict must be approve or request_changes")
    if review.get("tree") != tree:
        raise ValueError("review refers to a different Git tree")
    if not isinstance(review.get("summary"), str) or not review["summary"].strip():
        raise ValueError("review needs a summary")
    checks, findings = review.get("checks"), review.get("findings")
    if not isinstance(checks, list) or not isinstance(findings, list):
        raise ValueError("review needs checks and findings lists")
    expected = {row["id"] for row in acceptance}
    if len(checks) != len(expected):
        raise ValueError("review must check every acceptance criterion once")
    seen = set()
    for row in checks:
        if (not isinstance(row, dict) or row.get("criterion") not in expected
                or row["criterion"] in seen or type(row.get("passed")) is not bool
                or not isinstance(row.get("evidence"), str) or not row["evidence"].strip()):
            raise ValueError("invalid, duplicate or unevidenced acceptance check")
        seen.add(row["criterion"])
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("severity") not in ("blocker", "major", "minor"):
            raise ValueError("each finding needs a severity")
        for field in ("path", "issue", "verification"):
            if not isinstance(finding.get(field), str) or not finding[field].strip():
                raise ValueError(f"each finding needs {field}")
        path = Path(finding["path"])
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("finding paths must stay relative to the repository")
        if type(finding.get("line")) is not int or finding["line"] < 1:
            raise ValueError("finding line must be a positive integer")
    blockers = any(f["severity"] in ("blocker", "major") for f in findings)
    if review["verdict"] == "approve" and (blockers or any(not row["passed"] for row in checks)):
        raise ValueError("approval contradicts its findings or acceptance checks")
    if review["verdict"] == "request_changes" and not findings and all(row["passed"] for row in checks):
        raise ValueError("request_changes must identify a finding or failed criterion")
    return review


def failure_signature(stage, details):
    # Stable enough to detect repeating the same failed approach after a repair.
    text = re.sub(r"\b[0-9a-f]{7,40}\b|\b\d+(?:\.\d+)?s\b", "<value>", details.lower())
    return hashlib.sha256(f"{stage}:{text}".encode()).hexdigest()[:16]


class Attempt:
    """Single-writer artifact bundle. It survives worktree cleanup and restarts."""
    def __init__(self, root, name, task, worktree, branch):
        self.path = Path(root) / "attempts" / name
        self.path.mkdir(parents=True, exist_ok=True)
        self.data = {"schema": 1, "id": name, "task": task, "worktree": str(worktree),
                     "branch": branch, "started": time.time(), "phase": "created",
                     "role_calls": 0, "repairs": 0, "events": []}
        self.record("created")

    def write(self, name, value):
        path = self.path / name
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(value if isinstance(value, str) else json.dumps(value, indent=2))
        temp.replace(path)
        return str(path)

    def record(self, phase, **data):
        event = {"time": time.time(), "phase": phase, **data}
        self.data.update(data, phase=phase)
        self.data["events"].append(event)
        self.write("attempt.json", self.data)

    def finish(self, stage, note):
        self.record(stage, note=note, finished=time.time(),
                    seconds=round(time.time() - self.data["started"], 3))
        task = self.data["task"]
        self.write("HANDOFF.md", "\n".join([
            f"# {task['title']}", "", f"Outcome: {stage}",
            f"Branch: {self.data['branch']}", f"Worktree: {self.data['worktree']}",
            f"Commit: {self.data.get('commit', '(not integrated)')}",
            f"Strategy: {self.data.get('strategy', 'none')}",
            f"Role calls: {self.data['role_calls']}; repairs: {self.data['repairs']}",
            "", "## Result / next action", "", note,
            "", "## Evidence", "", "See attempt.json, contract.json, review-*.json, and gate-*.log.",
            "Reviewer observations are claims; gate logs and Git IDs are supervisor observations.",
        ]) + "\n")


REVIEW = """You are an independent code reviewer. Inspect files; do not modify them.
Assess the change against the contract and the actual test evidence. Read full
files when the diff is truncated. Do not approve solely because tests passed.
Find concrete defects and explain how the implementer can reproduce each one.

CONTRACT
{contract}

GIT TREE TO REVIEW: {tree}

SUPERVISOR TEST EVIDENCE
{tests}

DIFF
{diff}

Output ONLY a JSON object with this exact structure. Include one check per
criterion ID, naming the relevant test/file and reasoning in evidence. Do not
claim you ran commands; the supplied gate records say what actually ran.
{{"verdict":"approve|request_changes", "tree":"{tree}", "summary":"...",
"checks":[{{"criterion":"C1","passed":true,"evidence":"..."}}],
"findings":[{{"severity":"blocker|major|minor","path":"relative/file.py",
"line":1,"issue":"concrete defect","verification":"input/test and expected result"}}],
"lesson":"optional, specific advice supported by this change",
"follow_ups":[{{"title":"...","detail":"why this advances the same goal",
"acceptance":["concrete check"]}}]}}
Use an empty findings list when there are none. Blockers/major findings or failed
criteria require request_changes. Suggestions for future features are not blockers.
"""


REPAIR = """Continue the existing implementation in this worktree; preserve prior work.
Fix the observed failures below. Add a regression test for each actual defect,
then run the configured checks. Do not weaken tests or commit/switch/reset Git.
If this failure repeats, change your approach based on evidence.

CONTRACT
{contract}

FAILURE / REVIEW HANDOFF
{failure}

VERIFICATION COMMAND: {test_cmd}

Finish with changes made, commands/results and remaining issues. Stay within
the original acceptance criteria; do not expand the task to fill the budget.
"""
