"""GitHub client stubs for Joint CI scheduler (dry-run Fake by default)."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
from joint_key import canonical_json  # noqa: E402

if TYPE_CHECKING:
    from scheduler import JointState


def _params_key(params: Optional[Dict[str, Any]]) -> str:
    return canonical_json(params or {})


class GhClient:
    """Thin wrapper; replace with real App auth in production.

    When JOINT_DRY_RUN=1 (default), prefer FakeGhClient via default_client().
    """

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

    def upsert_issue(self, state: "JointState") -> "JointState":
        print(f"[gh] upsert issue for {state.joint_id} status={state.status}")
        if state.issue_number is None:
            state.issue_number = 0 if self.dry_run else None
        return state

    def get_issue(self, joint_id_or_number: Any) -> Optional["JointState"]:
        return None

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
        return "success"

    def add_pr(self, pr: Dict[str, Any]) -> None:
        raise NotImplementedError("use FakeGhClient for in-memory PR registry")

    def seed_pr(
        self,
        repo: str,
        branch: str,
        sha: str,
        draft: bool = False,
        pr_number: Optional[int] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        raise NotImplementedError("use FakeGhClient")


class FakeGhClient(GhClient):
    """In-memory client for dry-run / local experiments (no real GitHub)."""

    def __init__(self) -> None:
        super().__init__()
        self.dry_run = True
        self.prs: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self.issues: Dict[str, Any] = {}
        self.issues_by_number: Dict[int, Any] = {}
        self.checks: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        self.dispatches: List[Dict[str, Any]] = []
        self.run_conclusions: Dict[str, str] = {}
        self.fail_fixtures: Set[Tuple[str, str]] = set()
        self.runner_busy_seconds: float = 0.0
        self._seq = 0
        self.last_state: Optional[Any] = None

    # ----- PR registry -----
    def add_pr(self, pr: Dict[str, Any]) -> None:
        key = (pr["repo"], pr.get("branch") or pr.get("head_branch") or "")
        self.prs.setdefault(key, []).append(pr)

    def seed_pr(
        self,
        repo: str,
        branch: str,
        sha: str,
        draft: bool = False,
        pr_number: Optional[int] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Seed helper for tests (alias-friendly entry for add_pr)."""
        self._seq += 1
        pr = {
            "repo": repo,
            "branch": branch,
            "head_sha": sha,
            "is_draft": draft,
            "pr_number": pr_number if pr_number is not None else self._seq,
            **extra,
        }
        self.add_pr(pr)
        return pr

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
    def upsert_issue(self, state: Any) -> Any:
        if state.issue_number is None:
            self._seq += 1
            state.issue_number = self._seq
        self.issues[state.joint_id] = state
        self.issues_by_number[state.issue_number] = state
        self.last_state = state
        return state

    def get_issue(self, joint_id_or_number: Any) -> Optional[Any]:
        if isinstance(joint_id_or_number, int):
            return self.issues_by_number.get(joint_id_or_number)
        return self.issues.get(joint_id_or_number)

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

    def get_check(self, repo: str, sha: str, name: str = "joint-ci") -> Optional[Dict[str, Any]]:
        return self.checks.get((repo, sha, name))

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
        # Waiting phase never occupies runners; dispatch itself is instantaneous
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

    def public_run_counts(self) -> Dict[str, int]:
        """Alias for public_runs()."""
        return self.public_runs()

    def dispatch_count(self) -> int:
        return len(self.dispatches)

    def check_conclusion(self, repo: str, sha: str, name: str = "joint-ci") -> Optional[str]:
        c = self.checks.get((repo, sha, name))
        return c["conclusion"] if c else None

    def check_details_url(self, repo: str, sha: str, name: str = "joint-ci") -> Optional[str]:
        c = self.checks.get((repo, sha, name))
        return c.get("details_url") if c else None


def default_client() -> GhClient:
    """Return FakeGhClient when JOINT_DRY_RUN=1 (default), else stub GhClient."""
    if os.environ.get("JOINT_DRY_RUN", "1") == "1":
        return FakeGhClient()
    return GhClient()
