"""Author: zhekui. Late successful native events must not require a user rerun.

Real controller/store/worker, simulated GitHub transport; no remote acceptance.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from .controller import Controller
from .fixtures import REPOS
from .model import canonical
from .test_review_regressions import FakeLive, config
from .worker import drain


class LateEventRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.api = FakeLive()
        self.c = Controller(self.root / 'state.sqlite', self.api, config())
        self.c.sync()
        self.gid = self.c.c.store.export()['groups'][0]['id']
        drain(self.c.c.store.path, self.root / 'workers')
        self.api.run.update(status='completed', conclusion='success')
        self.hint = {'repo': REPOS[0], 'run_id': 123}

    def test_completed_success_and_duplicate_do_not_rerun_public_work(self):
        before = self.c.c.store.export()['tasks']
        for delivery in range(2):
            result = self.c.cycle(self.hint, publish=True)
            self.assertEqual(result['errors'], [])
            self.assertEqual(len(result['statuses']), 4 if delivery == 0 else 0)
            self.assertTrue(all(s['state'] == 'success' for s in result['statuses']))
        self.assertEqual(self.c.c.store.export()['tasks'], before)
        self.assertEqual(self.c.c.summary(self.gid)['attempt'], 1)

    def test_concurrent_duplicate_delivery_preserves_single_public_execution(self):
        before = self.c.c.store.export()['tasks']
        def deliver(_):
            c = Controller(self.c.c.store.path, self.api, config())
            return c.cycle(self.hint, publish=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(deliver, range(8)))
        self.assertTrue(all(not r['errors'] for r in results), results)
        self.assertEqual(self.c.c.store.export()['tasks'], before)
        with self.c.c.store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM native_runs').fetchone()[0], 1)
        self.assertTrue(self.c.c.summary(self.gid)['merge_ready'])

    def test_existing_pending_receipt_recovers_without_new_native_attempt(self):
        key = f'{REPOS[0]}:123:1'
        with self.c.c.store.transaction() as db:
            group = json.loads(db.execute('SELECT body FROM groups WHERE id=?', (self.gid,)).fetchone()[0])
            group['native_blockers'] = {key: [REPOS[0] + '#1']}
            db.execute('UPDATE groups SET body=? WHERE id=?', (canonical(group), self.gid))
            db.execute('INSERT INTO native_runs VALUES(?,?)',
                       (f'pending:{REPOS[0]}:123', canonical(dict(repo=REPOS[0],
                        blocker=f'unverified:{REPOS[0]}:123', head='a' * 40))))
        result = self.c.cycle(publish=True)
        self.assertEqual(result['errors'], [])
        self.assertTrue(self.c.c.summary(self.gid)['merge_ready'])
        self.assertTrue(all(s['state'] == 'success' for s in result['statuses']))

    def test_non_success_and_missed_rerun_stay_blocked(self):
        for status, conclusion, attempt in [('completed', 'cancelled', 1),
                ('completed', 'failure', 1), ('queued', None, 1),
                ('completed', 'success', 2)]:
            with self.subTest(status=status, conclusion=conclusion, attempt=attempt):
                self.api.run.update(status=status, conclusion=conclusion, run_attempt=attempt)
                result = self.c.cycle(self.hint, publish=True)
                self.assertTrue(result['errors'])
                self.assertFalse(self.c.c.summary(self.gid)['merge_ready'])
                self.assertFalse(any(s['state'] == 'success' for s in result['statuses']))
                self.assertEqual(self.c.c.summary(self.gid)['attempt'], 1)

    def test_head_pr_workflow_and_fork_mismatch_never_bind(self):
        import copy
        original_run, original_prs = copy.deepcopy(self.api.run), copy.deepcopy(self.api.prs)
        for mismatch in ('head', 'pr', 'workflow', 'fork', 'base'):
            with self.subTest(mismatch=mismatch):
                self.api.run, self.api.prs = copy.deepcopy(original_run), copy.deepcopy(original_prs)
                if mismatch == 'head': self.api.run['head_sha'] = 'f' * 40
                if mismatch == 'pr': self.api.run['pull_requests'] = [{'number': 2}]
                if mismatch == 'workflow': self.api.run['workflow_id'] = 999
                if mismatch == 'fork': self.api.prs[REPOS[0]]['head']['repo']['full_name'] = 'fork/arsenal'
                if mismatch == 'base': self.api.prs[REPOS[0]]['base']['sha'] = 'f' * 40
                # Isolate identity validation from inventory adopting the mutation.
                self.c.sync = lambda: []
                self.c.cycle(self.hint)
                with self.c.c.store.transaction() as db:
                    self.assertIsNone(db.execute('SELECT body FROM native_runs WHERE id=?',
                                                (f'{REPOS[0]}:123:1',)).fetchone())

    def test_exact_blocker_only_and_private_draft_gates_preserved(self):
        with self.c.c.store.transaction() as db:
            g = json.loads(db.execute('SELECT body FROM groups WHERE id=?', (self.gid,)).fetchone()[0])
            g['native_blockers'] = {f'{REPOS[0]}:123:1': [REPOS[0] + '#1'],
                                    f'{REPOS[0]}:456:1': [REPOS[0] + '#1']}
            db.execute('UPDATE groups SET body=? WHERE id=?', (canonical(g), self.gid))
        self.api.prs[REPOS[0]]['draft'] = True
        self.api.private_checks = lambda *args: {'private': 'pending'}
        result = self.c.cycle(self.hint, publish=True)
        self.assertFalse(result['errors'])
        g = self.c.c.store.export()['groups'][0]
        self.assertEqual(g['native_blockers'], {f'{REPOS[0]}:456:1': [REPOS[0] + '#1']})
        blockers = self.c.c.summary(self.gid)['blockers']
        self.assertIn('native_event_unresolved', blockers)
        self.assertTrue(any('draft' in b for b in blockers))
        self.assertTrue(any('private' in b for b in blockers))
        self.assertTrue(all(s['state'] == 'pending' for s in result['statuses']))

    def test_terminal_receipt_cannot_cancel_later_generation(self):
        first = self.c.cycle(self.hint)
        self.assertFalse(first['errors'])
        p = self.c.c.store.export()['groups'][0]['plan']
        p['versions'][REPOS[1]]['head'] = 'e' * 40
        self.c.c.reconcile(p)
        self.c.sync = lambda: []
        self.api.run.update(conclusion='cancelled')
        result = self.c.cycle(self.hint)
        self.assertTrue(result['errors'])
        self.assertEqual(self.c.c.store.export()['groups'][0]['cancelled'], [])

    def test_first_success_can_acknowledge_current_fourth_generation(self):
        p = self.c.c.store.export()['groups'][0]['plan']
        for head in ('d', 'e', 'f'):
            p['versions'][REPOS[1]]['head'] = head * 40
            self.c.c.reconcile(p)
        self.c.sync = lambda: []
        before = self.c.c.store.export()['tasks']
        result = self.c.cycle(self.hint)
        self.assertFalse(result['errors'])
        self.assertTrue(result['native_event']['terminal_only'])
        self.assertEqual(result['native_event']['generation'], 4)
        self.assertEqual(self.c.c.store.export()['tasks'], before)
        self.assertFalse(self.c.c.summary(self.gid)['merge_ready'])

    def test_older_attempt_after_observed_rerun_is_rejected(self):
        self.c.cycle(self.hint)
        self.api.run.update(status='in_progress', conclusion=None, run_attempt=2)
        self.assertFalse(self.c.cycle(self.hint)['errors'])
        before = self.c.c.store.export()['tasks']
        self.api.run.update(status='completed', conclusion='success', run_attempt=1)
        result = self.c.cycle(self.hint)
        self.assertTrue(result['errors'])
        self.assertEqual(self.c.c.store.export()['tasks'], before)

    def test_empty_database_recovers_on_next_cycle_without_native_rerun(self):
        c = Controller(self.root / 'empty.sqlite', self.api, config())
        first = c.cycle(self.hint)
        self.assertTrue(first['errors'])
        second = c.cycle()
        self.assertFalse(second['errors'])
        gid = c.c.store.export()['groups'][0]['id']
        self.assertNotIn('native_event_unresolved', c.c.summary(gid)['blockers'])
        self.assertEqual(c.c.summary(gid)['attempt'], 1)
        self.assertFalse(c.c.summary(gid)['merge_ready'])
        drain(c.c.store.path, self.root / 'empty-workers')
        self.assertFalse(c.cycle(publish=True)['errors'])
        self.assertTrue(c.c.summary(gid)['merge_ready'])

    def test_recovery_is_bounded_and_rotates_past_unresolved_receipts(self):
        with self.c.c.store.transaction() as db:
            for run_id in range(100, 117):
                db.execute('INSERT INTO native_runs VALUES(?,?)',
                           (f'pending:{REPOS[0]}:{run_id}', canonical(dict(repo=REPOS[0],
                            blocker=f'unverified:{REPOS[0]}:{run_id}', head='a' * 40))))
        original = self.api.request
        calls = []
        def request(repo, path, *args):
            if path.startswith('actions/runs/'):
                calls.append(path)
                if path != 'actions/runs/116':
                    raise RuntimeError('GitHub request failed; HTTP 503; attempts=4')
            return original(repo, path, *args)
        self.api.request = request
        self.assertTrue(self.c.cycle()['errors'])
        self.assertEqual(len(calls), 16)
        self.assertNotIn('actions/runs/116', calls)
        self.c.cycle()
        self.assertEqual(calls[16], 'actions/runs/116')
        with self.c.c.store.transaction() as db:
            self.assertIsNotNone(db.execute('SELECT body FROM native_runs WHERE id=?',
                                           (f'{REPOS[0]}:116:1',)).fetchone())
        self.assertFalse(self.c.c.summary(self.gid)['merge_ready'])


if __name__ == '__main__':
    unittest.main()
