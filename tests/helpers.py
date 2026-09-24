"""Shared fixtures for the offline test suites."""
import json
import re


def review(prompt, verdict="approve", issue="off-by-one at the last index"):
    """A well-formed structured review of the tree named in the reviewer prompt.

    parse_review() rejects prose, stale trees and unevidenced checks, so tests
    that need a review to be *accepted* must build one from the prompt they were
    actually given rather than hardcoding a verdict string.
    """
    tree = re.search(r"GIT TREE TO REVIEW: (\w+)", prompt).group(1)
    ids = dict.fromkeys(re.findall(r'"id": "(C\d+)"', prompt))
    approve = verdict == "approve"
    return json.dumps({"verdict": verdict, "tree": tree, "summary": "reviewed",
                       "checks": [{"criterion": i, "passed": approve, "evidence": "tests"} for i in ids],
                       "findings": [] if approve else [{"severity": "blocker", "path": "app.txt", "line": 1,
                                                        "issue": issue, "verification": "page 10 of 10"}]})
