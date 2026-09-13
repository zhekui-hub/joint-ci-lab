#!/usr/bin/env python3
"""Local Joint CI experiment runner against FakeGhClient (no real GitHub).

Usage:
  python run_local.py                     # run all scenario_*.yaml
  python run_local.py scenario_e2.yaml    # run one / many paths
  python run_local.py --list
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import traceback
from typing import Any, Dict, List, Optional

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
PROTO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROTO, "arsenal"))
sys.path.insert(0, os.path.join(PROTO, "shared"))
sys.path.insert(0, HERE)

from assert_public_runs import (  # noqa: E402
    assert_checks,
    assert_dispatch_count,
    assert_exclusive_isolation,
    assert_joint_status,
    assert_no_joint_issue,
    assert_public_runs,
    assert_same_run_url,
    assert_wait_not_holding_pod,
)
from fake_gh import FakeGhClient  # noqa: E402
from joint_key import merge_remote_tests  # noqa: E402
from scheduler import invalidate, run as schedule_run, workflow_key  # noqa: E402


DEFAULT_TESTS = {
    "driver": [
        {"id": "multirepo_runtest", "params": {"profile": "default"}},
        {"id": "pseudo", "params": {"cards": 16}, "exclusive_to": "driver"},
    ],
    "synapse": [
        {"id": "multirepo_runtest", "params": {"profile": "default"}},
        {"id": "pseudo", "params": {"cards": 4}},
    ],
    "sim": [
        {"id": "arc_multirepo", "params": {"enable_abs_unit": False}},
        {"id": "multirepo_runtest", "params": {"profile": "default"}},
    ],
}


class ExperimentContext:
    def __init__(self) -> None:
        self.gh = FakeGhClient()
        self.reports: Dict[str, Dict[str, Any]] = {}
        self.pr_seq = 100
        self.joint_id = "joint-local-1"
        self.last_state = None
        self.sha_by_repo: Dict[str, str] = {}
        self.history: List[str] = []
        self.prev_joint_keys: List[str] = []

    def active_reports(self) -> List[Dict[str, Any]]:
        return list(self.reports.values())


def _parse_body_flags(body: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"deps": {}, "ci_mode": "normal", "joint_wait": False}
    if not body:
        return out
    for line in body.splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k == "CI_MODE":
            out["ci_mode"] = v
        elif k == "JOINT_WAIT":
            out["joint_wait"] = v.lower() in ("1", "true", "yes")
        elif k == "DLC_KERNEL_DRIVER_BRANCH":
            out["deps"]["driver_branch"] = v
        elif k == "DLC_SIM_BRANCH":
            out["deps"]["sim_branch"] = v
    return out


def _make_report(ctx: ExperimentContext, spec: Dict[str, Any]) -> Dict[str, Any]:
    repo = spec["repo"]
    branch = spec.get("branch", f"feature/{repo}")
    body = spec.get("body") or ""
    flags = _parse_body_flags(body)
    ci_mode = spec.get("ci_mode", flags["ci_mode"])
    joint_wait = flags["joint_wait"]
    if "joint_wait" in spec:
        joint_wait = bool(spec["joint_wait"])
    deps = spec.get("deps") or flags["deps"]
    ready = spec.get("ready", True)
    if "is_draft" in spec:
        is_draft = bool(spec["is_draft"])
    elif "ready" in spec:
        is_draft = not bool(spec["ready"])
    else:
        is_draft = not ready

    ctx.pr_seq += 1
    pr_number = spec.get("pr_number", ctx.pr_seq)
    head_sha = spec.get("head_sha") or f"{repo}-sha-{pr_number}"
    remote_tests = spec.get("remote_tests")
    if remote_tests is None:
        remote_tests = list(
            DEFAULT_TESTS.get(repo, [{"id": "multirepo_runtest", "params": {"profile": "default"}}])
        )

    return {
        "schema_version": 1,
        "repo": repo,
        "pr_number": pr_number,
        "branch": branch,
        "head_sha": head_sha,
        "base_ref": spec.get("base_ref", "main"),
        "ci_mode": ci_mode,
        "joint_wait": joint_wait,
        "deps": deps,
        "is_draft": is_draft,
        "remote_tests": remote_tests,
        "local_tests": spec.get("local_tests", ["format"]),
    }


def public_runs_from_state(ctx: ExperimentContext) -> Dict[str, int]:
    """Count public (non-exclusive) tests in the *current* joint state's workflow_runs."""
    state = ctx.last_state
    if not state or not getattr(state, "workflow_runs", None):
        return {}
    merged = merge_remote_tests([r for r in ctx.active_reports() if r.get("ci_mode") == "joint"])
    exclusive_keys = {workflow_key(t) for t in merged if t.get("exclusive_to")}
    counts: Dict[str, int] = {}
    for key in state.workflow_runs:
        if key in exclusive_keys:
            continue
        tid = key.split(":", 1)[0]
        counts[tid] = counts.get(tid, 0) + 1
    return counts


