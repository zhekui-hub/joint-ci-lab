"""In-memory FakeGhClient for local Joint CI experiments (no real GitHub)."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Set, Tuple

# Allow importing scheduler.GhClient interface without circular issues
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "arsenal"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
from scheduler import GhClient, JointState  # noqa: E402
from joint_key import canonical_json  # noqa: E402


def _params_key(params: Optional[Dict[str, Any]]) -> str:
    return canonical_json(params or {})


class FakeGhClient(GhClient):
    """Minimal fake backend implementing GhClient methods + assertion counters."""

    def __init__(self) -> None:
        super().__init__()
        self.dry_run = True
        self.prs: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self.issues: Dict[str, JointState] = {}
        self.checks: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        self.dispatches: List[Dict[str, Any]] = []
        self.run_conclusions: Dict[str, str] = {}
        self.fail_fixtures: Set[Tuple[str, str]] = set()  # (test_id, params_json)
        self.runner_busy_seconds: float = 0.0
        self._seq = 0
        self.last_state: Optional[JointState] = None

    # ----- PR registry -----
    def add_pr(self, pr: Dict[str, Any]) -> None:
        key = (pr["repo"], pr.get("branch") or pr.get("head_branch") or "")
        self.prs.setdefault(key, []).append(pr)

    def update_pr(self, repo: str, pr_number: int, **fields: Any) -> None:
        for lst in self.prs.values():
            for p in lst:
                if p.get("repo") == repo and p.get("pr_number") == pr_number:
                    p.update(fields)

    def close_pr(self, repo: str, pr_number: int) -> None:
        for key in list(self.prs.keys()):
            self.prs[key] = [
                p
                for p in self.prs[key]
                if not (p.get("repo") == repo and p.get("pr_number") == pr_number)
            ]
            if not self.prs[key]:
                del self.prs[key]

    def find_open_prs(self, repo: str, branch: str) -> List[Dict[str, Any]]:
        return list(self.prs.get((repo, branch), []))

    def find_open_pr(self, repo: str, branch: str) -> Optional[Dict[str, Any]]:
        prs = self.find_open_prs(repo, branch)
        if len(prs) > 1:
            return {"__ambiguous__": True, "count": len(prs), "repo": repo, "branch": branch}
        return prs[0] if prs else None

    # ----- Issue / checks / dispatch -----
    def upsert_issue(self, state: JointState) -> JointState:
        if state.issue_number is None:
            self._seq += 1
            state.issue_number = self._seq
        self.issues[state.joint_id] = state
        self.last_state = state
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
        self.checks[(repo, sha, name)] = {
            "conclusion": conclusion,
            "details_url": details_url,
            "output_summary": output_summary,
        }

    def inject_failure(self, test_id: str, params: Optional[Dict[str, Any]] = None) -> None:
        self.fail_fixtures.add((test_id, _params_key(params)))

    def clear_failures(self) -> None:
        self.fail_fixtures.clear()

    def dispatch_test(self, test: Dict[str, Any], versions: Dict[str, str]) -> str:
        self._seq += 1
        run_id = f"fake-{test['id']}-{self._seq}"
        params = test.get("params") or {}
        fixture = (test["id"], _params_key(params))
        conclusion = "failure" if fixture in self.fail_fixtures else "success"
        self.run_conclusions[run_id] = conclusion
        self.dispatches.append(
            {
                "run_id": run_id,
                "test_id": test["id"],
                "params": params,
                "versions": dict(versions),
                "exclusive_to": test.get("exclusive_to"),
                "conclusion": conclusion,
            }
        )
        # Local fake never occupies runners during waiting; busy only while "running" tests
        # (for assertions we keep wait-phase busy at 0; dispatch itself is instantaneous)
        return run_id

    def get_run_conclusion(self, run_id: str) -> str:
        return self.run_conclusions.get(run_id, "success")

    # ----- Metrics helpers for asserts -----
    def public_runs(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for d in self.dispatches:
            if d.get("exclusive_to"):
                continue
            counts[d["test_id"]] = counts.get(d["test_id"], 0) + 1
        return counts

    def dispatch_count(self) -> int:
        return len(self.dispatches)

    def check_conclusion(self, repo: str, sha: str, name: str = "joint-ci") -> Optional[str]:
        c = self.checks.get((repo, sha, name))
        return c["conclusion"] if c else None

    def check_details_url(self, repo: str, sha: str, name: str = "joint-ci") -> Optional[str]:
        c = self.checks.get((repo, sha, name))
        return c.get("details_url") if c else None
