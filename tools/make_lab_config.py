#!/usr/bin/env python3
# Author: zhekui. Generate isolated four-repository smoke configuration.
# main: write trusted argv and explicit private-check scopes; no remote mutations.
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cloud_joint.fixtures import SMOKE
from cloud_joint.github import GitHub


def main():
    p = argparse.ArgumentParser()
    for role in ("arsenal", "driver", "synapse", "sim"):
        p.add_argument("--" + role, required=True, help="owner/joint-ci-* isolated repository")
    p.add_argument("--check-app-id", type=int, required=True, help="verified App id producing lab-private")
    p.add_argument("--out", required=True)
    args = p.parse_args()
    repos = [getattr(args, r) for r in ("arsenal", "driver", "synapse", "sim")]
    api = GitHub(repos)
    roles = {"arsenal": "ARSENAL_BRANCH", "driver": "DLC_KERNEL_DRIVER_BRANCH",
             "synapse": "DLC_SYNAPSE_BRANCH", "sim": "DLC_SIM_BRANCH"}
    config = dict(policy_version="lab-smoke-v1", environment={"runtime": "local-python-smoke"}, repositories=[])
    for role, field in roles.items():
        repo = getattr(args, role)
        info = api.request(repo, "")
        workflows = api.pages(repo, "actions/workflows", "workflows")
        native = [w["id"] for w in workflows if w["name"] == "lab-private"]
        if len(native) != 1:
            raise ValueError("exactly one installed lab-private workflow required in " + repo)
        config["repositories"].append(dict(repo=repo, field=field, default_branch=info["default_branch"],
            required_app_checks={"lab-private": args.check_app_id}, private_scopes={"lab-private": "head"},
            approval_policy="required", aggregate_name="joint-ci-cloud", native_workflow_ids=native,
            tasks=[dict(suite=role + "-public", params={}, env={}, command=["python3", "-c", SMOKE])]))
    output = Path(args.out)
    with output.open("x") as stream:
        json.dump(config, stream, indent=2)
    print("Config written. Approval policy defaults required; verify actual lab branch rules before changing it.")


if __name__ == "__main__":
    main()