def step_create_pr(ctx: ExperimentContext, spec: Dict[str, Any]) -> None:
    report = _make_report(ctx, spec)
    key = f"{report['repo']}#{report['pr_number']}"
    ctx.reports[key] = report
    ctx.sha_by_repo[report["repo"]] = report["head_sha"]
    ctx.gh.add_pr(report)
    ctx.history.append(f"create_pr {key} ci_mode={report['ci_mode']} draft={report['is_draft']}")
    if spec.get("schedule", True):
        step_schedule(ctx, {})


def step_schedule(ctx: ExperimentContext, _spec: Dict[str, Any]) -> None:
    reports = ctx.active_reports()
    if not reports:
        ctx.last_state = None
        return
    before = ctx.gh.dispatch_count()
    state = schedule_run(reports, joint_id=ctx.joint_id, gh=ctx.gh, prior_state=ctx.last_state)
    if state.joint_key:
        ctx.prev_joint_keys.append(state.joint_key)
    ctx.last_state = state
    ctx.history.append(f"schedule -> {state.status} dispatches+={ctx.gh.dispatch_count() - before}")


def step_sleep(ctx: ExperimentContext, spec: Any) -> None:
    minutes = spec if isinstance(spec, (int, float)) else (spec or {}).get("minutes", 0)
    ctx.gh.runner_busy_seconds = 0.0
    ctx.history.append(f"sleep_minutes={minutes} (skipped locally; busy=0)")


def step_ready(ctx: ExperimentContext, spec: Dict[str, Any]) -> None:
    repo = spec["repo"]
    for r in ctx.reports.values():
        if r["repo"] == repo:
            r["is_draft"] = False
            ctx.gh.update_pr(repo, r["pr_number"], is_draft=False)
    ctx.history.append(f"ready {repo}")
    step_schedule(ctx, {})


def step_push(ctx: ExperimentContext, spec: Dict[str, Any]) -> None:
    repo = spec["repo"]
    new_sha = spec.get("new_sha") or f"{repo}-sha-pushed-{ctx.pr_seq}"
    if ctx.last_state and ctx.last_state.status in ("succeeded", "failed", "running"):
        invalidate(ctx.last_state, ctx.gh, reason="push")
        ctx.history.append("invalidate after push")
    for r in ctx.reports.values():
        if r["repo"] == repo:
            r["head_sha"] = new_sha
            ctx.sha_by_repo[repo] = new_sha
            ctx.gh.update_pr(repo, r["pr_number"], head_sha=new_sha)
    ctx.history.append(f"push {repo} -> {new_sha}")
    step_schedule(ctx, {})


def step_close_pr(ctx: ExperimentContext, spec: Dict[str, Any]) -> None:
    repo = spec["repo"]
    to_del = [k for k, r in ctx.reports.items() if r["repo"] == repo]
    for k in to_del:
        r = ctx.reports.pop(k)
        ctx.gh.close_pr(repo, r["pr_number"])
    if ctx.last_state:
        ctx.last_state.status = "cancelled"
        ctx.last_state.failure_reason = "peer_closed"
        ctx.gh.upsert_issue(ctx.last_state)
        for r in list(ctx.reports.values()):
            ctx.gh.write_check(
                r["repo"], r["head_sha"], "joint-ci", "failure", output_summary="peer_cancelled"
            )
    ctx.history.append(f"close_pr {repo}")


def step_inject_failure(ctx: ExperimentContext, spec: Dict[str, Any]) -> None:
    ctx.gh.inject_failure(spec["test_id"], spec.get("params"))
    ctx.history.append(f"inject_failure {spec['test_id']} {spec.get('params')}")


def step_set_remote_tests(ctx: ExperimentContext, spec: Dict[str, Any]) -> None:
    repo = spec["repo"]
    for r in ctx.reports.values():
        if r["repo"] == repo:
            r["remote_tests"] = spec["remote_tests"]
    ctx.history.append(f"set_remote_tests {repo}")


