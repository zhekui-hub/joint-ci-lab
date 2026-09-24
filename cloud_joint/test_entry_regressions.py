"""C1/C2 regressions: exercise the actual CLI with a subprocess gh transport stub.

The stub simulates HTTP only; these tests do not claim real GitHub acceptance.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

from .controller import Controller
from .github import GitHub
from .test_review_regressions import FakeLive, config
from .fixtures import REPOS, plan
from .worker import drain

ROOT = Path(__file__).resolve().parents[1]


class EntryRegressions(unittest.TestCase):
    def test_make_config_cli_root_endpoint(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            stub = root/'gh'
            stub.write_text('#!' + sys.executable + '\n' + '''import sys,json
endpoint=next(x for x in sys.argv if x.startswith('repos/'))
if endpoint.endswith('/'):
 print('gh: Not Found (HTTP 404)',file=sys.stderr);sys.exit(1)
if '/actions/workflows?' in endpoint:
 print(json.dumps({'workflows':[{'name':'lab-private','id':9}]}))
else: print(json.dumps({'default_branch':'main'}))
''')
            stub.chmod(0o700)
            args = [sys.executable, str(ROOT/'tools/make_lab_config.py'), '--check-app-id', '42', '--out', str(root/'config.json')]
            for role in ('arsenal','driver','synapse','sim'):
                args += ['--'+role, 'owner/joint-ci-'+role]
            result = subprocess.run(args, env=dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH']),
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            cfg = json.loads((root/'config.json').read_text())
            self.assertEqual(len(cfg['repositories']), 4)
            self.assertTrue(all(s['approval_policy']=='required' for s in cfg['repositories']))

    def test_root_url_and_redacted_http_status(self):
        command = Mock(return_value=subprocess.CompletedProcess([], 0, '{"default_branch":"main"}', ''))
        api = GitHub(['owner/joint-ci-test'], command=command)
        api.request('owner/joint-ci-test', '')
        self.assertEqual(command.call_args.args[0][-1], 'repos/owner/joint-ci-test')
        command.return_value = subprocess.CompletedProcess([], 1, '', 'secret-value gh: Forbidden (HTTP 403)')
        with self.assertRaisesRegex(RuntimeError, 'HTTP 403; attempts=1') as caught:
            api.request('owner/joint-ci-test', 'pulls')
        self.assertNotIn('secret-value', str(caught.exception))

    def test_failed_event_fetch_blocks_future_finalize(self):
        with tempfile.TemporaryDirectory() as td:
            api = FakeLive()
            c = Controller(Path(td)/'state.sqlite', api, config())
            c.sync()
            drain(c.c.store.path, Path(td)/'workers')
            original = api.request
            def failed(repo, path, *args):
                if path.startswith('actions/runs/'):
                    raise RuntimeError('GitHub request failed; HTTP 503; attempts=4')
                return original(repo, path, *args)
            api.request = failed
            r = c.cycle({'repo': REPOS[0], 'run_id': 123}, publish=True)
            self.assertEqual(r['errors'][0]['stage'], 'native_event')
            self.assertTrue(all(x['state']=='pending' for x in r['statuses']))
            api.request = original
            c.publish()
            self.assertTrue(all(not c.c.summary(g['id'])['merge_ready'] for g in c.c.store.export()['groups']))
            r = c.cycle({'repo': REPOS[0], 'run_id': 123}, publish=True)
            self.assertFalse(r['errors'])
            self.assertTrue(all(x['state']=='success' for x in r['statuses']))

    def test_fetched_unbound_event_does_not_block_other_head(self):
        with tempfile.TemporaryDirectory() as td:
            api = FakeLive()
            c = Controller(Path(td)/'state.sqlite', api, config())
            c.sync()
            unrelated = c.c.reconcile(plan(2))
            c.sync = lambda: []  # isolate event scoping from discovery changes
            api.run.update(status='completed', conclusion='cancelled')
            observed = c.cycle({'repo': REPOS[0], 'run_id': 123})
            self.assertTrue(observed['errors'])
            groups = {g['id']: g for g in c.c.store.export()['groups']}
            self.assertFalse(groups[unrelated].get('native_blockers'))
            self.assertTrue(any(g.get('native_blockers') for g in groups.values()))

    def test_cli_handles_cancel_before_failing_inventory(self):
        # Seed a known group through the same inventory API, then launch the
        # unchanged public CLI in another process. A fake gh fails all inventory
        # reads. The cancel must still persist and old green must become pending.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            api = FakeLive()
            cfg = config()
            for s in cfg['repositories']: s['approval_policy'] = 'not_required'
            mapping = {r: 'owner/joint-ci-' + r.split('/')[1] for r in REPOS}
            api.allowed = set(mapping.values())
            api.prs = {mapping[r]: dict(p, head=dict(p['head'], repo={'full_name': mapping[r]})) for r,p in api.prs.items()}
            # FakeLive branch names still match the original PR body values.
            for s in cfg['repositories']: s['repo'] = mapping[s['repo']]
            db = root/'state.sqlite'
            c = Controller(db, api, cfg)
            c.sync()
            repo = mapping[REPOS[0]]
            c.native_event(repo, 123)
            drain(db, root/'workers')
            c.publish()
            (root/'config.json').write_text(json.dumps(cfg))
            (root/'event.json').write_text(json.dumps({'repo': repo, 'run_id': 123}))
            (root/'run.json').write_text(json.dumps(dict(api.run, status='completed', conclusion='cancelled')))
            (root/'prs.json').write_text(json.dumps(api.prs))
            stub = root/'gh'
            stub.write_text('#!' + sys.executable + '\n' + '''import sys,json,os
from pathlib import Path
r=Path(os.environ['STUB_ROOT'])
a=sys.argv[1:]
with (r/'calls.jsonl').open('a') as f:f.write(json.dumps(a)+'\\n')
if any('actions/runs/123' in x for x in a):
 print((r/'run.json').read_text())
elif 'POST' in a:
 with (r/'writes.jsonl').open('a') as f:f.write(sys.stdin.read()+'\\n')
 print('{}')
else:
 if not (r/'healthy').exists():
  print('gh: Not Found (HTTP 404)',file=sys.stderr);sys.exit(1)
 endpoint=next(x for x in a if x.startswith('repos/'))
 _,owner,name,path=endpoint.split('/',3)
 pr=json.loads((r/'prs.json').read_text())[owner+'/'+name]
 if path.startswith('pulls?'): print(json.dumps([pr]))
 elif path.startswith('pulls/'): print(json.dumps(pr))
 elif '/check-runs' in path:
  print(json.dumps({'check_runs':[{'name':'private','head_sha':'a'*40,'app':{'id':42},'status':'completed','conclusion':'success'}]}))
 elif path.startswith('commits/'):
  print(json.dumps({'sha':'c'*40 if path.endswith('c'*40) else 'a'*40}))
 else: raise RuntimeError('Unexpected fixture endpoint')
''')
            stub.chmod(0o700)
            result = subprocess.run([sys.executable, str(ROOT/'tools/controller.py'), '--config', str(root/'config.json'),
                '--db', str(db), '--event', str(root/'event.json'), '--apply-lab-status'],
                env=dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'], STUB_ROOT=str(root)),
                capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 2, result.stderr)
            observed = json.loads(result.stdout)
            self.assertIsNotNone(observed['native_event'])
            self.assertEqual(observed['errors'][0]['stage'], 'sync')
            self.assertIn('HTTP 404', observed['errors'][0]['error'])
            self.assertEqual(len(observed['statuses']), 4)
            self.assertTrue(all(x['state']=='pending' for x in observed['statuses']))
            calls = [json.loads(x) for x in (root/'calls.jsonl').read_text().splitlines()]
            self.assertTrue(any('actions/runs/123' in x for x in calls[0]))
            gid=observed['native_event']['group']
            self.assertIn('cancelled_consumers', c.c.summary(gid)['blockers'])
            # Recover through the same CLI, including a native rerun and actual
            # public subprocess work. No direct native_event recovery shortcut.
            (root/'healthy').touch()
            (root/'run.json').write_text(json.dumps(dict(api.run, run_attempt=2)))
            command = [sys.executable, str(ROOT/'tools/controller.py'), '--config', str(root/'config.json'),
                       '--db', str(db), '--apply-lab-status']
            env = dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'], STUB_ROOT=str(root))
            retried = subprocess.run(command + ['--event', str(root/'event.json')], env=env,
                                     capture_output=True, text=True, timeout=20)
            self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
            receipt = json.loads(retried.stdout)['native_event']
            self.assertEqual(receipt['attempt'], 2)
            self.assertNotIn('cancelled_consumers', c.c.summary(gid)['blockers'])
            drain(db, root/'retry-workers')
            final = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(final.returncode, 0, final.stdout + final.stderr)
            final_statuses = json.loads(final.stdout)['statuses']
            self.assertEqual(len(final_statuses), 4)
            self.assertTrue(all(s['state']=='success' for s in final_statuses))
            # First event in an empty DB: failed attribution must survive new
            # group discovery, public execution and subsequent CLI finalize.
            empty_db = root/'empty.sqlite'
            command[command.index('--db')+1] = str(empty_db)
            (root/'run.json').write_text(json.dumps(dict(api.run, status='completed', conclusion='cancelled')))
            first = subprocess.run(command + ['--event', str(root/'event.json')], env=env,
                                   capture_output=True, text=True, timeout=20)
            self.assertEqual(first.returncode, 2, first.stdout + first.stderr)
            drain(empty_db, root/'empty-workers')
            later = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(later.returncode, 0, later.stdout + later.stderr)
            empty = Controller(empty_db, api, cfg)
            self.assertTrue(all('native_event_unresolved' in empty.c.summary(g['id'])['blockers']
                                for g in empty.c.store.export()['groups']))
            self.assertFalse(any(x['state']=='success' for x in json.loads(later.stdout)['statuses']))


if __name__ == '__main__':
    unittest.main()
