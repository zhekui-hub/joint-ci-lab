#!/usr/bin/env python3
# Author: zhekui. Read-only isolated GitHub inventory and association preview.
# main: query live lab PRs and checks, persist proposals; never publish statuses.
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cloud_joint.adapter import associate, inventory
from cloud_joint.github import GitHub


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    config = json.loads(Path(args.config).read_text())
    api = GitHub([r["repo"] for r in config["repositories"]])
    data = inventory(api, config)
    output = Path(args.out)
    if output.exists():
        raise ValueError("refusing overwrite")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(dict(inventory=data, proposals=associate(data),
        mode="READ_ONLY_LAB_SHADOW", approvals="unverified; merge_ready must remain false"), indent=2) + "\n")


if __name__ == "__main__":
    main()
