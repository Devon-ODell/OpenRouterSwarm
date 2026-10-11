"""Regressions found by watching the first live SkyRing shift (2026-10-11).

Numbers follow bench/skyring/GUIDANCE.md (bugs 1-12, N1-N7, stalls S1-S8); each class says
which one it guards. The first:

1. An attempt stopped by a provider outage or the allowance re-raised before its requests were
   banked, so the task's requests_spent stayed 0 after 16+ requests: task_request_cap could never
   stop it and the scorecard could not see the attempts.
"""
import threading
from unittest.mock import patch

from swarm import swarmd
from tests.test_attempt_stages import RepoCase


def _task():
    return {"id": "t1", "title": "Add a stdlib validator", "detail": "write tools/x.py",
            "kind": "feature", "origin": "human", "acceptance": ["tools/x.py exists"]}


class DeferredRequestsTests(RepoCase):
    def outage(self, prompt, cwd, c_, role, worker, *a):
        swarmd.count_requests(worker, 8)            # what the real turn's sidecar would record
        raise swarmd.ProviderDown(a[-1] if a else "a:free", "provider failed mid-reply")

    def test_a_deferred_attempt_banks_its_requests_and_journals_itself(self):
        w = self.worker()
        with patch.object(swarmd, "flint", side_effect=self.outage), \
                self.assertRaises(swarmd.ProviderDown):
            w.do_task(_task(), "goal")
        self.assertEqual(w.attempt_requests, 8)
        self.assertEqual(swarmd.count_requests("w0"), 0)          # banked, not left behind
        rows = [r for r in swarmd._read(swarmd.STATE / "journal.jsonl")
                if r.get("event") == "attempt_deferred"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["id"], rows[0]["requests"], rows[0]["reason"]),
                         ("t1", 8, "ProviderDown"))

    def test_the_worker_loop_charges_them_to_the_task(self):
        q = swarmd.Queue()
        task = q.add(**{k: v for k, v in _task().items() if k != "id"})
        stop = threading.Event()

        def outage(*args):
            stop.set()                              # let the loop exit after this release
            return self.outage(*args)

        w = swarmd.Worker(0, self.cfg(), q, budget=None, stop=stop)
        w.name = "w0"
        with patch.object(swarmd, "flint", side_effect=outage), \
                patch.object(swarmd, "read_goal", return_value="goal"):
            w.run()
        row = next(r for r in q.pending() if r["id"] == task["id"])
        self.assertEqual(row["requests_spent"], 8)
        self.assertEqual(row.get("attempts", 0), 0)               # an outage costs no attempt
        self.assertEqual(row.get("role_turns"), 1)


class BackoffIsNotModelTimeTests(RepoCase):
    """2. While a deferred task waits for a model, the worker said it was still on the last
    model's turn, and the wait was banked as model time."""

    def test_the_wait_after_an_outage_is_reported_and_banked_as_backoff(self):
        q = swarmd.Queue()
        q.add(**{k: v for k, v in _task().items() if k != "id"})
        seen = []
        holder = {}

        class RecordingStop(threading.Event):
            def wait(self, timeout=None):
                w = holder.get("w")
                if w is not None and self.is_set():
                    seen.append((w.doing, w.state))
                return super().wait(0)

        stop = RecordingStop()

        def outage(prompt, cwd, c_, role, worker, *a):
            stop.set()
            raise swarmd.ProviderDown("a:free", "provider failed mid-reply")

        w = swarmd.Worker(0, self.cfg(), q, budget=None, stop=stop)
        w.name = "w0"
        holder["w"] = w
        with patch.object(swarmd, "flint", side_effect=outage), \
                patch.object(swarmd, "read_goal", return_value="goal"):
            w.run()
        self.assertTrue(seen, "the worker never waited after the outage")
        doing, state = seen[0]
        self.assertEqual(state, "provider_backoff")
        self.assertIn("waiting for a model", doing)
        self.assertNotIn("implementer with", doing)