def step_assert(ctx: ExperimentContext, spec: Dict[str, Any]) -> None:
    state = ctx.last_state
    if "no_joint_issue" in spec and spec["no_joint_issue"]:
        assert_no_joint_issue(state)
    if "joint_status" in spec:
        assert_joint_status(getattr(state, "status", None), spec["joint_status"])
    if "runner_busy_seconds_max" in spec:
        assert_wait_not_holding_pod(ctx.gh.runner_busy_seconds, float(spec["runner_busy_seconds_max"]))
    if "dispatch_count" in spec:
        assert_dispatch_count(ctx.gh.dispatch_count(), int(spec["dispatch_count"]))
    if "dispatch_count_max" in spec:
        if ctx.gh.dispatch_count() > int(spec["dispatch_count_max"]):
            raise AssertionError(
                f"dispatch_count_max {spec['dispatch_count_max']}, got {ctx.gh.dispatch_count()}"
            )
    if "public_runs" in spec:
        assert_public_runs(public_runs_from_state(ctx), spec["public_runs"])
    if "public_runs_total" in spec:
        assert_public_runs(ctx.gh.public_runs(), spec["public_runs_total"])
    if "same_run_url_checks" in spec:
        urls = []
        for repo in spec["same_run_url_checks"]:
            sha = ctx.sha_by_repo.get(repo)
            urls.append(ctx.gh.check_details_url(repo, sha) if sha else None)
        assert_same_run_url(urls)
    if "checks" in spec:
        assert_checks(ctx.gh.check_conclusion, spec["checks"], ctx.sha_by_repo)
    if "exclusive_isolation" in spec:
        ei = spec["exclusive_isolation"]
        assert_exclusive_isolation(
            ctx.gh.check_conclusion("driver", ctx.sha_by_repo.get("driver", "")),
            ctx.gh.check_conclusion("synapse", ctx.sha_by_repo.get("synapse", "")),
            expect_driver=ei.get("driver", "failure"),
            expect_synapse=ei.get("synapse", "success"),
        )
    if "failure_reason" in spec:
        fr = getattr(state, "failure_reason", None)
        if fr != spec["failure_reason"]:
            raise AssertionError(f"failure_reason expected {spec['failure_reason']}, got {fr}")
    if "joint_key_changed" in spec and spec["joint_key_changed"]:
        keys = [k for k in ctx.prev_joint_keys if k]
        if len(set(keys)) < 2:
            raise AssertionError(f"expected joint_key to change after push; keys={keys}")
    if "merged_test_ids_contains" in spec:
        ids = {key.split(":", 1)[0] for key in (state.workflow_runs or {})}
        for need in spec["merged_test_ids_contains"]:
            if need not in ids:
                raise AssertionError(f"merged tests missing {need}; have {ids}")
    if "old_checks_not_success" in spec and spec["old_checks_not_success"]:
        if "invalidate after push" not in ctx.history:
            raise AssertionError("expected invalidate after push in history")
    ctx.history.append(f"assert ok {json.dumps(spec, sort_keys=True)}")


STEP_HANDLERS = {
    "create_pr": step_create_pr,
    "schedule": step_schedule,
    "sleep_minutes": step_sleep,
    "ready": step_ready,
    "push": step_push,
    "close_pr": step_close_pr,
    "inject_failure": step_inject_failure,
    "set_remote_tests": step_set_remote_tests,
    "assert": step_assert,
}


def run_scenario(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    ctx = ExperimentContext()
    ctx.joint_id = f"joint-{doc.get('id', 'X')}"
    steps = doc.get("steps") or []
    try:
        for step in steps:
            if not isinstance(step, dict) or len(step) != 1:
                raise ValueError(f"invalid step: {step}")
            name, spec = next(iter(step.items()))
            if name not in STEP_HANDLERS:
                raise ValueError(f"unknown step {name}")
            STEP_HANDLERS[name](ctx, spec if spec is not None else {})
        return {
            "id": doc.get("id"),
            "name": doc.get("name"),
            "path": path,
            "result": "PASS",
            "reason": "",
            "history": ctx.history,
            "final_status": getattr(ctx.last_state, "status", None),
            "public_runs": public_runs_from_state(ctx),
            "dispatch_count": ctx.gh.dispatch_count(),
        }
    except Exception as e:
        return {
            "id": doc.get("id"),
            "name": doc.get("name"),
            "path": path,
            "result": "FAIL",
            "reason": f"{type(e).__name__}: {e}",
            "history": ctx.history,
            "traceback": traceback.format_exc(),
            "final_status": getattr(ctx.last_state, "status", None),
            "public_runs": public_runs_from_state(ctx),
            "dispatch_count": ctx.gh.dispatch_count(),
        }


def discover_scenarios(root: str) -> List[str]:
    return sorted(glob.glob(os.path.join(root, "scenario_*.yaml")))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run Joint CI local experiment scenarios")
    parser.add_argument("scenarios", nargs="*", help="scenario yaml paths (default: all scenario_*.yaml)")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    parser.add_argument("--json", action="store_true", help="print machine-readable results")
    args = parser.parse_args(argv)

    paths = args.scenarios or discover_scenarios(HERE)
    paths = [os.path.abspath(p) for p in paths]

    if args.list:
        for p in paths:
            print(p)
        return 0

    results = [run_scenario(p) for p in paths]
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print("=== Joint CI local experiment results ===")
        for r in results:
            mark = r["result"]
            extra = f" — {r['reason']}" if r.get("reason") else ""
            print(f"{r.get('id') or '?'}: {mark}{extra}")
        passed = sum(1 for r in results if r["result"] == "PASS")
        print(f"--- {passed}/{len(results)} PASS ---")

    return 0 if all(r["result"] == "PASS" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
