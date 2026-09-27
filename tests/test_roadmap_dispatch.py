"""Roadmap dispatch uses committed evidence and never manufactures owner approval."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from swarm import swarmd


class RoadmapDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        self.roadmap = self.repo / 'docs/roadmap'
        self.roadmap.mkdir(parents=True)
        self.rows = [self.packet('BASE', dispatch='coordinator'), self.packet('F01', ['BASE']),
                     self.packet('F02', ['F01']), self.packet('reserved', ['BASE'], ['Owner handoff'])]
        self.ledger = {'implementation_started': True, 'packets': {
            'BASE': {'status': 'accepted', 'evidence': 'Baseline inspected'}}, 'gates': {}}
        self.save()
        self.git('branch', 'swarm/trunk')
        self.c = {'repo': str(self.repo), 'trunk': 'swarm/trunk', 'roadmap_file': 'docs/roadmap/tasks.json'}
        p = patch.object(swarmd, 'STATE', self.state)
        p.start(); self.addCleanup(p.stop)
        self.q = swarmd.Queue()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL, text=True).strip()

    def packet(self, name, deps=None, gates=None, dispatch='swarm'):
        return dict(id=name, title=name, dispatch=dispatch, depends_on=deps or [],
                    coordinator_gates=gates or [], detail='Implement packet', priority=1,
                    execution_class='standard', acceptance=['Check behavior'], commands=['python3 -m unittest'])

    def save(self):
        (self.roadmap / 'tasks.json').write_text(json.dumps({'tasks': self.rows}))
        (self.roadmap / 'EXECUTION.json').write_text(json.dumps(self.ledger))
        self.git('add', '.'); self.git('commit', '-m', 'coordinator evidence')

    def test_waits_for_accepted_dependencies_and_owner_evidence(self):
        self.assertEqual([t['title'] for t in swarmd.dispatch_roadmap(self.c, self.q)], ['F01'])
        self.assertEqual(swarmd.dispatch_roadmap(self.c, self.q), [])
        row = self.q.pending()[0]
        # A queue status alone is not proof of integration.
        self.q.release(row['id'], True, 'model said complete')
        self.assertEqual(swarmd.dispatch_roadmap(self.c, self.q), [])

    def test_real_integrated_commit_unlocks_next_packet(self):
        first = swarmd.dispatch_roadmap(self.c, self.q)[0]
        sha = self.git('rev-parse', 'swarm/trunk')
        self.q.release(first['id'], True, f'integrated {sha} on swarm/trunk; checked')
        self.assertEqual([t['title'] for t in swarmd.dispatch_roadmap(self.c, self.q)], ['F02'])
        report = json.loads((self.state / 'roadmap-execution.json').read_text())
        self.assertEqual(report['accepted']['F01']['accepted_commit'], sha)
        self.assertIn('reserved', report['blocked'])

    def test_uncommitted_owner_gate_cannot_unlock_work(self):
        self.ledger['gates']['Owner handoff'] = {'evidence': 'unreviewed working tree change'}
        (self.roadmap / 'EXECUTION.json').write_text(json.dumps(self.ledger))
        self.assertEqual([t['title'] for t in swarmd.dispatch_roadmap(self.c, self.q)], ['F01'])

    def test_queue_is_bounded_to_five_and_activation_is_required(self):
        self.rows += [self.packet('extra'+str(i), ['BASE']) for i in range(9)]
        self.save(); self.git('branch', '-f', 'swarm/trunk', 'HEAD')
        self.assertEqual(len(swarmd.dispatch_roadmap(self.c, self.q)), 5)
        self.assertEqual(swarmd.dispatch_roadmap(self.c, self.q), [])
        self.ledger['implementation_started'] = False
        self.save(); self.git('branch', '-f', 'swarm/trunk', 'HEAD')
        with self.assertRaisesRegex(ValueError, 'not been activated'):
            swarmd.dispatch_roadmap(self.c, self.q)
