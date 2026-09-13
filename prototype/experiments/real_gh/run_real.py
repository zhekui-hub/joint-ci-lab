#!/usr/bin/env python3
"""Run the live zhekui-hub Joint CI feasibility probes.

The command is intentionally conservative: it only addresses the four
``zhekui-hub/joint-ci-*`` repositories and performs writes when
``JOINT_DRY_RUN=0``.  A report is written on every invocation so a dry run is
still useful for checking configuration without pretending to be live evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


OWNER = "zhekui-hub"
REPOS = {
    "lab": "joint-ci-lab",
    "driver": "joint-ci-driver",
    "synapse": "joint-ci-synapse",
    "sim": "joint-ci-sim",
}
LABEL = "joint-ci"
ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "RESULTS_REAL.md"


class GhError(RuntimeError):
    pass


def repo_name(role: str) -> str:
    try:
        short = REPOS[role]
    except KeyError as exc:
        raise GhError(f"F16 refuse: unknown repository role {role!r}") from exc
    # Keep this assertion close to every API target; do not weaken it.
    if OWNER != "zhekui-hub" or not short.startswith("joint-ci-"):
        raise GhError(f"F16 refuse: {OWNER}/{short}")
    return f"{OWNER}/{short}"


def assert_allowed_target(target: str) -> None:
    """F16 guard used both by the executor and its self-test."""
    if "/" not in target:
        raise GhError(f"F16 refuse: malformed target {target!r}")
    owner, short = target.split("/", 1)
    if owner != OWNER or short not in REPOS.values() or not short.startswith("joint-ci-"):
        raise GhError(f"F16 refuse: target outside {OWNER}/joint-ci-* ({target})")


def gh_api(path: str, *args: str, method: Optional[str] = None, jq: Optional[str] = None) -> Any:
    """Call gh without exposing token values in logs or report files."""
    if not path.startswith("repos/") and path not in {"user"}:
        raise GhError(f"refusing non-repository API path: {path}")
    command = ["gh", "api"]
    if method:
        command += ["-X", method]
    command.append(path)
    command += list(args)
    if jq:
        command += ["--jq", jq]
    env = os.environ.copy()
    token = env.get("JOINT_GH_TOKEN")
    if token:
        env["GH_TOKEN"] = token
    try:
        proc = subprocess.run(command, check=False, capture_output=True, text=True, encoding='utf-8', errors='replace', env=env)
    except FileNotFoundError as exc:
        raise GhError("gh CLI is not installed or not on PATH") from exc
    if proc.returncode:
        detail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["unknown gh error"]
        raise GhError(f"gh api {path} failed (exit {proc.returncode}): {detail[0]}")
    raw = proc.stdout.strip()
    if jq:
        return raw
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def gh_api_optional(path: str, *args: str, method: Optional[str] = None, jq: Optional[str] = None) -> Any:
    try:
        return gh_api(path, *args, method=method, jq=jq)
    except GhError:
        return None


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Probe:
    probe: str
    result: str
    reason: str
    evidence: Dict[str, Any]


def resolve_sha(role: str) -> str:
    value = os.environ.get(f"JOINT_{role.upper()}_SHA", "").strip()
    if value:
        return value
    data = gh_api(f"repos/{repo_name(role)}/commits/main")
    sha = str((data or {}).get("sha", ""))
    if not sha:
        raise GhError(f"could not resolve main SHA for {role}")
    return sha


def create_issue(joint_id: str, shas: Dict[str, str]) -> Dict[str, Any]:
    body = (
        f"Real Joint CI feasibility probe `{joint_id}`.\n\n"
        "Explicit participant SHAs:\n" + "\n".join(f"- {k}: `{v}`" for k, v in shas.items())
    )
    args = ["-f", f"title=joint-ci real probe {joint_id}", "-f", f"body={body}", "-f", f"labels[]={LABEL}"]
    try:
        issue = gh_api(f"repos/{repo_name('lab')}/issues", *args, method="POST")
    except GhError as first:
        # A fresh lab may not have the label yet. Create it once, then retry.
        gh_api_optional(
            f"repos/{repo_name('lab')}/labels",
            "-f", f"name={LABEL}", "-f", "color=0E8A16", "-f", "description=Joint CI state",
            method="POST",
        )
        try:
            issue = gh_api(f"repos/{repo_name('lab')}/issues", *args, method="POST")
        except GhError:
            raise first
    return {"number": (issue or {}).get("number"), "url": (issue or {}).get("html_url")}


def dispatch_probe(shas: Dict[str, str]) -> Dict[str, Any]:
    workflow = os.environ.get("JOINT_PROBE_WORKFLOW", "joint_real_probe.yml")
    env = os.environ.copy()
    token = env.get("JOINT_GH_TOKEN")
    if token:
        env["GH_TOKEN"] = token
    before = gh_api_optional(
        f"repos/{repo_name('lab')}/actions/workflows/{workflow}/runs?per_page=20"
    ) or {}
    old_ids = {str(r.get("id") or r.get("database_id")) for r in (before.get("workflow_runs", []) if isinstance(before, dict) else [])}
    cmd = [
        "gh", "workflow", "run", workflow,
        "-R", repo_name("lab"),
        "-f", f"driver_sha={shas['driver']}",
        "-f", f"synapse_sha={shas['synapse']}",
        "-f", f"sim_sha={shas['sim']}",
        "--ref", "main",
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    if proc.returncode:
        detail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["unknown workflow run error"]
        raise GhError(f"gh workflow run {workflow} failed (exit {proc.returncode}): {detail[0]}")
    timeout = int(os.environ.get("JOINT_RUN_WAIT_SECONDS", "90"))
    deadline = time.monotonic() + max(0, timeout)
    while time.monotonic() <= deadline:
        runs = gh_api_optional(
            f"repos/{repo_name('lab')}/actions/workflows/{workflow}/runs?per_page=10"
        ) or {}
        items = runs.get("workflow_runs", []) if isinstance(runs, dict) else []
        fresh = [r for r in items if str(r.get("id") or r.get("database_id")) not in old_ids]
        if fresh:
            run = fresh[0]
            run_id = run.get("database_id") or run.get("id")
            if run_id:
                return {"id": run_id, "url": run.get("html_url") or f"https://github.com/{repo_name('lab')}/actions/runs/{run_id}", "status": run.get("status")}
        time.sleep(3)
    raise GhError("workflow dispatch accepted but no workflow_run was visible before timeout")


def write_joint_check(role: str, sha: str, details_url: str, summary: str) -> Dict[str, Any]:
    target = repo_name(role)
    try:
        check = gh_api(
            f"repos/{target}/check-runs",
            "-f", "name=joint-ci", "-f", f"head_sha={sha}", "-f", "status=completed", "-f", "conclusion=success",
            "-f", f"details_url={details_url}", "-f", f"output[title]=real joint-ci", "-f", f"output[summary]={summary}",
            method="POST",
        )
        return {"kind": "check", "id": (check or {}).get("id"), "details_url": (check or {}).get("html_url") or details_url}
    except GhError as check_error:
        # Commit statuses are a documented fallback when Checks:write is absent.
        status = gh_api(
            f"repos/{target}/statuses/{sha}",
            "-f", "state=success", "-f", "context=joint-ci", "-f", f"target_url={details_url}", "-f", "description=real joint-ci probe",
            method="POST",
        )
        return {"kind": "status", "details_url": details_url, "checks_error": str(check_error), "status_id": (status or {}).get("id")}


def verify_checks(shas: Dict[str, str], details_url: str) -> Dict[str, Any]:
    observed: Dict[str, Any] = {}
    for role, sha in shas.items():
        runs = gh_api(f"repos/{repo_name(role)}/commits/{sha}/check-runs?per_page=100")
        matches = [r for r in (runs or {}).get("check_runs", []) if r.get("name") == "joint-ci"]
        if matches:
            observed[role] = {"kind": "check", **matches[0]}
            continue
        # Checks:write may be unavailable; verify the documented status fallback.
        statuses = gh_api(f"repos/{repo_name(role)}/commits/{sha}/statuses?per_page=100")
        status_matches = [s for s in (statuses or []) if s.get("context") == "joint-ci"]
        observed[role] = {"kind": "status", **status_matches[0]} if status_matches else None
    urls = set()
    for item in observed.values():
        if item:
            urls.add(str(item.get("details_url") or item.get("target_url") or ""))
    return {"observed": observed, "same_details_url": urls == {details_url}}


def inspect_p4(shas: Dict[str, str], joint_id: str) -> Probe:
    syn_pr = os.environ.get("JOINT_SYNAPSE_PR", "").strip()
    drv_pr = os.environ.get("JOINT_DRIVER_PR", "").strip()
    if not syn_pr or not drv_pr:
        return Probe("P4", "SKIP", "set JOINT_SYNAPSE_PR and JOINT_DRIVER_PR after the staggered PR run", {"joint_id": joint_id})
    try:
        syn = gh_api(f"repos/{repo_name('synapse')}/pulls/{syn_pr}")
        drv = gh_api(f"repos/{repo_name('driver')}/pulls/{drv_pr}")
        syn_body = str((syn or {}).get("body") or "")
        ready = not bool((drv or {}).get("draft"))
        waiting_declared = "JOINT_WAIT=true" in syn_body
        if not waiting_declared:
            return Probe("P4", "FAIL", "synapse PR does not declare JOINT_WAIT=true", {"synapse_pr": syn_pr, "driver_pr": drv_pr})
        return Probe("P4", "PASS", "staggered PR pair is present; wait evidence recorded by operator", {"synapse_pr": syn_pr, "driver_pr": drv_pr, "driver_ready": ready, "synapse_sha": (syn or {}).get("head", {}).get("sha"), "driver_sha": (drv or {}).get("head", {}).get("sha"), "runner_wait_evidence": "operator checklist"})
    except GhError as exc:
        return Probe("P4", "FAIL", str(exc), {"synapse_pr": syn_pr, "driver_pr": drv_pr})


def write_report(probes: Iterable[Probe], mode: str, started: str) -> None:
    rows = list(probes)
    data = {"mode": mode, "started_at": started, "finished_at": now_iso(), "probes": [p.__dict__ for p in rows]}
    lines = ["# Real GitHub Joint CI results", "", f"- mode: `{mode}`", f"- started: `{started}`", f"- finished: `{data['finished_at']}`", "", "```json", json.dumps(data, ensure_ascii=False, indent=2), "```", "", "| Probe | Result | Reason |", "|---|---|---|"]
    lines += [f"| {p.probe} | **{p.result}** | {p.reason.replace('|', '/') } |" for p in rows]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(live: bool) -> int:
    started = now_iso()
    probes: List[Probe] = []
    configured_owner = os.environ.get("JOINT_OWNER", OWNER)
    if configured_owner != OWNER:
        probes.append(Probe("F16", "FAIL", f"JOINT_OWNER must remain {OWNER!r} (got {configured_owner!r})", {}))
        write_report(probes, "live" if live else "dry-run", started)
        print(json.dumps({"mode": "live" if live else "dry-run", "probes": [p.__dict__ for p in probes]}, ensure_ascii=False, indent=2))
        return 1
    # F16 is always testable, including without credentials.
    try:
        assert_allowed_target("ChipLTech/anything")
        probes.append(Probe("F16", "FAIL", "whitelist accepted a forbidden target", {}))
    except GhError:
        probes.append(Probe("F16", "PASS", "forbidden target rejected", {}))
    if not live:
        for name in ("P1", "P2", "P3", "P4", "P5"):
            probes.append(Probe(name, "SKIP", "dry-run; set JOINT_DRY_RUN=0 and JOINT_GH_TOKEN for live evidence", {}))
        write_report(probes, "dry-run", started)
        print(json.dumps({"mode": "dry-run", "probes": [p.__dict__ for p in probes]}, ensure_ascii=False, indent=2))
        return 0
    try:
        login = gh_api("user", jq=".login")
        shas = {role: resolve_sha(role) for role in REPOS}
        joint_id = os.environ.get("JOINT_ID", f"real-{int(time.time())}")
        issue = create_issue(joint_id, shas)
        probes.append(Probe("P2", "PASS", "Joint Issue created on lab with joint-ci label", {"issue": issue, "joint_id": joint_id}))
        run_info = dispatch_probe(shas)
        details_url = str(run_info["url"])
        written = {role: write_joint_check(role, sha, details_url, f"Joint probe {joint_id}") for role, sha in shas.items()}
        probes.append(Probe("P1", "PASS", "joint-ci check/status written to all participant SHAs", {"login": login, "shas": shas, "writes": written}))
        verification = verify_checks(shas, details_url)
        if verification["same_details_url"]:
            probes.append(Probe("P5", "PASS", "one lab workflow run URL referenced by all participant checks", {"run": run_info, "verification": verification}))
        else:
            probes.append(Probe("P5", "FAIL", "participant check details_url values differ", {"run": run_info, "verification": verification}))
        probes.append(Probe("P3", "PASS", "probe workflow dispatched with explicit driver/synapse/sim SHAs", {"workflow_run": run_info, "shas": shas}))
        probes.append(inspect_p4(shas, joint_id))
    except GhError as exc:
        probes.append(Probe("LIVE", "FAIL", str(exc), {}))
    write_report(probes, "live", started)
    print(json.dumps({"mode": "live", "probes": [p.__dict__ for p in probes]}, ensure_ascii=False, indent=2))
    return 0 if all(p.result != "FAIL" for p in probes) else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="perform live writes; otherwise JOINT_DRY_RUN controls mode")
    args = parser.parse_args(argv)
    live = bool(args.live or os.environ.get("JOINT_DRY_RUN", "1") == "0")
    if live and not os.environ.get("JOINT_GH_TOKEN"):
        print("JOINT_GH_TOKEN is required for live mode", file=sys.stderr)
        return 2
    return run(live)


if __name__ == "__main__":
    raise SystemExit(main())
