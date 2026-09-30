#!/usr/bin/env python3
"""Bounded-task and bounded-run request budgets (work order §1).

The queue's `role_calls` counts role processes, not API requests: one implementer
turn can spend sixteen requests and the queue cannot stop it because it does not
know its cost. This module is the enforceable budget —

- a per-task cap (`task_request_cap`) and a per-run cap (`run_request_cap`),
- checked before *every* model request, not merely before a role turn,
- with capacity reserved for verification: an implementation may use at most 70% of
  the task cap; review and one bounded repair share the remaining 30%,
- and a retained candidate is never thrown away because a cap was reached: the
  attempt is parked with reason `request_budget`, not classified as faulty code.
"""
# An implementation may use at most this share of the task request cap before
# review/repair must still fit within the same cap.
IMPLEMENTATION_SHARE = 0.70


def implementation_budget(task_cap):
    """The request budget an implementer may spend before review must fit too."""
    return max(0, int(task_cap * IMPLEMENTATION_SHARE)) if task_cap else None


def task_budget(task_cap, spent):
    """Requests still available to a task, or None when no cap is set."""
    if not task_cap:
        return None
    return max(0, int(task_cap) - int(spent or 0))


def run_budget(run_cap, spent):
    """Requests still available to the run, or None when no cap is set."""
    if not run_cap:
        return None
    return max(0, int(run_cap) - int(spent or 0))


def can_afford(task_left, run_left, need=1):
    """(allowed, scope) — scope is 'task' | 'run' | None when both are fine."""
    if task_left is not None and task_left < need:
        return False, "task"
    if run_left is not None and run_left < need:
        return False, "run"
    return True, None


def hold_reason(scope):
    return f"request_budget: {scope} request budget exhausted"