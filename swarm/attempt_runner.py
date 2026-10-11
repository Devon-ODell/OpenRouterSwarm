"""The attempt state machine: implement, gate, review, repair, integrate, judge, learn.

AttemptRunner works on its Worker's attempt fields and reaches every daemon service
through an injected AttemptDeps; it imports no daemon module and reads no daemon global.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from learn import (BREAKTHROUGH_WEIGHT, CRIME, FAULTY, PENALTY_WEIGHT, defect, exhibit,
                   format_playbook, is_breakthrough, json_array, novelty, parse_scores,
                   reward, weight)
from metrics import ACTIVE_MODEL, REVIEWING, TESTING
from pacing import effective_steps
from prompts import (ARCHITECT, DECOMPOSER, FAST_IMPLEMENTER, IMPLEMENTER, JUDGE,
                     PRIORITIES, REPAIR)
from task_rules import failure_class, goal_item_weight, goal_items, scope_gap
from workflow import (Attempt, REVIEW, STRATEGIES, contract, failure_signature,
                      parse_review, path_allowed, validate_scope)
from context_builder import named_paths
from verification import weakened_tests

try:  # one class identity with the daemon's turn errors
    from .agent_turn import (AgentTimeout, CapReached, ExplorationExhausted, ModelError,
                             ModelPoolExhausted, NoCredits, ProviderBusy, ProviderDown,
                             RequestBudget, ReviewFormatError, StepLimit)
except ImportError:  # pragma: no cover - script mode
    from agent_turn import (AgentTimeout, CapReached, ExplorationExhausted, ModelError,
                            ModelPoolExhausted, NoCredits, ProviderBusy, ProviderDown,
                            RequestBudget, ReviewFormatError, StepLimit)

try:  # same module identity as the daemon's workspace
    from . import workspace
except ImportError:  # pragma: no cover - script mode
    import workspace

try:  # same module identity as the daemon's
    from . import agent_turn, verification
except ImportError:  # pragma: no cover - script mode
    import agent_turn
    import verification


@dataclass(frozen=True)
class AttemptDeps:
    """The daemon services an attempt uses, read when the runner is built."""
    log: Callable
    journal: Callable
    notify: Callable
    git: Callable
    trunk_name: Callable
    state: Path            # the repository's state directory
    flint: Callable       # = flint
    rest: Callable        # = rest
    turn_timeout: Callable     # = turn_timeout
    count_requests: Callable   # = count_requests
    pool: Callable             # = pool
    refresh_view: Callable     # = refresh_view
    read_goal: Callable        # = read_goal
    read_only_roles: Any       # = READ_ONLY_ROLES
    kinds: Any                 # = KINDS
    ran_on: dict               # = _ran_on  (the dict object; mutate/read it in place)
    run: dict                  # = _run     (the dict object)
    view_lock: Any             # = _view_lock
    run_lock: Any              # = _run_lock
    attempt_lines: Callable    # = attempt_lines
    baseline_cache: Callable   # = baseline_cache
    baseline_key: Callable     # = baseline_key
    context_pack: Callable     # = context_pack
    count_edits: Callable      # = count_edits
    kin_text: Callable         # = kin_text
    mark_progress: Callable    # = mark_progress
    mit_arm: Callable          # = mit_arm
    study: Callable            # = study
    checked_out_at: Callable   # = checked_out_at
    trunk_locked: Callable     # = trunk_locked
    count_study: Callable      # = count_study
    bank_deferred_work: Callable  # = bank_deferred_work
    count_usd: Callable        # = count_usd
    forget_worker: Callable    # = forget_worker
    paid_pool: Callable        # = paid_pool
    spend_left: Callable       # = spend_left
    ensure_trunk: Callable     # = ensure_trunk
    wt_root: Callable          # = wt_root    (the function itself)
    take_resume: Callable      # = take_resume
    run_gate: Callable         # = run_gate
    run_prov_hooks: Callable   # = run_prov_hooks


# The stage vocabulary is closed: failure_class(), TASK_STAGES, HARNESS_STAGES and every
# consumer depend on these exact strings.
STAGES = frozenset({
    "accepted",
    "refused", "out_of_scope", "baseline", "conflict",
    "rejected", "tests_failed", "weakened_tests",
    "no_change", "agent_timeout",
    "model_error", "review_error", "review_format_error",
    "model_pool_exhausted", "exploration_exhausted", "request_budget",
    "interrupted", "deferred",
})


@dataclass(frozen=True)
class AttemptOutcome:
    """What one attempt at a task came to. Normal task outcomes are data, not exceptions."""
    stage: str                      # one of STAGES (or "error" if the attempt never set one)
    note: str
    failure_class: str | None       # "task" | "model" | "harness" | "review_format"; None when accepted
    implementer: str | None
    reviewer: str | None
    commit: str | None
    requests: int
    evidence_path: Path | None
    scores: dict | None

    @property
    def accepted(self) -> bool:
        return self.stage == "accepted"


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
        if c.get("follow_ups") is False:
            return  # A fixed backlog (a benchmark) must not grow tasks of its own.
        if not scores or not (breakthrough or r >= 0.75):
            return
        if len(w.q.pending()) >= c.get("max_queue", 20):
            return
        for f in scores.get("follow_ups", [])[:2 if breakthrough else 1]:
            if not f.get("acceptance"):
                # The scope gate lets a task with no criteria through on purpose (a person may
                # file one), so a judge's vague idea used to be queued with nothing to check.
                d.journal("follow_up_skipped", parent=task["id"], title=f["title"],
                          reason="no acceptance criteria")
                continue
            paths = [p if not p.endswith("/") and "." in Path(p).name else p.rstrip("/") + "/"
                     for p in f.get("paths") or []]
            meta = dict(persona=task.get("persona"), planner_model=task.get("planner_model"),
                        parent=task["id"], priority=2 if breakthrough else 0, origin="follow_up",
                        depth=task.get("depth", 0), acceptance=f["acceptance"],
                        allowed_paths=paths or None)
            try:
                t = w.q.add(f["title"], f["detail"], "feature", **meta)
            except ValueError:
                meta["allowed_paths"] = None   # a malformed path list is not worth losing the idea
                t = w.q.add(f["title"], f["detail"], "feature", **meta)
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

    def call(self, role, prompt, cwd, steps, model, avoid=(), c=None, attempt_budget=None):
        """One role turn. If the model is busy or down, the turn goes to another model (never
        one in `avoid`, so nobody grades their own work) instead of abandoning the attempt.
        A role that edits files is handed over only while it has changed nothing, so partial
        work is never finished by a different author. self.last_model names the model that
        took the turn last, whether or not it succeeded."""
        w, d = self.w, self.d
        c, attempt_budget = w._attempt_inputs(c, attempt_budget)
        w.last_model = model
        w.last_role = role
        task_cap = max(0, int(c.get("task_request_cap") or 0))
        run_cap = max(0, int(c.get("run_request_cap") or 0))
        prior_task_requests = int((w.task or {}).get("requests_spent") or 0)
        # Turn size comes from measured latency, not configured hope (work order §2):
        # `effective_steps` caps the round count so a final edit, tests and a final answer
        # still fit inside the wall-clock timeout. Fast-execution tasks keep their small
        # budget on top of the request caps.
        if getattr(w, "execution_class", "standard") != "fast" and role not in d.read_only_roles:
            steps = effective_steps(role, model, d.turn_timeout(c, role), steps, w.ledger)
        limits = [int(steps)]
        if task_cap:
            limits.append(task_cap - prior_task_requests - d.count_requests(w.name))
        if run_cap:
            limits.append(run_cap - int(d.run.get("requests_started", 0)))
        steps = min(limits)
        if steps <= 0:
            scope = ("task" if task_cap and
                     prior_task_requests + d.count_requests(w.name) >= task_cap else "run")
            raise RequestBudget(f"{scope} request budget exhausted before {role}")
        if w.evidence:
            if w.role_calls >= c.get("max_role_calls", 10):
                raise ModelError("per-attempt role-call budget exhausted")
            w.role_calls += 1
            w.evidence.record(role, role_calls=w.role_calls, active_model=model)
            w.evidence.write(f"prompt-{w.role_calls:02d}-{role}.txt", prompt)
        turn_c = dict(c, study=False) if getattr(w, "mit", None) == "off" else c
        if w.task and w.task.get("allowed_paths"):
            turn_c = dict(turn_c, _allowed_paths=w.task["allowed_paths"])
        if getattr(w, "execution_class", "standard") == "fast":
            turn_c = dict(turn_c, study=False)
            if role in ("implementer", "repair"):
                steps = min(steps, 4)
                turn_c["role_timeouts"] = dict(turn_c.get("role_timeouts", {}), **{role: 300})
        # Tell the model its effective request and time budget (work order §2). A turn
        # sized from measured latency is only useful if the model knows the round count it
        # is measured against and the wall clock it must finish inside.
        budget_line = (f"\n\nTURN BUDGET: this role gets at most {steps} model round(s) and "
                       f"{d.turn_timeout(turn_c, role)}s of wall time. Leave time for the final edit, "
                       f"the tests and your final answer; stop reading early.")
        prompt = prompt + budget_line
        w.doing = f"{role} with {model}"
        w.publish(ACTIVE_MODEL)
        editing = role not in d.read_only_roles and w.wt is not None and Path(cwd) == Path(w.wt)
        before = w.snapshot()[0] if editing else None
        tried = set()
        while True:
            w.doing = f"{role} with {model}"
            try:
                out = d.flint(prompt, cwd, turn_c, role, w.name, attempt_budget, steps, model)
                # A paid stand-in may have taken the turn. Credit the model that actually
                # answered, or the bandit learns from work another model did: a model with
                # no paid twin would be scored for every turn `paid_stand_in` handed away.
                ran = d.ran_on.pop(w.name, None)
                if ran and ran != model:
                    asked = model
                    model = w.last_model = ran
                    w.doing = f"{role} with {ran}"
                    if w.evidence:
                        w.evidence.record(role, role_calls=w.role_calls, active_model=ran,
                                          stood_in_for=asked)
                break
            except AgentTimeout as exc:
                w.last_model = exc.model
                w.agent_timed_out = True
                if w.evidence:
                    w.evidence.record("agent_timeout", role=role, active_model=exc.model,
                                      timeout=exc.timeout, log=str(exc.logfile))
                d.log(str(exc), w.name)
                raise
            except ProviderDown as e:
                d.log(d.rest(w.ledger, e), w.name)
                tried.add(model)
                if editing and w.snapshot()[0] != before:
                    raise
                nxt = w.pick(set(avoid) | tried |
                             (w.no_review(c) if role == "adversary" else set()),
                             role=role, c=c)
                if nxt is None:
                    raise
                d.log(f"handing {role} to {nxt}", w.name)
                d.journal("handoff", role=role, worker=w.name, model=model, to=nxt,
                          busy=isinstance(e, ProviderBusy))
                if w.evidence:
                    w.evidence.record(role, role_calls=w.role_calls, active_model=nxt, handoff_from=model)
                model = w.last_model = nxt
        w.ledger.warm(model)
        if w.evidence:
            w.evidence.write(f"response-{w.role_calls:02d}-{role}.txt", out)
        return out

    def review(self, tree, diff, tests, model, avoid=(), allow_test_changes=False,
               c=None, attempt_budget=None):
        w, d = self.w, self.d
        c, attempt_budget = w._attempt_inputs(c, attempt_budget)
        w.evidence.record("reviewing", reviewed_tree=tree, reviewer=model)
        w.publish(REVIEWING)
        note = ("\nThe owner authorised test changes for this task; check that the changes to "
                "existing tests match what was asked, and reject them if they do not.\n"
                if allow_test_changes else "")
        prompt = REVIEW.format(contract=json.dumps(w.contract, indent=2), tree=tree,
                               tests=tests, diff=diff[:c.get("max_diff", 24000)]) + note
        prompt += getattr(w, "_review_format_hint", "")
        try:
            raw = w.call("adversary", prompt, w.wt, c["steps"]["adversary"], model,
                         avoid, c=c, attempt_budget=attempt_budget)
            after, _ = w.snapshot()
            if after != tree:
                raise ModelError("reviewer modified the candidate it was asked to inspect")
            review = parse_review(raw, tree, w.contract["acceptance"])
        except ValueError as exc:
            w.evidence.write(f"review-{w.role_calls:02d}-invalid.txt", raw)
            raise ReviewFormatError(f"invalid review: {exc}") from exc
        w.evidence.write(f"review-{w.role_calls:02d}.json", review)
        after, _ = w.snapshot()
        if after != tree:
            raise ModelError("reviewer modified the candidate it was asked to inspect")
        return review

    def reviewed(self, tree, diff, tests, adv, impl, allowed, c=None, attempt_budget=None):
        """(review, reviewer, error) for this candidate, retrying a reviewer that cannot
        produce a readable verdict.

        `parse_review` refuses a malformed, stale or unsupported review, and that refusal used
        to end the attempt: an implementation whose tests had just passed was thrown away
        because a third model could not emit JSON. A review that cannot be read is a fact
        about the reviewer, not about the candidate. So the same tree, the same diff and the
        same test output go to another reviewer, and only a run of reviewers that all fail
        ends the attempt. It costs the repair budget nothing — no repair is warranted while
        nothing has yet been said about the code."""
        w, d = self.w, self.d
        c, attempt_budget = w._attempt_inputs(c, attempt_budget)
        tries = max(1, min(3, 1 + int(c.get("max_review_formats", 2))))
        tried, err = set(), None
        w._review_format_hint = ""
        for n in range(tries):
            try:
                review = w.review(tree, diff, tests, adv, avoid={impl} | tried,
                                  allow_test_changes=allowed, c=c,
                                  attempt_budget=attempt_budget)
                return review, (w.last_model or adv), None
            except (ModelError, StepLimit) as exc:
                ran, err = w.last_model or adv, exc
                if "modified the candidate" in str(exc):
                    # Not a formatting slip: the tree this review was about no longer exists,
                    # so there is nothing left for another reviewer to be asked about.
                    return None, ran, exc
                tried.add(ran)
                w.evidence.record("review_unreadable", reviewer=ran, tried=n + 1,
                                  reason=str(exc)[:300])
                nxt = w.pick(tried | {impl} | w.no_review(c), role="adversary", c=c)
                if n == tries - 1:
                    return None, ran, exc
                if nxt is None:
                    if not isinstance(exc, ReviewFormatError) or ran == impl or ran in w.no_review(c):
                        return None, ran, exc
                    nxt = ran  # Same independent reviewer may correct its own JSON, bounded above.
                    tried.discard(ran)
                w._review_format_hint = ("\nFORMAT CORRECTION: " + str(exc)
                    + "\nReinspect the same immutable tree. Return the exact schema above, "
                      "one evidenced check per criterion, and verification for every finding.\n")
                d.log(f"reviewer {ran} returned no usable verdict ({str(exc)[:90]}); putting the "
                      f"same candidate to {nxt}", w.name)
                adv = nxt
        return None, adv, err

    def decompose(self, task, goal, avoid=(), c=None, attempt_budget=None):
        """Split after a timeout or repeated failure, within the recursion limits.

        A split that returns no subtasks strands the task and everything waiting on it, so one
        empty answer is retried on a different model before the task is given up on."""
        w, d = self.w, self.d
        c, attempt_budget = w._attempt_inputs(c, attempt_budget)
        if c.get("roadmap_file"):
            return 0  # A coordinator must repoint dependencies before splitting a packet.
        # Between attempts: not the last attempt's evidence, role-call budget or MIT arm.
        w.evidence, w.mit = None, None
        w.execution_class = "standard"
        model = (w.ledger.pick("planner", d.pool(c, "decomposer"), exclude=set(avoid))
                 or w.pick(set(avoid), role="decomposer", c=c))
        if model is None:
            return 0
        with d.view_lock:
            view = d.refresh_view(c)
            try:
                out = w.call("decomposer", DECOMPOSER.format(
                    goal=goal, title=task["title"], detail=task["detail"], test_cmd=c["test_cmd"],
                    notes="\n---\n".join(task.get("notes", [])) or task.get("note", "")
                    or task.get("harness_note", "")),
                    view, c["steps"].get("decomposer", 6), model, c=c,
                    attempt_budget=attempt_budget)
            except (StepLimit, ModelError, AgentTimeout) as e:
                w.ledger.update("planner", model, 0.0)
                d.log(f"decomposer {model} failed ({type(e).__name__}) on '{task['title']}'", w.name)
                return w.retry_split(task, goal, model, avoid, f"{type(e).__name__}",
                                     c, attempt_budget)
        subtasks = json_array(out)
        if subtasks is None:
            w.ledger.update("planner", model, 0.0)
            subtasks = []
        added = 0
        dependencies = list(task.get("depends_on", []))
        for t in subtasks[:3]:
            if (not isinstance(t, dict) or not isinstance(t.get("title"), str)
                    or t.get("kind", "feature") not in d.kinds):
                break  # Do not enqueue a dependent suffix without its prerequisite.
            child = w.q.add(t["title"], str(t.get("detail", "")), t.get("kind", "feature"),
                            persona=task.get("persona"), planner_model=model,
                            parent=task["id"], priority=max(1, task.get("priority", 1)), origin="split",
                            depends_on=dependencies, acceptance=t.get("acceptance"),
                            depth=task.get("depth", 0) + 1)
            if not child:
                break
            added += 1
            dependencies = [child["id"]]
        if not added:
            return w.retry_split(task, goal, model, avoid, "no usable subtasks",
                                 c, attempt_budget)
        moved = w.q.repoint(task["id"], dependencies[0])
        d.log(f"split '{task['title']}' into {added} smaller task(s)"
              + (f"; {moved} waiting task(s) moved to the last child" if moved else ""), w.name)
        d.journal("split", id=task["id"], title=task["title"], added=added, model=model, repointed=moved)
        return added

    def _reinforce(self, task, stage, info, c=None, attempt_budget=None):
        w, d = self.w, self.d
        c, attempt_budget = w._attempt_inputs(c, attempt_budget)
        L, impl = w.ledger, info["implementer"]
        if stage != "accepted" and failure_class(stage, info.get("note", ""),
                                                 edited=info.get("edit_calls")) in ("harness", "review_format"):
            # The model answered; this swarm lost the turn. Scoring it down at penalty weight
            # teaches the bandit about our step limits, not about the model.
            d.journal("harness_failure", id=task["id"], title=task["title"], stage=stage,
                      implementer=impl, edit_calls=info.get("edit_calls"))
            d.log(f"{task['id']}: {stage} — harness failure, not scored against {impl}", w.name)
            return
        scores = info.get("scores")
        text = f"{task['title']} {task.get('detail', '')}"
        nov = novelty(text, L.accepted_texts()) if stage == "accepted" else 0.0
        if stage == "review_error" and info.get("reviewer"):
            # A reviewer that cannot deliver a verdict wastes the implementer's work.
            L.update("implementer", info["reviewer"], 0.0, weight=PENALTY_WEIGHT["model_error"])
            d.log(f"reviewer {info['reviewer']} failed to deliver a verdict; penalised", w.name)
        items = goal_items(d.read_goal(c))
        info["goal_item"] = int((scores or {}).get("goal_item") or 0) if items else None
        r = reward(stage, scores, nov, repairs=info.get("repairs", 0),
                   goal_weight=goal_item_weight(info["goal_item"], items))
        if r is None:
            return
        # Only the author of faulty code pays the penalty weight; the planner whose
        # idea it was learns at normal weight. Success pays everyone at full weight.
        rw = weight(stage)
        credit = rw if stage == "accepted" else 1.0
        L.update("implementer", impl, r, weight=rw)
        if task.get("persona"):
            L.update("persona", task["persona"], r, weight=credit)
        if task.get("planner_model"):
            L.update("planner", task["planner_model"], r, weight=credit)
        L.credit(info.get("lessons", []), r)
        breakthrough = False
        if stage == "accepted":
            breakthrough = is_breakthrough(r, scores, L.accepted_rewards())
            L.record_accepted({"id": task["id"], "title": task["title"], "detail": task.get("detail", ""),
                               "reward": r, "novelty": nov, "scores": scores, "implementer": impl,
                               "reviewer": info.get("reviewer"), "persona": task.get("persona"),
                               "origin": task.get("origin", "human"), "breakthrough": breakthrough,
                               # Work order §3/§16: malformed or missing judge JSON is missing
                               # evidence, never a neutral assessment.
                               "quality_score": "unscored" if not scores else None})
            if scores and scores.get("lesson"):
                L.add_lesson(scores["lesson"], "lesson", task["id"], r, pinned=breakthrough)
            if breakthrough:
                # Reinforce again at full reward: the draw that produced this should come up again.
                L.update("implementer", impl, 1.0, weight=BREAKTHROUGH_WEIGHT)
                if task.get("persona"):
                    L.update("persona", task["persona"], 1.0, weight=BREAKTHROUGH_WEIGHT)
                w.celebrate(task, r, scores, impl, c)
            w.follow_up(task, scores, r, breakthrough, c)
        elif stage in FAULTY:
            w.hang(task, stage, info, impl, c)
        elif info.get("pitfall"):
            L.add_lesson(info["pitfall"], "pitfall", task["id"], 0.5)
        info["reward"] = r
        d.journal("reward", id=task["id"], title=task["title"], stage=stage, reward=r, weight=rw,
                  novelty=nov, implementer=impl, persona=task.get("persona"),
                  goal_item=info.get("goal_item"),
                  scores={k: v for k, v in (scores or {}).items() if k != "follow_ups"},
                  breakthrough=breakthrough)
        d.log(f"{task['id']}: {stage}, reward {r:.2f}{' ★ BREAKTHROUGH' if breakthrough else ''}", w.name)

    def _attempt(self, task, goal, info, c=None, attempt_budget=None):
        w, d = self.w, self.d
        c, attempt_budget = w._attempt_inputs(c, attempt_budget)
        wname = w.name
        if task.get("kind") == "harness":
            return "refused", "target a separate harness checkout; live harness editing is disabled", info
        try:
            validate_scope(task)
        except ValueError as exc:
            return "out_of_scope", str(exc), info
        gap = scope_gap(task, c) if c.get("scope_gate", True) else ""
        if gap:
            # Before the worktree, before the baseline suite, before the first request.
            d.log(f"{task['id']} has no workable scope: {gap}", wname)
            d.journal("out_of_scope", id=task["id"], title=task["title"],
                      origin=task.get("origin"), gap=gap)
            return "out_of_scope", gap, info
        d.log(f"claim {task['id']} — {task['title']}", wname)
        w.ensure_worktree(task, c)
        wd = w.wt
        # Named for the task, not the worker. `attempts/w0` was overwritten by every task that
        # worker took, so a parked task's evidence was gone by the time anyone looked for it —
        # and the panel's own lookup already globbed `attempts/<task id>-*`, which nothing had
        # ever written. One directory per attempt: the link resolves and the history survives.
        w.evidence = Attempt(d.state, f"{task['id']}-{w.branch.split('/')[-1]}-"
                                       f"{dt.datetime.now():%Y%m%d-%H%M%S}", task, wd, w.branch)
        info["artifact"] = str(w.evidence.path)
        info["attempt_id"] = w.evidence.data["id"]
        _, w.review_base = d.git(["rev-parse", "HEAD"], cwd=wd, check=True)
        strategy = task.get("strategy") or w.ledger.pick("strategy", list(STRATEGIES)) or "regression_first"
        if strategy not in STRATEGIES:
            return "refused", "unknown implementation strategy", info
        w.contract = contract(task, goal, w.review_base, strategy)
        w.evidence.write("contract.json", w.contract)
        w.evidence.record("baseline", strategy=strategy, base_commit=w.review_base)
        info["strategy"] = strategy
        # Trunk at this commit either passed its own gate recently or it did not; running the
        # suite again proves the same thing and costs the studio 105 seconds per attempt.
        key = d.baseline_key(c, w.review_base)
        cached = d.baseline_cache(key, ttl=c.get("baseline_ttl", 86_400))
        if cached:
            ok, output = True, f"baseline: cached pass for {w.review_base[:12]}"
            w.evidence.record("baseline_cached", base_commit=w.review_base,
                              at=cached["at"], key=key)
        else:
            ok, output = w.gate("baseline", c)
            d.baseline_cache(key, passed=ok)
        _, dirty = d.git(["status", "--porcelain"], cwd=wd, check=True)
        if not ok or dirty:
            return "baseline", "baseline tests failed or changed tracked/unignored files; no model calls spent:\n" + output, info
        fast = w.execution_class == "fast"
        w.mit = info["mit"] = "off" if fast else d.mit_arm(c)
        corpus = d.study(f"{task['title']} {task['detail']}", c) if w.mit == "on" else ""
        info["mit_injected"] = bool(corpus)
        w.evidence.record("study", mit=w.mit, injected=bool(corpus))
        # The code the task names, read once here rather than over and over by the model.
        kin = d.kin_text(task, w.q)
        context = d.context_pack(task, wd, also=kin)
        info["context_chars"] = len(context)
        w.evidence.record("context", chars=len(context), paths=named_paths(task, kin))
        lessons, pitfalls = ([], []) if fast else w.ledger.playbook()
        # Lessons shown to this implementer share the attempt's reward (see Ledger.credit).
        info["lessons"] = [l["id"] for l in lessons + pitfalls if l.get("kind") in ("lesson", "pitfall")]
        failed = w.excluded_models(role="implementer")
        impl = w.select_implementer(failed, c)
        if impl is None and failed:
            # Every candidate implementer has failed this task/role within the exclusion
            # window. Do not cycle back to an excluded model (work order §5): park with
            # evidence and let a human wake it or requeue it.
            raise ModelPoolExhausted("every implementer in the pool is excluded on this task: "
                                     + ", ".join(sorted(failed)))
        if impl is None:
            raise ProviderDown(None, "every model in the pool is resting after provider failures")
        info["implementer"] = impl
        prompt_contract = {k: v for k, v in w.contract.items() if k != "goal"}
        prompt_contract["detail"] = task["detail"]
        spec = json.dumps(prompt_contract, indent=2)
        if not fast:
            spec += "\nSTRATEGY: " + STRATEGIES[strategy]
        if not fast and c["steps"].get("architect", 0):
            spec += "\nARCHITECT NOTES (the contract still controls scope):\n" + w.call(
                "architect", ARCHITECT.format(goal=goal, title=task["title"], detail=task["detail"],
                 corpus=corpus, test_cmd=c["test_cmd"]), wd, c["steps"]["architect"],
                 w.pick({impl}, role="architect", c=c) or impl,
                avoid={impl}, c=c, attempt_budget=attempt_budget)
        # What earlier attempts hit, as lines to act on. Parent handoffs stay artifacts on disk,
        # not mutable model memory, so only their outcome is repeated here.
        lines = d.attempt_lines(task)
        previous = f"\nWHAT EARLIER ATTEMPTS AT THIS TASK HIT\n{lines}\n" if lines else ""
        # Work a restart cut short, if there is any: re-apply it and go straight to the gate.
        # It still faces the tests and the adversary; nothing is trusted, only reused.
        resumed = w.adopt_retained(task, wd)
        if resumed:
            info["resumed_from"] = resumed.get("attempt")
            impl = info["implementer"] = resumed.get("implementer") or impl
            w.evidence.record("resumed", attempt=resumed.get("attempt"), implementer=impl,
                              lines=resumed.get("lines"))
        else:
            w.evidence.record("implementing", implementer=impl)
            try:
                handoff = w.call("implementer", (FAST_IMPLEMENTER if fast else IMPLEMENTER).format(
                    goal=goal, spec=spec, previous=previous, corpus=corpus, test_cmd=c["test_cmd"],
                    context=context, title=task["title"][:200],
                    playbook=format_playbook(lessons, pitfalls, named_paths(task, kin))),
                    wd, c["steps"]["implementer"], impl, c=c,
                    attempt_budget=attempt_budget)
                w.evidence.write("implementation.txt", handoff)
            except AgentTimeout:
                w.evidence.record("timeout_recovery", note="verify partial implementation through normal gates")
            except StepLimit:
                w.evidence.record("step_limit", note="verify the partial implementation before continuing")
            except ModelError as exc:
                info["implementer"] = w.last_model or impl
                return "model_error", str(exc), info
            # After a handoff the model that actually wrote the code is its author.
            impl = info["implementer"] = w.last_model or impl
        adv = w.pick({impl} | w.no_review(c), role="adversary", c=c) or impl
        info["reviewer"] = adv
        info["same_model_review"] = adv == impl
        seen = set()
        repairs = max(0, min(3, int(c.get("max_repairs", 2))))
        for cycle in range(repairs + 1):
            w.evidence.record("candidate", repairs=cycle)
            _, head = d.git(["rev-parse", "HEAD"], cwd=wd, check=True)
            if head != w.review_base:
                return "rejected", "agent changed commit history outside supervisor control", info
            tree, diff = w.snapshot()
            outside = w.scope_violations()
            if outside:
                return "rejected", "outside allowed_paths: " + ", ".join(outside), info
            if not diff.strip():
                info["edit_calls"] = tried = d.count_edits(wname)
                stage = "agent_timeout" if w.agent_timed_out else "no_change"
                how = (f"{tried} edit call(s) changed nothing" if tried
                       else "the turn never reached an edit")
                return stage, f"no implementation changes: {how}; evidence: {w.evidence.path}", info
            allowed = bool(task.get("allow_test_changes"))
            lost = weakened_tests(diff, allow_test_changes=allowed)
            if lost:
                return ("weakened_tests",
                        f"{lost} existing assertion(s) removed with nothing in their place; "
                        "manual review required", info)
            ok, tests = w.gate(f"candidate-{cycle}", c)
            after, _ = w.snapshot()
            if after != tree:
                return "rejected", "test command modified the candidate; fix test isolation/ignored files", info
            review = None
            if ok:
                review, adv, exc = w.reviewed(tree, diff, tests, adv, impl, allowed,
                                              c, attempt_budget)
                info["reviewer"] = adv          # the model that answered, or failed to
                if exc is not None:
                    return ("review_format_error" if isinstance(exc, ReviewFormatError) else "review_error"), str(exc), info
                info["same_model_review"] = adv == impl
                if review["verdict"] == "approve":
                    info["review"] = review
                    break
            failure = json.dumps(review, indent=2) if review else tests
            stage = "rejected" if review else "tests_failed"
            signature = failure_signature(stage, failure)
            w.evidence.record("repair_needed", failure=signature, reason=failure[-6000:])
            info["pitfall"] = failure[-600:]
            if cycle == repairs or signature in seen:
                return stage, f"repair budget/repeated failure; evidence: {w.evidence.path}\n{failure[-2500:]}", info
            seen.add(signature)
            w.evidence.record("repairing")
            pre_repair_tree, _ = w.snapshot()
            try:
                handoff = w.call("repair", REPAIR.format(contract=json.dumps(w.contract, indent=2),
                                    failure=failure, test_cmd=c["test_cmd"], context=context), wd,
                                    c["steps"].get("repair", c["steps"]["implementer"]), impl,
                                    avoid={adv}, c=c, attempt_budget=attempt_budget)
                w.evidence.write(f"repair-{cycle + 1}.txt", handoff)
            except AgentTimeout:
                # Work order §6: a repair that times out may have made things worse. Keep the
                # timed-out patch as evidence, but do not roll back useful repair work blindly:
                # a repair that stayed inside scope and removed the failing file must still be
                # verified and can land. Only a repair that escaped the allowed scope (wrote
                # outside it, or deleted files it had no business touching) is reverted to the
                # last pre-repair tree before the gates run.
                w.evidence.record("timeout_recovery", note="verify partial repair through normal gates")
                after_tree, repair_diff = w.snapshot()
                if repair_diff and after_tree != pre_repair_tree:
                    w.evidence.write(f"repair-{cycle + 1}-timed-out.diff", repair_diff)
                    if w.scope_violations():
                        d.git(["read-tree", pre_repair_tree], cwd=wd)
                        d.git(["checkout-index", "-a", "-f"], cwd=wd)
                        d.git(["clean", "-fd"], cwd=wd)
                        d.git(["add", "-A"], cwd=wd)
                        _, restored = d.git(["write-tree"], cwd=wd)
                        if restored != pre_repair_tree:
                            # The tree could not be restored byte-for-byte; keep the repair
                            # diff and let the gate decide rather than losing the attempt.
                            w.evidence.record("repair_restore_failed", tree=restored)
            except StepLimit:
                pass
            except ModelError as exc:
                return "model_error", str(exc), info
        d.git(["commit", "-q", "--no-verify", "-m",
             f"swarm: {task['title']}\n\nSwarm-Task: {task['id']}\n"
             f"Swarm-Implementer: {impl}\nSwarm-Reviewer: {adv}\nSwarm-Strategy: {strategy}"], cwd=wd, check=True)
        w._review_model, w._author = adv, impl
        landed, why = w.integrate(c, attempt_budget)
        if not landed:
            return "conflict", why, info
        _, info["commit"] = d.git(["rev-parse", "HEAD"], cwd=wd, check=True)
        info["repairs"] = cycle
        info["role_calls"] = w.role_calls
        # Record integration before optional learning/reporting. Recovery consults Git too.
        w.evidence.record("integrated", commit=info["commit"])
        with d.run_lock:
            d.run.update(last_accepted_commit=info["commit"], last_accepted_at=time.time())
        d.mark_progress("accepted commit")
        d.log(f"{task['id']}: landed on {d.trunk_name(c)}; evidence: {w.evidence.path}", wname)
        # A zero judge step budget disables optional post-landing scoring entirely instead of
        # secretly spending flint's minimum one request.
        info["judge"], info["scores"] = None, None
        if int(c["steps"].get("judge", 0)) > 0:
            info["judge"] = w.pick({impl, adv}, role="judge", c=c) or adv
            _, landed_diff = d.git(["diff", "--unified=3", w.review_base, "HEAD"], cwd=wd)
            info["scores"] = w.judge(task, goal, landed_diff,
                                      "APPROVE: " + str((info.get("review") or {}).get("summary", "")),
                                      info["judge"], avoid={impl, adv}, c=c,
                                      attempt_budget=attempt_budget)
            info["judge"] = w.last_model or info["judge"]
            if info["scores"]:
                w.evidence.write("judge.json", info["scores"])
        return "accepted", f"integrated {info['commit']} on {d.trunk_name(c)}; handoff: {w.evidence.path / 'HANDOFF.md'}", info

    def integrate(self, c=None, attempt_budget=None):
        """Review/test a rebased candidate before compare-and-swap of swarm trunk."""
        w, d = self.w, self.d
        c, attempt_budget = w._attempt_inputs(c, attempt_budget)
        t = d.trunk_name(c)
        for _ in range(2):
            _, old = d.git(["rev-parse", t], cwd=c["repo"], check=True)
            rc, _ = d.git(["merge-base", "--is-ancestor", old, "HEAD"], cwd=w.wt)
            if rc != 0:
                if not workspace.rebase_onto(w.wt, old, git=d.git):
                    return False, f"conflicts with newer trunk; work retained on {w.branch}"
                w.review_base = old
                tree, diff = w.snapshot()
                if w.scope_violations():
                    return False, "rebased candidate changed paths outside allowed_paths"
                ok, output = w.gate("rebased", c)
                after, _ = w.snapshot()
                if not ok or after != tree or weakened_tests(
                        diff, allow_test_changes=bool(w.task.get("allow_test_changes"))):
                    return False, "rebased candidate failed verification or changed during tests"
                review, _, error = w.reviewed(
                    tree, diff, output, w._review_model, w._author,
                    bool(w.task.get("allow_test_changes")), c, attempt_budget)
                if error:
                    raise error
                if review["verdict"] != "approve":
                    return False, "rebased candidate needs changes; fresh review saved in artifacts"
            status, note = workspace.land_on_trunk(
                c, w.wt, old, w.evidence.data.get("reviewed_tree"),
                git=d.git, locked=d.trunk_locked, checked_out_at=d.checked_out_at)
            if status == "landed":
                return True, ""
            if status in ("checked_out", "mismatch"):
                return False, note
            # "moved" and "failed" fall through to the next iteration, exactly as before
        return False, "trunk kept moving; retained candidate for a later attempt"

    def run(self, task, goal, c=None, attempt_budget=None):
        """One attempt at a task, from provisioning to release: run() is do_task's body."""
        w, d = self.w, self.d
        c, attempt_budget = w._attempt_inputs(c, attempt_budget)
        w.task, w.stage, w.wt = task, "error", None
        w.doing, w.started = "starting", time.time()
        w.evidence, w.role_calls, w.gate_count = None, 0, 0
        w.attempt_requests = 0
        # Provisioning belongs to the lifetime of this fresh worktree, not to each gate.
        # Candidate repair cycles and a rebase reuse the same installed dependencies.
        w.worktree_provisioned = False
        w.last_model, w.last_role = None, None
        w.mit = None
        w.agent_timed_out = False
        w.failure_class = "task"
        w.execution_class = task.get("execution_class", "standard")
        # Not just the counters: a task starts with no inherited model, waiting reason or
        # live-turn log either, or its first status line describes the task before it.
        d.forget_worker(w.name)
        note, info = "attempt interrupted before completion", {}
        try:
            w.stage, note, info = w._attempt(task, goal, info, c, attempt_budget)
        except AgentTimeout as exc:
            w.stage, note = "agent_timeout", str(exc)
        except ExplorationExhausted as exc:
            # The turn spent 70% of its rounds without editing. The swarm's harness let it
            # explore, so it is not scored against the model as faulty code, but the pairing
            # is excluded for the rest of the run/24h (work order §5): the failure_class
            # below stays harness, which parks/holds rather than consuming an attempt.
            w.last_role = getattr(w, "last_role", None)
            w.stage, note = "exploration_exhausted", str(exc)
        except ReviewFormatError as exc:
            w.stage, note = "review_format_error", str(exc)
        except RequestBudget as exc:
            w.stage, note = "request_budget", str(exc)
        except ModelPoolExhausted as exc:
            w.stage, note = "model_pool_exhausted", str(exc)
        except (CapReached, ProviderDown, NoCredits) as exc:
            w.stage = "deferred"
            # The attempt may have spent requests before the provider or the allowance stopped
            # it. Bank them before re-raising: the worker's release charges them to the task, so
            # task_request_cap sees them, and the journal row lets a scorecard count the attempt.
            # Skipping this left a task 16 requests deep reporting requests_spent: 0.
            w.attempt_requests = d.count_requests(w.name, reset=True)
            d.journal("attempt_deferred", id=task["id"], title=task["title"],
                      reason=type(exc).__name__, model=getattr(exc, "model", None) or w.last_model,
                      role=w.last_role, requests=w.attempt_requests,
                      edit_calls=d.count_edits(w.name))
            raise
        finally:
            if w.evidence:
                w.evidence.finish(w.stage, note)
            try:
                w._cleanup(c)
            except Exception as exc:
                # Preserve the outcome and artifact if shutdown/Git prevents cleanup.
                d.log(f"cleanup deferred for {w.branch}: {exc}", w.name)
            if w.stage == "deferred" and w.evidence:
                # Cleanup has committed the edits to the attempt's branch; offer them to the
                # task's next claim now rather than only after a daemon restart.
                try:
                    lines = d.bank_deferred_work(c, w.evidence.path, w.evidence.data)
                    if lines:
                        d.log(f"{task['id']}: {lines} lines of work kept for the retry", w.name)
                except Exception as exc:
                    d.log(f"could not keep the deferred work for a retry: {exc}", w.name)
        w.attempt_requests = info["requests"] = d.count_requests(w.name, reset=True)
        w.failure_class = ("task" if w.stage == "accepted" else
                           failure_class(w.stage, note, edited=info.get("edit_calls")))
        if info.get("implementer"):
            try:
                w._reinforce(task, w.stage, info, c, attempt_budget)
            except Exception as exc:
                d.journal("learning_error", id=task["id"], err=str(exc), stage=w.stage)
                d.log(f"learning update failed; task outcome retained: {exc}", w.name)
            # One row per attempt that spent model calls: the MIT experiment's raw data.
            d.journal("attempt", id=task["id"], title=task["title"], stage=w.stage,
                      goal_item=info.get("goal_item"), usd=d.count_usd(w.name, reset=True),
                      failure_class=None if w.stage == "accepted" else w.failure_class,
                      edit_calls=info.get("edit_calls"),
                      mit=info.get("mit"), injected=info.get("mit_injected", False),
                      study_calls=d.count_study(w.name, reset=True), role_calls=w.role_calls,
                      requests=w.attempt_requests, commit=info.get("commit"),
                      implementer=info["implementer"], reviewer=info.get("reviewer"),
                      judge=info.get("judge"), reward=info.get("reward"),
                      scores={k: v for k, v in (info.get("scores") or {}).items()
                              if k in ("impact", "creativity", "quality", "breakthrough")},
                      persona=task.get("persona"), origin=task.get("origin", "human"))
        evidence = w.evidence.path if w.evidence else None
        return AttemptOutcome(
            stage=w.stage, note=note,
            failure_class=None if w.stage == "accepted" else w.failure_class,
            implementer=info.get("implementer"), reviewer=info.get("reviewer"),
            commit=info.get("commit"), requests=w.attempt_requests,
            evidence_path=evidence, scores=info.get("scores"))

    def pick(self, exclude=(), role="implementer", c=None):
        """Implementers by Thompson sampling. Reviewers and judges come from the
        same posterior, excluding the implementer so nobody grades their own work.

        `role` is the job this model is being drawn for, so `role_models` can keep a model
        out of the roles it is bad at while leaving it in the ones it is good at. The
        posterior is still the implementer's: one model's record is one record.

        When every free model is resting, a paid stand-in keeps the attempt alive rather
        than abandoning work already done — but only while the day's budget allows."""
        w, d = self.w, self.d
        c = w._attempt_config(c)
        return agent_turn.pick_model(w.ledger, c, exclude, role,
                                     pool=d.pool, paid_pool=d.paid_pool, spend_left=d.spend_left)

    def select_implementer(self, failed, c=None):
        """An implementer for this attempt, or None when none is usable.

        `failed` is the set of models the task/model circuit breaker already excluded on
        this task. `pick()` is given those exclusions directly (the ledger only sees the
        arms it is allowed), so a proven-bad pairing is never redrawn for the same task.
        """
        w, d = self.w, self.d
        c = w._attempt_config(c)
        return w.pick(failed, role="implementer", c=c)

    def no_review(self, c=None):
        """Models the owner has barred from reviewing (config `reviewer_exclude`)."""
        w, d = self.w, self.d
        c = w._attempt_config(c)
        return set(c.get("reviewer_exclude") or ())

    def excluded_models(self, role=None):
        """Models the task/model circuit breaker has excluded from `role` on this task.

        One timeout, incomplete response, length termination or no-edit exhaustion on a task
        excludes that model from that task/role for the rest of the run or 24 hours (work
        order §5). A provider-busy handoff before any edit stays cheap and does not poison
        the pairing, so those failures are not recorded as exclusions. A harness timeout is
        still an exclusion: the pairing demonstrably cannot deliver inside the budget, so the
        next implementation attempt must use another model.
        """
        w, d = self.w, self.d
        return agent_turn.excluded_models(w.task, role, now=time.time())

    def ensure_worktree(self, task, c=None):
        w, d = self.w, self.d
        c = w._attempt_config(c)
        w.branch, w.wt = workspace.create_worktree(
            c, task["id"], git=d.git, ensure_trunk=d.ensure_trunk, worktrees=d.wt_root)

    def adopt_retained(self, task, wd):
        w, d = self.w, self.d
        return workspace.adopt_retained(task, wd, take_resume=d.take_resume, git=d.git,
                                        log=d.log, journal=d.journal, worker=w.name)

    def own_branch(self):
        w, d = self.w, self.d
        return workspace.owns_branch(w.wt, w.branch, git=d.git)

    def gate(self, label, c=None):
        """Run configured commands and retain supervisor-owned evidence."""
        w, d = self.w, self.d
        c = w._attempt_config(c)
        w.gate_count += 1
        w.doing = f"running the tests ({label})"
        w.publish(TESTING)
        commands = [c["test_cmd"], *c.get("validation_commands", [])]
        if label != "baseline":
            commands += (w.task or {}).get("verification_commands", [])
        commands = list(dict.fromkeys(commands))
        evidence, passed, w.worktree_provisioned = verification.run_gate_commands(
            c, w.wt, commands, label, w.gate_count, w.evidence.path,
            w.worktree_provisioned,
            run_gate=d.run_gate, run_prov_hooks=d.run_prov_hooks, mark_progress=d.mark_progress)
        w.evidence.write(f"gate-{w.gate_count:02d}.json", evidence)
        w.evidence.record("verifying", gate=label, gates=evidence)
        return passed, json.dumps(evidence, indent=2)

    def snapshot(self):
        w, d = self.w, self.d
        return workspace.snapshot(w.wt, w.branch, w.review_base, git=d.git,
                                  owns_branch=w.own_branch, error=ModelError)

    def _cleanup(self, c=None):
        w, d = self.w, self.d
        c = w._attempt_config(c)
        return workspace.cleanup(c, w.wt, w.branch, w.stage, w.task,
                                 git=d.git, owns_branch=w.own_branch)
