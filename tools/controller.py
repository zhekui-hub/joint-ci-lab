#!/usr/bin/env python3
# Author: zhekui. One-shot controller for event relay and scheduled compensation.
# main: refresh live lab inputs, map native events, optionally publish statuses.
# This command never executes PR code, mutates branch rules, or merges a PR.
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cloud_joint.controller import Controller
from cloud_joint.github import GitHub


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--apply-lab-status", action="store_true")
    p.add_argument("--event", help="untrusted repo/run_id hint; live run is re-read")
    args = p.parse_args()
    config = json.loads(Path(args.config).read_text())
    api = GitHub([r["repo"] for r in config["repositories"]], writable=args.apply_lab_status)
    c = Controller(args.db, api, config)
    hint = json.loads(Path(args.event).read_text()) if args.event else None
    result = c.cycle(hint, publish=args.apply_lab_status)
    print(json.dumps(result, indent=2))
    return 2 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
