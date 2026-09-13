"""GitHub clients for Joint CI scheduler.

- FakeGhClient: in-memory dry-run (JOINT_DRY_RUN=1, default via default_client)
- RealGhClient: live `gh api` against zhekui-hub/joint-ci-* (JOINT_DRY_RUN=0)

NOTE: Keep FakeGhClient API stable for unit/experiment tests.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
from joint_key import canonical_json  # noqa: E402

if TYPE_CHECKING:
    from scheduler import JointState


def _params_key(params: Optional[Dict[str, Any]]) -> str:
    return canonical_json(params or {})


class CheckWriteError(RuntimeError):
    """Raised by FakeGhClient when check-write failure is injected."""


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

    def is_invalidated(self, joint_id: str) -> bool:
        return False


class FakeGhClient(GhClient):
    """In-memory client for dry-run / local experiments (no real GitHub)."""

    def __init__(self) -> None:
        super().__init__()
        self.dry_run = True
        self._lock = threading.RLock()
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
        # Robustness hooks
        self._fail_next_write_checks = 0
        self.fail_checks_for: Set[str] = set()
        self.check_write_fail_repos: Set[str] = set()
        self.write_check_errors: List[Dict[str, Any]] = []
        self.check_write_failures: List[Dict[str, Any]] = []
        self.check_write_attempts: List[Dict[str, Any]] = []
        self._defer_conclusions = False
        self.dispatch_sleep_seconds: float = 0.0
        self.dispatch_crash_after: Optional[int] = None
        self._crash_armed = False
        self._invalidated_ids: Set[str] = set()
        self._invalidate_reasons: Dict[str, str] = {}
        self.cancelled_runs: List[str] = []

    def add_pr(self, pr: Dict[str, Any]) -> None:
        with self._lock:
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
        with self._lock:
            self._seq += 1
            n = pr_number if pr_number is not None else self._seq
        pr = {
            "repo": repo,
            "branch": branch,
            "head_sha": sha,
            "is_draft": draft,
            "pr_number": n,
            **extra,
        }
        self.add_pr(pr)
        return pr

    def update_pr(self, repo: str, pr_number: int, **fields: Any) -> None:
        with self._lock:
            for lst in self.prs.values():
                for p in lst:
                    if p.get("repo") == repo and p.get("pr_number") == pr_number:
                        p.update(fields)

    def close_pr(self, repo: str, pr_number: int) -> None:
        with self._lock:
            for key in list(self.prs.keys()):
                self.prs[key] = [
                    p
                    for p in self.prs[key]
                    if not (p.get("repo") == repo and p.get("pr_number") == pr_number)
                ]
                if not self.prs[key]:
                    del self.prs[key]

    def find_open_prs(self, repo: str, branch: str) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.prs.get((repo, branch), []))

    def find_open_pr(self, repo: str, branch: str) -> Optional[Dict[str, Any]]:
        prs = self.find_open_prs(repo, branch)
        if len(prs) > 1:
            return {"__ambiguous__": True, "count": len(prs), "repo": repo, "branch": branch}
        return prs[0] if prs else None

    def force_push_branch(self, repo: str, branch: str, new_sha: str) -> int:
        with self._lock:
            updated = 0
            for p in self.prs.get((repo, branch), []):
                p["head_sha"] = new_sha
                updated += 1
            return updated

    def upsert_issue(self, state: Any) -> Any:
        with self._lock:
            if state.issue_number is None:
                self._seq += 1
                state.issue_number = self._seq
            self.issues[state.joint_id] = state
            self.issues_by_number[state.issue_number] = state
            self.last_state = state
            if getattr(state, "status", None) == "invalidated":
                self._invalidated_ids.add(state.joint_id)
            else:
                self._invalidated_ids.discard(state.joint_id)
                self._invalidate_reasons.pop(state.joint_id, None)
            return state

    def get_issue(self, joint_id_or_number: Any) -> Optional[Any]:
        with self._lock:
            if isinstance(joint_id_or_number, int):
                return self.issues_by_number.get(joint_id_or_number)
            return self.issues.get(joint_id_or_number)

    def is_invalidated(self, joint_id: str) -> bool:
        with self._lock:
            if joint_id in self._invalidated_ids:
                return True
            st = self.issues.get(joint_id)
            return bool(st is not None and getattr(st, "status", None) == "invalidated")

    def invalidation_reason(self, joint_id: str) -> Optional[str]:
        with self._lock:
            if joint_id in self._invalidate_reasons:
                return self._invalidate_reasons[joint_id]
            st = self.issues.get(joint_id)
            if st is not None and getattr(st, "status", None) == "invalidated":
                return getattr(st, "failure_reason", None)
            return None

    def set_invalidated(self, joint_id: str, reason: str = "push") -> None:
        with self._lock:
            self._invalidated_ids.add(joint_id)
            self._invalidate_reasons[joint_id] = reason or "push"

    def mark_invalidated(self, joint_id: str) -> None:
        self.set_invalidated(joint_id, "push")

    def clear_invalidated(self, joint_id: str) -> None:
        with self._lock:
            self._invalidated_ids.discard(joint_id)
            self._invalidate_reasons.pop(joint_id, None)

    @property
    def invalidate_flags(self) -> Set[str]:
        """Compatibility view for scheduler; prefer is_invalidated / set_invalidated."""
        return self._invalidated_ids

    @property
    def fail_write_check_for(self) -> Set[str]:
        return self.fail_checks_for

    def cancel_run(self, run_id: str) -> None:
        with self._lock:
            if run_id not in self.cancelled_runs:
                self.cancelled_runs.append(run_id)
            self.run_conclusions[run_id] = "cancelled"

    def inject_check_write_fail(self, count: int = 1) -> None:
        with self._lock:
            self._fail_next_write_checks = max(0, int(count))

    def inject_check_write_failure(self, repo: str) -> None:
        with self._lock:
            self.fail_checks_for.add(repo)
            self.check_write_fail_repos.add(repo)

    def clear_check_write_failures(self) -> None:
        with self._lock:
            self.fail_checks_for.clear()
            self.check_write_fail_repos.clear()
            self._fail_next_write_checks = 0

    def set_defer_conclusions(self, defer: bool = True) -> None:
        with self._lock:
            self._defer_conclusions = bool(defer)

    def arm_dispatch_crash(self, after: int = 1) -> None:
        with self._lock:
            self.dispatch_crash_after = after
            self._crash_armed = True

    def write_check(
        self,
        repo: str,
        sha: str,
        name: str,
        conclusion: str,
        details_url: str = "",
        output_summary: str = "",
    ) -> None:
        with self._lock:
            attempt = {
                "repo": repo,
                "sha": sha,
                "name": name,
                "conclusion": conclusion,
                "details_url": details_url,
                "output_summary": output_summary,
            }
            self.check_write_attempts.append(attempt)
            fail = False
            if self._fail_next_write_checks > 0:
                self._fail_next_write_checks -= 1
                fail = True
            if repo in self.fail_checks_for or repo in self.check_write_fail_repos:
                fail = True
            if fail:
                self.write_check_errors.append(attempt)
                self.check_write_failures.append(attempt)
                raise CheckWriteError(f"injected write_check failure for {repo}@{sha}")
            self.checks[(repo, sha, name)] = {
                "conclusion": conclusion,
                "details_url": details_url,
                "output_summary": output_summary,
            }

    def get_check(self, repo: str, sha: str, name: str = "joint-ci") -> Optional[Dict[str, Any]]:
        with self._lock:
            return self.checks.get((repo, sha, name))

    def inject_failure(self, test_id: str, params: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self.fail_fixtures.add((test_id, _params_key(params)))

    def clear_failures(self) -> None:
        with self._lock:
            self.fail_fixtures.clear()

    def dispatch_test(self, test: Dict[str, Any], versions: Dict[str, str]) -> str:
        sleep_s = float(getattr(self, "dispatch_sleep_seconds", 0) or 0)
        if sleep_s > 0:
            time.sleep(sleep_s)
        with self._lock:
            if self._crash_armed and self.dispatch_crash_after is not None:
                if len(self.dispatches) + 1 >= self.dispatch_crash_after:
                    self._crash_armed = False
                    self.dispatch_crash_after = None
                    raise RuntimeError("simulated_scheduler_crash")
            self._seq += 1
            run_id = f"fake-{test['id']}-{self._seq}"
            params = test.get("params") or {}
            fixture = (test["id"], _params_key(params))
            if self._defer_conclusions:
                conclusion = "pending"
            else:
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
        return run_id

    def get_run_conclusion(self, run_id: str) -> str:
        with self._lock:
            return self.run_conclusions.get(run_id, "success")

    def set_run_conclusion(self, run_id: str, conclusion: str) -> None:
        with self._lock:
            self.run_conclusions[run_id] = conclusion

    def persist(self) -> Dict[str, Any]:
        with self._lock:
            checks = {
                f"{r}|{s}|{n}": copy.deepcopy(v) for (r, s, n), v in self.checks.items()
            }
            prs = {
                f"{repo}|{branch}": copy.deepcopy(lst) for (repo, branch), lst in self.prs.items()
            }
            issues = {}
            for jid, st in self.issues.items():
                issues[jid] = copy.deepcopy(st.__dict__) if hasattr(st, "__dict__") else st
            return {
                "prs": prs,
                "issues": issues,
                "checks": checks,
                "dispatches": copy.deepcopy(self.dispatches),
                "run_conclusions": copy.deepcopy(self.run_conclusions),
                "fail_fixtures": list(self.fail_fixtures),
                "runner_busy_seconds": self.runner_busy_seconds,
                "seq": self._seq,
                "write_check_errors": copy.deepcopy(self.write_check_errors),
                "invalidated_ids": list(self._invalidated_ids),
            }

    def restore(self, blob: Dict[str, Any], joint_state_cls: Any = None) -> None:
        with self._lock:
            self.prs = {}
            for k, lst in (blob.get("prs") or {}).items():
                repo, branch = k.split("|", 1)
                self.prs[(repo, branch)] = copy.deepcopy(lst)
            self.checks = {}
            for k, v in (blob.get("checks") or {}).items():
                repo, sha, name = k.split("|", 2)
                self.checks[(repo, sha, name)] = copy.deepcopy(v)
            self.dispatches = copy.deepcopy(blob.get("dispatches") or [])
            self.run_conclusions = copy.deepcopy(blob.get("run_conclusions") or {})
            self.fail_fixtures = set(tuple(x) for x in (blob.get("fail_fixtures") or []))
            self.runner_busy_seconds = float(blob.get("runner_busy_seconds") or 0.0)
            self._seq = int(blob.get("seq") or 0)
            self.write_check_errors = copy.deepcopy(blob.get("write_check_errors") or [])
            self.check_write_failures = list(self.write_check_errors)
            self._invalidated_ids = set(blob.get("invalidated_ids") or [])
            self.issues = {}
            self.issues_by_number = {}
            for jid, data in (blob.get("issues") or {}).items():
                if joint_state_cls is not None and isinstance(data, dict):
                    st = joint_state_cls(**data)
                else:
                    st = data
                self.issues[jid] = st
                num = getattr(st, "issue_number", None)
                if num is not None:
                    self.issues_by_number[num] = st
            self.last_state = None
            if self.issues:
                self.last_state = next(iter(self.issues.values()))

    def public_runs(self) -> Dict[str, int]:
        with self._lock:
            counts: Dict[str, int] = {}
            for d in self.dispatches:
                if d.get("exclusive_to"):
                    continue
                counts[d["test_id"]] = counts.get(d["test_id"], 0) + 1
            return counts

    def public_run_counts(self) -> Dict[str, int]:
        return self.public_runs()

    def dispatch_count(self) -> int:
        with self._lock:
            return len(self.dispatches)

    def check_conclusion(self, repo: str, sha: str, name: str = "joint-ci") -> Optional[str]:
        with self._lock:
            c = self.checks.get((repo, sha, name))
            return c["conclusion"] if c else None

    def check_details_url(self, repo: str, sha: str, name: str = "joint-ci") -> Optional[str]:
        with self._lock:
            c = self.checks.get((repo, sha, name))
            return c.get("details_url") if c else None


class RealGhError(RuntimeError):
    """Raised when a live `gh api` / `gh` call fails."""


class RealGhClient(GhClient):
    """Live GitHub client via `gh` CLI (JOINT_GH_TOKEN / GH_TOKEN).

    F16: owner fixed to zhekui-hub; only joint-ci-* repositories.
    """

    OWNER = "zhekui-hub"
    ROLE_REPOS = {
        "lab": "joint-ci-lab",
        "driver": "joint-ci-driver",
        "synapse": "joint-ci-synapse",
        "sim": "joint-ci-sim",
    }
    ALLOWED_REPOS = frozenset(ROLE_REPOS.values())
    LABEL = "joint-ci"
    PROBE_WORKFLOW = "joint_real_probe.yml"

    def __init__(self) -> None:
        super().__init__()
        self.dry_run = False
        self._lock = threading.RLock()
        self._invalidated_ids: Set[str] = set()
        self._invalidate_reasons: Dict[str, str] = {}
        self.cancelled_runs: List[str] = []
        # Ensure gh sees a token (Actions often sets GITHUB_TOKEN).
        token = (
            os.environ.get("JOINT_GH_TOKEN")
            or os.environ.get("GH_TOKEN")
            or os.environ.get("GITHUB_TOKEN")
        )
        if token and not os.environ.get("GH_TOKEN"):
            os.environ["GH_TOKEN"] = token

    # --- F16 / repo helpers -------------------------------------------------

    def _short_repo(self, repo: str) -> str:
        """Map role or full name to short joint-ci-* repo name."""
        raw = (repo or "").strip()
        if "/" in raw:
            owner, short = raw.split("/", 1)
            if owner != self.OWNER:
                raise RealGhError(f"F16 refuse: owner {owner!r} (want {self.OWNER})")
            raw = short
        # role alias → joint-ci-*
        if raw in self.ROLE_REPOS:
            raw = self.ROLE_REPOS[raw]
        if raw not in self.ALLOWED_REPOS or not raw.startswith("joint-ci-"):
            raise RealGhError(f"F16 refuse: repo outside {self.OWNER}/joint-ci-* ({repo!r})")
        return raw

    def _full(self, repo: str) -> str:
        return f"{self.OWNER}/{self._short_repo(repo)}"

    def _lab(self) -> str:
        return self._full("lab")

    def _gh_env(self) -> Dict[str, str]:
        env = os.environ.copy()
        token = (
            env.get("JOINT_GH_TOKEN")
            or env.get("GH_TOKEN")
            or env.get("GITHUB_TOKEN")
        )
        if token:
            env["GH_TOKEN"] = token
        # Never leak token into child locale surprises
        env.setdefault("LC_ALL", "C.UTF-8")
        env.setdefault("LANG", "C.UTF-8")
        return env

    def _run_gh(
        self,
        args: List[str],
        *,
        check: bool = True,
        input_text: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        try:
            proc = subprocess.run(
                args,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=self._gh_env(),
                input=input_text,
            )
        except FileNotFoundError as exc:
            raise RealGhError("gh CLI is not installed or not on PATH") from exc
        if check and proc.returncode:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            msg = detail[-1] if detail else f"exit {proc.returncode}"
            raise RealGhError(f"gh {' '.join(args[1:4])} failed: {msg}")
        return proc

    def gh_api(
        self,
        path: str,
        *extra: str,
        method: Optional[str] = None,
        jq: Optional[str] = None,
        input_text: Optional[str] = None,
    ) -> Any:
        """Call `gh api` with UTF-8; refuse non-repo paths (except user)."""
        if not path.startswith("repos/") and path not in {"user"}:
            raise RealGhError(f"refusing non-repository API path: {path}")
        if path.startswith("repos/"):
            # F16: only zhekui-hub/joint-ci-*
            parts = path.split("/")
            if len(parts) >= 3:
                target = f"{parts[1]}/{parts[2]}"
                owner, short = parts[1], parts[2]
                if owner != self.OWNER or short not in self.ALLOWED_REPOS:
                    raise RealGhError(f"F16 refuse: {target}")
        cmd = ["gh", "api"]
        if method:
            cmd += ["-X", method]
        cmd.append(path)
        cmd += list(extra)
        if jq:
            cmd += ["--jq", jq]
        if input_text is not None:
            cmd += ["--input", "-"]
        proc = self._run_gh(cmd, check=True, input_text=input_text)
        raw = (proc.stdout or "").strip()
        if jq:
            return raw
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def _gh_api_raw(
        self,
        path: str,
        *extra: str,
        method: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        """Like gh_api but returns the CompletedProcess (no raise on non-zero)."""
        if path.startswith("repos/"):
            parts = path.split("/")
            if len(parts) >= 3:
                owner, short = parts[1], parts[2]
                if owner != self.OWNER or short not in self.ALLOWED_REPOS:
                    raise RealGhError(f"F16 refuse: {owner}/{short}")
        cmd = ["gh", "api"]
        if method:
            cmd += ["-X", method]
        cmd.append(path)
        cmd += list(extra)
        return self._run_gh(cmd, check=False)

    # --- PR lookup ----------------------------------------------------------

    def find_open_prs(self, repo: str, branch: str) -> List[Dict[str, Any]]:
        full = self._full(repo)
        short = self._short_repo(repo)
        # head filter: owner:branch
        data = self.gh_api(
            f"repos/{full}/pulls?state=open&head={self.OWNER}:{branch}&per_page=100",
        )
        items = data if isinstance(data, list) else []
        # Scheduler reports use role names ("driver"); map short → role
        rev = {v: k for k, v in self.ROLE_REPOS.items()}
        role = repo if repo in self.ROLE_REPOS else rev.get(short, short)
        out: List[Dict[str, Any]] = []
        for pr in items:
            head = pr.get("head") or {}
            out.append(
                {
                    "repo": role,
                    "branch": head.get("ref") or branch,
                    "head_sha": head.get("sha") or "",
                    "is_draft": bool(pr.get("draft")),
                    "pr_number": pr.get("number"),
                    "html_url": pr.get("html_url"),
                    "title": pr.get("title"),
                }
            )
        return out

    # --- Issues (lab) -------------------------------------------------------

    def _ensure_label(self) -> None:
        lab = self._lab()
        proc = self._gh_api_raw(f"repos/{lab}/labels/{self.LABEL}")
        if proc.returncode == 0:
            return
        self._gh_api_raw(
            f"repos/{lab}/labels",
            "-f",
            f"name={self.LABEL}",
            "-f",
            "color=0E8A16",
            "-f",
            "description=Joint CI state",
            method="POST",
        )

    def _state_body(self, state: Any) -> str:
        if hasattr(state, "to_issue_body"):
            return state.to_issue_body()
        payload = dict(getattr(state, "__dict__", {}) or {})
        return (
            "<!-- joint-ci-state\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
            + "\n-->\n\n"
            + f"## Joint `{payload.get('joint_id', '')}`\n"
        )

    def _parse_state(self, body: str) -> Optional[Any]:
        from scheduler import JointState  # lazy: avoid import cycle at module load

        return JointState.from_issue_body(body or "")

    def upsert_issue(self, state: Any) -> Any:
        lab = self._lab()
        self._ensure_label()
        title = f"joint-ci {state.joint_id}"
        body = self._state_body(state)
        if state.issue_number:
            payload = json.dumps({"title": title, "body": body}, ensure_ascii=False)
            self.gh_api(
                f"repos/{lab}/issues/{state.issue_number}",
                method="PATCH",
                input_text=payload,
            )
        else:
            payload = json.dumps(
                {"title": title, "body": body, "labels": [self.LABEL]},
                ensure_ascii=False,
            )
            try:
                issue = self.gh_api(
                    f"repos/{lab}/issues",
                    method="POST",
                    input_text=payload,
                )
            except RealGhError:
                self._ensure_label()
                issue = self.gh_api(
                    f"repos/{lab}/issues",
                    method="POST",
                    input_text=payload,
                )
            state.issue_number = (issue or {}).get("number")
        if getattr(state, "status", None) == "invalidated":
            self.set_invalidated(state.joint_id, getattr(state, "failure_reason", None) or "invalidated")
        return state

    def get_issue(self, joint_id_or_number: Any) -> Optional[Any]:
        lab = self._lab()
        if isinstance(joint_id_or_number, int) or (
            isinstance(joint_id_or_number, str) and joint_id_or_number.isdigit()
        ):
            num = int(joint_id_or_number)
            issue = self.gh_api(f"repos/{lab}/issues/{num}")
            if not issue:
                return None
            st = self._parse_state(issue.get("body") or "")
            if st is not None:
                st.issue_number = issue.get("number") or num
            return st

        joint_id = str(joint_id_or_number)
        # Search open + closed labeled issues (paginate lightly)
        data = self.gh_api(
            f"repos/{lab}/issues?labels={self.LABEL}&state=all&per_page=100",
        )
        items = data if isinstance(data, list) else []
        for issue in items:
            if issue.get("pull_request"):
                continue
            title = issue.get("title") or ""
            body = issue.get("body") or ""
            if joint_id in title or joint_id in body:
                st = self._parse_state(body)
                if st is not None and st.joint_id == joint_id:
                    st.issue_number = issue.get("number")
                    return st
                if joint_id in title:
                    st = self._parse_state(body)
                    if st is not None:
                        st.issue_number = issue.get("number")
                        return st
        return None

    # --- Checks / statuses --------------------------------------------------

    @staticmethod
    def _status_state(conclusion: str) -> str:
        c = (conclusion or "").lower()
        if c in ("success", "successful"):
            return "success"
        if c in ("pending", "in_progress", "queued", "neutral"):
            return "pending"
        if c in ("failure", "failed", "cancelled", "canceled", "timed_out", "action_required", "error"):
            return "failure" if c in ("failure", "failed", "timed_out", "action_required") else "error"
        return "failure"

    def write_check(
        self,
        repo: str,
        sha: str,
        name: str,
        conclusion: str,
        details_url: str = "",
        output_summary: str = "",
    ) -> None:
        full = self._full(repo)
        summary = output_summary or conclusion
        fallback = os.environ.get("JOINT_STATUS_FALLBACK", "0") == "1"
        conc = (conclusion or "").lower()
        check_args: List[str] = [
            "-f",
            f"name={name}",
            "-f",
            f"head_sha={sha}",
        ]
        if conc in ("pending", "in_progress", "queued"):
            check_args += ["-f", "status=in_progress"]
        else:
            check_args += [
                "-f",
                "status=completed",
                "-f",
                f"conclusion={conc if conc in ('success', 'failure', 'neutral', 'cancelled', 'skipped', 'timed_out', 'action_required') else 'failure'}",
            ]
        if details_url:
            check_args += ["-f", f"details_url={details_url}"]
        check_args += [
            "-f",
            f"output[title]={name}",
            "-f",
            f"output[summary]={summary}",
        ]

        proc = self._gh_api_raw(
            f"repos/{full}/check-runs",
            *check_args,
            method="POST",
        )
        if proc.returncode == 0:
            return

        err = (proc.stderr or proc.stdout or "").strip()
        is_403 = "403" in err or "Resource not accessible" in err or "Checks" in err
        if not (fallback or is_403):
            raise RealGhError(f"check-runs failed for {full}@{sha[:7]}: {err.splitlines()[-1] if err else 'unknown'}")

        # Commit status fallback (Statuses:write)
        state = self._status_state(conclusion)
        # Map cancelled → error for statuses API allowed values
        if state not in ("error", "failure", "pending", "success"):
            state = "failure"
        status_args = [
            "-f",
            f"state={state}",
            "-f",
            f"context={name}",
            "-f",
            f"description={(summary or name)[:140]}",
        ]
        if details_url:
            status_args += ["-f", f"target_url={details_url}"]
        self.gh_api(
            f"repos/{full}/statuses/{sha}",
            *status_args,
            method="POST",
        )

    # --- Actions dispatch / poll --------------------------------------------

    def dispatch_test(self, test: Dict[str, Any], versions: Dict[str, str]) -> str:
        lab = self._lab()
        workflow = os.environ.get("JOINT_PROBE_WORKFLOW", self.PROBE_WORKFLOW)

        def _sha(role: str) -> str:
            # versions keys are role names from reports ("driver", …)
            return (
                versions.get(role)
                or versions.get(self.ROLE_REPOS[role])
                or ""
            )

        driver_sha = _sha("driver")
        synapse_sha = _sha("synapse")
        sim_sha = _sha("sim")
        # Probe workflow requires all three; fill missing with lab main tip if needed
        if not driver_sha or not synapse_sha or not sim_sha:
            main = self.gh_api(f"repos/{lab}/commits/main")
            tip = str((main or {}).get("sha") or "")
            driver_sha = driver_sha or tip
            synapse_sha = synapse_sha or tip
            sim_sha = sim_sha or tip
        if not (driver_sha and synapse_sha and sim_sha):
            raise RealGhError("dispatch_test: missing driver/synapse/sim SHAs in versions")

        before = self.gh_api(
            f"repos/{lab}/actions/workflows/{workflow}/runs?per_page=20",
        ) or {}
        old_ids = {
            str(r.get("id") or r.get("database_id"))
            for r in (before.get("workflow_runs") or [] if isinstance(before, dict) else [])
        }

        # Prefer `gh workflow run` so inputs are nested correctly (top-level -f driver_sha → 422).
        cmd = [
            "gh",
            "workflow",
            "run",
            workflow,
            "-R",
            lab,
            "--ref",
            "main",
            "-f",
            f"driver_sha={driver_sha}",
            "-f",
            f"synapse_sha={synapse_sha}",
            "-f",
            f"sim_sha={sim_sha}",
        ]
        proc = self._run_gh(cmd, check=True)
        if proc.returncode:
            raise RealGhError(f"gh workflow run {workflow} failed")

        timeout = int(os.environ.get("JOINT_RUN_WAIT_SECONDS", "90"))
        deadline = time.monotonic() + max(5, timeout)
        while time.monotonic() <= deadline:
            runs = self.gh_api(
                f"repos/{lab}/actions/workflows/{workflow}/runs?per_page=10",
            ) or {}
            items = runs.get("workflow_runs") or [] if isinstance(runs, dict) else []
            fresh = [
                r
                for r in items
                if str(r.get("id") or r.get("database_id")) not in old_ids
            ]
            if fresh:
                run = fresh[0]
                run_id = run.get("database_id") or run.get("id")
                if run_id is not None:
                    return str(run_id)
            time.sleep(3)
        raise RealGhError(
            f"workflow_dispatch of {workflow} accepted but no run appeared before timeout"
        )

    def get_run_conclusion(self, run_id: str) -> str:
        if str(run_id).startswith(("dry-", "fake-")):
            return "success"
        lab = self._lab()
        poll = os.environ.get("JOINT_POLL_RUNS", "1") == "1"
        timeout = int(os.environ.get("JOINT_RUN_WAIT_SECONDS", "180"))
        deadline = time.monotonic() + (timeout if poll else 0)
        while True:
            data = self.gh_api(f"repos/{lab}/actions/runs/{run_id}")
            if not isinstance(data, dict):
                return "failure"
            status = (data.get("status") or "").lower()
            conclusion = (data.get("conclusion") or "").lower()
            if status in ("queued", "in_progress", "waiting", "requested", "pending"):
                if poll and time.monotonic() < deadline:
                    time.sleep(3)
                    continue
                return "pending"
            if status == "completed":
                return "success" if conclusion == "success" else "failure"
            if conclusion == "success":
                return "success"
            if conclusion:
                return "failure"
            if poll and time.monotonic() < deadline:
                time.sleep(3)
                continue
            return "pending"

    def cancel_run(self, run_id: str) -> None:
        if not run_id or str(run_id).startswith(("dry-", "fake-")):
            return
        lab = self._lab()
        with self._lock:
            if run_id not in self.cancelled_runs:
                self.cancelled_runs.append(str(run_id))
        proc = self._gh_api_raw(
            f"repos/{lab}/actions/runs/{run_id}/cancel",
            method="POST",
        )
        # 409 if already completed — ignore
        if proc.returncode not in (0,) and "409" not in (proc.stderr or ""):
            # best-effort; do not raise hard for cancel
            pass

    # --- Invalidation (in-memory; issue status also via upsert) --------------

    def is_invalidated(self, joint_id: str) -> bool:
        with self._lock:
            return joint_id in self._invalidated_ids

    def invalidation_reason(self, joint_id: str) -> Optional[str]:
        with self._lock:
            return self._invalidate_reasons.get(joint_id)

    def set_invalidated(self, joint_id: str, reason: str = "push") -> None:
        with self._lock:
            self._invalidated_ids.add(joint_id)
            self._invalidate_reasons[joint_id] = reason or "push"

    def mark_invalidated(self, joint_id: str) -> None:
        self.set_invalidated(joint_id, "push")

    def clear_invalidated(self, joint_id: str) -> None:
        with self._lock:
            self._invalidated_ids.discard(joint_id)
            self._invalidate_reasons.pop(joint_id, None)

    @property
    def invalidate_flags(self) -> Set[str]:
        return self._invalidated_ids


def default_client() -> GhClient:
    if os.environ.get("JOINT_DRY_RUN", "1") == "1":
        return FakeGhClient()
    return RealGhClient()
