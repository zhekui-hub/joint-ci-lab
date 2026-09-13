"""Offline assertions used by experiment runner (prototype)."""
from __future__ import annotations

from typing import Dict


def assert_public_runs(actual: Dict[str, int], expected: Dict[str, int]) -> None:
    for k, v in expected.items():
        if actual.get(k, 0) != v:
            raise AssertionError(f"public_runs[{k}] expected {v}, got {actual.get(k, 0)}")


def assert_wait_not_holding_pod(busy_seconds: float, max_seconds: float = 30.0) -> None:
    if busy_seconds > max_seconds:
        raise AssertionError(f"runner busy during wait: {busy_seconds}s > {max_seconds}s")
