"""Compute joint_run_key / joint_key and merge public test requests.

Plan (2026-09-13): Arsenal merges and dispatches **public tests only**.
Tests marked exclusive_to are ignored here — they belong on business-repo
*-independent-ci workflows, not joint-ci.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional, Tuple


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def make_joint_key(versions: Dict[str, str], image: str, test_config_hash: str) -> str:
    """Legacy fingerprint (versions + image + test_config_hash). Prefer make_joint_run_key."""
    payload = {
        "versions": versions,
        "image": image,
        "test_config_hash": test_config_hash,
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"joint-{digest[:16]}"


def make_joint_run_key(
    joint_id: str,
    versions: Dict[str, str],
    image: str,
    test_config_hash: str,
    *,
    base_sha: str = "",
    arsenal_ref: str = "",
) -> str:
    """Public-run fingerprint aligned with design §1.6.

    joint_run_key = hash(joint_id, SHAs, base_sha, arsenal_ref, image, test_parameters)
    Stored on JointState as joint_key for prototype compatibility; callers may also
    read joint_run_key when present.
    """
    payload = {
        "joint_id": joint_id,
        "versions": versions,
        "base_sha": base_sha or "",
        "arsenal_ref": arsenal_ref or "",
        "image": image,
        "test_config_hash": test_config_hash,
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"joint-{digest[:16]}"


def is_exclusive(test: Dict[str, Any]) -> bool:
    excl = test.get("exclusive_to")
    return bool(excl)


def is_public_test(test: Dict[str, Any]) -> bool:
    return not is_exclusive(test)


def requested_tests(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Tests this report asks Arsenal to consider.

    Prefer explicit public_tests_requested; otherwise take remote_tests and
    drop exclusive_to items (compat with older fixtures).
    """
    if "public_tests_requested" in report and report.get("public_tests_requested") is not None:
        return list(report.get("public_tests_requested") or [])
    out: List[Dict[str, Any]] = []
    for test in report.get("remote_tests") or []:
        if is_public_test(test):
            out.append(test)
    return out


def test_identity(test: Dict[str, Any]) -> Tuple:
    return (
        test.get("id"),
        canonical_json(test.get("params") or {}),
        canonical_json(test.get("env") or {}),
    )


def workflow_key(test: Dict[str, Any]) -> str:
    """Stable key for a merged/dispatched test (id + params)."""
    return test["id"] + ":" + canonical_json(test.get("params") or {})


def hash_merged_tests(merged: Iterable[Dict[str, Any]]) -> str:
    """Canonical hash of merged remote-test identities (not len(merged))."""
    identities = sorted(
        [
            {
                "id": t.get("id"),
                "params": t.get("params") or {},
                "env": t.get("env") or {},
            }
            for t in merged
        ],
        key=lambda x: canonical_json(x),
    )
    return hashlib.sha256(canonical_json(identities).encode("utf-8")).hexdigest()[:16]


def test_config_hash(merged: Iterable[Dict[str, Any]]) -> str:
    """Alias used by scheduler for joint_key input."""
    return hash_merged_tests(merged)


def merge_remote_tests(reports: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Union of **public** remote tests; identical identity collapsed once.

    exclusive_to / independent-ci tests are skipped (Plan: Arsenal public-only).
    Ownership is recorded so results can be mapped back per-PR (consumers).
    """
    merged: Dict[Tuple, Dict[str, Any]] = {}
    for report in reports:
        repo = report["repo"]
        pr = report["pr_number"]
        for test in requested_tests(report):
            if is_exclusive(test):
                # public_tests_requested should never carry exclusive; belt+suspenders
                continue
            key = test_identity(test)
            if key not in merged:
                item = {
                    "id": test["id"],
                    "params": test.get("params") or {},
                    "env": test.get("env") or {},
                    "consumers": [],
                    "exclusive_to": None,
                }
                merged[key] = item
            merged[key]["consumers"].append({"repo": repo, "pr": pr})
    return list(merged.values())


def per_pr_required(report: Dict[str, Any], merged_tests: List[Dict[str, Any]]) -> List[str]:
    """Return merged public test keys relevant to this PR (consumers or requested).

    Under Plan, joint-ci is the same public result for all participants; this
    helper remains for compatibility / debugging.
    """
    needed = {test_identity(t) for t in requested_tests(report)}
    out = []
    for t in merged_tests:
        if is_exclusive(t):
            continue
        ident = (t["id"], canonical_json(t["params"]), canonical_json(t["env"]))
        consumers = t.get("consumers") or []
        in_consumers = any(c.get("repo") == report.get("repo") for c in consumers)
        if ident in needed or in_consumers:
            out.append(f"{t['id']}:{canonical_json(t['params'])}")
    return out
