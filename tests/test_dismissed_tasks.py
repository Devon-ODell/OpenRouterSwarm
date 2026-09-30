import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from swarm import swarmd


class DismissedTasksTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        p = patch.object(swarmd, 'STATE', self.root)
        p.start()
        self.addCleanup(p.stop)
        self.q = swarmd.Queue()
        rows = [{'id': f't{i}', 'title': f'Task {i}', 'status': 'parked' if i % 2 else 'split',
                 'attempts': 2, 'note': 'original evidence'} for i in range(25)]
        rows.append({'id': 'done', 'title': 'Accepted', 'status': 'done'})
        swarmd._write(self.q.done, rows)

    def test_bulk_clears_beyond_display_limit_without_deleting_history(self):
        self.assertEqual(len(self.q.dismiss_finished()), 25)
        rows = swarmd._read(self.q.done)
        self.assertEqual(len(rows), 26)
        self.assertTrue(all(r.get('dismissed_at') for r in rows[:-1]))
        self.assertTrue(all(r['note'] == 'original evidence' for r in rows[:-1]))
        self.assertNotIn('dismissed_at', rows[-1])
        self.assertEqual(self.q.dismiss_finished(), [])

    def test_individual_dismissal_does_not_unlock_dependencies(self):
        child = self.q.add('Dependent', depends_on=['t1'])
        self.assertEqual(self.q.dismiss_finished('t1'), ['t1'])
        self.assertEqual(self.q.ready(), [])
        self.assertEqual(self.q.pending()[0]['id'], child['id'])
        self.assertNotIn('dismissed_at', swarmd._read(self.q.done)[0])

    def test_queued_and_accepted_work_cannot_be_dismissed(self):
        swarmd._write(self.q.path, [{'id': 't1', 'title': 'Already requeued'}])
        for tid in ['t1', 'done', 'missing']:
            with self.assertRaises(ValueError):
                self.q.dismiss_finished(tid)
        self.assertNotIn('t1', self.q.dismiss_finished())
        self.assertEqual(self.q.pending()[0]['id'], 't1')

    def test_explicit_requeue_can_restore_a_dismissed_task(self):
        self.q.dismiss_finished('t1')
        task = self.q.requeue('t1')
        self.assertNotIn('dismissed_at', task)
        self.assertEqual(task['attempts'], 0)


if __name__ == '__main__':
    unittest.main()
