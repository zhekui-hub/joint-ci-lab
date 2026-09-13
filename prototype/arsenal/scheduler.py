#!/usr/bin/env python3
"""Arsenal joint CI scheduler (prototype).

Responsibilities:
- parse reports
- load/create joint issue state
- wait without holding test pods (exit when deps missing)
- merge tests, compute joint_key, dispatch once
- map results back (dispatch hooks left as TODOs wired to existing workflows)

This file is meant to be moved to Arsenal under joint_ci/scheduler.py.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# When vendored into Arsenal, import from joint_ci.shared
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
from joint_key import make_joint_key, merge_remote_tests, per_pr_required, canonical_json  # noqa: E402


TERMINAL = {"succeeded", "failed", "cancelled"}


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


def workflow_key(test: Dict[str, Any]) -> str:
    return test["id"] + ":" + canonical_json(test.get("params") or {})


class GhClient:
    """Thin wrapper; replace with real PyGithub / ghapi + App auth in production."""

    def __init__(self) -> None:
        self.dry_run = os.environ.get("JOINT_DRY_RUN", "1") == "1"

    def find_open_prs(self, repo: str, branch: str) -> List[Dict[str, Any]]:
        print(f"[gh] find_open_prs {repo} {branch}")
        return []

    def find_open_pr(self, repo: str, branch: str) -> Optional[Dict[str, Any]]:
        prs = self.find_open_prs(repo, branch)
        if len(prs) > 1:
            return {"__ambiguous__": True, "count": len(prs), "repo": repo, "branch": branch}
        return prs[0] if prs else None

    def upsert_issue(self, state: JointState) -> JointState:
        print(f"[gh] upsert issue for {state.joint_id} status={state.status}")
        if state.issue_number is None:
            state.issue_number = 0 if self.dry_run else None
        return state

    def write_check(
        self,
        repo: str,
        sha: str,
        name: str,
        conclusion: str,
        details_url: str = "",
        output_summary: str = "",
    ) -> None:
        print(f"[gh] check {repo}@{sha[:7]} {name} -> {conclusion} {details_url}")

    def dispatch_test(self, test: Dict[str, Any], versions: Dict[str, str]) -> str:
        run_id = f"dry-{test['id']}-{versions.get('driver', '')[:6]}"
        print(f"[gh] dispatch {test['id']} params={test.get('params')} versions={versions} -> {run_id}")
        return run_id

    def get_run_conclusion(self, run_id: str) -> str:
        """Prototype: dry-run always succeeds. FakeGhClient overrides."""
        return "success"


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
            # Also count reports targeting that branch
            report_hits = [
                r for r in reports if r.get("repo") == repo and r.get("branch") == branch
            ]
            # Prefer live PR registry; fall back to duplicate reports
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
    for r in state.participants:
        gh.write_check(
            r["repo"],
            r["head_sha"],
            "joint-ci",
            "pending",
            output_summary=f"invalidated:{reason}",
        )
    return gh.upsert_issue(state)


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


def run(
    reports: List[Dict[str, Any]],
    joint_id: str = "",
    gh: Optional[GhClient] = None,
    prior_state: Optional[JointState] = None,
) -> JointState:
    gh = gh or GhClient()
    if not reports:
        raise ValueError("reports required")

    # Non-joint reports: do not create joint issues / do not dispatch via joint scheduler
    joint_reports = [r for r in reports if r.get("ci_mode") == "joint"]
    if not joint_reports:
        state = JointState(joint_id=joint_id or "none", status="skipped_non_joint", participants=reports)
        state.failure_reason = "ci_mode!=joint"
        # Do not upsert a real joint issue for baseline
        return state

    joint_id = joint_id or f"joint-auto-{joint_reports[0]['repo']}-{joint_reports[0]['pr_number']}"
    state = JointState(joint_id=joint_id, status="waiting_deps", participants=joint_reports)

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
    state.versions = versions
    state.joint_key = make_joint_key(
        versions,
        image=os.environ.get("JOINT_IMAGE", "default"),
        test_config_hash=canonical_json(
            sorted(
                [
                    {
                        "id": t["id"],
                        "params": t.get("params") or {},
                        "env": t.get("env") or {},
                    }
                    for t in merged
                ],
                key=lambda x: json.dumps(x, sort_keys=True),
            )
        ),
    )

    # If prior succeeded/failed with different versions → caller should invalidate first.
    # Still safe to proceed with a new key.
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
    state = run(reports, joint_id=joint_id)
    print(canonical_state(state))


def canonical_state(state: JointState) -> str:
    return json.dumps(state.__dict__, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
