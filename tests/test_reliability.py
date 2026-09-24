"""Offline regression tests: no real API calls or user repositories."""
import contextlib
import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import flint
from swarm import swarmd
from swarm.budget import Budget
from tests.helpers import review


def agent():
    a = flint.Agent.__new__(flint.Agent)
    a.headless = True
    a.read_only = False
    a.yolo = True
    a.always = set()
    a.messages = []
    a.model = "test:free"
    a.throttle = Mock()
    a.last_prompt_tokens = 0
    return a


class Stream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        yield from self.chunks

    def close(self):
        self.closed = True


def chunk(content="", finish=None, calls=None):
    return NS(choices=[NS(finish_reason=finish,
                         delta=NS(content=content, tool_calls=calls or []))])


class AgentTests(unittest.TestCase):
    def test_read_only_never_executes_write(self):
        a = agent()
        a.read_only = True
        with patch.dict(flint.TOOLS, write_file=Mock()) as tools:
            result = a.run_tool({"function": {"name": "write_file", "arguments": '{}'}})
            self.assertTrue(result.startswith("Denied:"))
            tools["write_file"].assert_not_called()
        names = [t["function"]["name"] for t in a._request_kwargs()["tools"]]
        self.assertNotIn("bash", names)
        self.assertIn("read_file", names)

    def test_stream_closes_and_rejects_truncated_tools(self):
        a = agent()
        for finish in (None, "length", "error"):
            stream = Stream([chunk(finish=finish, calls=[NS(index=0, id="c1",
                             function=NS(name="write_file", arguments='{"path":'))])])
            a.client = NS(chat=NS(completions=NS(create=Mock(return_value=stream))))
            with self.assertRaises(flint.IncompleteResponse):
                a._stream_once()
            self.assertTrue(stream.closed)

    def test_stream_reassembles_tools(self):
        a = agent()
        stream = Stream([
            chunk(calls=[NS(index=0, id="c1", function=NS(name="read_file", arguments='{"path":'))]),
            chunk(finish="tool_calls", calls=[NS(index=0, id=None, function=NS(name=None, arguments='"x"}'))]),
        ])
        a.client = NS(chat=NS(completions=NS(create=Mock(return_value=stream))))
        _, calls = a._stream_once()
        self.assertEqual(json.loads(calls[0]["args"]), {"path": "x"})
        self.assertTrue(stream.closed)

    def test_step_limit_is_not_success(self):
        a = agent()
        a.complete = Mock(return_value=("working", [{"id": "x", "name": "read_file", "args": "{}"}]))
        a.run_tool = Mock(return_value="ok")
        with patch.object(flint, "MAX_STEPS", 1), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(flint.StepLimitReached):
                a.turn("task")

    def test_ctrl_c_exits_130_without_traceback(self):
        a = agent()
        a.turn = Mock(side_effect=KeyboardInterrupt)
        error = io.StringIO()
        with patch.object(flint, "Agent", return_value=a), patch.object(sys, "argv", ["flint", "-p", "task"]), contextlib.redirect_stderr(error):
            with self.assertRaises(SystemExit) as exc:
                flint.main()
        self.assertEqual(exc.exception.code, 130)
        self.assertNotIn("Traceback", error.getvalue())

    def test_stream_429_retries_then_succeeds(self):
        a = agent()
        error = flint.APIError("provider busy", request=Mock(), body={"code": 429, "message": "upstream overloaded"})
        a._stream_once = Mock(side_effect=[error, ("done", [])])
        with patch.object(flint.time, "sleep"), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(a.complete(), ("done", []))
        self.assertEqual(a.throttle.acquire.call_count, 2)

    def test_stream_daily_cap_is_not_retried(self):
        a = agent()
        error = flint.APIError("daily", request=Mock(), body={"code": 429, "message": "free-models-per-day"})
        a._stream_once = Mock(side_effect=error)
        with self.assertRaises(flint.DailyCapReached):
            a.complete()
        self.assertEqual(a._stream_once.call_count, 1)
        a.throttle.block.assert_called_once()


class BudgetTests(unittest.TestCase):
    def test_reserve_is_enforced_using_locked_request_state(self):
        b = Budget(cap=50, reserve=10, owner_window=("00:00", "00:00"))
        state = {"day": datetime.datetime.now(datetime.timezone.utc).date().isoformat(), "count": 40}
        self.assertFalse(b.check(state=state)[0])

    def test_throttle_checks_budget_before_increment(self):
        with tempfile.TemporaryDirectory() as td, patch.object(flint, "STATE_DIR", Path(td)), patch.dict(os.environ, FLINT_SWARM_BUDGET='{"cap":50,"reserve":10}'):
            throttle = flint.Throttle()
            with patch("swarm.budget.Budget.check", return_value=(False, 10, "reserve")):
                with self.assertRaises(flint.BudgetPaused):
                    throttle.acquire()
            self.assertEqual(throttle.today(), 0)


class SwarmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.logs = self.root / "logs"
        self.logs.mkdir()
        for name, value in (("STATE", self.state), ("LOGS", self.logs), ("HERE", self.root)):
            p = patch.object(swarmd, name, value)
            p.start()
            self.addCleanup(p.stop)
        swarmd._stop.clear()

    def test_queue_separate_instances_do_not_lose_tasks(self):
        def add(i):
            swarmd.Queue().add(f"task {i}")
        threads = [threading.Thread(target=add, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        q = swarmd.Queue()
        self.assertEqual(len(q.pending()), 20)
        claimed = q.claim()
        q.recover()
        self.assertEqual(q.claim()["id"], claimed["id"])

    def test_worker_does_not_inject_empty_api_key(self):
        budget = Budget(cap=50, reserve=10)
        budget.check = Mock(return_value=(True, 0, "ok"))
        cfg = {"model": "test", "python": sys.executable}
        p = Mock(returncode=0)
        p.communicate.return_value = ("APPROVE: fine", None)
        @contextlib.contextmanager
        def fake_process(cmd, **kw):
            self.assertNotIn("OPENROUTER_API_KEY", kw["env"])
            self.assertIn("--read-only", cmd)
            self.assertIn("FLINT_SWARM_BUDGET", kw["env"])
            yield p
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}), patch.object(swarmd, "process", fake_process):
            self.assertEqual(swarmd.flint("task", self.root, cfg, "planner", "w0", budget, 3), "APPROVE: fine")

    def test_quota_pause_does_not_exhaust_task_retries(self):
        q = swarmd.Queue()
        task = q.add("task")
        for _ in range(4):
            q.release(task["id"], False, "quota", defer=60)
        self.assertEqual(q.pending()[0]["attempts"], 0)
        self.assertEqual(q.pending()[0]["status"], "retry")

    def repo(self):
        repo = self.root / "repo"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-b", "main")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test")
        (repo / "app.txt").write_text("baseline\n")
        git("add", ".")
        git("commit", "-m", "baseline")
        return repo, git

    def test_worker_keeps_dirty_checkout_and_returns_review_branch(self):
        repo, git = self.repo()
        head = git("rev-parse", "HEAD")
        (repo / "app.txt").write_text("user work in progress\n")
        cfg = {"repo": str(repo), "base_branch": "main", "test_cmd": "true",
               "steps": {"architect": 0, "implementer": 2, "adversary": 2}}
        worker = swarmd.Worker(0, cfg, swarmd.Queue(), Mock(), threading.Event())
        def fake_flint(prompt, cwd, c, role, *args):
            if role == "implementer":
                (cwd / "app.txt").write_text("worker change\n")
                return "implemented"
            return review(prompt) if role == "adversary" else "done"
        with patch.object(swarmd, "flint", side_effect=fake_flint):
            ok, note = worker.do_task({"id": "task", "title": "improve", "detail": "change app"}, "goal")
        self.assertTrue(ok, note)
        self.assertEqual(git("rev-parse", "HEAD"), head)
        self.assertEqual((repo / "app.txt").read_text(), "user work in progress\n")
        self.assertEqual(git("show", f"{worker.branch}:app.txt"), "worker change")

    def test_baseline_failure_spends_no_requests(self):
        repo, _ = self.repo()
        cfg = {"repo": str(repo), "base_branch": "main", "test_cmd": "false"}
        worker = swarmd.Worker(0, cfg, swarmd.Queue(), Mock(), threading.Event())
        with patch.object(swarmd, "flint") as model:
            ok, note = worker.do_task({"id": "task", "title": "fix", "detail": ""}, "goal")
        self.assertFalse(ok)
        self.assertIn("baseline", note)
        model.assert_not_called()

    def test_review_must_explicitly_approve(self):
        repo, git = self.repo()
        head = git("rev-parse", "HEAD")
        cfg = {"repo": str(repo), "base_branch": "main", "test_cmd": "true",
               "steps": {"architect": 0, "implementer": 2, "adversary": 2}}
        for verdict in ("", "looks fine", "REJECT: defect", "APPROVE: fine\nREJECT: defect"):
            with self.subTest(verdict=verdict):
                worker = swarmd.Worker(0, cfg, swarmd.Queue(), Mock(), threading.Event())
                def fake_flint(prompt, cwd, c, role, *args):
                    if role == "implementer":
                        (cwd / "app.txt").write_text("worker change\n")
                        return "implemented"
                    return verdict
                with patch.object(swarmd, "flint", side_effect=fake_flint):
                    ok, _ = worker.do_task({"id": "task", "title": "fix", "detail": ""}, "goal")
                self.assertFalse(ok)
                self.assertEqual(git("rev-parse", "HEAD"), head)
                self.assertEqual((worker.wt / "app.txt").read_text(), "worker change\n")

    def test_missing_config_reports_setup_instead_of_traceback(self):
        config = self.root / "config.json"
        config.write_text(json.dumps({"repo": "__REPO__"}))
        with patch.object(swarmd, "CONFIG", config), self.assertRaisesRegex(SystemExit, "not configured"):
            swarmd.cfg()

    def test_timeout_reaps_child_process(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            swarmd.sh([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.05)
        self.assertFalse(swarmd._processes)


if __name__ == "__main__":
    unittest.main()
