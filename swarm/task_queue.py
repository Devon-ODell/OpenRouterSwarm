"""JSONL task persistence, pure queue policy, and queue orchestration.

This module deliberately has no dependency on ``swarmd``.  The compatibility facade
constructs :class:`TaskQueue` with its per-repository paths and run-state objects.
"""
from __future__ import annotations

import fcntl
import json
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

try:  # package import in tests; top-level import when swarmd.py is executed directly
    from .task_rules import condense, estimate_requests
    from .workflow import criteria, validate_scope
except ImportError:  # pragma: no cover - exercised by the top-level swarmd identity
    from task_rules import condense, estimate_requests
    from workflow import criteria, validate_scope


KINDS = ("feature", "bugfix", "test", "refactor")
META = ("persona", "planner_model", "priority", "depth", "parent", "origin", "acceptance",
        "depends_on", "root", "strategy", "execution_class", "allow_test_changes", "serves",
        "packet", "allowed_paths", "verification_commands", "request_class", "must_run_first")
TEST_CHANGE_ORIGINS = frozenset({"human", "cursor"})


class JsonlTaskStore:
    """Pending/done JSONL guarded by one process and one ``fcntl`` lock."""

    def __init__(self, pending, done, lock_path):
        self.pending = Path(pending)
        self.done = Path(done)
        self.lock_path = Path(lock_path)
        self.thread_lock = threading.Lock()

    @staticmethod
    def read(path):
        path = Path(path)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    @staticmethod
    def write(path, rows):
        path = Path(path)
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(row) + "\n" for row in rows))
        tmp.replace(path)

    @contextmanager
    def locked(self):
        with self.thread_lock, open(self.lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def pending_rows(self):
        return self.read(self.pending)

    def done_rows(self):
        return self.read(self.done)

    def write_pending(self, rows):
        self.write(self.pending, rows)

    def write_done(self, rows):
        self.write(self.done, rows)

    def append_done(self, rows):
        with open(self.done, "a") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")


@dataclass(frozen=True)
class TaskTransition:
    status: str
    not_before: float | None
    failure_class: str
    cool_model: tuple[str, str] | None
    counters: Mapping[str, int]
    note: str | None


class TaskPolicy:
    """Pure claim ranking and release decisions; no files, globals, or ledgers."""

    MAX_HARNESS_FAILURES = 3
    MAX_EXPLORATION_FAILURES = 2

    def __init__(self, max_depth=2, max_attempts=2):
        self.max_depth = max_depth
        self.max_attempts = max_attempts

    @staticmethod
    def selection_score(row, now=None):
        """Expected value per request, with explicit priority kept dominant.

        Executable verification, bounded paths and independence add confidence;
        estimated request cost and harness failures reduce the score.
        """
        priority = 0
        try:
            priority = int(row.get("priority") or 0)
        except (TypeError, ValueError):
            pass
        score = 100.0 * priority
        score += 25.0 if row.get("verification_commands") else 0.0
        score += 15.0 if row.get("allowed_paths") else 0.0
        score += 0.0 if row.get("failed_root") else 10.0
        score -= estimate_requests(row)
        score -= 20.0 * max(0, int(row.get("harness_failures") or 0))
        return score

    def claim(self, rows, completed, now, requests_left=None):
        ready = [row for row in rows
                 if not row.get("claimed") and row.get("not_before", 0) <= now
                 and row.get("request_class") != "manual"
                 and set(row.get("depends_on", [])).issubset(completed)]
        if not ready:
            return None
        if requests_left is not None:
            fresh = [row for row in ready if not row.get("attempts")]
            started = [row for row in ready if row.get("attempts")]
            if started and fresh and requests_left < min(estimate_requests(row) for row in fresh):
                ready = started

        def key(row):
            return (row.get("priority", 0), 1 if row.get("must_run_first") else 0,
                    self.selection_score(row, now), -row.get("created", 0))

        return max(ready, key=key)["id"]

    @staticmethod
    def counts_toward_circuit(failure_class):
        return failure_class not in ("harness", "provider_busy", "review_format")

    def release(self, task, ok, note="", defer=0, split_now=False,
                failure_class="task", park=False, requests=0, turn_seconds=0,
                role_turns=0, stage=None, failed_model=None, failed_role=None,
                model_failure_count=0, now=None):
        now = time.time() if now is None else now
        requests = max(0, int(requests or 0))
        counters = {}
        format_error = not ok and not defer and failure_class == "review_format"
        harness = not ok and not defer and failure_class == "harness"
        parked = bool(park or format_error) and not ok and not defer
        if format_error:
            counters["review_format_failures"] = task.get("review_format_failures", 0) + 1
        attempts = task.get("attempts", 0) + (0 if defer or harness or parked else 1)
        counters["attempts"] = attempts
        stored_note = None if harness else condense(note, 800)
        not_before = None

        if defer:
            status, not_before = "retry", now + defer
        elif ok:
            status = "done"
        elif parked:
            if stage == "model_pool_exhausted" and task.get("depth", 0) < self.max_depth:
                status = "split"
            else:
                status, stored_note = "parked", condense(note, 800)
        elif harness:
            harness_failures = task.get("harness_failures", 0) + 1
            exploration = task.get("exploration_failures", 0)
            if stage == "exploration_exhausted":
                exploration += 1
            counters["harness_failures"] = harness_failures
            if exploration:
                counters["exploration_failures"] = exploration
            if exploration >= self.MAX_EXPLORATION_FAILURES and task.get("depth", 0) < self.max_depth:
                status = "split"
            elif harness_failures < self.MAX_HARNESS_FAILURES:
                status = "retry"
                low = note.lower()
                if "request_budget" in low or "model_pool_exhausted" in low:
                    delay = defer or 0
                elif "provider busy" in low or "busy (rate-limited" in low:
                    delay = 0
                elif "timeout" in low or "timed out" in low:
                    delay = 30 * 60
                elif requests >= 3 or "incomplete" in low or "no usable" in low:
                    delay = 15 * 60
                else:
                    delay = 2 * 60
                not_before = now + max(defer, delay)
            else:
                status = "parked"
                stored_note = f"needs harness look: {condense(note, 500)}"
        elif attempts < self.max_attempts and not split_now:
            status, not_before = "retry", now + 300 * attempts ** 2
        elif task.get("depth", 0) < self.max_depth:
            status = "split"
        else:
            status = "parked"

        cool = None
        if (failed_model and not ok and self.counts_toward_circuit(failure_class)
                and model_failure_count >= 2):
            role = failed_role or "implementer"
            cool = (failed_model, f"{model_failure_count}x {failure_class} failures this run ({role})")
        return TaskTransition(status, not_before, failure_class, cool, counters, stored_note)


class TaskQueue:
    """Public queue API: lock, load, ask policy, and persist the result."""

    MAX_HARNESS_FAILURES = TaskPolicy.MAX_HARNESS_FAILURES
    MAX_EXPLORATION_FAILURES = TaskPolicy.MAX_EXPLORATION_FAILURES

    def __init__(self, store, policy=None, max_depth=2, max_queue=20,
                 max_children=3, max_descendants=12, run_state=None, run_lock=None):
        self.store = store
        self.policy = policy or TaskPolicy(max_depth=max_depth)
        self.path, self.done = store.pending, store.done
        self.lock = store.thread_lock
        self.max_depth = max_depth
        self.max_queue = max_queue
        self.max_children = max_children
        self.max_descendants = max_descendants
        self.run_state = run_state if run_state is not None else {}
        self.run_lock = run_lock or threading.RLock()

    @contextmanager
    def locked(self):
        with self.store.locked():
            yield

    def resolve(self, refs):
        with self.locked():
            rows = self.store.pending_rows() + self.store.done_rows()
        ids = {row["id"] for row in rows}
        titles = {row["title"].strip().lower(): row["id"] for row in rows}
        out, missing = [], []
        for ref in refs:
            key = str(ref).strip()
            if key in ids:
                out.append(key)
            elif key.lower() in titles:
                out.append(titles[key.lower()])
            else:
                missing.append(ref)
        if missing:
            raise ValueError(f"no queued or finished task matches {missing}; "
                             "depend on a task's exact title or its id")
        return out

    def seen_titles(self):
        with self.locked():
            rows = self.store.pending_rows() + self.store.done_rows()
            return {row["title"].strip().lower() for row in rows}

    def recent_titles(self, n=80):
        with self.locked():
            rows = sorted(self.store.pending_rows() + self.store.done_rows(),
                          key=lambda row: row.get("created", 0))
        return list(dict.fromkeys(row["title"] for row in rows))[-n:]

    def add(self, title, detail="", kind="feature", **meta):
        if kind not in KINDS:
            raise ValueError("unsupported task kind; self-editing harness tasks are disabled")
        with self.locked():
            rows = self.store.pending_rows()
            history = rows + self.store.done_rows()
            if len(rows) >= self.max_queue:
                return None
            if (not title.strip() or title.strip().lower()
                    in {row["title"].strip().lower() for row in history}):
                return None
            task = {"id": f"t{uuid.uuid4().hex[:12]}", "title": title.strip(),
                    "detail": detail.strip(), "kind": kind, "attempts": 0,
                    "created": time.time(), "priority": 0, "depth": 0}
            task.update({key: meta[key] for key in META if meta.get(key) is not None})
            if task.get("execution_class", "standard") not in ("standard", "fast"):
                raise ValueError("execution_class must be standard or fast")
            if task.get("request_class") not in (None, "small", "medium", "manual"):
                raise ValueError("request_class must be small, medium or manual")
            if task.get("allow_test_changes"):
                if task.get("origin") not in TEST_CHANGE_ORIGINS:
                    raise ValueError("only a person can allow a task to change existing tests")
                task["allow_test_changes"] = True
            criteria(task)
            validate_scope(task)
            known = {row["id"]: row for row in history}
            dependencies = task.get("depends_on", [])
            if (not isinstance(dependencies, list)
                    or any(not isinstance(dep, str) or dep not in known for dep in dependencies)):
                raise ValueError("dependencies must reference existing task IDs")
            parent = task.get("parent")
            if parent:
                if parent not in known:
                    raise ValueError("recursive tasks must reference an existing parent")
                task["depth"] = known[parent].get("depth", 0) + 1
                task["root"] = known[parent].get("root", parent)
                if (task["depth"] > self.max_depth
                        or sum(row.get("parent") == parent for row in history) >= self.max_children
                        or sum(row.get("root") == task["root"] and row["id"] != task["root"]
                               for row in history) >= self.max_descendants):
                    return None
            else:
                task["depth"], task["root"] = 0, task["id"]
            rows.append(task)
            self.store.write_pending(rows)
            return task

    def repoint(self, old, new):
        if not new or old == new:
            return 0
        with self.locked():
            rows, moved = self.store.pending_rows(), 0
            for row in rows:
                deps = row.get("depends_on") or []
                if old in deps:
                    row["depends_on"] = [dep for dep in deps if dep != old] + [new]
                    moved += 1
            if moved:
                self.store.write_pending(rows)
            return moved

    def strand(self, tid, why):
        with self.locked():
            rows = self.store.pending_rows()
            dead, parked, changed = {tid}, [], True
            while changed:
                changed = False
                for row in rows:
                    if row["id"] in dead or row.get("claimed"):
                        continue
                    if dead & set(row.get("depends_on") or []):
                        dead.add(row["id"])
                        changed = True
            now, keep = time.time(), []
            for row in rows:
                if row["id"] in dead and row["id"] != tid and not row.get("claimed"):
                    row["status"], row["finished"] = "parked", now
                    row["note"] = f"blocked: {why}"[-800:]
                    parked.append(row)
                else:
                    keep.append(row)
            if parked:
                self.store.write_pending(keep)
                self.store.append_done(parked)
            return [row["title"] for row in parked]

    def ready(self):
        now = time.time()
        with self.locked():
            complete = {row["id"] for row in self.store.done_rows()
                        if row.get("status") == "done"}
            return [row for row in self.store.pending_rows()
                    if not row.get("claimed") and row.get("not_before", 0) <= now
                    and set(row.get("depends_on", [])).issubset(complete)]

    selection_score = staticmethod(TaskPolicy.selection_score)

    def claim(self, requests_left=None):
        with self.locked():
            rows, now = self.store.pending_rows(), time.time()
            complete = {row["id"] for row in self.store.done_rows()
                        if row.get("status") == "done"}
            tid = self.policy.claim(rows, complete, now, requests_left)
            if tid is None:
                return None
            task = next(row for row in rows if row["id"] == tid)
            task["claimed"] = now
            task["selection"] = {
                "score": round(self.policy.selection_score(task, now), 2),
                "estimated_requests": estimate_requests(task), "at": now}
            self.store.write_pending(rows)
            return task

    def release(self, tid, ok, note="", defer=0, split_now=False, failure_class="task",
                park=False, requests=0, failed_model=None, failed_role=None, ledger=None,
                turn_seconds=0, role_turns=0, stage=None):
        with self.locked():
            rows = self.store.pending_rows()
            task = next((row for row in rows if row["id"] == tid), None)
            if task is None:
                return None
            rows = [row for row in rows if row["id"] != tid]
            task.pop("claimed", None)
            task["requests_spent"] = (task.get("requests_spent", 0)
                                      + max(0, int(requests or 0)))
            task["turn_seconds"] = (task.get("turn_seconds", 0)
                                    + max(0, float(turn_seconds or 0)))
            task["role_turns"] = (task.get("role_turns", 0)
                                  + max(0, int(role_turns or 0)))
            now, model_failure_count = time.time(), 0
            if failed_model and not ok:
                failures = task.get("failed_models", [])
                failures.append({"model": failed_model,
                                 "role": failed_role or "implementer",
                                 "class": failure_class,
                                 "requests": max(0, int(requests or 0)), "at": now,
                                 "note": condense(note, 200)})
                task["failed_models"] = failures[-8:]
                if self.policy.counts_toward_circuit(failure_class):
                    key = f"{failed_model}|{failed_role or 'implementer'}"
                    with self.run_lock:
                        self.run_state.setdefault("model_failures", {})
                        model_failure_count = self.run_state["model_failures"].get(key, 0) + 1
                        self.run_state["model_failures"][key] = model_failure_count
            transition = self.policy.release(
                task, ok, note, defer, split_now, failure_class, park, requests,
                turn_seconds, role_turns, stage, failed_model, failed_role,
                model_failure_count, now)
            task.update(transition.counters)
            task["status"] = transition.status
            if transition.not_before is not None:
                task["not_before"] = transition.not_before
            if transition.note is not None:
                task["note"] = transition.note
            format_error = not ok and not defer and failure_class == "review_format"
            harness = not ok and not defer and failure_class == "harness"
            effective_park = bool(park or format_error) and not ok and not defer
            if harness and not effective_park:
                task["harness_note"] = condense(note, 400)
            elif not ok and not defer and not effective_park:
                task["notes"] = (task.get("notes", []) + [condense(note, 800)])[-2:]
            task["finished"] = now
            self.store.write_pending(rows + ([task] if transition.status == "retry" else []))
            if transition.status != "retry":
                self.store.append_done([task])
            if transition.cool_model and ledger is not None:
                ledger.cool(*transition.cool_model)
            return task

    def pending(self):
        with self.locked():
            return self.store.pending_rows()

    def get(self, tid):
        with self.locked():
            return next((row for row in self.store.pending_rows() if row["id"] == tid), None)

    @staticmethod
    def blocked_by(tid, rows):
        doomed, grew = {tid}, True
        while grew:
            grew = False
            for row in rows:
                if row["id"] not in doomed and doomed.intersection(row.get("depends_on") or []):
                    doomed.add(row["id"])
                    grew = True
        return [row for row in rows if row["id"] in doomed and row["id"] != tid]

    def remove(self, tid, cascade=False):
        with self.locked():
            rows = self.store.pending_rows()
            if not any(row["id"] == tid for row in rows):
                raise KeyError(tid)
            doomed = self.blocked_by(tid, rows)
            if doomed and not cascade:
                raise ValueError(f"{len(doomed)} queued task(s) depend on this one: "
                                 + ", ".join(row["title"] for row in doomed))
            gone = {tid} | {row["id"] for row in doomed}
            self.store.write_pending([row for row in rows if row["id"] not in gone])
            return [row for row in rows if row["id"] in gone]

    def clear(self, include_claimed=False):
        with self.locked():
            rows = self.store.pending_rows()
            kept = [] if include_claimed else [row for row in rows if row.get("claimed")]
            keep = {row["id"] for row in kept}
            self.store.write_pending(kept)
            return [row for row in rows if row["id"] not in keep], kept

    def dismiss_finished(self, tid=None):
        with self.locked():
            rows = self.store.done_rows()
            active = {row["id"] for row in self.store.pending_rows()}
            eligible = [row for row in rows if row.get("status") in ("split", "parked")
                        and not row.get("dismissed_at") and row["id"] not in active]
            selected = [row for row in eligible if tid is None or row["id"] == tid]
            if tid is not None and not selected:
                raise ValueError("task is not an undismissed split or parked task, or is queued again")
            at = time.time()
            for row in selected:
                row["dismissed_at"] = at
            self.store.write_done(rows)
            return list(dict.fromkeys(row["id"] for row in selected))

    def edit(self, tid, title=None, detail=None, kind=None, priority=None, acceptance=None,
             allow_test_changes=None, allowed_paths=None):
        with self.locked():
            rows = self.store.pending_rows()
            task = next((row for row in rows if row["id"] == tid), None)
            if task is None:
                raise KeyError(tid)
            if task.get("claimed"):
                raise ValueError("a worker is running this task right now; stop the swarm, "
                                 "or wait for the attempt to finish")
            new = dict(task)
            if title is not None:
                if not title.strip():
                    raise ValueError("a task needs a title")
                taken = {row["title"].strip().lower()
                         for row in rows + self.store.done_rows() if row["id"] != tid}
                if title.strip().lower() in taken:
                    raise ValueError("another task already has that title")
                new["title"] = title.strip()
            if detail is not None:
                new["detail"] = detail.strip()
            if kind is not None:
                if kind not in KINDS:
                    raise ValueError(f"kind must be one of {', '.join(KINDS)}")
                new["kind"] = kind
            if priority is not None:
                if not 0 <= int(priority) <= 2:
                    raise ValueError("priority must be 0, 1 or 2")
                new["priority"] = int(priority)
            if acceptance is not None:
                new["acceptance"] = [item.strip() for item in acceptance if item and item.strip()] or None
                if new["acceptance"] is None:
                    del new["acceptance"]
            if allow_test_changes is not None:
                new["allow_test_changes"] = bool(allow_test_changes)
                if not new["allow_test_changes"]:
                    del new["allow_test_changes"]
            if allowed_paths is not None:
                # A packet whose scope leaves out a file its own change has to touch can never
                # pass (the SkyRing stamina packets: three copies of one size check, two of
                # them out of scope). Widening it is how such a packet is repaired in place.
                new["allowed_paths"] = list(dict.fromkeys(p.strip() for p in allowed_paths
                                                          if p and p.strip()))
                validate_scope(new)
            criteria(new)
            new.pop("not_before", None)
            self.store.write_pending([new if row["id"] == tid else row for row in rows])
            return new

    def retry_now(self, tid):
        with self.locked():
            rows = self.store.pending_rows()
            task = next((row for row in rows if row["id"] == tid), None)
            if task is None:
                raise KeyError(tid)
            task.pop("not_before", None)
            self.store.write_pending(rows)
            return task

    def requeue(self, tid):
        with self.locked():
            done = self.store.done_rows()
            task = next((row for row in reversed(done) if row["id"] == tid), None)
            if task is None:
                raise KeyError(tid)
            rows = self.store.pending_rows()
            if any(row["id"] == tid for row in rows):
                raise ValueError("that task is already in the queue")
            if len(rows) >= self.max_queue:
                raise ValueError("the queue is full")
            task = dict(task, attempts=0, harness_failures=0, origin="human")
            for key in ("status", "finished", "not_before", "claimed", "note",
                        "harness_note", "dismissed_at"):
                task.pop(key, None)
            rows.append(task)
            self.store.write_pending(rows)
            self.store.write_done([row for row in done if row["id"] != tid])
            return task

    def recover(self, accepted=None):
        with self.locked():
            rows, done = self.store.pending_rows(), self.store.done_rows()
            accepted = accepted or {}
            for task in rows:
                task.pop("claimed", None)
                if task["id"] in accepted:
                    task.update(status="done",
                                note=f"Recovered integrated commit {accepted[task['id']]}",
                                finished=time.time())
                    if not any(row["id"] == task["id"] for row in done):
                        done.append(task)
            self.store.write_done(done)
            self.store.write_pending([row for row in rows if row["id"] not in accepted])
