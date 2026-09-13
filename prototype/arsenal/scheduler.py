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
from gh_client import GhClient, FakeGhClient, RealGhClient, CheckWriteError, default_client  # noqa: E402

# Re-export for experiments/run_local.py
__all__ = [
    "JointState",
    "run",
    "invalidate",
    "invalidate_on_push",
    "workflow_key",
    "missing_deps",
    "detect_cyclic_deps",
    "GhClient",
    "FakeGhClient",
    "RealGhClient",
    "CheckWriteError",
    "serialize_state",
    "deserialize_state",
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
    check_errors: List[str] = field(default_factory=list)

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
        data.setdefault("check_errors", [])
        return JointState(**data)


def serialize_state(state: JointState) -> Dict[str, Any]:
    return dict(state.__dict__)


def deserialize_state(data: Dict[str, Any]) -> JointState:
    payload = dict(data)
    payload.setdefault("check_errors", [])
    return JointState(**payload)


def missing_deps(reports: List[Dict[str, Any]], gh: GhClient) -> List[Dict[str, str]]:
    missing: List[Dict[str, str]] = []
    for report in reports:
        if not report.get("joint_wait"):
            continue
        deps = report.get("deps") or {}
        mapping = {
            "driver_branch": "driver",
            "sim_branch": "sim",
            "synapse_branch": "synapse",
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


def detect_cyclic_deps(reports: List[Dict[str, Any]]) -> Optional[List[str]]:
    """Detect simple cycles among joint participants via declared deps.

    Returns cycle path like ["driver", "synapse", "driver"] or None.
    """
    mapping = {
        "driver_branch": "driver",
        "sim_branch": "sim",
        "synapse_branch": "synapse",
    }
    graph: Dict[str, List[str]] = {}
    repos_present = {r["repo"] for r in reports if r.get("ci_mode") == "joint"}
    for report in reports:
        if report.get("ci_mode") != "joint":
            continue
        src = report["repo"]
        deps = report.get("deps") or {}
        targets = []
        for key, repo in mapping.items():
            if deps.get(key) and repo in repos_present and repo != src:
                targets.append(repo)
        graph[src] = targets

    def dfs(node: str, stack: List[str], visiting: set) -> Optional[List[str]]:
        if node in visiting:
            # cycle
            if node in stack:
                i = stack.index(node)
                return stack[i:] + [node]
            return stack + [node]
        visiting.add(node)
        stack.append(node)
        for nxt in graph.get(node, []):
            cyc = dfs(nxt, stack, visiting)
            if cyc:
                return cyc
        stack.pop()
        visiting.discard(node)
        return None

    for n in list(graph.keys()):
        cyc = dfs(n, [], set())
        if cyc and len(cyc) > 2:
            return cyc
    return None


def find_ambiguous_deps(reports: List[Dict[str, Any]], gh: GhClient) -> Optional[Dict[str, Any]]:
    """Return ambiguity descriptor if a JOINT_WAIT dep branch has multiple open PRs."""
    for report in reports:
        if report.get("ci_mode") != "joint":
            continue
        deps = report.get("deps") or {}
        mapping = {
            "driver_branch": "driver",
            "sim_branch": "sim",
            "synapse_branch": "synapse",
        }
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
    def _url(run_id: str) -> str:
        rid = str(run_id)
        if rid.isdigit():
            return f"https://github.com/zhekui-hub/joint-ci-lab/actions/runs/{rid}"
        return f"fake://runs/{rid}"

    for key, run_id in state.workflow_runs.items():
        if key.startswith("multirepo_runtest:") or key.startswith("arc_multirepo"):
            return _url(run_id)
    if state.workflow_runs:
        run_id = next(iter(state.workflow_runs.values()))
        return _url(run_id)
    return ""



def _ensure_check_errors(state: JointState) -> List[str]:
    if getattr(state, "check_errors", None) is None:
        state.check_errors = []
    return state.check_errors


def _safe_write_check(
    gh: GhClient,
    state: JointState,
    repo: str,
    sha: str,
    conclusion: str,
    details_url: str = "",
    output_summary: str = "",
    name: str = "joint-ci",
) -> None:
    """write_check that records failures on state.check_errors without raising."""
    try:
        gh.write_check(
            repo,
            sha,
            name,
            conclusion,
            details_url=details_url,
            output_summary=output_summary,
        )
    except Exception as exc:  # noqa: BLE001 — one-sided check failure must not abort
        err = f"{repo}@{sha}: {type(exc).__name__}({exc})"
        _ensure_check_errors(state).append(err)


def _gh_is_invalidated(gh: GhClient, joint_id: str) -> bool:
    if hasattr(gh, "is_invalidated") and gh.is_invalidated(joint_id):
        return True
    flags = getattr(gh, "invalidate_flags", None)
    if isinstance(flags, (dict, set)) and joint_id in flags:
        return True
    loaded = gh.get_issue(joint_id) if hasattr(gh, "get_issue") else None
    return loaded is not None and getattr(loaded, "status", None) == "invalidated"


def _gh_invalidation_reason(gh: GhClient, joint_id: str) -> Optional[str]:
    if hasattr(gh, "invalidation_reason"):
        reason = gh.invalidation_reason(joint_id)
        if reason:
            return reason
    flags = getattr(gh, "invalidate_flags", None)
    if isinstance(flags, dict) and joint_id in flags:
        return flags.get(joint_id)
    loaded = gh.get_issue(joint_id) if hasattr(gh, "get_issue") else None
    if loaded is not None and getattr(loaded, "status", None) == "invalidated":
        return getattr(loaded, "failure_reason", None) or "invalidated"
    return None


def _cancel_runs(gh: GhClient, state: JointState) -> None:
    if not hasattr(gh, "cancel_run"):
        return
    for run_id in list(state.workflow_runs.values()):
        try:
            gh.cancel_run(run_id)
        except Exception:  # noqa: BLE001
            pass


def invalidate(state: JointState, gh: GhClient, reason: str = "push") -> JointState:
    """Mark joint invalidated; works while status is running via shared Fake flag.

    Sets invalidate_flags so an in-flight dispatch loop can abort between tests
    without taking the joint lock (avoids deadlock with the runner thread).
    """
    if hasattr(gh, "set_invalidated"):
        gh.set_invalidated(state.joint_id, reason)
    elif hasattr(gh, "mark_invalidated"):
        gh.mark_invalidated(state.joint_id)

    state.status = "invalidated"
    state.failure_reason = reason
    _cancel_runs(gh, state)
    # Clear joint_key reuse: old key must not be reused after invalidate
    for r in state.participants:
        _safe_write_check(
            gh,
            state,
            r["repo"],
            r["head_sha"],
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
    If any conclusion is still pending, returns 'running'.
    One-sided write_check failures are recorded on state.check_errors and do not
    abort aggregation / issue upsert.
    """
    details = _public_details_url(state)
    public_failed = False
    any_pending = False
    for t in merged:
        key = workflow_key(t)
        conc = results.get(key)
        if conc == "pending":
            any_pending = True
        if conc == "failure" and not t.get("exclusive_to"):
            public_failed = True

    if any_pending:
        for r in reports:
            _safe_write_check(
                gh,
                state,
                r["repo"],
                r["head_sha"],
                "pending",
                details_url=details,
                output_summary="running",
            )
        return "running"

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
        _safe_write_check(
            gh,
            state,
            r["repo"],
            r["head_sha"],
            conclusion,
            details_url=details,
            output_summary=summary,
        )
    # Cross-repo check write failures are visible and block joint success
    if getattr(state, "check_errors", None):
        state.failure_reason = "check_write_failed"
        return "failed"
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
            and getattr(st, "workflow_runs", None) is not None
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
        # Re-load under lock so a concurrent waiter sees the first run's
        # workflow_runs and reuses (no double dispatch).
        if hasattr(gh, "get_issue"):
            loaded = gh.get_issue(joint_id)
            if loaded is not None:
                prior_state = loaded
        return _run_locked(joint_reports, joint_id, gh, prior_state)



def _abort_invalidated(
    state: JointState,
    joint_reports: List[Dict[str, Any]],
    gh: GhClient,
    joint_id: str,
) -> JointState:
    reason = (
        _gh_invalidation_reason(gh, joint_id)
        or state.failure_reason
        or "invalidated"
    )
    state.status = "invalidated"
    state.failure_reason = reason
    _cancel_runs(gh, state)
    for r in joint_reports:
        _safe_write_check(
            gh,
            state,
            r["repo"],
            r["head_sha"],
            "pending",
            output_summary=f"invalidated:{reason}",
        )
    return gh.upsert_issue(state)


def _run_locked(
    joint_reports: List[Dict[str, Any]],
    joint_id: str,
    gh: GhClient,
    prior_state: Optional[JointState],
) -> JointState:
    state = JointState(joint_id=joint_id, status="waiting_deps", participants=joint_reports)
    if prior_state is not None and prior_state.issue_number is not None:
        state.issue_number = prior_state.issue_number

    cycle = detect_cyclic_deps(joint_reports)
    if cycle:
        state.status = "failed"
        state.failure_reason = "cyclic_deps"
        state.waiting_for = [{"cycle": "->".join(cycle)}]
        for r in joint_reports:
            _safe_write_check(
                gh,
                state,
                r["repo"],
                r["head_sha"],
                "failure",
                output_summary="cyclic_deps",
            )
        return gh.upsert_issue(state)

    ambiguous = find_ambiguous_deps(joint_reports, gh)
    if ambiguous:
        state.status = "failed"
        state.failure_reason = "ambiguous_prs"
        state.waiting_for = [ambiguous]
        for r in joint_reports:
            _safe_write_check(
                gh,
                state,
                r["repo"],
                r["head_sha"],
                "failure",
                output_summary="ambiguous_prs",
            )
        return gh.upsert_issue(state)

    missing = missing_deps(joint_reports, gh)
    if missing:
        state.waiting_for = missing
        state.status = "waiting_deps"
        for r in joint_reports:
            _safe_write_check(gh, state, r["repo"], r["head_sha"], "pending")
        return gh.upsert_issue(state)

    if not all_ready(joint_reports):
        state.status = "waiting_deps"
        state.waiting_for = [{"reason": "draft"}]
        for r in joint_reports:
            _safe_write_check(gh, state, r["repo"], r["head_sha"], "pending")
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
        state.workflow_runs = dict(reusable.workflow_runs or {})
        state.issue_number = reusable.issue_number or state.issue_number
        if getattr(reusable, "check_errors", None):
            state.check_errors = list(reusable.check_errors)
        needed_keys = {workflow_key(t) for t in merged}
        incomplete = needed_keys - set(state.workflow_runs.keys())
        if reusable.status == "running" and incomplete:
            # Crash recovery: resume remaining dispatches (fall through)
            state.status = "running"
            prior_state = reusable
        elif reusable.status == "running":
            # If deferred conclusions are now terminal, finalize instead of staying running
            pending_left = False
            for run_id in state.workflow_runs.values():
                if gh.get_run_conclusion(run_id) == "pending":
                    pending_left = True
                    break
            if not pending_left and state.workflow_runs:
                state.status = _rewrite_checks_from_reuse(joint_reports, merged, state, gh)
                return gh.upsert_issue(state)
            state.status = "running"
            for r in joint_reports:
                _safe_write_check(gh, state, r["repo"], r["head_sha"], "pending")
            return gh.upsert_issue(state)
        else:
            state.status = _rewrite_checks_from_reuse(joint_reports, merged, state, gh)
            return gh.upsert_issue(state)

    # Fresh dispatch: drop stale mid-run invalidate flag so a deliberate re-run proceeds
    # (But keep flag if we are aborting — only clear when starting a new dispatch cycle.)
    if state.status != "running" and hasattr(gh, "clear_invalidated"):
        gh.clear_invalidated(joint_id)

    # ready → running (immediate in P1; ready is transient)
    if state.status != "running":
        state.status = "ready"
        gh.upsert_issue(state)
        state.status = "running"
        gh.upsert_issue(state)

    results: Dict[str, str] = {}
    for test in merged:
        if _gh_is_invalidated(gh, joint_id):
            return _abort_invalidated(state, joint_reports, gh, joint_id)
        key = workflow_key(test)
        if key in state.workflow_runs:
            run_id = state.workflow_runs[key]
            results[key] = gh.get_run_conclusion(run_id)
            continue
        # Only resume prior runs for crash-recovery on the SAME joint_key.
        # After invalidate / new joint_key, never reuse old workflow_runs.
        if (
            not skip_reuse
            and prior_state is not None
            and prior_state.status != "invalidated"
            and prior_state.workflow_runs
            and key in prior_state.workflow_runs
        ):
            run_id = prior_state.workflow_runs[key]
            state.workflow_runs[key] = run_id
            results[key] = gh.get_run_conclusion(run_id)
            continue
        run_id = gh.dispatch_test(test, versions)
        state.workflow_runs[key] = run_id
        results[key] = gh.get_run_conclusion(run_id)
        # Abort before upserting a still-running snapshot so a mid-run
        # invalidate flag is not clobbered by this wave.
        if _gh_is_invalidated(gh, joint_id):
            return _abort_invalidated(state, joint_reports, gh, joint_id)
        # Keep issue visible so concurrent invalidate can observe workflow_runs
        gh.upsert_issue(state)

    if _gh_is_invalidated(gh, joint_id):
        return _abort_invalidated(state, joint_reports, gh, joint_id)

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
