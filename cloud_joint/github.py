"""Author: zhekui. Bounded GitHub CLI transport and independent check inspection.

GitHub.request/pages: retries and pagination; private_checks: source-aware checks;
probe: read-only lab readiness. Writes require explicit isolated-repo allowlist.
No production credentials are required for local/cloud acceptance.
"""
from __future__ import annotations

import json
import re
import subprocess
import time


class GitHub:
    def __init__(self, allowed_repos=(), writable=False, command=None, sleep=time.sleep):
        self.allowed = set(allowed_repos)
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]+/joint-ci-[A-Za-z0-9_.-]+", r)
               or r.lower().startswith("chipltech/") for r in self.allowed):
            raise ValueError("only explicitly named joint-ci-* lab repositories allowed")
        self.writable = writable
        self.command = command or self._command
        self.sleep = sleep

    @staticmethod
    def _command(args, body):
        return subprocess.run(args, input=body, text=True, capture_output=True, timeout=40)

    def request(self, repo, path, method="GET", body=None):
        if repo not in self.allowed:
            raise PermissionError("repository outside explicit lab allowlist")
        if method != "GET" and not self.writable:
            raise PermissionError("read-only transport")
        args = ["gh", "api", "--method", method, "-H", "X-GitHub-Api-Version: 2022-11-28",
                f"repos/{repo}/{path}"]
        if body is not None:
            args += ["--input", "-"]
        for attempt in range(4):
            result = self.command(args, json.dumps(body) if body is not None else None)
            if result.returncode == 0:
                return json.loads(result.stdout) if result.stdout.strip() else None
            # For non-idempotent dispatch, caller must reconcile uncertain delivery.
            transient = re.search(r"HTTP (429|500|502|503|504)\b", result.stderr)
            if not transient or attempt == 3 or method != "GET":
                raise RuntimeError("GitHub request failed; see redacted HTTP class: " +
                                   (transient[0] if transient else "non-retryable/unknown"))
            self.sleep(min(2 ** attempt, 8))
        raise RuntimeError("unreachable")

    def pages(self, repo, path, key=None):
        result = []
        for page in range(1, 101):
            delimiter = "&" if "?" in path else "?"
            payload = self.request(repo, f"{path}{delimiter}per_page=100&page={page}")
            items = payload[key] if key else payload
            if not isinstance(items, list):
                raise ValueError("malformed GitHub page")
            result.extend(items)
            if len(items) < 100:
                return result
        raise RuntimeError("pagination exceeded bound; refusing partial evidence")

    def private_checks(self, repo, sha, required, scopes=None):
        """required maps exact check name -> expected App id.

        Missing/untrusted/duplicate ambiguous check never becomes success.
        Exclude the joint aggregate to avoid circular dependencies.
        """
        checks = self.pages(repo, f"commits/{sha}/check-runs?filter=latest", "check_runs")
        result = {}
        for name, app_id in required.items():
            matches = [x for x in checks if x.get("name") == name and
                       x.get("head_sha") == sha and x.get("app", {}).get("id") == app_id]
            result[name] = (matches[0].get("conclusion") if len(matches) == 1 and
                            matches[0].get("status") == "completed" else "pending")
            if (scopes or {}).get(name) == "snapshot":
                result[name] = dict(conclusion=result[name],
                                    snapshot=matches[0].get("external_id") if len(matches) == 1 else None)
        return result

    def probe(self, repo):
        info = self.request(repo, "")
        branch = info["default_branch"]
        runners = self.pages(repo, "actions/runners", "runners")
        return dict(repo=repo, default_branch=branch,
                    runners=[{"id": r["id"], "status": r["status"],
                              "labels": [l["name"] for l in r["labels"]]} for r in runners])

    def review_decision(self, repo, number):
        if repo not in self.allowed:
            raise PermissionError("repository outside allowlist")
        owner, name = repo.split("/")
        query = "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewDecision}}}"
        result = self.command(["gh", "api", "graphql", "-f", "query=" + query,
                               "-f", "owner=" + owner, "-f", "name=" + name,
                               "-F", "number=" + str(number)], None)
        if result.returncode:
            raise RuntimeError("review decision unavailable")
        return json.loads(result.stdout)["data"]["repository"]["pullRequest"]["reviewDecision"]
