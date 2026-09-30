"""Regression checks for the September 27 scope and review failures."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import flint
from swarm import swarmd
from swarm.workflow import validate_scope


class RecoveryContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patcher = patch.object(swarmd, 'STATE', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.q = swarmd.Queue()

    def worker(self):
        return swarmd.Worker(0, {'steps': {'adversary': 2}, 'test_cmd': 'general'},
                             self.q, Mock(), threading.Event(), ledger=Mock())

    def test_format_failure_parks_without_spending_implementation_attempt(self):
        task = self.q.add('repair rules', acceptance=['Rules opens'])
        self.q.claim()
        row = self.q.release(task['id'], False, 'invalid JSON; candidate retained',
                             failure_class='review_format')
        self.assertEqual(row['status'], 'parked')
        self.assertEqual(row['attempts'], 0)
        self.assertEqual(row['review_format_failures'], 1)
        self.assertEqual(swarmd.failure_class('review_format_error'), 'review_format')

    def test_one_independent_reviewer_can_correct_its_format(self):
        w = self.worker()
        w.evidence = Mock()
        w.last_model = 'reviewer:free'
        w.pick = Mock(return_value=None)
        w.no_review = Mock(return_value=set())
        w.review = Mock(side_effect=[swarmd.ReviewFormatError('missing C2'), {'verdict': 'approve'}])
        with patch.object(swarmd, 'log'):
            verdict, model, error = w.reviewed('tree', 'diff', 'tests', 'reviewer:free', 'author:free', False)
        self.assertIsNone(error)
        self.assertEqual(verdict['verdict'], 'approve')
        self.assertEqual(w.review.call_count, 2)
        self.assertEqual(w.review.call_args.args[:3], ('tree', 'diff', 'tests'))
        self.assertEqual(model, 'reviewer:free')

    def test_reasking_same_reviewer_is_bounded(self):
        w = self.worker()
        w.evidence = Mock()
        w.last_model = 'reviewer:free'
        w.pick = Mock(return_value=None)
        w.no_review = Mock(return_value=set())
        w.review = Mock(side_effect=swarmd.ReviewFormatError('missing checks'))
        with patch.object(swarmd, 'log'):
            result = w.reviewed('tree', 'diff', 'tests', 'reviewer:free', 'author:free', False)
        self.assertIsInstance(result[2], swarmd.ReviewFormatError)
        self.assertEqual(w.review.call_count, 3)

    def test_review_mutation_never_qualifies_for_format_retry(self):
        w = self.worker()
        w.evidence = Mock()
        w.last_model = 'reviewer:free'
        w.review = Mock(side_effect=swarmd.ModelError('reviewer modified the candidate'))
        result = w.reviewed('tree', 'diff', 'tests', 'reviewer:free', 'author:free', False)
        self.assertIsNotNone(result[2])
        self.assertEqual(w.review.call_count, 1)

    def test_cjs_context_and_contract_metadata_survive_queue(self):
        task = self.q.add('Rules gate', 'Read tests/tutorial_browser.cjs',
                          allowed_paths=['tests/tutorial_browser.cjs'],
                          verification_commands=['node tests/tutorial_browser.cjs'])
        self.assertEqual(swarmd.named_paths(task), ['tests/tutorial_browser.cjs'])
        claimed = self.q.claim()
        self.assertEqual(claimed['allowed_paths'], ['tests/tutorial_browser.cjs'])

    def test_task_gates_run_on_candidate_but_not_baseline(self):
        w = self.worker()
        w.task = {'verification_commands': ['browser-gate']}
        w.wt = self.root
        w.gate_count = 0
        w.evidence = Mock(path=self.root)
        with patch.object(swarmd, 'run_gate', return_value=(True, 'pass')) as gate:
            w.gate('baseline')
            self.assertEqual([c.args[1]['test_cmd'] for c in gate.call_args_list], ['general'])
            gate.reset_mock()
            w.gate('candidate-0')
            self.assertEqual([c.args[1]['test_cmd'] for c in gate.call_args_list], ['general', 'browser-gate'])

    def test_diff_scope_includes_both_ends_of_rename(self):
        w = self.worker()
        w.task = {'allowed_paths': ['tests/']}
        w.wt, w.review_base = self.root, 'base'
        with patch.object(swarmd, 'git', return_value=(0, 'tests/old.py\0site/arcade.js\0')) as git:
            self.assertEqual(w.scope_violations(), ['site/arcade.js'])
            self.assertIn('--no-renames', git.call_args.args[0])

    def test_bad_scopes_are_rejected(self):
        for scope in [['../secret'], ['/tmp/file'], ['.git/config'], ['**'], ['.'], []]:
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                validate_scope({'allowed_paths': scope})

    def test_native_edits_cannot_escape_scope_via_symlink(self):
        old = Path.cwd()
        self.addCleanup(os.chdir, old)
        os.chdir(self.root)
        (self.root / 'tests').mkdir()
        (self.root / 'site.js').write_text('original')
        (self.root / 'tests/link.js').symlink_to(self.root / 'site.js')
        with patch.dict(os.environ, {'FLINT_ALLOWED_PATHS': '["tests/"]'}):
            flint.write_file('tests/gate.cjs', 'check')
            with self.assertRaises(PermissionError):
                flint.write_file('tests/link.js', 'bad')
            with self.assertRaises(PermissionError):
                flint.edit_file('site.js', 'original', 'bad')
        self.assertEqual((self.root / 'site.js').read_text(), 'original')

    def test_heartbeat_does_not_reset_progress(self):
        with patch.dict(swarmd._run, {'id': 'run', 'last_progress_at': 10, 'state': 'Running'}, clear=True), \
                patch.object(swarmd.time, 'time', return_value=100), \
                patch.dict(swarmd._turn_logs, {}, clear=True):
            row = swarmd.run_status([], {})
            self.assertEqual(row['last_progress_at'], 10)
            self.assertEqual(row['progress_age'], 90)

    def test_parked_dependency_is_visible_without_a_roadmap(self):
        task = self.q.add('Rules gate', acceptance=['Rules opens'])
        self.q.claim()
        self.q.release(task['id'], False, 'review formats exhausted', failure_class='review_format')
        with patch.dict(swarmd._run, {'id': 'run', 'started': 0}, clear=True), \
                patch.object(swarmd._stop, 'is_set', return_value=False):
            row = swarmd.run_status([], {})
        self.assertEqual(row['state'], 'Blocked')
        self.assertIn(task['id'], row['root_blockers'])
        self.assertIn('review formats', row['blocked_tasks'][0]['reason'])

    def test_reporting_edits_do_not_restart_the_daemon(self):
        watched = {p.name for p in swarmd.source_files()}
        self.assertNotIn('board.py', watched)
        self.assertNotIn('bridge.py', watched)
        self.assertIn('workflow.py', watched)


if __name__ == '__main__':
    unittest.main()
