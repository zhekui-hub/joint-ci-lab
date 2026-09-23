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
    proposals = c.sync()
    event = None
    if args.event:
        hint = json.loads(Path(args.event).read_text())
        if hint.get("run_id"):
            event = c.native_event(hint["repo"], hint["run_id"])
    print(json.dumps(dict(proposals=proposals, native_event=event,
                         statuses=c.publish() if args.apply_lab_status else [],
                         mode="LAB_STATUS_WRITE" if args.apply_lab_status else "READ_ONLY_SHADOW"), indent=2))


if __name__ == "__main__":
    main()