class DeferredWorkIsResumedTests(RepoCase):
    """4. A turn killed by a provider error left its edits on the attempt's branch, but the
    retry in the same run started from nothing: only a daemon restart looked for that work."""

    def test_the_next_attempt_in_the_same_run_starts_from_the_deferred_work(self):
        from tests.test_attempt_stages import review
        calls = []

        def first(prompt, cwd, c_, role, worker, *a):
            if role == "implementer":
                (cwd / "feature.txt").write_text("half written\n")
                raise swarmd.ProviderDown(a[-1], "provider failed mid-reply")
            return "done"

        # Two models: the one that failed rests, and the retry needs another to take it.
        w = self.worker(self.cfg(models=["a:free", "b:free"]))
        with patch.object(swarmd, "flint", side_effect=first), \
                self.assertRaises(swarmd.ProviderDown):
            w.do_task(_task(), "goal")
        self.assertIn("t1", swarmd.load_resume())

        def second(prompt, cwd, c_, role, worker, *a):
            calls.append((role, (cwd / "feature.txt").exists() and (cwd / "feature.txt").read_text()))
            if role == "implementer":
                (cwd / "feature.txt").write_text("finished\n")
                return "done"
            return review(prompt) if role == "adversary" else "done"

        with patch.object(swarmd, "flint", side_effect=second):
            ok, note = w.do_task(_task(), "goal")
        self.assertTrue(ok, note)
        # The retry re-applied the deferred edits and verified them instead of re-implementing:
        # the first turn it made already saw them, and no implementer turn started from nothing.
        self.assertEqual(calls[0][1], "half written\n")
        self.assertNotIn(("implementer", False), calls)
        self.assertNotIn("t1", swarmd.load_resume())             # handed over once


class FollowUpTests(RepoCase):
    """11. A judge's follow-up was queued with no acceptance criteria and no file limits: the
    judge's criteria were dropped on parse, and the scope gate admits criteria-less tasks."""

    def follow(self, ideas, **cfg):
        from swarm import attempt_runner
        from tests.test_attempt_runner import build_deps
        q = swarmd.Queue()
        parent = q.add("Add a stdlib validator", "x", "feature", acceptance=["tools/x.py exists"])
        w = swarmd.Worker(0, self.cfg(**cfg), q, budget=None, stop=threading.Event())
        deps = build_deps()
        runner = attempt_runner.AttemptRunner(w, deps)
        runner.follow_up(parent, {"follow_ups": ideas}, 0.9, False, self.cfg(**cfg))
        return [t for t in q.pending() if t.get("origin") == "follow_up"], deps

    def test_criteria_and_paths_from_the_judge_are_kept(self):
        from swarm import learn
        scores = learn.parse_scores('{"impact": 5, "creativity": 4, "quality": 6, "follow_ups": ['
                                    '{"title": "Wire the validator into verify", "detail": "d", '
                                    '"acceptance": ["python3 tools/verify.py --profile local passes"], '
                                    '"paths": ["tools/verify.py", "tools/tests"]}]}')
        queued, _ = self.follow(scores["follow_ups"])
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["acceptance"], ["python3 tools/verify.py --profile local passes"])
        self.assertEqual(queued[0]["allowed_paths"], ["tools/verify.py", "tools/tests/"])

    def test_a_follow_up_without_criteria_is_not_queued(self):
        queued, deps = self.follow([{"title": "Integrate it everywhere", "detail": "d",
                                     "acceptance": [], "paths": []}])
        self.assertEqual(queued, [])
        self.assertIn("follow_up_skipped", [c.args[0] for c in deps.journal.call_args_list])

    def test_a_fixed_backlog_can_turn_follow_ups_off(self):
        queued, _ = self.follow([{"title": "Next", "detail": "d", "acceptance": ["a check"],
                                  "paths": []}], follow_ups=False)
        self.assertEqual(queued, [])


