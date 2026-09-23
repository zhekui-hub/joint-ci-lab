#!/usr/bin/env python3
# Author: zhekui. Configurable real-process capacity/fairness stress experiment.
# main: bounded concurrent registration, drain, independently calculate queue and
# execution metrics. Synthetic versions and local-process evidence only.
from __future__ import annotations

import argparse
import concurrent.futures
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cloud_joint.coordinator import Coordinator
from cloud_joint.evidence import source_identity
from cloud_joint.fixtures import plan
from cloud_joint.worker import drain


def percentile(values, fraction):
    return sorted(values)[min(len(values) - 1, int((len(values) - 1) * fraction))] if values else 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--groups", type=int, default=20)
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--controllers", type=int, default=4)
    p.add_argument("--per-group", type=int, default=2)
    p.add_argument("--delay", type=float, default=.03)
    p.add_argument("--max-seconds", type=float, default=300)
    args = p.parse_args()
    if not (1 <= args.groups <= 2000 and 1 <= args.jobs <= 32 and 1 <= args.controllers <= 32
            and 1 <= args.per_group <= args.jobs and 0 <= args.delay <= 10):
        raise ValueError("capacity bounds violated")
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    c = Coordinator(out / "state.sqlite")
    c.store.configure(max_running=args.jobs, per_group=args.per_group,
                      max_groups=args.groups, max_queued=args.groups * 4)
    source = source_identity(ROOT)[0]
    begun = time.monotonic()
    latencies = []
    def register(n):
        start = time.monotonic()
        gid = Coordinator(out / "state.sqlite").reconcile(plan(n + 1, args.delay))
        return gid, time.monotonic() - start
    with concurrent.futures.ThreadPoolExecutor(args.controllers) as pool:
        registered = list(pool.map(register, range(args.groups)))
    groups, latencies = zip(*registered)
    registrations_done = time.monotonic()
    drain(out / "state.sqlite", out / "workers", args.jobs, timeout=15, deadline=begun + args.max_seconds)
    duration = time.monotonic() - begun
    state = c.store.export()
    events = []
    waits = []
    for task in state["tasks"]:
        if "started_at" in task and "finished_at" in task:
            events.extend([(task["started_at"], 1, task["group"]),
                           (task["finished_at"], -1, task["group"])])
            waits.append(task["started_at"] - task["queued_at"])
    active, peak, per_group_peak = 0, 0, 0
    group_active = {}
    for _, delta, gid in sorted(events):
        active += delta
        group_active[gid] = group_active.get(gid, 0) + delta
        peak = max(peak, active)
        per_group_peak = max(per_group_peak, group_active[gid])
    claim_ids = [json.loads(a["body"])["task"] for a in state["audit"] if a["kind"] == "claim"]
    success = sum(c.summary(g)["merge_ready"] for g in groups)
    metrics = dict(source_id=source, evidence_class="LOCAL_REAL_SUT", groups=args.groups,
                   tasks=len(state["tasks"]), controllers=args.controllers, workers=args.jobs,
                   duration=duration, registration_seconds=registrations_done - begun,
                   registration_p50=percentile(latencies, .5), registration_p95=percentile(latencies, .95),
                   registration_p99=percentile(latencies, .99), queue_p95=percentile(waits, .95),
                   queue_max=max(waits, default=0), tasks_per_second=len(claim_ids) / duration,
                   actual_peak=peak, per_group_peak=per_group_peak, duplicates=len(claim_ids) - len(set(claim_ids)),
                   completed_groups=success, pending=len(c.pending()),
                   unclean=sum(not t["clean"] for t in state["tasks"]),
                   database_bytes=(out / "state.sqlite").stat().st_size)
    metrics["passed"] = (success == args.groups and metrics["duplicates"] == 0 and metrics["unclean"] == 0
                         and peak <= args.jobs and per_group_peak <= args.per_group
                         and duration <= args.max_seconds and source_identity(ROOT)[0] == source)
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    (out / "state.json").write_text(json.dumps(state, indent=2) + "\n")
    print(json.dumps(metrics, indent=2))
    return 0 if metrics["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
