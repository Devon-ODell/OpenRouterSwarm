"""The attempt state machine: implement, gate, review, repair, integrate, judge, learn.

AttemptRunner works on its Worker's attempt fields and reaches every daemon service
through an injected AttemptDeps; it imports no daemon module and reads no daemon global.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from learn import CRIME, defect, exhibit, parse_scores
from prompts import JUDGE, PRIORITIES
from task_rules import goal_items
from workflow import path_allowed


@dataclass(frozen=True)
class AttemptDeps:
    """The daemon services an attempt uses, read when the runner is built."""
    log: Callable
    journal: Callable
    notify: Callable
    git: Callable
    trunk_name: Callable
    state: Path            # the repository's state directory


class AttemptRunner:
    def __init__(self, worker, deps: AttemptDeps):
        self.w = worker
        self.d = deps

    def judge(self, task, goal, diff, verdict, model, avoid=(), c=None,
              attempt_budget=None):
        w, d = self.w, self.d
        c, attempt_budget = w._attempt_inputs(c, attempt_budget)
        items = goal_items(goal)
        priorities = PRIORITIES.format(
            items="\n".join(f"{i}. {t}" for i, t in enumerate(items, 1))) if items else ""
        try:
            out = w.call("judge", JUDGE.format(
                goal=goal, priorities=priorities, title=task["title"],
                detail=task["detail"], verdict=verdict,
                diff=diff[:c.get("max_diff", 24000)]),
                w.wt, c["steps"].get("judge", 4), model, avoid, c=c,
                attempt_budget=attempt_budget)
        except Exception as e:  # the change already landed; never lose it to a judge failure
            d.log(f"judge unavailable ({type(e).__name__}); accepted work remains unscored", w.name)
            return None
        scores = parse_scores(out)
        if scores is None:
            d.log(f"judge {model} returned no usable JSON; accepted work remains unscored", w.name)
        return scores

    def follow_up(self, task, scores, r, breakthrough, c=None):
        """Recursion on success: strong work spawns the next step it made possible."""
        w, d = self.w, self.d
        c = w._attempt_config(c)
        if c.get("roadmap_file"):
            return  # Future packets are dispatched only by their recorded roadmap gates.
        if not scores or not (breakthrough or r >= 0.75):
            return
        if len(w.q.pending()) >= c.get("max_queue", 20):
            return
        for f in scores.get("follow_ups", [])[:2 if breakthrough else 1]:
            t = w.q.add(f["title"], f["detail"], "feature", persona=task.get("persona"),
                        planner_model=task.get("planner_model"), parent=task["id"],
                        priority=2 if breakthrough else 0, origin="follow_up",
                        depth=task.get("depth", 0))
            if t:
                d.log(f"follow-up queued: {t['title']}", w.name)

    def celebrate(self, task, r, scores, impl, c=None):
        w, d = self.w, self.d
        c = w._attempt_config(c)
        s = scores or {}
        entry = (f"\n## {dt.datetime.now():%Y-%m-%d %H:%M} — {task['title']}\n\n"
                 f"reward {r:.2f} · impact {s.get('impact', '?')} · creativity {s.get('creativity', '?')}"
                 f" · quality {s.get('quality', '?')} · implementer `{impl}`"
                 f" · persona {task.get('persona', 'human')}\n\n{s.get('why', '')}\n\n"
                 f"Lesson: {s.get('lesson', '')}\n")
        with open(d.state / "BREAKTHROUGHS.md", "a") as f:
            f.write(entry)
        d.log(f"★ BREAKTHROUGH: {task['title']} ({impl})", w.name)
        d.notify(c, "breakthrough", task["title"])

    def hang(self, task, stage, info, impl, c=None):
        """Faulty code goes up on the rafters, under its author's name: shown to every
        later implementer and planner, written to RAFTERS.md, and announced."""
        w, d = self.w, self.d
        c = w._attempt_config(c)
        base = getattr(w, "review_base", None) or d.trunk_name(c)
        _, diff = d.git(["diff", "--unified=0", base, w.branch, "--"], cwd=c["repo"])
        what = defect(stage, info.get("artifact"), info.get("pitfall", ""))
        code = exhibit(diff, stage)
        w.ledger.hang(impl, task, stage, what, code, info.get("artifact", ""))
        with open(d.state / "RAFTERS.md", "a") as f:
            f.write(f"\n## {dt.datetime.now():%Y-%m-%d %H:%M} — `{impl}` {CRIME[stage]}\n\n"
                    f"Task: {task['title']}  \nDefect: {what}  \nBranch: `{w.branch}`"
                    + (f"  \nEvidence: {info['artifact']}" if info.get("artifact") else "")
                    + (f"\n\n```diff\n{code}\n```\n" if code else "\n"))
        d.log(f"HUNG FROM THE RAFTERS: {impl} {CRIME[stage]} — {what[:140]}", w.name)
        d.notify(c, "hung from the rafters", f"{impl}: {what[:120]}")

    def strand(self, task, how):
        """Park what depends on a task that can no longer finish, and say so loudly."""
        w, d = self.w, self.d
        titles = w.q.strand(task["id"], f"prerequisite '{task['title'][:80]}' {how}")
        if titles:
            d.log(f"'{task['title'][:60]}' {how}; parked {len(titles)} task(s) that depended on it: "
                  + "; ".join(t[:50] for t in titles), w.name)
            d.journal("stranded", id=task["id"], title=task["title"], how=how, parked=titles)
        return titles

    def retry_split(self, task, goal, model, avoid, why, c=None, attempt_budget=None):
        """One more decomposition on a different model. Returns 0 once that has been tried."""
        w, d = self.w, self.d
        if avoid:
            d.log(f"'{task['title']}' was not split: {why} from {model} as well", w.name)
            d.journal("split_failed", id=task["id"], title=task["title"], why=why,
                      models=[*avoid, model])
            return 0
        d.log(f"decomposer {model}: {why}; trying another model before giving up on "
              f"'{task['title']}'", w.name)
        return w.decompose(task, goal, avoid={*avoid, model}, c=c,
                           attempt_budget=attempt_budget)

    def scope_violations(self):
        w, d = self.w, self.d
        scope = (w.task or {}).get("allowed_paths")
        if not scope:
            return []
        _, names = d.git(["diff", "--cached", "--name-only", "--no-renames", "-z", w.review_base],
                         cwd=w.wt, check=True)
        return [p for p in names.split("\0") if p and not path_allowed(p, scope)]