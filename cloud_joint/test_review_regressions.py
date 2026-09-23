"""Author: zhekui. Regression tests for review-discovered protocol/evidence defects.

ReviewRegressions: consumer retry, fixed references, private snapshot identities,
independent gate recomputation, hostile evidence paths and GitHub bridge behavior.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from .adapter import inventory
from .controller import Controller
from .coordinator import Coordinator, Conflict
from .evidence import validate_observation
from .fixtures import plan, reports, FIELDS, REPOS
from .model import member_id
from .scenarios import Scenario
from .worker import drain


class FakeLive:
    def __init__(self):
        self.allowed = set(REPOS)
        self.writes = []
        self.prs = {r: dict(number=1, body="\n".join(f"{k}=feature-{v.split('/')[1]}" for k, v in FIELDS.items() if v != r),
                           state="open", draft=False, merged=False,
                           head=dict(sha="a" * 40, ref="feature-" + r.split("/")[1], repo=dict(full_name=r)),
                           base=dict(sha="b" * 40), merge_commit_sha="c" * 40) for r in REPOS}
        self.run = dict(workflow_id=9, run_attempt=1, head_sha="a" * 40,
                        status="in_progress", conclusion=None, pull_requests=[{"number": 1}])

    def pages(self, repo, path, key=None):
        if path.startswith("pulls"):
            return [self.prs[repo]] if self.prs[repo]["state"] == "open" else []
        raise AssertionError(path)

    def request(self, repo, path, method="GET", body=None):
        if method == "POST":
            self.writes.append((repo, path, copy.deepcopy(body)))
            return {}
        if path.startswith("pulls/"):
            return copy.deepcopy(self.prs[repo])
        if path.startswith("actions/runs/"):
            return copy.deepcopy(self.run)
        if path.startswith("commits/"):
            return {"sha": "c" * 40 if path.endswith("c" * 40) else "a" * 40}
        raise AssertionError(path)

    def private_checks(self, repo, sha, required, scopes=None):
        return {name: "success" for name in required}

    def review_decision(self, repo, number):
        return "APPROVED"


def config():
    p = plan()
    return dict(environment=p["environment"], policy_version="test",
                repositories=[dict(repo=r, field=k, default_branch="main",
                    required_app_checks={"private": 42}, private_scopes={"private": "head"},
                    approval_policy="required", aggregate_name="joint", native_workflow_ids=[9],
                    tasks=[dict(p["tasks"][i])]) for i, (k, r) in enumerate(FIELDS.items())])


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_consumer_retry_does_not_revive_other(self):
        e = Scenario(self.root / "case").run(40)
        self.assertIsNone(e["error"])
        self.assertTrue(validate_observation(e))

    def test_fixed_sha_and_default_not_replaced_by_pr_candidate(self):
        api = FakeLive()
        api.prs[REPOS[0]]["body"] = "DLC_KERNEL_DRIVER_BRANCH=" + "a" * 40
        data = inventory(api, config())
        r = next(x for x in data["reports"] if x["repo"] == REPOS[0])
        self.assertEqual(r["versions"][REPOS[1]]["candidate"], "a" * 40)
        self.assertEqual(r["versions"][REPOS[2]]["candidate"], "a" * 40)
        self.assertEqual(r["versions"][REPOS[0]]["candidate"], "c" * 40)

    def test_snapshot_private_check_requires_matching_receipt(self):
        c = Coordinator(self.root / "db")
        p = plan()
        p["members"][0]["private_scopes"]["private"] = "snapshot"
        gid = c.reconcile(p)
        drain(c.store.path, self.root / "workers")
        self.assertFalse(c.summary(gid)["merge_ready"])
        p["members"][0]["private"]["private"] = {"conclusion": "success", "snapshot": c.summary(gid)["snapshot"]}
        c.reconcile(p)
        self.assertTrue(c.summary(gid)["merge_ready"])
        p["versions"][REPOS[1]]["head"] = "f" * 40
        c.reconcile(p)
        drain(c.store.path, self.root / "workers")
        self.assertFalse(c.summary(gid)["merge_ready"])

    def test_oracle_independently_rejects_gate_tamper(self):
        e = Scenario(self.root / "case").run(40)
        self.assertTrue(validate_observation(e))
        e["summaries"][0]["merge_ready"] = True
        self.assertFalse(validate_observation(e))

    def test_symlink_tree_rejected_before_copy(self):
        from tools.acceptance import reject_links
        directory = self.root / "results"
        directory.mkdir()
        secret = self.root / "outside"
        secret.write_text("not-to-be-copied")
        (directory / "extra").symlink_to(secret)
        with self.assertRaises(ValueError):
            reject_links(directory)

    def test_live_controller_publish_and_partial_merge(self):
        api = FakeLive()
        controller = Controller(self.root / "db", api, config())
        proposals = controller.sync()
        self.assertEqual(proposals[0]["mode"], "joint")
        drain(controller.c.store.path, self.root / "workers")
        statuses = controller.publish()
        self.assertEqual(len(statuses), 4)
        self.assertTrue(all(s["state"] == "success" for s in statuses))
        api.prs[REPOS[0]].update(merged=True, state="closed", merge_commit_sha="d" * 40, merged_at="2026-09-23T00:00:00Z")
        controller.sync()
        groups = controller.c.store.export()["groups"]
        self.assertTrue(any(g.get("partial_merge", [{}])[0].get("commit") == "d" * 40 for g in groups))

    def test_native_cancel_targets_bound_attempt(self):
        api = FakeLive()
        controller = Controller(self.root / "db", api, config())
        controller.sync()
        binding = controller.native_event(REPOS[0], 123)
        api.run.update(status="completed", conclusion="cancelled")
        controller.native_event(REPOS[0], 123)
        self.assertIn("cancelled_consumers", controller.c.summary(binding["group"])["blockers"])

    def test_late_native_rerun_remains_blocked_after_finalize(self):
        api = FakeLive()
        controller = Controller(self.root / "db", api, config())
        controller.sync()
        drain(controller.c.store.path, self.root / "workers")
        controller.publish()
        api.run.update(run_attempt=2, status="completed", conclusion="success")
        with self.assertRaises(Conflict):
            controller.native_event(REPOS[0], 123)
        statuses = controller.publish()
        self.assertTrue(statuses)
        self.assertTrue(all(s["state"] == "pending" for s in statuses))
        controller.sync()
        for g in controller.c.store.export()["groups"]:
            self.assertFalse(controller.c.summary(g["id"])["merge_ready"])


if __name__ == "__main__":
    unittest.main()
