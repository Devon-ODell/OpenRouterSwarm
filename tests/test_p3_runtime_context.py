"""P3: per-repository RuntimeContext (DECOMPOSE-SWARMD.md, packet P3).

The packet's whole point: two repository contexts coexist in one Python process and
cannot write to each other's queue, logs, baseline cache, worktrees or attempt
artifacts. Everything here uses temp repositories and temp swarm roots (``swarmd.HERE``
rebound per test) and never touches the live checkout's ``state/``, ``logs/``, ``wt/``
or ``configs/``.

Queue note: P3 owns ``use_repo`` only — queue per-context namespacing and its lock
policy are P5's work order. The coexistence queue test therefore drives the daemon's
existing explicit-path persistence helpers (``swarmd._read``/``swarmd._write``) under
each context's explicit path namespace, and does not change Queue policy or locking.
"""
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import date as _date
from pathlib import Path
from types import MappingProxyType
from unittest import mock

from swarm import swarmd
from runtime_context import (ConfigSnapshot, ProcessRegistry, RepoPaths,
                             RuntimeContext)


def _make_repo(base, name, marker):
    """A real git repository with one commit and a distinctive tracked file."""
    repo = base / name
    repo.mkdir()
    (repo / "README.md").write_text(f"# {name}\nmarker={marker}\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "swarm@test.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "swarm test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=repo, check=True)
    return repo


class _FixedDateTime(dt.datetime):
    """A datetime type whose now() answers a fixed instant (deterministic logs)."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 11, 12, 34, 56)


class P3TestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        # Redirect the swarm root so per-repo state/logs/wt/configs go under the temp dir.
        self._old_here = swarmd.HERE
        self._old_state, self._old_logs, self._old_slug = swarmd.STATE, swarmd.LOGS, swarmd.SLUG
        swarmd.HERE = self.base
        self.addCleanup(self._restore_globals)

    def _restore_globals(self):
        # use_repo() rebinds STATE/LOGS/SLUG to the temp root; restore them so tests that
        # run after this one (which patch only STATE, or rely on the daemon's own LOGS)
        # see the pre-test values instead of a deleted temp directory.
        swarmd.HERE = self._old_here
        swarmd.STATE, swarmd.LOGS, swarmd.SLUG = self._old_state, self._old_logs, self._old_slug

    def make_context(self, repo_name, marker, **extra):
        repo = _make_repo(self.base, repo_name, marker)
        cfg = {"repo": str(repo), "test_cmd": "true", "models": ["model-a:free"],
               "workers": 1, **extra}
        return repo, swarmd.use_repo(cfg)

    @staticmethod
    def read_jsonl(path):
        if not Path(path).exists():
            return []
        return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


class CoexistenceTests(P3TestCase):
    """Acceptance 1: two repo contexts in one process, interleaved, no cross-talk."""

    def test_two_contexts_never_write_each_others_queue_logs_cache_worktrees_or_attempts(self):
        repo_a, a = self.make_context("repo-alpha", "ALPHA")
        repo_b, b = self.make_context("repo-beta", "BETA")

        # Distinct identity fronts: no alias, no shared state.
        self.assertIsNot(a, b)
        self.assertIsNot(a.paths, b.paths)
        self.assertIsNot(a.config, b.config)
        self.assertIsNot(a.events, b.events)
        self.assertIsNot(a.run_state, b.run_state)
        self.assertIsNot(a.processes, b.processes)
        self.assertIsNot(a.stop, b.stop)
        self.assertIsNot(a.paths.state, b.paths.state)
        self.assertIsNot(a.paths.logs, b.paths.logs)
        self.assertIsNot(a.paths.worktrees, b.paths.worktrees)

        # Distinct slugs: distinct roots. (Two repos with the same basename would share a
        # slug — repo_slug stems from the resolved path — that is daemon semantics, not a
        # coexistence claim.)
        self.assertNotEqual(a.paths.slug, b.paths.slug)

        # Interleave log writes and journal writes.
        for i in range(3):
            a.events.log(f"alpha line {i}", worker="wA")
            a.events.journal("event_a", i=i)
            b.events.log(f"beta line {i}", worker="wB")
            b.events.journal("event_b", i=i)

        rows_a = self.read_jsonl(a.paths.state / "journal.jsonl")
        rows_b = self.read_jsonl(b.paths.state / "journal.jsonl")
        self.assertEqual([r["event"] for r in rows_a], ["event_a"] * 3)
        self.assertEqual([r["event"] for r in rows_b], ["event_b"] * 3)
        self.assertNotIn("event_b", {r["event"] for r in rows_a})
        self.assertNotIn("event_a", {r["event"] for r in rows_b})

        # Interleave queue writes under each context's explicit path namespace, through the
        # daemon's own explicit-path persistence helpers. Queue per-context namespacing and
        # Queue.locked() policy are P5's work order — P3 must not change Queue.
        queue_a, queue_b = a.paths.state / "queue.jsonl", b.paths.state / "queue.jsonl"
        done_a, done_b = a.paths.state / "done.jsonl", b.paths.state / "done.jsonl"
        ta1 = {"id": "A1", "title": "A1", "priority": 0}
        ta2 = {"id": "A2", "title": "A2", "priority": 0}
        tb1 = {"id": "B1", "title": "B1", "priority": 0}
        swarmd._write(queue_a, [ta1, ta2])
        swarmd._write(done_a, [])
        swarmd._write(queue_b, [tb1])
        swarmd._write(done_b, [])
        swarmd._write(done_a, swarmd._read(done_a) + [ta1])
        swarmd._write(done_a, swarmd._read(done_a) + [ta2])
        swarmd._write(done_b, swarmd._read(done_b) + [tb1])
        swarmd._write(queue_a, [])
        swarmd._write(queue_b, [])

        done_a_ids = {r["id"] for r in self.read_jsonl(done_a)}
        done_b_ids = {r["id"] for r in self.read_jsonl(done_b)}
        queue_a_ids = {r["id"] for r in self.read_jsonl(queue_a)}
        queue_b_ids = {r["id"] for r in self.read_jsonl(queue_b)}
        self.assertEqual(done_a_ids, {"A1", "A2"})
        self.assertEqual(done_b_ids, {"B1"})
        self.assertNotIn("A1", queue_b_ids)
        self.assertNotIn("B1", queue_a_ids)
        self.assertEqual(queue_a_ids, set())
        self.assertEqual(queue_b_ids, set())

        # Interleave baseline cache writes through the daemon's own function, each against
        # its own context's state dir (the daemon reads the STATE global; we bind it).
        key_a, key_b = "key-a", "key-b"
        old_state = swarmd.STATE
        try:
            swarmd.STATE = a.paths.state
            swarmd.baseline_cache(key_a, passed=True)
            swarmd.STATE = b.paths.state
            swarmd.baseline_cache(key_b, passed=True)
        finally:
            swarmd.STATE = old_state
        self.assertTrue((a.paths.state / "baseline.json").exists())
        self.assertTrue((b.paths.state / "baseline.json").exists())
        self.assertEqual(json.loads((a.paths.state / "baseline.json").read_text())["key"], key_a)
        self.assertEqual(json.loads((b.paths.state / "baseline.json").read_text())["key"], key_b)

        # Attempt artifacts land under each context's state/attempts.
        att_a = a.paths.state / "attempts" / "attempt-a"
        att_b = b.paths.state / "attempts" / "attempt-b"
        att_a.mkdir(parents=True)
        att_b.mkdir(parents=True)
        att_a.joinpath("attempt.json").write_text(json.dumps({"ctx": "a"}))
        att_b.joinpath("attempt.json").write_text(json.dumps({"ctx": "b"}))
        self.assertEqual(
            json.loads((a.paths.state / "attempts" / "attempt-a" / "attempt.json").read_text()),
            {"ctx": "a"})
        self.assertEqual(
            json.loads((b.paths.state / "attempts" / "attempt-b" / "attempt.json").read_text()),
            {"ctx": "b"})
        self.assertFalse((a.paths.state / "attempts" / "attempt-b").exists())
        self.assertFalse((b.paths.state / "attempts" / "attempt-a").exists())

        # Worktrees: each root is committed; neither leaks into the other or into the
        # other's sibling namespace.
        (a.paths.worktrees / "wt-a").mkdir(parents=True)
        (b.paths.worktrees / "wt-b").mkdir(parents=True)
        (a.paths.worktrees / "wt-a" / "marker.txt").write_text("alpha")
        (b.paths.worktrees / "wt-b" / "marker.txt").write_text("beta")
        self.assertEqual((a.paths.worktrees / "wt-a" / "marker.txt").read_text(), "alpha")
        self.assertEqual((b.paths.worktrees / "wt-b" / "marker.txt").read_text(), "beta")
        self.assertFalse((a.paths.worktrees / "wt-b").exists())
        self.assertFalse((b.paths.worktrees / "wt-a").exists())
        # The global wt_root() follows swarmd.SLUG (rebound to b) and must not have
        # absorbed a's worktree.
        global_wt = swarmd.wt_root()
        self.assertEqual(global_wt, b.paths.worktrees)
        self.assertNotIn("wt-a", {p.name for p in global_wt.iterdir()})

        # Logs stayed per-context: same-date files live in different directories and
        # carry only their own context's lines.
        logs_a_names = {p.name for p in a.paths.logs.glob("*.log")}
        logs_b_names = {p.name for p in b.paths.logs.glob("*.log")}
        self.assertEqual(len(logs_a_names), 1)
        self.assertEqual(len(logs_b_names), 1)
        self.assertEqual(logs_a_names, logs_b_names)   # same date file, but different dirs
        text_a = (a.paths.logs / sorted(logs_a_names)[0]).read_text()
        text_b = (b.paths.logs / sorted(logs_b_names)[0]).read_text()
        self.assertIn("alpha line 0", text_a)
        self.assertNotIn("beta line", text_a)
        self.assertIn("beta line 0", text_b)
        self.assertNotIn("alpha line", text_b)

        # The globals ended up pointing at b (last use_repo), but a's own context never
        # moved: holders of ctx_a keep using a's files.
        self.assertEqual(swarmd.SLUG, b.paths.slug)
        self.assertEqual(swarmd.STATE, b.paths.state)
        self.assertEqual(swarmd.LOGS, b.paths.logs)
        self.assertEqual(a.paths.state, self.base / "state" / a.paths.slug)

        # Config snapshots are isolated and match each repo's config.
        self.assertEqual(a.config.values["repo"], str(repo_a))
        self.assertEqual(b.config.values["repo"], str(repo_b))
        self.assertNotEqual(a.config.values["repo"], b.config.values["repo"])

    def test_use_repo_builds_from_locals_not_globals_even_under_racing_globals(self):
        """The returned context is exactly the repo it was asked for, even when the
        compatibility globals are rebound (by a second use_repo or mid-build rebinding)
        — _build_context's inputs are locals, never STATE/LOGS/SLUG."""
        repo_a, a = self.make_context("repo-a", "AAA")
        repo_b, b = self.make_context("repo-b", "BBB")
        self.assertNotEqual(a.paths.slug, b.paths.slug)
        self.assertEqual(a.paths.state, self.base / "state" / a.paths.slug)
        self.assertEqual(a.paths.logs, self.base / "logs" / a.paths.slug)
        self.assertEqual(a.paths.worktrees, self.base / "wt" / a.paths.slug)
        self.assertEqual(a.config.values["repo"], str(repo_a))
        self.assertEqual(b.config.values["repo"], str(repo_b))
        self.assertIs(a.stop, a.processes._stopping)
        self.assertIs(b.stop, b.processes._stopping)

    def test_concurrent_use_repo_returns_two_internally_consistent_contexts(self):
        """Two simultaneous use_repo calls (different repos, same process) return
        contexts whose paths never mix: every context's state/logs/worktrees/config are
        internally consistent for its own slug. Exercises the real contract — each
        returned context is exactly the repo its own thread asked for, even though the
        compat globals are only rebound after each build and end up at the last caller's.
        """
        repo_a = _make_repo(self.base, "conc-a", "CA")
        repo_b = _make_repo(self.base, "conc-b", "CB")
        cfg_a = {"repo": str(repo_a), "test_cmd": "true", "models": ["model-a:free"], "workers": 1}
        cfg_b = {"repo": str(repo_b), "test_cmd": "true", "models": ["model-a:free"], "workers": 1}
        barrier = threading.Barrier(2)
        results = []

        def build(cfg):
            barrier.wait()
            return swarmd.use_repo(cfg)

        ta = threading.Thread(target=lambda: results.append(build(cfg_a)))
        tb = threading.Thread(target=lambda: results.append(build(cfg_b)))
        ta.start(); tb.start(); ta.join(); tb.join()

        got = {r.paths.slug: r for r in results}
        self.assertEqual(len(got), 2)
        for r in results:
            self.assertEqual(r.paths.state, self.base / "state" / r.paths.slug)
            self.assertEqual(r.paths.logs, self.base / "logs" / r.paths.slug)
            self.assertEqual(r.paths.worktrees, self.base / "wt" / r.paths.slug)
            self.assertEqual(r.paths.repo, Path(r.config.values["repo"]).resolve())
            self.assertIs(r.stop, r.processes._stopping)
            # Each thread's context is internally consistent for its own slug, and its
            # state/logs dirs were created by its own use_repo call.
            self.assertTrue(r.paths.state.exists())
            self.assertTrue(r.paths.logs.exists())
            # The context was built before the compat globals were assigned: the
            # returned context must not track the last writer's globals.
            self.assertEqual(r.paths.state, self.base / "state" / r.paths.slug)


class EventSinkTests(P3TestCase):
    """Acceptance 2: EventSink reproduces log()/journal() byte-for-byte + lock behavior."""

    def test_log_line_matches_daemon_log_format_and_writes_todays_file(self):
        _, ctx = self.make_context("repo-sink", "SNK")
        sink = ctx.events
        msg = "hello from the sink"
        buf = io.StringIO()
        with mock.patch("datetime.datetime", _FixedDateTime), redirect_stdout(buf):
            sink.log(msg, worker="w0")
        line = buf.getvalue().rstrip("\n")
        self.assertEqual(line, "12:34:56 [w0] hello from the sink")
        logfile = ctx.paths.logs / f"{_date.today()}.log"
        self.assertTrue(logfile.exists())
        self.assertEqual(logfile.read_text().splitlines()[-1], line)
        # The daemon's own log() produces the identical line for the same input.
        old_logs = swarmd.LOGS
        try:
            swarmd.LOGS = ctx.paths.logs
            buf2 = io.StringIO()
            with mock.patch("datetime.datetime", _FixedDateTime), redirect_stdout(buf2):
                swarmd.log(msg, worker="w0")
            daemon_line = buf2.getvalue().rstrip("\n")
        finally:
            swarmd.LOGS = old_logs
        self.assertEqual(daemon_line, "12:34:56 [w0] hello from the sink")
        self.assertEqual(daemon_line, line)
        with open(ctx.paths.logs / f"{_date.today()}.log") as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[-2:], [line, daemon_line])

    def test_log_worker_defaults_to_swarm(self):
        _, ctx = self.make_context("repo-sink2", "SNK")
        buf = io.StringIO()
        with mock.patch("datetime.datetime", _FixedDateTime), redirect_stdout(buf):
            ctx.events.log("plain")
        self.assertEqual(buf.getvalue().rstrip("\n"), "12:34:56 [swarm] plain")

    def test_journal_records_daemon_shape_with_live_run_id(self):
        _, ctx = self.make_context("repo-sink3", "SNK")
        ctx.run_state["id"] = "run-123"
        ctx.events.journal("attempt", stage="accepted", extra={"k": 1})
        rows = self.read_jsonl(ctx.paths.state / "journal.jsonl")
        self.assertEqual(len(rows), 1)
        rec = rows[0]
        self.assertEqual(rec["event"], "attempt")
        self.assertEqual(rec["run_id"], "run-123")
        self.assertIsInstance(rec["t"], float)
        self.assertRegex(rec["iso"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
        self.assertEqual(rec["stage"], "accepted")
        self.assertEqual(rec["extra"], {"k": 1})
        # Daemon's journal() writes the identical JSON object for the same inputs: freeze
        # both clock sources so the comparison is byte-for-byte, not same-second luck.
        old_state, old_run = swarmd.STATE, swarmd._run
        try:
            swarmd.STATE = ctx.paths.state
            swarmd._run = {"id": "run-123"}
            with mock.patch("datetime.datetime", _FixedDateTime), \
                    mock.patch("time.time", return_value=1750000000.0):
                ctx.events.journal("attempt", stage="accepted", extra={"k": 1})
            with mock.patch("datetime.datetime", _FixedDateTime), \
                    mock.patch("time.time", return_value=1750000000.0):
                swarmd.journal("attempt", stage="accepted", extra={"k": 1})
        finally:
            swarmd.STATE, swarmd._run = old_state, old_run
        rows2 = self.read_jsonl(ctx.paths.state / "journal.jsonl")
        self.assertEqual(len(rows2), 3)
        # Frozen clock + frozen time.time: the sink's record and the daemon's record are
        # byte-for-byte identical (deterministic, not same-second luck).
        self.assertEqual(rows2[1], rows2[2])
        self.assertEqual(json.dumps(rows2[1]), json.dumps(rows2[2]))

    def test_journal_captures_t_per_call(self):
        _, ctx = self.make_context("repo-sink3b", "SNK")
        ctx.run_state["id"] = "run-1"
        with mock.patch("datetime.datetime", _FixedDateTime), \
                mock.patch("time.time", side_effect=[100.0, 200.0]):
            ctx.events.journal("a")
            ctx.events.journal("b")
        rows = self.read_jsonl(ctx.paths.state / "journal.jsonl")
        self.assertEqual([r["t"] for r in rows], [100.0, 200.0])

    def test_concurrent_journal_writers_do_not_interleave_or_lose_lines(self):
        _, ctx = self.make_context("repo-sink4", "SNK")
        sink = ctx.events
        n_threads, n_each = 4, 60
        barrier = threading.Barrier(n_threads)

        def writer(tid):
            barrier.wait()
            for i in range(n_each):
                sink.journal("line", tid=tid, i=i)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        rows = self.read_jsonl(ctx.paths.state / "journal.jsonl")
        self.assertEqual(len(rows), n_threads * n_each)
        for tid in range(n_threads):
            mine = [r for r in rows if r["tid"] == tid]
            self.assertEqual(len(mine), n_each)
            self.assertEqual([r["i"] for r in mine], list(range(n_each)))

    def test_concurrent_log_writers_produce_only_whole_lines(self):
        _, ctx = self.make_context("repo-sink5", "SNK")
        n_threads, n_each = 4, 40
        barrier = threading.Barrier(n_threads)

        def writer(tid):
            barrier.wait()
            for i in range(n_each):
                ctx.events.log(f"line {tid}-{i}", worker=f"w{tid}")

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        logfile = ctx.paths.logs / f"{_date.today()}.log"
        lines = logfile.read_text().splitlines()
        self.assertEqual(len(lines), n_threads * n_each)
        pat = re.compile(r"^\d{2}:\d{2}:\d{2} \[w(\d+)\] line (\d+)-(\d+)$")
        pairs = set()
        for line in lines:
            m = pat.match(line)
            self.assertIsNotNone(m, line)
            self.assertEqual(int(m.group(1)), int(m.group(2)))
            pairs.add((int(m.group(2)), int(m.group(3))))
        # Every writer produced every line, each as one whole line: no interleaving and
        # no lost writes under the per-context print lock.
        self.assertEqual(pairs, {(t, i) for t in range(n_threads) for i in range(n_each)})

    def test_event_sink_paths_and_locks_are_per_context(self):
        _, a = self.make_context("repo-sink6a", "SNK")
        _, b = self.make_context("repo-sink6b", "SNK")
        self.assertIsNot(a.events._print_lock, b.events._print_lock)
        self.assertIsNot(a.events._journal_lock, b.events._journal_lock)


class ProcessRegistryTests(P3TestCase):
    """Acceptance 3: ProcessRegistry preserves process()/kill_group()/shutdown() semantics."""

    def test_process_yields_live_child_and_registry_tracks_it(self):
        reg = ProcessRegistry()
        with reg.process([sys.executable, "-c", "import time; time.sleep(30)"]) as p:
            self.assertIsNone(p.poll())
            self.assertIn(p, reg.processes)
        self.assertIsNotNone(p.poll())
        self.assertNotIn(p, reg.processes)
        self.assertEqual(len(reg.processes), 0)

    def test_manual_enter_exit_matches_the_generator_contract(self):
        """A caller that drives process() by hand (the old swarmd.process finally block)
        must see the same semantics: the child is registered, and exiting kills it, reaps
        it and unregisters it."""
        reg = ProcessRegistry()
        cm = reg.process([sys.executable, "-c", "import time; time.sleep(30)"])
        p = cm.__enter__()
        self.assertIsNone(p.poll())
        self.assertIn(p, reg.processes)
        cm.__exit__(None, None, None)
        p.wait(timeout=10)
        self.assertIsNotNone(p.poll())
        self.assertNotIn(p, reg.processes)
        self.assertEqual(len(reg.processes), 0)

    def test_kill_group_kills_the_whole_process_group(self):
        reg = ProcessRegistry()
        with reg.process(["/bin/sh", "-c",
                          f"sleep 30 & echo $! > {self.base / 'child.pid'}; " +
                          "echo started; sleep 30"]) as p:
            child_pid = None
            for _ in range(200):            # wait for the grandchild, up to ~2s
                if (self.base / "child.pid").exists():
                    child_pid = int((self.base / "child.pid").read_text().strip())
                    break
                time.sleep(0.01)
            self.assertIsNotNone(child_pid, "grandchild never appeared")
            self.assertNotEqual(child_pid, p.pid)
            reg.kill_group(p)
            p.wait(timeout=10)
        # Both the shell and the grandchild are gone; no orphans left behind.
        for pid in (p.pid, child_pid):
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                alive = False
            except PermissionError:
                alive = True
            self.assertFalse(alive, f"pid {pid} still alive after kill_group")

    def test_shutdown_kills_registered_processes_and_blocks_new_ones(self):
        reg = ProcessRegistry()
        cm = reg.process([sys.executable, "-c", "import time; time.sleep(30)"])
        p = cm.__enter__()
        self.assertEqual(len(reg.processes), 1)
        reg.shutdown()
        self.assertTrue(reg.is_stopping())
        p.wait(timeout=10)
        self.assertIsNotNone(p.poll())
        with self.assertRaisesRegex(RuntimeError, "swarm is stopping"):
            with reg.process([sys.executable, "-c", "pass"]):
                self.fail("process() must refuse after shutdown")
        # The refused call never spawned a child.
        self.assertEqual(len(reg.processes), 1)
        # Exiting the still-open context unregisters the dead child.
        cm.__exit__(None, None, None)
        self.assertEqual(len(reg.processes), 0)

    def test_shutdown_sets_the_shared_stop_event_so_every_registry_blocks_new_children(self):
        stop = threading.Event()
        reg = ProcessRegistry(stop=stop)
        reg2 = ProcessRegistry(stop=stop)
        cm = reg.process([sys.executable, "-c", "import time; time.sleep(30)"])
        p = cm.__enter__()
        self.assertEqual(len(reg.processes), 1)
        reg2.shutdown()                     # sets the shared event (the legacy _stop)
        self.assertTrue(reg.is_stopping())
        self.assertTrue(reg2.is_stopping())
        # Every registry sharing the event refuses new children, not just the one told
        # to stop — the legacy _stop global blocked process() everywhere.
        for r in (reg, reg2):
            with self.assertRaisesRegex(RuntimeError, "swarm is stopping"):
                r.process([sys.executable, "-c", "pass"]).__enter__()
        # Each registry kills its own children; reg's shutdown reaps the child it
        # started (the pre-shutdown child is still registered until its context exits).
        reg.shutdown()
        p.wait(timeout=10)
        self.assertIsNotNone(p.poll())
        cm.__exit__(None, None, None)
        self.assertEqual(len(reg.processes), 0)

    def test_context_exit_kills_still_running_child(self):
        reg = ProcessRegistry()
        with reg.process([sys.executable, "-c", "import time; time.sleep(30)"]) as p:
            pass
        p.wait(timeout=10)
        self.assertIsNotNone(p.poll())

    def test_two_registries_are_independent(self):
        r1, r2 = ProcessRegistry(), ProcessRegistry()
        cm = r1.process([sys.executable, "-c", "import time; time.sleep(30)"])
        p = cm.__enter__()
        self.assertEqual(len(r1.processes), 1)
        self.assertEqual(len(r2.processes), 0)
        r1.shutdown()
        self.assertFalse(r2.is_stopping())
        p.wait(timeout=10)
        self.assertIsNotNone(p.poll())          # r1's child was killed and reaped
        cm.__exit__(None, None, None)
        with r2.process([sys.executable, "-c", "pass"]) as q:   # r2 still works
            self.assertIsNotNone(q)
        self.assertEqual(len(r2.processes), 0)

    def test_standalone_registry_uses_its_own_stop_event(self):
        reg = ProcessRegistry()
        self.assertIsNotNone(reg._stopping)
        self.assertFalse(reg.is_stopping())
        # Not shared with any other registry or context by default.
        reg2 = ProcessRegistry()
        self.assertIsNot(reg._stopping, reg2._stopping)


class ConfigSnapshotTests(P3TestCase):
    """The Contracts section: ConfigSnapshot is immutable and prevents ordinary mutation."""

    def test_frozen_fields_are_immutable(self):
        snap = ConfigSnapshot(values={"k": [1, 2], "n": 1}, mtime=12.5)
        with self.assertRaises(Exception):      # dataclasses.FrozenInstanceError
            snap.mtime = 1.0
        with self.assertRaises(Exception):
            snap.values = {"other": 1}
        self.assertEqual(snap.mtime, 12.5)
        self.assertEqual(snap.get("k"), (1, 2))
        self.assertEqual(snap["k"], (1, 2))
        self.assertIn("k", snap)
        self.assertEqual(list(snap), ["k", "n"])
        self.assertEqual(len(snap), 2)
        self.assertEqual(snap.get("missing", "dflt"), "dflt")

    def test_top_level_and_nested_mutation_all_fail(self):
        snap = ConfigSnapshot(values={"k": [1, 2], "n": 1}, mtime=12.5)
        with self.assertRaises(TypeError):      # mappingproxy: no item assignment
            snap.values["k"] = [9]
        with self.assertRaises((TypeError, AttributeError)):  # tuple: no append
            snap.values["k"].append(3)
        with self.assertRaises(TypeError):      # frozen tuple: no item assignment
            snap.values["k"][0] = 9
        with self.assertRaises(AttributeError):  # mappingproxy has no update()
            snap.values.update({"x": 1})
        self.assertEqual(snap.values["k"], (1, 2))
        self.assertIsInstance(snap.values, MappingProxyType)

    def test_nested_containers_are_recursively_frozen(self):
        snap = ConfigSnapshot(values={"cfg": {"nested": {"deep": [1, [2, 3]]},
                                              "lst": [1, {"a": 2}], "t": (1, 2),
                                              "s": {1, 2}}, "top": [1, 2]}, mtime=0.0)
        self.assertEqual(snap["cfg"]["nested"]["deep"], (1, (2, 3)))
        self.assertEqual(snap["cfg"]["lst"], (1, {"a": 2}))
        self.assertEqual(snap["cfg"]["t"], (1, 2))
        self.assertEqual(snap["cfg"]["s"], frozenset({1, 2}))
        self.assertEqual(snap["top"], (1, 2))
        with self.assertRaises(TypeError):
            snap["cfg"]["nested"]["deep"][0] = 99
        with self.assertRaises((TypeError, AttributeError)):
            snap["cfg"]["nested"]["deep"].append(4)
        with self.assertRaises(TypeError):
            snap["cfg"]["lst"][1] = "x"
        with self.assertRaises((TypeError, AttributeError)):
            snap["cfg"]["s"].add(3)

    def test_original_input_mutation_cannot_change_the_snapshot(self):
        cfg = {"k": [1, 2], "nested": {"a": {"b": [1]}}, "s": {1}}
        snap = ConfigSnapshot(values=cfg, mtime=12.5)
        cfg["k"].append(3)
        cfg["nested"]["a"]["b"][0] = 99
        cfg["s"].add(2)
        cfg["new"] = "later"
        self.assertEqual(snap.values["k"], (1, 2))
        self.assertEqual(snap.values["nested"]["a"]["b"], (1,))
        self.assertEqual(snap.values["s"], frozenset({1}))
        self.assertNotIn("new", snap)

    def test_context_snapshots_are_isolated_and_match_each_repo(self):
        _, a = self.make_context("repo-snap-a", "SNAP")
        _, b = self.make_context("repo-snap-b", "SNAP")
        self.assertIsNot(a.config, b.config)
        self.assertNotEqual(a.config.values["repo"], b.config.values["repo"])
        self.assertEqual(a.config.values["repo"], str(self.base / "repo-snap-a"))
        self.assertEqual(b.config.values["repo"], str(self.base / "repo-snap-b"))
        self.assertEqual(a.config.values["models"], ("model-a:free",))
        self.assertEqual(a.config.values["workers"], 1)


class RuntimeContextShapesTests(P3TestCase):
    """The Contracts section dataclass shapes and the use_repo() contract."""

    def test_repo_paths_contract_fields(self):
        repo, ctx = self.make_context("repo-shape", "SHAPE")
        p = ctx.paths
        self.assertIsInstance(p, RepoPaths)
        for attr in ("repo", "state", "logs", "worktrees", "config"):
            self.assertIsInstance(getattr(p, attr), Path)
        self.assertIsInstance(p.slug, str)
        self.assertEqual(p.repo, repo.resolve())
        self.assertEqual(p.state, self.base / "state" / p.slug)
        self.assertEqual(p.logs, self.base / "logs" / p.slug)
        self.assertEqual(p.worktrees, self.base / "wt" / p.slug)
        self.assertTrue(p.state.exists())
        self.assertTrue(p.logs.exists())

    def test_use_repo_populates_compat_globals_and_returns_context(self):
        repo, ctx = self.make_context("repo-globals", "GLB")
        self.assertEqual(swarmd.SLUG, ctx.paths.slug)
        self.assertEqual(swarmd.STATE, ctx.paths.state)
        self.assertEqual(swarmd.LOGS, ctx.paths.logs)
        self.assertIsInstance(ctx, RuntimeContext)
        self.assertEqual(ctx.paths.repo, repo.resolve())
        self.assertEqual(ctx.config.values["repo"], str(repo))

    def test_context_stop_is_the_exact_event_process_registry_checks(self):
        """ctx.stop must be exactly the event ProcessRegistry.process() checks and
        shutdown() sets — the legacy _stop was a single object shared by both."""
        repo, ctx = self.make_context("repo-stop", "STOP")
        reg = ctx.processes
        self.assertIs(reg._stopping, ctx.stop)
        cm = reg.process([sys.executable, "-c", "import time; time.sleep(30)"])
        p = cm.__enter__()
        reg.shutdown()                      # sets the context's stop event, kills children
        self.assertTrue(ctx.stop.is_set())
        p.wait(timeout=10)
        # process() on the stopped registry must refuse when the generator is entered.
        with self.assertRaisesRegex(RuntimeError, "swarm is stopping"):
            reg.process([sys.executable, "-c", "pass"]).__enter__()
        cm.__exit__(None, None, None)

    def test_both_module_identities_expose_the_p3_names(self):
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location("swarmd_p3_top", str(Path(swarmd.__file__).resolve()))
        top = _ilu.module_from_spec(spec)
        spec.loader.exec_module(top)
        for mod in (swarmd, top):
            for name in ("RepoPaths", "ConfigSnapshot", "RunState", "EventSink",
                         "ProcessRegistry", "RuntimeContext"):
                self.assertTrue(hasattr(mod, name), f"{mod.__name__} lost {name}")

    def test_config_mtime_is_a_float(self):
        _, ctx = self.make_context("repo-mtime", "MTIME")
        self.assertIsInstance(ctx.config.mtime, float)

    def test_exactly_one_top_level_config_mtime_definition(self):
        """The reload helper and the context snapshot builder must share the single
        no-argument ``config_mtime``. A second top-level definition would shadow the
        original and let the two diverge, so pin the count."""
        import ast
        src = Path(swarmd.__file__).read_text()
        tree = ast.parse(src)
        defs = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == "config_mtime"]
        self.assertEqual(len(defs), 1,
                         "config_mtime must be defined exactly once at module top level")
        self.assertEqual(len(defs[0].args.args), 0,
                         "config_mtime must stay a no-argument helper")

    def test_use_repo_builds_context_before_assigning_compat_globals(self):
        """The stated invariant: the compat globals are populated only after the context
        is built. _build_context must be called before SLUG/STATE/LOGS are assigned, so
        a mid-build rebinding by another thread can never leak into the returned context.
        """
        import ast
        src = Path(swarmd.__file__).read_text()
        tree = ast.parse(src)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and n.name == "use_repo")
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "_build_context"]
        assigns = [n for n in fn.body if isinstance(n, ast.Assign)
                   and any(
                       (isinstance(t, ast.Name) and t.id == "SLUG") or
                       (isinstance(t, ast.Tuple) and any(
                           isinstance(e, ast.Name) and e.id == "SLUG" for e in t.elts))
                       for t in n.targets)]
        self.assertEqual(len(calls), 1, "use_repo must call _build_context exactly once")
        self.assertEqual(len(assigns), 1,
                         "use_repo must assign the compat globals exactly once")
        self.assertLess(calls[0].lineno, assigns[0].lineno,
                        "_build_context must run before the SLUG/STATE/LOGS assignment")


if __name__ == "__main__":
    unittest.main()