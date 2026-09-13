#!/usr/bin/env python3
"""Arsenal joint CI scheduler (prototype).

Responsibilities:
- parse reports
- load/create joint issue state
- wait without holding test pods (exit when deps missing)
- merge tests, compute joint_key, dispatch once (dedupe by joint_key)
- map results back per-PR (exclusive failures isolated)

This file is meant to be moved to Arsenal under joint_ci/scheduler.py.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
sys.path.insert(0, os.path.dirname(__file__))

from joint_key import (  # noqa: E402
    make_joint_key,
    merge_remote_tests,
    per_pr_required,
    canonical_json,
    hash_merged_tests,
    workflow_key,
)
from gh_client import GhClient, FakeGhClient, default_client  # noqa: E402

# Re-export for experiments/run_local.py
__all__ = [
    "JointState",
    "run",
    "invalidate",
    "invalidate_on_push",
    "workflow_key",
    "missing_deps",
    "GhClient",
    "FakeGhClient",
]

# In-process serialization keyed by joint_id
_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(joint_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        if joint_id not in _LOCKS:
            _LOCKS[joint_id] = threading.Lock()
        return _LOCKS[joint_id]


TERMINAL = {"succeeded", "failed", "cancelled"}
REUSABLE = {"running", "succeeded"}


@dataclass
class JointState:
    joint_id: str
    status: str
    participants: List[Dict[str, Any]] = field(default_factory=list)
    waiting_for: List[Dict[str, str]] = field(default_factory=list)
    versions: Dict[str, str] = field(default_factory=dict)
    joint_key: Optional[str] = None
    workflow_runs: Dict[str, str] = field(default_factory=dict)
    issue_number: Optional[int] = None
    failure_reason: Optional[str] = None

    def to_issue_body(self) -> str:
        return (
            "<!-- joint-ci-state\n"
            + json.dumps(self.__dict__, ensure_ascii=False, indent=2)
            + "\n-->\n\n"
            + f"## Joint `{self.joint_id}`\n\n"
            + f"- status: `{self.status}`\n"
            + f"- joint_key: `{self.joint_key}`\n"
            + f"- waiting_for: `{json.dumps(self.waiting_for, ensure_ascii=False)}`\n"
            + f"- versions: `{json.dumps(self.versions, ensure_ascii=False)}`\n"
        )

    @staticmethod
    def from_issue_body(body: str) -> Optional["JointState"]:
        marker = "<!-- joint-ci-state"
        if marker not in body:
            return None
        start = body.index(marker) + len(marker)
        end = body.index("-->", start)
        data = json.loads(body[start:end].strip())
        return JointState(**data)


def missing_deps(reports: List[Dict[str, Any]], gh: GhClient) -> List[Dict[str, str]]:
    missing: List[Dict[str, str]] = []
    for report in reports:
        if not report.get("joint_wait"):
            continue
        deps = report.get("deps") or {}
        mapping = {
            "driver_branch": "driver",
            "sim_branch": "sim",
        }
        for key, repo in mapping.items():
            branch = deps.get(key)
            if not branch:
                continue
            if report.get("repo") == repo:
                continue
            if any(r.get("repo") == repo for r in reports):
                continue
            pr = gh.find_open_pr(repo, branch)
            if pr is None:
                missing.append({"repo": repo, "branch": branch})
            elif isinstance(pr, dict) and pr.get("__ambiguous__"):
                # Ambiguity handled separately; treat as not a usable single dep
                pass
    return missing


def find_ambiguous_deps(reports: List[Dict[str, Any]], gh: GhClient) -> Optional[Dict[str, Any]]:
    """Return ambiguity descriptor if a JOINT_WAIT dep branch has multiple open PRs."""
    for report in reports:
        if report.get("ci_mode") != "joint":
            continue
        deps = report.get("deps") or {}
        mapping = {"driver_branch": "driver", "sim_branch": "sim"}
        for key, repo in mapping.items():
            branch = deps.get(key)
            if not branch:
                continue
            if report.get("repo") == repo:
                continue
            prs = gh.find_open_prs(repo, branch)
            report_hits = [
                r for r in reports if r.get("repo") == repo and r.get("branch") == branch
            ]
            count = len(prs) if prs else len(report_hits)
            if count > 1:
                return {"repo": repo, "branch": branch, "count": count, "reason": "ambiguous_prs"}
            pr = gh.find_open_pr(repo, branch)
            if isinstance(pr, dict) and pr.get("__ambiguous__"):
                return pr
    return None


def all_ready(reports: List[Dict[str, Any]]) -> bool:
    return all(not r.get("is_draft") for r in reports)


def _public_details_url(state: JointState) -> str:
    for key, run_id in state.workflow_runs.items():
        if key.startswith("multirepo_runtest:") or key.startswith("arc_multirepo"):
            return f"fake://runs/{run_id}"
    if state.workflow_runs:
        run_id = next(iter(state.workflow_runs.values()))
        return f"fake://runs/{run_id}"
    return ""


def invalidate(state: JointState, gh: GhClient, reason: str = "push") -> JointState:
    """Mark joint result invalidated and reset checks to pending/waiting."""
    state.status = "invalidated"
    state.failure_reason = reason
    # Clear joint_key reuse: old key must not be reused after invalidate
    for r in state.participants:
        gh.write_check(
            r["repo"],
            r["head_sha"],
            "joint-ci",
            "pending",
            output_summary=f"invalidated:{reason}",
        )
    return gh.upsert_issue(state)


def invalidate_on_push(
    reports: List[Dict[str, Any]],
    prior_state: Optional[JointState],
    gh: Optional[GhClient] = None,
) -> Optional[JointState]:
    """E5 helper: invalidate prior state when participant SHAs change."""
    if prior_state is None:
        return None
    gh = gh or default_client()
    old_versions = prior_state.versions or {}
    new_versions = {r["repo"]: r["head_sha"] for r in reports if r.get("ci_mode") == "joint"}
    if old_versions and old_versions != new_versions:
        return invalidate(prior_state, gh, reason="push")
    return prior_state


def aggregate_and_write_checks(
    reports: List[Dict[str, Any]],
    merged: List[Dict[str, Any]],
    results: Dict[str, str],
    state: JointState,
    gh: GhClient,
) -> str:
    """Write per-PR checks. Exclusive failures only fail that PR.

    Returns overall joint status: failed if any *public* test failed, else succeeded.
    """
    details = _public_details_url(state)
    public_failed = False
    for t in merged:
        key = workflow_key(t)
        if results.get(key) == "failure" and not t.get("exclusive_to"):
            public_failed = True

    for r in reports:
        req = set(per_pr_required(r, merged))
        failed = False
        for t in merged:
            key = workflow_key(t)
            if key not in req:
                continue
            if results.get(key) != "failure":
                continue
            excl = t.get("exclusive_to")
            if excl and excl != r["repo"]:
                continue
            failed = True
        conclusion = "failure" if failed else "success"
        summary = "public_or_exclusive_failure" if failed else "ok"
        gh.write_check(
            r["repo"],
            r["head_sha"],
            "joint-ci",
            conclusion,
            details_url=details,
            output_summary=summary,
        )
    return "failed" if public_failed else "succeeded"


def _find_reusable(
    joint_key: str,
    versions: Dict[str, str],
    prior_state: Optional[JointState],
    gh: GhClient,
) -> Optional[JointState]:
    """Reuse existing running/succeeded result for same joint_key + versions."""
    candidates: List[JointState] = []
    if prior_state is not None:
        candidates.append(prior_state)
    issues = getattr(gh, "issues", None)
    if isinstance(issues, dict):
        for st in issues.values():
            if st is prior_state:
                continue
            candidates.append(st)
    for st in candidates:
        if (
            getattr(st, "joint_key", None) == joint_key
            and getattr(st, "status", None) in REUSABLE
            and getattr(st, "versions", None) == versions
            and getattr(st, "workflow_runs", None)
        ):
            return st
    return None


def _rewrite_checks_from_reuse(
    reports: List[Dict[str, Any]],
    merged: List[Dict[str, Any]],
    state: JointState,
    gh: GhClient,
) -> str:
    """Re-aggregate checks using stored run conclusions for reused workflow_runs."""
    results: Dict[str, str] = {}
    for t in merged:
        key = workflow_key(t)
        run_id = state.workflow_runs.get(key)
        if run_id:
            results[key] = gh.get_run_conclusion(run_id)
        else:
            results[key] = "success"
    return aggregate_and_write_checks(reports, merged, results, state, gh)


def run(
    reports: List[Dict[str, Any]],
    joint_id: str = "",
    gh: Optional[GhClient] = None,
    prior_state: Optional[JointState] = None,
) -> JointState:
    gh = gh or default_client()
    if not reports:
        raise ValueError("reports required")

    # Non-joint reports: do not create joint issues / do not dispatch
    joint_reports = [r for r in reports if r.get("ci_mode") == "joint"]
    if not joint_reports:
        state = JointState(
            joint_id=joint_id or "none",
            status="skipped_non_joint",
            participants=reports,
        )
        state.failure_reason = "ci_mode!=joint"
        return state

    joint_id = joint_id or f"joint-auto-{joint_reports[0]['repo']}-{joint_reports[0]['pr_number']}"

    # Resume from prior / stored issue if available
    if prior_state is None and hasattr(gh, "get_issue"):
        loaded = gh.get_issue(joint_id)
        if loaded is not None:
            prior_state = loaded

    lock = _lock_for(joint_id)
    with lock:
        return _run_locked(joint_reports, joint_id, gh, prior_state)


def _run_locked(
    joint_reports: List[Dict[str, Any]],
    joint_id: str,
    gh: GhClient,
    prior_state: Optional[JointState],
) -> JointState:
    state = JointState(joint_id=joint_id, status="waiting_deps", participants=joint_reports)
    if prior_state is not None and prior_state.issue_number is not None:
        state.issue_number = prior_state.issue_number

    ambiguous = find_ambiguous_deps(joint_reports, gh)
    if ambiguous:
        state.status = "failed"
        state.failure_reason = "ambiguous_prs"
        state.waiting_for = [ambiguous]
        for r in joint_reports:
            gh.write_check(
                r["repo"],
                r["head_sha"],
                "joint-ci",
                "failure",
                output_summary="ambiguous_prs",
            )
        return gh.upsert_issue(state)

    missing = missing_deps(joint_reports, gh)
    if missing:
        state.waiting_for = missing
        state.status = "waiting_deps"
        for r in joint_reports:
            gh.write_check(r["repo"], r["head_sha"], "joint-ci", "pending")
        return gh.upsert_issue(state)

    if not all_ready(joint_reports):
        state.status = "waiting_deps"
        state.waiting_for = [{"reason": "draft"}]
        for r in joint_reports:
            gh.write_check(r["repo"], r["head_sha"], "joint-ci", "pending")
        return gh.upsert_issue(state)

    versions = {r["repo"]: r["head_sha"] for r in joint_reports}
    merged = merge_remote_tests(joint_reports)
    cfg_hash = hash_merged_tests(merged)
    state.versions = versions
    state.joint_key = make_joint_key(
        versions,
        image=os.environ.get("JOINT_IMAGE", "default"),
        test_config_hash=cfg_hash,
    )

    # joint_key reuse: same key + versions in {running, succeeded} → no re-dispatch
    # Skip reuse when prior was invalidated (caller invalidates on push, then re-runs).
    skip_reuse = prior_state is not None and prior_state.status == "invalidated"
    reusable = None if skip_reuse else _find_reusable(state.joint_key, versions, prior_state, gh)
    if reusable is not None:
        state.workflow_runs = dict(reusable.workflow_runs)
        state.issue_number = reusable.issue_number or state.issue_number
        if reusable.status == "running":
            state.status = "running"
            for r in joint_reports:
                gh.write_check(r["repo"], r["head_sha"], "joint-ci", "pending")
        else:
            state.status = _rewrite_checks_from_reuse(joint_reports, merged, state, gh)
        return gh.upsert_issue(state)

    # ready → running (immediate in P1; ready is transient)
    state.status = "ready"
    gh.upsert_issue(state)

    state.status = "running"
    gh.upsert_issue(state)

    results: Dict[str, str] = {}
    for test in merged:
        run_id = gh.dispatch_test(test, versions)
        key = workflow_key(test)
        state.workflow_runs[key] = run_id
        results[key] = gh.get_run_conclusion(run_id)

    state.status = aggregate_and_write_checks(joint_reports, merged, results, state, gh)
    return gh.upsert_issue(state)


def main() -> None:
    raw = os.environ.get("REPORT_JSON") or "[]"
    data = json.loads(raw)
    if isinstance(data, dict) and "reports" in data:
        reports = data["reports"]
        joint_id = data.get("joint_id") or os.environ.get("JOINT_ID") or ""
    elif isinstance(data, dict):
        reports = [data]
        joint_id = os.environ.get("JOINT_ID") or ""
    else:
        reports = data
        joint_id = os.environ.get("JOINT_ID") or ""

    gh = default_client()
    prior = None
    if joint_id and hasattr(gh, "get_issue"):
        prior = gh.get_issue(joint_id)

    state = run(reports, joint_id=joint_id, gh=gh, prior_state=prior)
    print(canonical_state(state))


def canonical_state(state: JointState) -> str:
    return json.dumps(state.__dict__, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
