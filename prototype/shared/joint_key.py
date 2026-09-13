"""Compute joint_key and merge test requests."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Tuple


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def make_joint_key(versions: Dict[str, str], image: str, test_config_hash: str) -> str:
    payload = {
        "versions": versions,
        "image": image,
        "test_config_hash": test_config_hash,
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"joint-{digest[:16]}"


def test_identity(test: Dict[str, Any]) -> Tuple:
    return (
        test.get("id"),
        canonical_json(test.get("params") or {}),
        canonical_json(test.get("env") or {}),
    )


def merge_remote_tests(reports: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Union of remote tests; identical identity collapsed once.

    Ownership is recorded so results can be mapped back per-PR.
    """
    merged: Dict[Tuple, Dict[str, Any]] = {}
    for report in reports:
        repo = report["repo"]
        pr = report["pr_number"]
        for test in report.get("remote_tests") or []:
            key = test_identity(test)
            if key not in merged:
                item = {
                    "id": test["id"],
                    "params": test.get("params") or {},
                    "env": test.get("env") or {},
                    "consumers": [],
                    "exclusive_to": test.get("exclusive_to"),
                }
                merged[key] = item
            merged[key]["consumers"].append({"repo": repo, "pr": pr})
            # If any side marks exclusive_to, keep the narrower ownership note
            if test.get("exclusive_to"):
                merged[key]["exclusive_to"] = test["exclusive_to"]
    return list(merged.values())


def per_pr_required(report: Dict[str, Any], merged_tests: List[Dict[str, Any]]) -> List[str]:
    """Return merged test keys required by this PR (for aggregation)."""
    needed = {test_identity(t) for t in report.get("remote_tests") or []}
    out = []
    for t in merged_tests:
        ident = (t["id"], canonical_json(t["params"]), canonical_json(t["env"]))
        if ident in needed:
            out.append(f"{t['id']}:{canonical_json(t['params'])}")
    return out
