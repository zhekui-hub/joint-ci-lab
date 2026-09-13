#!/usr/bin/env python3
"""Collect / assert E3–E5 real-path evidence on zhekui-hub only.

Default is read-only collect (needs gh auth or JOINT_GH_TOKEN).
Mutating E5 push requires --push-e5 and JOINT_ALLOW_MUTATION=1.

Usage:
  python3 assert_e3_e5.py collect          # E3 evidence from open pair
  python3 assert_e3_e5.py assert-e3        # fail if public_runs!=1 or URLs differ
  python3 assert_e3_e5.py plan-e5          # print invalidate steps
  python3 assert_e3_e5.py assert-e4 --driver-sha SHA --synapse-sha SHA
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

OWNER = os.environ.get("JOINT_OWNER", "zhekui-hub")
LAB = f"{OWNER}/joint-ci-lab"
DRIVER = f"{OWNER}/joint-ci-driver"
SYNAPSE = f"{OWNER}/joint-ci-synapse"
STATUS_CTX = os.environ.get("JOINT_STATUS_CONTEXT", "joint-ci")

# Known live pair from coordinator (override via env)
DRIVER_PR = int(os.environ.get("JOINT_DRIVER_PR", "1"))
SYNAPSE_PR = int(os.environ.get("JOINT_SYNAPSE_PR", "1"))


def _run(cmd: List[str]) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def gh_json(args: List[str]) -> Any:
    code, out, err = _run(["gh", *args])
    if code != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {err.strip() or out.strip()}")
    return json.loads(out) if out.strip() else None


def ensure_owner_safe(repo: str) -> None:
    if not repo.startswith(f"{OWNER}/joint-ci-"):
        raise SystemExit(f"F16 refuse: {repo}")


def pr_head(repo: str, number: int) -> Dict[str, Any]:
    ensure_owner_safe(repo)
    return gh_json(
        [
            "pr",
            "view",
            str(number),
            "-R",
            repo,
            "--json",
            "url,headRefOid,headRefName,state,statusCheckRollup,body",
        ]
    )


def commit_statuses(repo: str, sha: str) -> List[Dict[str, Any]]:
    ensure_owner_safe(repo)
    data = gh_json(["api", f"repos/{repo}/commits/{sha}/status"])
    return list((data or {}).get("statuses") or [])


def pick_joint(statuses: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for s in statuses:
        if s.get("context") == STATUS_CTX:
            return s
    # fallback: any context containing joint
    for s in statuses:
        if "joint" in (s.get("context") or "").lower():
            return s
    return None


def list_lab_runs(limit: int = 30) -> List[Dict[str, Any]]:
    ensure_owner_safe(LAB)
    return gh_json(
        [
            "run",
            "list",
            "-R",
            LAB,
            "--limit",
            str(limit),
            "--json",
            "databaseId,url,headSha,displayTitle,conclusion,status,createdAt,name",
        ]
    ) or []


def collect() -> Dict[str, Any]:
    d = pr_head(DRIVER, DRIVER_PR)
    s = pr_head(SYNAPSE, SYNAPSE_PR)
    d_sha, s_sha = d["headRefOid"], s["headRefOid"]
    d_st = pick_joint(commit_statuses(DRIVER, d_sha))
    s_st = pick_joint(commit_statuses(SYNAPSE, s_sha))
    runs = list_lab_runs()
    out = {
        "driver_pr": d.get("url"),
        "synapse_pr": s.get("url"),
        "driver_sha": d_sha,
        "synapse_sha": s_sha,
        "driver_status": d_st,
        "synapse_status": s_st,
        "same_target_url": bool(
            d_st and s_st and d_st.get("target_url") and d_st.get("target_url") == s_st.get("target_url")
        ),
        "lab_runs_sample": runs[:10],
    }
    # Heuristic public_runs: distinct target_url run ids referenced by joint statuses
    urls = {u for u in [ (d_st or {}).get("target_url"), (s_st or {}).get("target_url") ] if u}
    out["public_run_urls"] = sorted(urls)
    out["public_runs_estimate"] = len(urls)
    return out


def assert_e3(data: Optional[Dict[str, Any]] = None) -> None:
    data = data or collect()
    errors = []
    if not data.get("same_target_url"):
        errors.append("E3: driver/synapse joint target_url differ or missing")
    if data.get("public_runs_estimate", 0) != 1:
        errors.append(
            f"E3: expected public_runs==1 (by shared target_url), got {data.get('public_runs_estimate')} urls={data.get('public_run_urls')}"
        )
    d_st, s_st = data.get("driver_status"), data.get("synapse_status")
    if not d_st or not s_st:
        errors.append("E3: missing joint-ci status on driver or synapse")
    if errors:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        raise SystemExit("FAIL\n" + "\n".join(errors))
    print("E3 PASS")
    print(json.dumps({k: data[k] for k in ("driver_sha", "synapse_sha", "public_run_urls", "same_target_url")}, indent=2))


def assert_e4(driver_sha: str, synapse_sha: str) -> None:
    """Expect exclusive failure isolation: driver failure, synapse success (public ok)."""
    d_st = pick_joint(commit_statuses(DRIVER, driver_sha))
    s_st = pick_joint(commit_statuses(SYNAPSE, synapse_sha))
    d_state = (d_st or {}).get("state")
    s_state = (s_st or {}).get("state")
    # GitHub commit status: success|failure|pending|error
    if d_state != "failure" or s_state != "success":
        raise SystemExit(f"E4 FAIL: driver={d_state} synapse={s_state} (want failure/success)")
    print("E4 PASS")


def plan_e5() -> None:
    print(
        """# E5 plan (after E3 green)
1. Record joint_key / statuses / public run URL from: python3 assert_e3_e5.py collect
2. On synapse PR branch: empty commit + push
3. Expect: old success cleared (pending/failure/invalidated), new round, new public run
4. Re-run: python3 assert_e3_e5.py collect && python3 assert_e3_e5.py assert-e3
5. Mutating push only on Thinkbook with token:
   JOINT_ALLOW_MUTATION=1  # and follow DISPATCH / CHECKLIST — do not run from shared box without auth
"""
    )


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    cmd = sys.argv[1]
    if cmd == "collect":
        print(json.dumps(collect(), indent=2, ensure_ascii=False))
    elif cmd == "assert-e3":
        assert_e3()
    elif cmd == "assert-e4":
        # crude argv parse
        args = dict(zip(sys.argv[2::2], sys.argv[3::2]))
        assert_e4(args["--driver-sha"], args["--synapse-sha"])
    elif cmd == "plan-e5":
        plan_e5()
    else:
        raise SystemExit(f"unknown command: {cmd}")


if __name__ == "__main__":
    main()