class EveryReleaseChargesTests(RepoCase):
    """N2. Only the outage path charged the attempt's requests to the task. The 17:12 stop
    killed a hit-arbitration turn 7 requests in, and the task kept requests_spent 8 of 15."""

    def test_a_stopped_turn_charges_its_requests_and_journals_itself(self):
        q = swarmd.Queue()
        task = q.add(**{k: v for k, v in _task().items() if k != "id"})
        stop = threading.Event()

        def killed(prompt, cwd, c_, role, worker, *a):
            swarmd.count_requests(worker, 7)
            stop.set()
            raise swarmd.Stopped(f"{role} turn on a:free was stopped with the run")

        w = swarmd.Worker(0, self.cfg(), q, budget=None, stop=stop)
        w.name = "w0"
        with patch.object(swarmd, "flint", side_effect=killed), \
                patch.object(swarmd, "read_goal", return_value="goal"):
            w.run()
        row = next(r for r in q.pending() if r["id"] == task["id"])
        self.assertEqual(row["requests_spent"], 7)
        self.assertEqual(row.get("attempts", 0), 0)               # a stop costs no attempt
        self.assertEqual(row.get("role_turns"), 1)
        rows = [r for r in swarmd._read(swarmd.STATE / "journal.jsonl")
                if r.get("event") == "attempt_stopped"]
        self.assertEqual([(r["id"], r["requests"]) for r in rows], [(task["id"], 7)])


class DrainEndsTheBackoffTests(RepoCase):
    """N4. A deferred task's worker slept its whole retry delay after a drain began, so a
    source-change restart waited 120 s with nothing in flight (16:41:42 to 16:43:42)."""

    def test_a_drain_wakes_a_worker_waiting_for_a_model(self):
        import time
        q = swarmd.Queue()
        q.add(**{k: v for k, v in _task().items() if k != "id"})
        w = swarmd.Worker(0, self.cfg(), q, budget=None, stop=threading.Event())
        w.name = "w0"

        def outage(prompt, cwd, c_, role, worker, *a):
            w.tally.drain.set()                     # a source change arrives mid-turn
            raise swarmd.ProviderDown("a:free", "provider failed mid-reply")

        t0 = time.time()
        with patch.object(swarmd, "flint", side_effect=outage), \
                patch.object(swarmd, "read_goal", return_value="goal"):
            w.run()
        self.assertLess(time.time() - t0, 10, "the worker slept through the drain")


class ReconcileCountsWhatWasObservedTests(RepoCase):
    """N3. The run's reconciliation was computed before the local count was filled in, so
    `other_unattributed` was always the whole provider delta (138 of 138, with 154 observed)."""

    def test_requests_the_run_observed_are_not_unattributed(self):
        from swarm.metrics import Reconcile
        s = Reconcile.summary([], provider_before=0, provider_after=138, local_count=154)
        self.assertEqual((s["locally_observed"], s["other_unattributed"]), (154, 0))
        s = Reconcile.summary([], provider_before=0, provider_after=200, local_count=154)
        self.assertEqual(s["other_unattributed"], 46)


class TestsKeepOutOfTheReceiptLedgerTests(RepoCase):
    """9. Fixture agents wrote 804 `test:free` rows into ~/.albatross/routes.jsonl."""

    def test_the_suite_writes_receipts_somewhere_temporary(self):
        from pathlib import Path
        from swarm import receipts
        self.assertNotEqual(receipts.routes_path(),
                            Path("~/.albatross/routes.jsonl").expanduser())


