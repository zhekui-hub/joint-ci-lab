"""Author: zhekui. Operator CLI for portable experiments (not a user PR workflow).

main: reconcile trusted plans, execute admitted workers, cancel/retry or inspect.
"""
import argparse
import json
from pathlib import Path

from .coordinator import Coordinator
from .model import discover
from .worker import drain


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    rec = sub.add_parser("reconcile")
    rec.add_argument("plan")
    disc = sub.add_parser("discover")
    disc.add_argument("inventory", help="trusted live reports, fields, defaults")
    work = sub.add_parser("work")
    work.add_argument("--out", required=True)
    work.add_argument("--jobs", type=int, default=2)
    work.add_argument("--timeout", type=float, default=30)
    for action in ("summary", "cancel", "retry"):
        cmd = sub.add_parser(action)
        cmd.add_argument("group")
        if action != "summary":
            cmd.add_argument("--event-id", required=True)
            cmd.add_argument("--generation", type=int, required=True)
            cmd.add_argument("--attempt", type=int, required=True)
        if action in ("cancel", "retry"):
            cmd.add_argument("--consumer")
    sub.add_parser("export")
    cap = sub.add_parser("capacity")
    for name in ("max-running", "per-group", "max-queued", "max-groups"):
        cap.add_argument("--" + name, type=int)
    args = parser.parse_args()
    c = Coordinator(args.db)
    if args.action == "reconcile":
        result = {"group": c.reconcile(json.loads(Path(args.plan).read_text()))}
    elif args.action == "discover":
        result = discover(**json.loads(Path(args.inventory).read_text()))
        if result["mode"] == "joint":
            result["group"] = c.reconcile(result.pop("plan"))
    elif args.action == "work":
        result = drain(args.db, args.out, args.jobs, args.timeout)
    elif args.action == "summary":
        result = c.summary(args.group)
    elif args.action == "cancel":
        c.cancel(args.group, args.event_id, args.consumer, (args.generation, args.attempt))
        result = c.summary(args.group)
    elif args.action == "retry":
        c.retry(args.group, args.event_id, (args.generation, args.attempt), args.consumer)
        result = c.summary(args.group)
    elif args.action == "capacity":
        limits = {k: v for k, v in vars(args).items() if k in
                  ("max_running", "per_group", "max_queued", "max_groups") and v is not None}
        c.store.configure(**limits)
        result = {"configured": limits}
    else:
        result = c.store.export()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
