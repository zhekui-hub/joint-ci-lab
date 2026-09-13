#!/usr/bin/env python3
"""Document intended GitHub CLI / API calls for Joint CI live validation.

Import-safe without credentials: planning helpers never call the network unless
explicitly asked with --execute and JOINT_GH_TOKEN set.

Usage:
  python3 gh_calls_stub.py --help
  python3 gh_calls_stub.py preflight
  python3 gh_calls_stub.py plan --scenario e2
  python3 gh_calls_stub.py plan --scenario e3 --execute   # requires token + owner guard
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence

OWNER_DEFAULT = "zhekui-hub"
ALLOWED_REPOS = (
    "joint-ci-lab",
    "joint-ci-driver",
    "joint-ci-synapse",
    "joint-ci-sim",
)


def owner() -> str:
    return os.environ.get("JOINT_OWNER", OWNER_DEFAULT)


def full_name(repo: str) -> str:
    return f"{owner()}/{repo}"


def assert_whitelist(repo: str) -> None:
    """F16: refuse anything outside zhekui-hub/joint-ci-*."""
    o = owner()
    if o != "zhekui-hub":
        raise SystemExit(f"F16 refuse: JOINT_OWNER={o!r} must be 'zhekui-hub'")
    short = repo.split("/")[-1]
    if short not in ALLOWED_REPOS:
        raise SystemExit(f"F16 refuse: repo {repo!r} not in {ALLOWED_REPOS}")


def intended_env_names() -> List[str]:
    return [
        "JOINT_OWNER",
        "JOINT_GH_TOKEN",
        "JOINT_APP_ID",
        "JOINT_APP_INSTALLATION_ID",
        "JOINT_APP_PRIVATE_KEY_PATH",
        "JOINT_DRY_RUN",
        "JOINT_LABEL",
        "JOINT_BRANCH_PREFIX",
    ]


def plan_e2() -> List[Dict[str, Any]]:
    """Staggered wait: synapse first, then driver."""
    prefix = os.environ.get("JOINT_BRANCH_PREFIX", "exp/ci-")
    syn_branch = f"{prefix}e2-synapse"
    drv_branch = f"{prefix}e2-driver"
    syn_body = (
        "CI_MODE=joint\n"
        "JOINT_WAIT=true\n"
        f"DLC_KERNEL_DRIVER_BRANCH={drv_branch}\n"
    )
    drv_body = "CI_MODE=joint\n"
    return [
        {
            "step": 1,
            "title": "create synapse experiment branch",
            "gh": [
                "gh",
                "api",
                "-X",
                "POST",
                f"repos/{full_name('joint-ci-synapse')}/git/refs",
                "-f",
                f"ref=refs/heads/{syn_branch}",
                "-f",
                "sha=<main_tip_sha>",
            ],
            "note": "resolve main tip first via repos/.../git/ref/heads/main",
        },
        {
            "step": 2,
            "title": "open synapse PR (joint wait)",
            "gh": [
                "gh",
                "pr",
                "create",
                "--repo",
                full_name("joint-ci-synapse"),
                "--base",
                "main",
                "--head",
                syn_branch,
                "--title",
                "exp: joint E2 synapse",
                "--body",
                syn_body,
            ],
        },
        {
            "step": 3,
            "title": "assert waiting: list checks / no heavy workflow yet",
            "gh": [
                "gh",
                "pr",
                "checks",
                "<synapse_pr_number>",
                "--repo",
                full_name("joint-ci-synapse"),
            ],
            "manual": "Confirm Actions has no multirepo runner job for this joint during wait",
        },
        {
            "step": 4,
            "title": "create driver branch + Ready PR",
            "gh": [
                "gh",
                "pr",
                "create",
                "--repo",
                full_name("joint-ci-driver"),
                "--base",
                "main",
                "--head",
                drv_branch,
                "--title",
                "exp: joint E2 driver",
                "--body",
                drv_body,
            ],
        },
        {
            "step": 5,
            "title": "poll both PR checks + lab joint issue",
            "gh": [
                "gh",
                "issue",
                "list",
                "--repo",
                full_name("joint-ci-lab"),
                "--label",
                os.environ.get("JOINT_LABEL", "joint-ci"),
                "--state",
                "open",
            ],
            "expect": "public_runs=1; both checks leave waiting; same details_url (E3 overlap)",
        },
    ]


def plan_e3() -> List[Dict[str, Any]]:
    return [
        {
            "step": 1,
            "title": "from green E2 pair, inspect check-runs for details_url equality",
            "gh": [
                "gh",
                "api",
                f"repos/{full_name('joint-ci-driver')}/commits/<driver_sha>/check-runs",
                "--jq",
                ".check_runs[] | {name,conclusion,details_url}",
            ],
        },
        {
            "step": 2,
            "title": "same on synapse",
            "gh": [
                "gh",
                "api",
                f"repos/{full_name('joint-ci-synapse')}/commits/<synapse_sha>/check-runs",
                "--jq",
                ".check_runs[] | {name,conclusion,details_url}",
            ],
            "expect": "joint check details_url identical; one workflow_run id",
        },
    ]


def plan_e4() -> List[Dict[str, Any]]:
    return [
        {
            "step": 1,
            "title": "trigger joint with driver exclusive failure fixture",
            "manual": "Use lab workflow input or mock fail flag documented in joint-ci-lab",
            "gh": [
                "gh",
                "workflow",
                "run",
                "joint_ci.yml",
                "--repo",
                full_name("joint-ci-lab"),
                "-f",
                "inject_exclusive_fail=true",
            ],
            "note": "workflow name may differ; adjust to lab's actual file",
        },
        {
            "step": 2,
            "title": "compare conclusions",
            "expect": "driver joint failure; synapse success if public ok",
        },
    ]


def plan_e5() -> List[Dict[str, Any]]:
    return [
        {
            "step": 1,
            "title": "empty commit on synapse PR branch",
            "gh": [
                "gh",
                "api",
                "-X",
                "POST",
                f"repos/{full_name('joint-ci-synapse')}/git/commits",
                "-f",
                "message=exp: e5 empty",
                "-f",
                "tree=<tree_sha>",
                "-f",
                "parents[]=<parent_sha>",
            ],
            "alt": "git commit --allow-empty && git push (on Thinkbook clone)",
        },
        {
            "step": 2,
            "title": "verify old checks invalidated / pending",
            "gh": [
                "gh",
                "pr",
                "checks",
                "<synapse_pr>",
                "--repo",
                full_name("joint-ci-synapse"),
            ],
            "expect": "no stale success for joint; new joint_key round",
        },
    ]


PLANS = {
    "e2": plan_e2,
    "e3": plan_e3,
    "e4": plan_e4,
    "e5": plan_e5,
}


def cmd_preflight(_: argparse.Namespace) -> int:
    print("=== Joint CI real_gh preflight (no secrets printed) ===")
    print(f"owner={owner()}")
    print("allowed_repos=", ", ".join(full_name(r) for r in ALLOWED_REPOS))
    print("env_names=", ", ".join(intended_env_names()))
    token_set = bool(os.environ.get("JOINT_GH_TOKEN"))
    print(f"JOINT_GH_TOKEN_set={token_set}")
    print(f"gh_on_path={bool(shutil.which('gh'))}")
    print(f"JOINT_DRY_RUN={os.environ.get('JOINT_DRY_RUN', '1')}")
    try:
        for r in ALLOWED_REPOS:
            assert_whitelist(r)
        print("whitelist_guard=OK")
    except SystemExit as e:
        print("whitelist_guard=FAIL", e)
        return 1
    print("OK: import/preflight does not require credentials")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    scen = args.scenario.lower()
    if scen not in PLANS:
        print(f"unknown scenario {scen}; choose from {sorted(PLANS)}", file=sys.stderr)
        return 2
    for r in ALLOWED_REPOS:
        assert_whitelist(r)
    steps = PLANS[scen]()
    print(json.dumps({"scenario": scen, "owner": owner(), "steps": steps}, ensure_ascii=False, indent=2))
    if not args.execute:
        return 0
    # Execute only documents subprocess intent for list-style gh commands; still guarded.
    if os.environ.get("JOINT_DRY_RUN", "1") == "1":
        print("JOINT_DRY_RUN=1 → not executing (set JOINT_DRY_RUN=0 to allow)", file=sys.stderr)
        return 0
    if not os.environ.get("JOINT_GH_TOKEN"):
        print("JOINT_GH_TOKEN missing → refuse execute", file=sys.stderr)
        return 1
    if not shutil.which("gh"):
        print("gh CLI missing", file=sys.stderr)
        return 1
    print("Execute mode: run checklist_e2_e5.sh for interactive live steps;")
    print("this stub refuses auto-running placeholder SHAs to avoid accidental writes.")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p0 = sub.add_parser("preflight", help="print env names + whitelist; no network")
    p0.set_defaults(func=cmd_preflight)
    p1 = sub.add_parser("plan", help="print intended gh calls as JSON")
    p1.add_argument("--scenario", required=True, choices=sorted(PLANS))
    p1.add_argument("--execute", action="store_true", help="guarded; still refuses placeholders")
    p1.set_defaults(func=cmd_plan)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
