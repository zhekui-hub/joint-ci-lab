"""Author: zhekui. Immutable inputs and conservative automatic association.

canonical/digest: deterministic identities; validate_plan: reject unsafe inputs;
discover: associate existing dependency fields without new user-facing markers.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate_plan(plan):
    p = copy.deepcopy(plan)
    if p.get("schema") != 1 or not p.get("members") or not p.get("versions"):
        raise ValueError("schema, members and versions are required")
    members = p["members"]
    if len({m["repo"] for m in members}) != len(members):
        raise ValueError("one participating PR per repository")
    for m in members:
        if not isinstance(m["pr"], int) or m["pr"] < 1:
            raise ValueError("invalid PR")
        if m["repo"] not in p["versions"]:
            raise ValueError("member has no source version")
        if not m.get("required"):
            raise ValueError("explicit private required checks needed")
        if any(m.get("private_scopes", {}).get(k) not in ("head", "snapshot") for k in m["required"]):
            raise ValueError("each private check needs explicit head or snapshot scope")
    for repo, version in p["versions"].items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("invalid repository")
        for key in ("head", "base", "candidate"):
            if not re.fullmatch(r"[0-9a-f]{40}", version.get(key, "")):
                raise ValueError(f"invalid {repo} {key}")
    if not p.get("environment") or not p.get("policy_version"):
        raise ValueError("environment and policy version required")
    if not p.get("tasks"):
        raise ValueError("empty public task list cannot pass")
    member_ids = {member_id(m) for m in members}
    merged = {}
    for task in p["tasks"]:
        if not task.get("suite") or not task.get("command"):
            raise ValueError("suite and trusted command required")
        if not isinstance(task["command"], list) or not all(
                isinstance(a, str) and a for a in task["command"]):
            raise ValueError("command must be an argv list, never shell text")
        consumers = set(task.get("consumers", []))
        if not consumers or not consumers <= member_ids:
            raise ValueError("invalid task consumers")
        identity = {k: v for k, v in task.items() if k != "consumers"}
        identity.setdefault("params", {})
        identity.setdefault("env", {})
        key = digest(identity)
        if key not in merged:
            merged[key] = dict(identity, consumers=[])
        merged[key]["consumers"] = sorted(set(merged[key]["consumers"]) | consumers)
    p["tasks"] = [merged[k] for k in sorted(merged)]
    p["members"] = sorted(members, key=member_id)
    for m in p["members"]:
        m["required"] = sorted(set(m["required"]))
    return p


def member_id(member):
    return f'{member["repo"]}#{member["pr"]}'


def group_id(plan):
    return digest(sorted(member_id(m) for m in plan["members"]))


def snapshot_id(plan):
    immutable = copy.deepcopy(plan)
    for m in immutable["members"]:
        for key in ("private", "draft", "open", "approved", "merged", "merge_commit_sha", "merged_at"):
            m.pop(key, None)
    return digest(immutable)


def parse_dependencies(body, fields):
    """Strict adapter for existing KEY=value lines; ambiguous values fail closed.

    fields maps existing field names to repositories. No eval or new PR markers.
    Compatibility with each production parser must be verified before rollout.
    """
    found = {}
    for line in body.splitlines():
        match = re.fullmatch(r"\s*([A-Z][A-Z0-9_]*)\s*=\s*([^\s`]+)\s*", line)
        if match and match[1] in fields:
            repo, ref = fields[match[1]], match[2]
            if repo in found and found[repo] != ref:
                raise ValueError("conflicting dependency field")
            found[repo] = ref
    return found


def discover(reports, fields, defaults):
    """Reports are trusted live reads, not webhook-supplied success assertions.

    Require equivalent resolved version vectors. Ambiguity returns legacy mode.
    Fixed refs and branches without a PR never introduce a missing-PR wait.
    """
    reports = copy.deepcopy(reports)
    if not reports:
        return {"mode": "legacy", "reason": "no participants"}
    if len({r["repo"] for r in reports}) != len(reports):
        return {"mode": "legacy", "reason": "multiple PRs in one repository"}
    by_repo = {r["repo"]: r for r in reports}
    edges = set()
    try:
        for r in reports:
            for repo, ref in parse_dependencies(r.get("body", ""), fields).items():
                if ref == defaults.get(repo) or re.fullmatch(r"[0-9a-f]{40}", ref):
                    continue
                other = by_repo.get(repo)
                if other and other["branch"] == ref and other.get("head_repo", repo) == repo:
                    edges.add(tuple(sorted((r["repo"], repo))))
    except ValueError as exc:
        return {"mode": "legacy", "reason": str(exc)}
    visited = {reports[0]["repo"]}
    while True:
        expanded = visited | {v for edge in edges if set(edge) & visited for v in edge}
        if expanded == visited:
            break
        visited = expanded
    if len(reports) < 2 or visited != set(by_repo):
        return {"mode": "legacy", "reason": "no unambiguous connected dependency group"}
    if len({digest(r["versions"]) for r in reports}) != 1:
        return {"mode": "legacy", "reason": "different existing test inputs"}
    if any(not r.get("open", True) for r in reports):
        return {"mode": "legacy", "reason": "closed participant"}
    plan = dict(schema=1, versions=reports[0]["versions"],
                environment=reports[0]["environment"],
                policy_version=reports[0]["policy_version"], members=[], tasks=[])
    if any(r["environment"] != plan["environment"] or
           r["policy_version"] != plan["policy_version"] for r in reports):
        return {"mode": "legacy", "reason": "different environment or routing policy"}
    for r in reports:
        plan["members"].append({k: r[k] for k in
            ("repo", "pr", "required", "private_scopes", "private", "draft", "open", "approved")})
        plan["tasks"].extend(dict(t, consumers=[member_id(r)]) for t in r["tasks"])
    return {"mode": "joint", "plan": validate_plan(plan)}
