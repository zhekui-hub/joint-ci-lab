"""Author: zhekui. Read-only GitHub inventory to transparent discovery adapter.

inventory: resolve PRs, existing dependency fields, exact commits and App checks;
associate: return conservative connected groups, never rewrite user test inputs.
Caller supplies operator-owned configuration and explicitly allowed lab repos.
"""
from __future__ import annotations

import copy
from urllib.parse import quote

from .model import discover, parse_dependencies


def inventory(api, config):
    repositories = config["repositories"]
    if {r["repo"] for r in repositories} != api.allowed:
        raise ValueError("config repositories must equal transport allowlist")
    fields = {r["field"]: r["repo"] for r in repositories}
    defaults = {r["repo"]: r["default_branch"] for r in repositories}
    rows = []
    for spec in repositories:
        for pr in api.pages(spec["repo"], "pulls?state=open"):
            # A fresh detailed read, not trusting webhook body/head/status assertions.
            live = api.request(spec["repo"], f'pulls/{pr["number"]}')
            if live["state"] != "open":
                continue
            rows.append((spec, live))
    reports = []
    for spec, pr in rows:
        if pr["head"]["repo"]["full_name"] != spec["repo"]:
            continue  # Fork execution/trust is deliberately not enabled.
        refs = dict(defaults)
        refs.update(parse_dependencies(pr.get("body") or "", fields))
        refs[spec["repo"]] = pr["head"]["sha"]
        versions = {}
        for dependency, ref in refs.items():
            commit = api.request(dependency, "commits/" + quote(ref, safe=""))
            sha = commit["sha"]
            is_self = dependency == spec["repo"]
            explicit_branch = dependency in parse_dependencies(pr.get("body") or "", fields) and ref != defaults[dependency]
            matches = [(other_spec, other_pr) for other_spec, other_pr in rows
                       if other_spec["repo"] == dependency and other_pr["head"]["sha"] == sha
                       and ((is_self and other_pr["number"] == pr["number"]) or
                            (not is_self and explicit_branch and other_pr["head"]["ref"] == ref))]
            if len(matches) == 1 and matches[0][1].get("merge_commit_sha"):
                target = matches[0][1]
                # Verify candidate exists. A commit SHA's format alone is not proof.
                candidate = api.request(dependency, "commits/" + target["merge_commit_sha"])["sha"]
                versions[dependency] = dict(head=sha, base=target["base"]["sha"], candidate=candidate)
            else:
                versions[dependency] = dict(head=sha, base=sha, candidate=sha)
        required = spec["required_app_checks"]
        if not required or spec.get("aggregate_name", "joint-ci") in required:
            raise ValueError("private checks required; joint aggregate cannot depend on itself")
        scopes = spec["private_scopes"]
        checks = api.private_checks(spec["repo"], pr["head"]["sha"], required, scopes)
        policy = spec.get("approval_policy", "required")
        approved = api.review_decision(spec["repo"], pr["number"]) == "APPROVED" if policy == "required" else policy == "not_required"
        reports.append(dict(repo=spec["repo"], pr=pr["number"], branch=pr["head"]["ref"],
                            head_repo=pr["head"]["repo"]["full_name"], body=pr.get("body") or "",
                            open=True, draft=pr["draft"], approved=approved,
                            private=checks, private_scopes=scopes, required=list(required), versions=versions,
                            environment=config["environment"], policy_version=config["policy_version"],
                            tasks=copy.deepcopy(spec["tasks"])))
    # not_required is explicit operator configuration for isolated lab repositories;
    # real repository review/branch rules remain independently enforced by GitHub.
    return dict(reports=reports, fields=fields, defaults=defaults)


def associate(data):
    reports, fields, defaults = data["reports"], data["fields"], data["defaults"]
    by_branch = {}
    for i, row in enumerate(reports):
        by_branch.setdefault((row["repo"], row["branch"]), []).append(i)
    adjacency = {i: set() for i in range(len(reports))}
    for i, row in enumerate(reports):
        for repo, ref in parse_dependencies(row.get("body", ""), fields).items():
            candidates = by_branch.get((repo, ref), [])
            if ref != defaults.get(repo) and len(candidates) == 1:
                j = candidates[0]
                adjacency[i].add(j)
                adjacency[j].add(i)
    unseen, groups = set(adjacency), []
    while unseen:
        component, todo = set(), [min(unseen)]
        while todo:
            i = todo.pop()
            if i in component:
                continue
            component.add(i)
            todo.extend(adjacency[i] - component)
        unseen -= component
        groups.append(discover([reports[i] for i in sorted(component)], fields, defaults))
    return groups