class PacketDefectTests(RepoCase):
    """S5. The stamina packet failed three candidates on one `44 == 36` static_assert that
    lives in plugin/src/Bridge.cpp and tools/verify.py, neither in allowed_paths. The repeat
    went unseen (each gate log named its own temp directory) and the model was hung from the
    rafters for the packet's defect."""

    GATE = ("test ! -f feature.txt || { echo \"/tmp/x-$$/abi.cpp:4:1: error: static assertion "
            "failed due to requirement 'sizeof(ErWorld) == 36'\"; "
            "echo 'static_assert(sizeof(ErWorld) == 36);'; exit 1; }")

    def attempt(self, check_path):
        from tests.test_attempt_stages import _git, review
        (self.repo / check_path).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / check_path).write_text("static_assert(sizeof(ErWorld) == 36);\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "size check")
        w = self.worker(self.cfg(test_cmd=self.GATE, max_repairs=3))
        repairs = []

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                (cwd / "feature.txt").write_text("stamina_current\n")
                return "done"
            if role == "repair":
                repairs.append(role)
                return "tried"
            return review(prompt)

        task = {**_task(), "allowed_paths": ["feature.txt", "core/"]}
        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(task, "goal")
        return w, note, repairs

    def test_a_repeated_failure_in_a_file_the_packet_forbids_parks_the_packet(self):
        w, note, repairs = self.attempt("checks/abi.cpp")
        self.assertEqual(w.stage, "out_of_scope")
        self.assertIn("checks/abi.cpp:1", note)
        self.assertEqual(len(repairs), 1, "the second identical failure should end it")
        self.assertFalse((swarmd.STATE / "RAFTERS.md").exists(), "the model is not to blame")
        self.assertIn("packet_defect", [r.get("event") for r in
                                        swarmd._read(swarmd.STATE / "journal.jsonl")])

    def test_a_failing_check_inside_the_packet_stays_the_models_to_fix(self):
        w, note, repairs = self.attempt("core/abi.cpp")
        self.assertEqual(w.stage, "tests_failed")
        self.assertEqual(len(repairs), 1)   # still stops on the repeat, now that it is seen

    def test_a_test_outside_the_packet_failing_on_a_value_is_still_the_models(self):
        from swarm.workflow import out_of_scope_check
        failure = ('FAIL: test_greets (tests.test_x.T)\nTraceback (most recent call last):\n'
                   '  File "tests/test_x.py", line 5, in test_greets\n'
                   "    self.assertEqual(greet(), 'hello world')\n"
                   "AssertionError: 'hello wrld' != 'hello world'")
        grep = lambda text: ["tests/test_x.py:5"] if "hello world" in text else []
        self.assertEqual(out_of_scope_check(failure, ["src/x.py"], grep), "")

    def test_the_signature_ignores_log_names_timing_and_temp_directories(self):
        from swarm.workflow import failure_signature
        def gate(n, tmp):
            return (f'[{{"log": "gate-0{n}-candidate-{n}-0.log", "seconds": {n}.17, '
                    f'"tail": "FAIL  c_abi\\n/var/folders/T/skyring-verify-{tmp}/abi.cpp:4:31: '
                    f"note: expression evaluates to '44 == 36'\"}}]")
        self.assertEqual(failure_signature("tests_failed", gate(3, "u2qarf3_")),
                         failure_signature("tests_failed", gate(4, "k81bz0q_")))
        self.assertNotEqual(failure_signature("tests_failed", gate(3, "a")),
                            failure_signature("tests_failed", gate(3, "a").replace("44", "40")))


class JudgeSalvageTests(RepoCase):
    """N5/3. Two of five judge replies were lost whole over malformed follow-ups while their
    scores were well formed; the log named the model asked, not the one that answered."""

    REPLIES = [
        # 15:48: no final `}`.
        '{"impact": 9, "creativity": 6, "quality": 8, "goal_item": 0, "breakthrough": false, '
        '"why": "w", "lesson": "l", "follow_ups": [{"title": "Bridge determinism", "detail": "d", '
        '"acceptance_criteria": "Two runs match."}]',
        # 16:54: a follow-up missing its `}` and a stray quote.
        '{"impact": 5, "creativity": 4, "quality": 8, "goal_item": 7, "breakthrough": false, '
        '"why": "w", "lesson": "l", "follow_ups": [{"title": "A", "detail": "d", '
        '{"title": "B", "detail": "e"}]"}',
    ]

    def test_scores_survive_a_malformed_follow_up(self):
        from swarm import learn
        first, second = (learn.parse_scores(r) for r in self.REPLIES)
        self.assertEqual((first["impact"], first["quality"], first["salvaged"]), (9.0, 8.0, True))
        self.assertEqual(first["follow_ups"][0]["acceptance"], ["Two runs match."])
        self.assertEqual((second["impact"], second["quality"]), (5.0, 8.0))
        self.assertNotIn("salvaged", learn.parse_scores(self.REPLIES[0] + "}"))

    def test_the_log_names_the_model_that_answered(self):
        from swarm import attempt_runner
        from tests.test_attempt_runner import build_deps
        w = self.worker()
        deps = build_deps()
        runner = attempt_runner.AttemptRunner(w, deps)

        def handed_off(*a, **kw):
            w.last_model = "nemotron:free"          # laguna was busy; the turn was handed on
            return "no json here"

        w.call = handed_off
        w.wt = self.repo
        task = {**_task(), "detail": "d"}
        self.assertIsNone(runner.judge(task, "goal", "diff", "approve", "laguna:free",
                                       c=self.cfg(), attempt_budget=object()))
        logged = " ".join(str(c.args[0]) for c in deps.log.call_args_list)
        self.assertIn("judge nemotron:free returned no usable JSON", logged)


class ObservationFindingTests(RepoCase):
    """7. The first adversary filed `severity: "info"` with no verification, and its whole
    approval was rejected as malformed: 5 requests and 39 s to re-review the same candidate."""

    def review(self, finding):
        import json
        from swarm.workflow import parse_review
        return parse_review(json.dumps({
            "verdict": "approve", "tree": "abc1234", "summary": "ok",
            "checks": [{"criterion": "C1", "passed": True, "evidence": "test_trace passes"}],
            "findings": [finding]}), "abc1234", [{"id": "C1", "text": "trace is stable"}])

    def test_an_observation_does_not_cost_the_verdict(self):
        out = self.review({"severity": "info", "issue": "uses a 600-tick trace"})
        self.assertEqual(out["verdict"], "approve")
        self.assertEqual(out["findings"][0]["severity"], "minor")

    def test_an_observation_about_a_missing_criterion_still_blocks_approval(self):
        with self.assertRaises(ValueError):
            self.review({"severity": "note", "issue": "acceptance criterion C1 has no test: missing"})

    def test_a_blocker_still_needs_its_verification(self):
        with self.assertRaises(ValueError):
            self.review({"severity": "blocker", "issue": "crashes on empty input"})


class RoleStarvationTests(RepoCase):
    """S1. The implementer role was dots and laguna. Both rested for most of an hour while three
    nemotron models sat idle, the log said "every model is resting", and the worker woke for
    whichever pool model was back first, implementer or not."""

    C = {"models": ["dots:free", "laguna:free", "nemo:free"],
         "role_models": {"implementer": ["dots:free", "laguna:free"]}}

    def ledger(self):
        from swarm import learn
        L = learn.Ledger(swarmd.STATE / "learn.json")
        L.cool("dots:free", "down")
        L.cool("laguna:free", "busy", busy=True)
        return L

    def test_the_log_names_the_idle_models_the_role_keeps_out(self):
        text = swarmd.all_resting(self.ledger(), self.C, "implementer")
        self.assertIn("every model allowed to be implementer is resting", text)
        self.assertIn("laguna:free is back at", text)
        self.assertIn("idle but not allowed to be implementer: nemo:free", text)

    def test_the_worker_wakes_for_a_model_that_may_implement(self):
        from swarm import learn
        L = learn.Ledger(swarmd.STATE / "learn.json")
        L.cool("dots:free", "down")
        L.cool("laguna:free", "down")
        L.cool("nemo:free", "busy", busy=True)          # back soonest, but may not implement
        self.assertEqual(swarmd.next_wake(L, self.C)[0], "nemo:free")
        self.assertIn(swarmd.next_wake(L, self.C, "implementer")[0], ("dots:free", "laguna:free"))

    def test_role_fallback_lets_an_idle_model_stand_in(self):
        from swarm import agent_turn
        L = self.ledger()
        kw = dict(pool=swarmd.pool, paid_pool=lambda c: [], spend_left=lambda c: 0)
        self.assertIsNone(agent_turn.pick_model(L, self.C, role="implementer", **kw))
        self.assertEqual(agent_turn.pick_model(L, {**self.C, "role_fallback": True},
                                               role="implementer", **kw), "nemo:free")


class BusyModelIsPassedOverTests(RepoCase):
    """S2. Laguna was rate-limited on 6 of its 9 turns (960 s thrown away), and every good turn
    erased its rest, so the bandit drew it again as if nothing had happened."""

    def test_a_model_busy_three_times_this_hour_yields_to_a_calm_one(self):
        from swarm import learn
        L = learn.Ledger(swarmd.STATE / "learn.json")
        L.update("implementer", "laguna:free", 1.0, weight=20)   # its record says pick it
        for _ in range(3):
            L.cool("laguna:free", "rate-limited", busy=True)
            L.warm("laguna:free")                                  # a good turn in between
        self.assertEqual({L.pick("implementer", ["laguna:free", "nemo:free"]) for _ in range(20)},
                         {"nemo:free"})
        # With nothing calmer free it is still used rather than leaving the role empty.
        self.assertEqual(L.pick("implementer", ["laguna:free"]), "laguna:free")


class WorktreeCleanupTests(RepoCase):
    """5. SkyRing kept 16 worktrees for 6 tasks, 9 for one task, most with no edits or landed."""

    def attempt(self, edit, **cfg):
        from tests.test_attempt_stages import review

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                if edit:
                    (cwd / "feature.txt").write_text("x\n")
                return "done"
            return review(prompt)

        import shutil
        w = self.worker(self.cfg(**{"keep_worktrees": True, **cfg}))
        with patch.object(swarmd, "flint", side_effect=fake):
            w.do_task(_task(), "goal")
        # A kept worktree sits under the harness's own swarm/wt; the repo it belongs to is temp.
        self.addCleanup(shutil.rmtree, w.wt, ignore_errors=True)
        return w

    def test_a_landed_attempt_leaves_no_worktree(self):
        w = self.attempt(edit=True)
        self.assertEqual(w.stage, "accepted")
        self.assertFalse(w.wt.exists())
        # Nor an empty folder for the repository: 148 of those had piled up under swarm/wt.
        self.assertFalse(w.wt.parent.exists())

    def test_an_attempt_that_changed_nothing_leaves_no_worktree(self):
        w = self.attempt(edit=False)
        self.assertEqual(w.stage, "no_change")
        self.assertFalse(w.wt.exists())

    def test_unlanded_work_keeps_its_worktree_and_all_keeps_everything(self):
        w = self.attempt(edit=True, test_cmd="test ! -f feature.txt", max_repairs=0)
        self.assertEqual(w.stage, "tests_failed")
        self.assertTrue(w.wt.exists())
        w = self.attempt(edit=False, keep_worktrees="all")
        self.assertTrue(w.wt.exists())


class RepoIdentityCaseTests(RepoCase):
    """8. .../SkyRing and .../skyRing are one directory on macOS but were two swarms, each with
    its own queue, ledger, config and daemon lock."""

    def setUp(self):
        super().setUp()
        self.dir = self.root / "skyRing"
        self.dir.mkdir()
        if not (self.root / "SKYRING").exists():
            self.skipTest("case-sensitive filesystem")
        p = patch.object(swarmd, "STATE_ROOT", self.root / "states")
        p.start()
        self.addCleanup(p.stop)
        (self.root / "states").mkdir()

    def test_every_spelling_is_one_swarm(self):
        names = {swarmd.repo_slug(self.root / n) for n in ("skyRing", "SkyRing", "SKYRING")}
        self.assertEqual(names, {swarmd._slug(swarmd.true_path(self.dir))})
        self.assertTrue(next(iter(names)).startswith("skyRing-"))

    def test_a_swarm_that_ran_under_another_spelling_keeps_its_state(self):
        legacy = swarmd._slug(self.dir.resolve().parent / "SkyRing")
        (self.root / "states" / legacy).mkdir()
        (self.root / "states" / legacy / "journal.jsonl").write_text("{}\n")
        for n in ("skyRing", "SkyRing", "SKYRING"):
            self.assertEqual(swarmd.repo_slug(self.root / n), legacy)


class WidenPacketScopeTests(RepoCase):
    """S5 follow-through. The stamina packets were repaired by widening allowed_paths in place,
    which the queue's edit could not do."""

    def test_an_edit_widens_the_scope_and_lifts_the_hold(self):
        q = swarmd.Queue()
        t = q.add("Let Skyrim supply the stamina pool", "d", "feature",
                  acceptance=["verify passes"], allowed_paths=["core/src/ffi.rs"])
        q.release(t["id"], False, "held", defer=86400, failure_class="harness")
        new = q.edit(t["id"], allowed_paths=["core/src/ffi.rs", "plugin/src/Bridge.cpp",
                                             "tools/verify.py"])
        self.assertEqual(new["allowed_paths"],
                         ["core/src/ffi.rs", "plugin/src/Bridge.cpp", "tools/verify.py"])
        self.assertNotIn("not_before", new)
        with self.assertRaises(ValueError):
            q.edit(t["id"], allowed_paths=["../outside.py"])


class UnscoredRewardPolicyTests(RepoCase):
    """N6. An accepted change the judge did not score earned 0.6 at full weight (judged work
    averaged 0.83). Which policy is right is an A/B (bench/unscored_ab.py); these pin the arms."""

    def test_each_policy_credits_what_it_says(self):
        from swarm import learn
        self.assertEqual(learn.unscored_update("neutral", 0.5),
                         (learn.reward("accepted", None, 0.5), learn.REWARD_WEIGHT))
        self.assertEqual(learn.unscored_update("floor", 0.5), (0.45, 1.0))
        self.assertEqual(learn.unscored_update("impute", 0.5, model_mean=0.86),
                         (0.86, learn.REWARD_WEIGHT))
        self.assertEqual(learn.unscored_update("impute", 0.5),          # nothing judged yet
                         learn.unscored_update("neutral", 0.5))
        self.assertIsNone(learn.unscored_update("skip", 0.5))

    def test_ab_splits_by_task_and_keeps_a_task_in_its_arm(self):
        from swarm import learn
        c = {"unscored_reward": "ab", "unscored_ab_arms": ["neutral", "impute"]}
        arms = {learn.unscored_policy(c, f"t{i}") for i in range(40)}
        self.assertEqual(arms, {"neutral", "impute"})
        self.assertEqual(learn.unscored_policy(c, "t7"), learn.unscored_policy(c, "t7"))
        # impute is the default the A/B chose (bench/history/unscored_ab.jsonl, 2026-10-11).
        self.assertEqual(learn.unscored_policy({}, "t7"), "impute")
        self.assertEqual(learn.unscored_policy({"unscored_reward": "bogus"}, "t7"), "impute")
        self.assertEqual(learn.unscored_policy({"unscored_reward": "neutral"}, "t7"), "neutral")

    def test_scored_mean_is_the_implementers_own_then_the_pool(self):
        from swarm import learn
        L = learn.Ledger(swarmd.STATE / "learn.json")
        self.assertIsNone(L.scored_mean("a:free"))
        for impl, r, scores in (("a:free", 0.9, {"impact": 7}), ("b:free", 0.7, {"impact": 5}),
                                ("a:free", 0.6, None)):           # unscored: not evidence
            L.record_accepted({"implementer": impl, "reward": r, "scores": scores,
                               "quality_score": None if scores else "unscored"})
        self.assertEqual(L.scored_mean("a:free"), 0.9)
        self.assertAlmostEqual(L.scored_mean("c:free"), 0.8)

    def test_skip_lands_the_work_but_teaches_the_bandit_nothing(self):
        from tests.test_attempt_stages import review
        w = self.worker(self.cfg(unscored_reward="skip"))

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                (cwd / "feature.txt").write_text("x\n")
                return "done"
            return review(prompt) if role == "adversary" else "no json at all"

        with patch.object(swarmd, "flint", side_effect=fake):
            ok, _ = w.do_task(_task(), "goal")
        self.assertTrue(ok)
        arms = w.ledger.leaderboard()
        self.assertFalse([r for r in arms if r["role"] == "implementer"])
        row = next(r for r in swarmd._read(swarmd.STATE / "journal.jsonl")
                   if r.get("event") == "reward")
        self.assertEqual(row["unscored_policy"], "skip")

    def test_the_ab_runner_decides_on_a_synthetic_history(self):
        import json
        import subprocess
        import sys
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        state = self.root / "states" / "r-000000"
        state.mkdir(parents=True)
        rows = []
        for i in range(30):
            m = "good:free" if i % 2 else "fair:free"
            ok = i % 5 != 0
            rows.append({"event": "attempt", "implementer": m,
                         "stage": "accepted" if ok else "tests_failed",
                         "failure_class": None if ok else "task"})
            if ok:
                rows.append({"event": "reward", "stage": "accepted", "implementer": m,
                             "reward": 0.9 if m == "good:free" else 0.75, "novelty": 0.5,
                             "scores": {"impact": 7, "creativity": 6, "quality": 8}
                             if i % 7 else {}})
        (state / "journal.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        out = subprocess.run([sys.executable, str(root / "bench" / "unscored_ab.py"), "--reps",
                              "40", "--horizon", "30", "--no-record", "--state",
                              str(self.root / "states")], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("decision: ", out.stdout)
        self.assertIn("per-model (primary)", out.stdout)
