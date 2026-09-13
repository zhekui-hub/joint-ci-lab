"""Unit tests for joint CI scheduler (FakeGhClient / dry-run).

Plan: Arsenal dispatches public tests only; exclusive stays on business repos.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))

from gh_client import FakeGhClient
from scheduler import invalidate, invalidate_on_push, run


def _synapse_wait(driver_branch="feature/e2-driver", sha="synapse-aaa", pr=456):
    return {"schema_version": 1, "repo": "synapse", "pr_number": pr,
            "branch": "feature/e2-synapse", "head_sha": sha, "ci_mode": "joint",
            "joint_wait": True, "deps": {"driver_branch": driver_branch},
            "is_draft": False,
            "remote_tests": [{"id": "multirepo_runtest", "params": {"profile": "default"}}]}


def _driver(branch="feature/e2-driver", sha="driver-bbb", pr=100, draft=False):
    return {"schema_version": 1, "repo": "driver", "pr_number": pr, "branch": branch,
            "head_sha": sha, "ci_mode": "joint", "joint_wait": False, "deps": {},
            "is_draft": draft,
            "remote_tests": [
                {"id": "multirepo_runtest", "params": {"profile": "default"}},
                {"id": "pseudo", "params": {"cards": 16}, "exclusive_to": "driver"},
            ]}


def _sim(branch="feature/e2-sim", sha="sim-ccc", pr=200):
    return {"schema_version": 1, "repo": "sim", "pr_number": pr,
            "branch": branch, "head_sha": sha, "ci_mode": "joint",
            "joint_wait": True, "deps": {"driver_branch": "feature/e2-driver"},
            "is_draft": False,
            "remote_tests": [
                {"id": "multirepo_runtest", "params": {"profile": "default"}},
                {"id": "arc_multirepo", "params": {"enable_abs_unit": False}},
            ]}


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
        self.assertIn(("synapse", "synapse-aaa", "joint-ci"), gh.checks)

    def test_both_ready_dispatch_public_only_once(self):
        gh = FakeGhClient(); syn = _synapse_wait(); drv = _driver()
        gh.add_pr(drv); gh.add_pr(syn)
        state = run([syn, drv], joint_id="joint-e3", gh=gh)
        self.assertEqual(state.status, "succeeded")
        self.assertEqual(gh.public_runs().get("multirepo_runtest"), 1)
        self.assertEqual(gh.dispatch_count(), 1)
        self.assertTrue(all(not d.get("exclusive_to") for d in gh.dispatches))
        self.assertEqual(state.joint_run_key, state.joint_key)
        self.assertEqual(gh.check_conclusion("driver", drv["head_sha"], "joint-ci"), "success")
        self.assertEqual(gh.check_conclusion("synapse", syn["head_sha"], "joint-ci"), "success")

    def test_identical_joint_key_no_extra_dispatch(self):
        gh = FakeGhClient(); syn = _synapse_wait(); drv = _driver()
        gh.add_pr(drv); gh.add_pr(syn)
        s1 = run([syn, drv], joint_id="joint-dedupe", gh=gh); n = gh.dispatch_count()
        s2 = run([syn, drv], joint_id="joint-dedupe", gh=gh, prior_state=s1)
        self.assertEqual(gh.dispatch_count(), n)
        self.assertEqual(s2.joint_run_key, s1.joint_run_key)
        self.assertEqual(s2.workflow_runs, s1.workflow_runs)
        self.assertIn(s2.status, ("succeeded", "running"))

    def test_exclusive_ignored_public_failure_fails_both(self):
        gh = FakeGhClient()
        gh.inject_failure("multirepo_runtest", {"profile": "default"})
        gh.inject_failure("pseudo", {"cards": 16})
        syn = _synapse_wait(); drv = _driver(); gh.add_pr(drv); gh.add_pr(syn)
        state = run([syn, drv], joint_id="joint-e4", gh=gh)
        self.assertEqual(state.status, "failed")
        self.assertEqual(gh.dispatch_count(), 1)
        self.assertEqual(gh.check_conclusion("driver", drv["head_sha"], "joint-ci"), "failure")
        self.assertEqual(gh.check_conclusion("synapse", syn["head_sha"], "joint-ci"), "failure")

    def test_exclusive_not_dispatched_when_public_ok(self):
        gh = FakeGhClient(); gh.inject_failure("pseudo", {"cards": 16})
        syn = _synapse_wait(); drv = _driver(); gh.add_pr(drv); gh.add_pr(syn)
        state = run([syn, drv], joint_id="joint-e4b", gh=gh)
        self.assertEqual(state.status, "succeeded")
        self.assertEqual(gh.dispatch_count(), 1)
        self.assertEqual(gh.check_conclusion("driver", drv["head_sha"], "joint-ci"), "success")
        self.assertEqual(gh.check_conclusion("synapse", syn["head_sha"], "joint-ci"), "success")

    def test_invalidate_then_new_sha_new_dispatch(self):
        gh = FakeGhClient(); syn = _synapse_wait(sha="synapse-a"); drv = _driver(sha="driver-a")
        gh.add_pr(drv); gh.add_pr(syn)
        s1 = run([syn, drv], joint_id="joint-e5", gh=gh)
        n1 = gh.dispatch_count(); key1 = s1.joint_run_key
        invalidate(s1, gh, reason="push")
        self.assertEqual(gh.check_conclusion("synapse", "synapse-a"), "pending")
        syn2 = dict(syn); syn2["head_sha"] = "synapse-b"
        gh.update_pr("synapse", syn["pr_number"], head_sha="synapse-b")
        s2 = run([syn2, drv], joint_id="joint-e5", gh=gh, prior_state=s1)
        self.assertEqual(s2.status, "succeeded")
        self.assertNotEqual(s2.joint_run_key, key1)
        self.assertGreater(gh.dispatch_count(), n1)

    def test_invalidate_on_push_helper(self):
        gh = FakeGhClient(); syn = _synapse_wait(sha="synapse-a"); drv = _driver(sha="driver-a")
        gh.add_pr(drv); gh.add_pr(syn)
        s1 = run([syn, drv], joint_id="joint-e5b", gh=gh)
        syn2 = dict(syn); syn2["head_sha"] = "synapse-b"
        out = invalidate_on_push([syn2, drv], s1, gh=gh)
        self.assertEqual(out.status, "invalidated")

    def test_changed_sha_does_not_reuse_old_workflow_run(self):
        gh = FakeGhClient()
        syn = _synapse_wait(sha="syn-sha-1", pr=457)
        drv = _driver(sha="drv-sha-1", pr=101)
        gh.add_pr(drv); gh.add_pr(syn)
        s1 = run([syn, drv], joint_id="joint-e5c", gh=gh)
        self.assertEqual(gh.dispatch_count(), 1)

        syn2 = dict(syn); syn2["head_sha"] = "syn-sha-2"
        drv2 = dict(drv)
        s2 = run([syn2, drv2], joint_id="joint-e5c", gh=gh, prior_state=s1)

        self.assertNotEqual(s1.joint_key, s2.joint_key)
        self.assertEqual(gh.dispatch_count(), 2)
        self.assertNotEqual(s1.workflow_runs, s2.workflow_runs)

    def test_multi_open_prs_fail_no_dispatch(self):
        gh = FakeGhClient()
        gh.seed_pr("driver", "feature/e6-shared", "sha-a", pr_number=601)
        gh.seed_pr("driver", "feature/e6-shared", "sha-b", pr_number=602)
        state = run([_synapse_wait(driver_branch="feature/e6-shared")], joint_id="joint-e6", gh=gh)
        self.assertEqual(state.failure_reason, "ambiguous_prs")
        self.assertEqual(gh.dispatch_count(), 0)

    def test_draft_participant_waiting(self):
        gh = FakeGhClient(); syn = _synapse_wait(); drv = _driver(draft=True)
        gh.add_pr(drv); gh.add_pr(syn)
        state = run([syn, drv], joint_id="joint-e7", gh=gh)
        self.assertEqual(state.status, "waiting_deps")
        self.assertEqual(gh.dispatch_count(), 0)

    def test_non_joint_skipped(self):
        gh = FakeGhClient()
        report = {"repo": "driver", "pr_number": 1, "head_sha": "x",
                  "ci_mode": "normal", "remote_tests": []}
        state = run([report], gh=gh)
        self.assertEqual(state.status, "skipped_non_joint")
        self.assertEqual(gh.dispatch_count(), 0)

    def test_three_repo_union_dedupes_common_and_keeps_distinct_public(self):
        gh = FakeGhClient()
        syn = _synapse_wait()
        drv = _driver()
        sim = _sim()
        gh.add_pr(drv); gh.add_pr(syn); gh.add_pr(sim)
        state = run([syn, drv, sim], joint_id="joint-f10", gh=gh)
        self.assertEqual(state.status, "succeeded")
        self.assertEqual(gh.public_runs(), {"multirepo_runtest": 1, "arc_multirepo": 1})
        self.assertEqual(gh.dispatch_count(), 2)
        self.assertEqual({r["repo"] for r in state.participants}, {"driver", "synapse", "sim"})

    def test_three_repo_public_failure_broadcasts_to_all(self):
        gh = FakeGhClient()
        gh.inject_failure("arc_multirepo", {"enable_abs_unit": False})
        syn = _synapse_wait(); drv = _driver(); sim = _sim()
        gh.add_pr(drv); gh.add_pr(syn); gh.add_pr(sim)
        state = run([syn, drv, sim], joint_id="joint-f11", gh=gh)
        self.assertEqual(state.status, "failed")
        for report in (syn, drv, sim):
            self.assertEqual(gh.check_conclusion(report["repo"], report["head_sha"]), "failure")

    def test_three_repo_private_failure_is_not_dispatched(self):
        gh = FakeGhClient(); gh.inject_failure("pseudo", {"cards": 16})
        syn = _synapse_wait(); drv = _driver(); sim = _sim()
        gh.add_pr(drv); gh.add_pr(syn); gh.add_pr(sim)
        state = run([syn, drv, sim], joint_id="joint-f12", gh=gh)
        self.assertEqual(state.status, "succeeded")
        self.assertNotIn("pseudo", {d["test_id"] for d in gh.dispatches})

    def test_check_write_failure_blocks_joint_success(self):
        gh = FakeGhClient(); gh.inject_check_write_failure("sim")
        syn = _synapse_wait(); drv = _driver(); sim = _sim()
        gh.add_pr(drv); gh.add_pr(syn); gh.add_pr(sim)
        state = run([syn, drv, sim], joint_id="joint-f13", gh=gh)
        self.assertEqual(state.status, "failed")
        self.assertEqual(state.failure_reason, "check_write_failed")
        self.assertTrue(state.check_errors)

    def test_running_result_recovers_without_duplicate_dispatch(self):
        gh = FakeGhClient(); gh.set_defer_conclusions(True)
        syn = _synapse_wait(); drv = _driver(); gh.add_pr(drv); gh.add_pr(syn)
        first = run([syn, drv], joint_id="joint-f14", gh=gh)
        self.assertEqual(first.status, "running")
        run_id = next(iter(first.workflow_runs.values()))
        gh.set_run_conclusion(run_id, "success")
        second = run([syn, drv], joint_id="joint-f14", gh=gh, prior_state=first)
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(gh.dispatch_count(), 1)

    def test_invalidation_cancels_running_public_runs(self):
        gh = FakeGhClient(); gh.set_defer_conclusions(True)
        syn = _synapse_wait(); drv = _driver(); gh.add_pr(drv); gh.add_pr(syn)
        state = run([syn, drv], joint_id="joint-f15", gh=gh)
        run_id = next(iter(state.workflow_runs.values()))
        invalidate(state, gh, reason="push")
        self.assertEqual(state.status, "invalidated")
        self.assertIn(run_id, gh.cancelled_runs)


if __name__ == "__main__":
    unittest.main()
