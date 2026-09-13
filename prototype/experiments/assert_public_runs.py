"""Offline assertions used by experiment runner (prototype)."""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional


def assert_public_runs(actual: Dict[str, int], expected: Dict[str, int]) -> None:
    for k, v in expected.items():
        if actual.get(k, 0) != v:
            raise AssertionError(f"public_runs[{k}] expected {v}, got {actual.get(k, 0)}")


def assert_wait_not_holding_pod(busy_seconds: float, max_seconds: float = 30.0) -> None:
    if busy_seconds > max_seconds:
        raise AssertionError(f"runner busy during wait: {busy_seconds}s > {max_seconds}s")


def assert_dispatch_count(actual: int, expected: int) -> None:
    if actual != expected:
        raise AssertionError(f"dispatch_count expected {expected}, got {actual}")


def assert_joint_status(actual: Optional[str], expected: str) -> None:
    if actual != expected:
        raise AssertionError(f"joint_status expected {expected}, got {actual}")


def assert_checks(
    get_conclusion,
    expected: Dict[str, str],
    sha_by_repo: Dict[str, str],
) -> None:
    """expected: repo -> conclusion."""
    for repo, want in expected.items():
        sha = sha_by_repo.get(repo)
        if not sha:
            raise AssertionError(f"no sha recorded for repo {repo}")
        got = get_conclusion(repo, sha)
        if got != want:
            raise AssertionError(f"check[{repo}] expected {want}, got {got}")


def assert_same_run_url(urls: Iterable[Optional[str]]) -> None:
    cleaned = [u for u in urls if u]
    if not cleaned:
        raise AssertionError("same_run_url: no details_url recorded")
    if len(set(cleaned)) != 1:
        raise AssertionError(f"same_run_url mismatch: {cleaned}")


def assert_no_joint_issue(state: Any) -> None:
    status = getattr(state, "status", None)
    issue_number = getattr(state, "issue_number", None)
    if status not in ("skipped_non_joint", None) and issue_number not in (None,):
        # skipped_non_joint is the prototype signal for E1
        if status != "skipped_non_joint":
            raise AssertionError(f"expected no joint issue, got status={status} issue={issue_number}")


def assert_exclusive_isolation(
    driver_conclusion: Optional[str],
    synapse_conclusion: Optional[str],
    expect_driver: str = "failure",
    expect_synapse: str = "success",
) -> None:
    if driver_conclusion != expect_driver:
        raise AssertionError(f"exclusive isolation: driver expected {expect_driver}, got {driver_conclusion}")
    if synapse_conclusion != expect_synapse:
        raise AssertionError(
            f"exclusive isolation: synapse expected {expect_synapse}, got {synapse_conclusion}"
        )


def assert_invalidated(checks_summary: Iterable[str]) -> None:
    """After push invalidation, checks should be pending (not leftover success)."""
    for s in checks_summary:
        if s == "success":
            raise AssertionError("stale success check after invalidation")
