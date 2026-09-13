"""Unit tests for joint CI scheduler (FakeGhClient / dry-run)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))

from gh_client import FakeGhClient
from scheduler import invalidate, invalidate_on_push, run


def _synapse_wait(driver_branch="feature/e2-driver", sha="synapse-aaa", pr=456):
    return {
        "schema_version": 1,
        "repo": "synapse",
        "pr_number": pr,
        "branch": "feature/e2-synapse",
        "head_sha": sha,
        "ci_mode": "joint",
        "joint_wait": True,
        "deps": {"driver_branch": driver_branch},
        "is_draft": False,
        "remote_tests": [{"id": "multirepo_runtest", "params": {"profile": "default"}}],
    }


def _driver(branch="feature/e2-driver", sha="driver-bbb", pr=100, draft=False):
    return {
        "schema_version": 1,
        "repo": "driver",
        "pr_number": pr,
        "branch": branch,
        "head_sha": sha,
        "ci_mode": "joint",
        "joint_wait": False,
        "deps": {},
        "is_draft": draft,
        "remote_tests": [
            {"id": "multirepo_runtest", "params": {"profile": "default"}},
            {"id": "pseudo", "params": {"cards": 16}, "exclusive_to": "driver"},
        ],
    }


class SchedulerTests(unittest.TestCase):
    def test_e2_waiting_deps_no_dispatch(self):
        gh = FakeGhClient()
        state = run([_synapse_wait()], joint_id="joint-e2", gh=gh)
        self.assertEqual(state.status, "waiting_deps")
        self.assertEqual(gh.dispatch_count(), 0)
        self.assertEqual(gh.runner_busy_seconds, 0.0)
        check = gh.get_check("synapse", "synapse-aaa")
        self.assertIsNotNone(check)
        self.assertEqual(check["conclusion"], "pending")

    def test_both_ready_dispatch_once_then_succeeded(self):
        gh = FakeGhClient()
        syn = _synapse_wait()
        drv = _driver()
        gh.add_pr(drv)
        gh.add_pr(syn)
        state = run([syn, drv], joint_id="joint-e3", gh=gh)
        self.assertEqual(state.status, "succeeded")
        self.assertEqual(gh.public_runs().get("multirepo_runtest"), 1)
        # exclusive also dispatched once
        self.assertEqual(gh.dispatch_count(), 2)
        self.assertIsNotNone(state.joint_key)

    def test_identical_joint_key_no_extra_dispatch(self):
        gh = FakeGhClient()
        syn = _synapse_wait()
        drv = _driver()
        gh.add_pr(drv)
        gh.add_pr(syn)
        s1 = run([syn, drv], joint_id="joint-dedupe", gh=gh)
        n = gh.dispatch_count()
        s2 = run([syn, drv], joint_id="joint-dedupe", gh=gh, prior_state=s1)
        self.assertEqual(gh.dispatch_count(), n)
        self.assertEqual(s2.joint_key, s1.joint_key)
        self.assertEqual(s2.workflow_runs, s1.workflow_runs)
        self.assertIn(s2.status, ("succeeded", "running"))

    def test_exclusive_failure_only_driver(self):
        gh = FakeGhClient()
        gh.inject_failure("pseudo", {"cards": 16})
        syn = _synapse_wait()
        # synapse also reports a 4-card pseudo so public path still succeeds
        syn = dict(syn)
        syn["remote_tests"] = [
            {"id": "multirepo_runtest", "params": {"profile": "default"}},
            {"id": "pseudo", "params": {"cards": 4}},
        ]
        drv = _driver()
        gh.add_pr(drv)
        gh.add_pr(syn)
        state = run([syn, drv], joint_id="joint-e4", gh=gh)
        # Issue-level: public ok → succeeded; exclusive failure isolated to driver check
        self.assertEqual(state.status, "succeeded")
        self.assertEqual(gh.check_conclusion("driver", drv["head_sha"]), "failure")
        self.assertEqual(gh.check_conclusion("synapse", syn["head_sha"]), "success")

    def test_invalidate_then_new_sha_new_dispatch(self):
        gh = FakeGhClient()
        syn = _synapse_wait(sha="synapse-a")
        drv = _driver(sha="driver-a")
        gh.add_pr(drv)
        gh.add_pr(syn)
        s1 = run([syn, drv], joint_id="joint-e5", gh=gh)
        self.assertEqual(s1.status, "succeeded")
        n1 = gh.dispatch_count()
        key1 = s1.joint_key

        invalidate(s1, gh, reason="push")
        self.assertEqual(s1.status, "invalidated")
        self.assertEqual(gh.check_conclusion("synapse", "synapse-a"), "pending")

        syn2 = dict(syn)
        syn2["head_sha"] = "synapse-b"
        gh.update_pr("synapse", syn["pr_number"], head_sha="synapse-b")
        s2 = run([syn2, drv], joint_id="joint-e5", gh=gh, prior_state=s1)
        self.assertEqual(s2.status, "succeeded")
        self.assertNotEqual(s2.joint_key, key1)
        self.assertGreater(gh.dispatch_count(), n1)

    def test_invalidate_on_push_helper(self):
        gh = FakeGhClient()
        syn = _synapse_wait(sha="synapse-a")
        drv = _driver(sha="driver-a")
        gh.add_pr(drv)
        gh.add_pr(syn)
        s1 = run([syn, drv], joint_id="joint-e5b", gh=gh)
        syn2 = dict(syn)
        syn2["head_sha"] = "synapse-b"
        out = invalidate_on_push([syn2, drv], s1, gh=gh)
        self.assertIsNotNone(out)
        self.assertEqual(out.status, "invalidated")

    def test_multi_open_prs_fail_no_dispatch(self):
        gh = FakeGhClient()
        gh.seed_pr("driver", "feature/e6-shared", "sha-a", pr_number=601)
        gh.seed_pr("driver", "feature/e6-shared", "sha-b", pr_number=602)
        syn = _synapse_wait(driver_branch="feature/e6-shared")
        state = run([syn], joint_id="joint-e6", gh=gh)
        self.assertEqual(state.status, "failed")
        self.assertEqual(state.failure_reason, "ambiguous_prs")
        self.assertEqual(gh.dispatch_count(), 0)
        self.assertEqual(gh.public_runs(), {})

    def test_draft_participant_waiting(self):
        gh = FakeGhClient()
        syn = _synapse_wait()
        drv = _driver(draft=True)
        gh.add_pr(drv)
        gh.add_pr(syn)
        state = run([syn, drv], joint_id="joint-e7", gh=gh)
        self.assertEqual(state.status, "waiting_deps")
        self.assertEqual(gh.dispatch_count(), 0)

    def test_non_joint_skipped(self):
        gh = FakeGhClient()
        report = {
            "repo": "driver",
            "pr_number": 1,
            "head_sha": "x",
            "ci_mode": "normal",
            "remote_tests": [{"id": "multirepo_runtest", "params": {}}],
        }
        state = run([report], gh=gh)
        self.assertEqual(state.status, "skipped_non_joint")
        self.assertIsNone(state.issue_number)
        self.assertEqual(gh.dispatch_count(), 0)


if __name__ == "__main__":
    unittest.main()
