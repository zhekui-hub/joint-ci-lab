"""Plan-ops helper executed inside joint-ci scheduler job (has JOINT_GH_TOKEN)."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List


def _find_request() -> Path | None:
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        cand = parent / "plan-ops" / "request.json"
        if cand.exists():
            return cand
        if (parent / ".git").exists():
            break
    return None


def _gh(args: List[str], input_text: str | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    token = env.get("JOINT_GH_TOKEN") or env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")
    if token:
        env["GH_TOKEN"] = token
    p = subprocess.run(["gh", *args], input=input_text, text=True, capture_output=True, env=env)
    if p.returncode:
        raise RuntimeError(f"gh {args[:4]} failed: {(p.stderr or p.stdout or '')[-500:]}")
    return p


def maybe_run_plan_ops() -> Dict[str, Any] | None:
    path = _find_request()
    if path is None:
        return None
    req = json.loads(path.read_text(encoding="utf-8"))
    op = req.get("op")
    if not op or op == "noop":
        return {"skipped": True, "op": op}
    evidence: Dict[str, Any] = {"op": op, "results": []}
    if op == "write_statuses":
        for item in req.get("statuses", []):
            owner_repo = item["repo"]
            sha = item["sha"]
            state = item["state"]
            context = item.get("context", "joint-ci")
            desc = item.get("description", "plan-ops")
            target = item.get("target_url", "https://github.com/zhekui-hub/joint-ci-lab")
            p = _gh([
                "api", f"repos/{owner_repo}/statuses/{sha}",
                "-f", f"state={state}", "-f", f"context={context}",
                "-f", f"description={desc}", "-f", f"target_url={target}",
            ])
            evidence["results"].append({"repo": owner_repo, "sha": sha, "state": state, "out": (p.stdout or "")[:300]})
    elif op == "dispatch_reports":
        joint_id = req["joint_id"]
        reports = req["reports"]
        mode = req.get("mode", "dual")
        if mode == "single_each":
            for r in reports:
                payload = {"event_type": "joint_ci_report", "client_payload": {"joint_id": joint_id, **r}}
                _gh(["api", "repos/zhekui-hub/joint-ci-lab/dispatches", "--input", "-"], input_text=json.dumps(payload))
                evidence["results"].append({"dispatched": r.get("repo")})
                time.sleep(1)
        else:
            payload = {
                "event_type": "joint_ci_report",
                "client_payload": {"joint_id": joint_id, "reports": reports, "schema_version": 1},
            }
            _gh(["api", "repos/zhekui-hub/joint-ci-lab/dispatches", "--input", "-"], input_text=json.dumps(payload))
            evidence["results"].append({"dispatched": "dual", "n": len(reports)})
    else:
        evidence["error"] = f"unknown op {op}"
    req["op"] = "noop"
    req["consumed_by"] = evidence
    path.write_text(json.dumps(req, indent=2) + "\n", encoding="utf-8")
    (path.parent / "LAST_EVIDENCE.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print("[plan-ops]", json.dumps(evidence))
    return evidence
