#!/usr/bin/env python3
# Author: zhekui. Read-only cloud readiness, resource budget and optional lab API probe.
# main: emit evidence/assumption/missing fields without secrets or production writes.
import argparse
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cloud_joint.github import GitHub
from acceptance import preflight


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--lab-repo", action="append", default=[])
    args = p.parse_args()
    result = {"local": preflight(), "resources": {"cpus": os.cpu_count(),
              "disk_free_bytes": shutil.disk_usage(ROOT).free}, "lab": [],
              "production": "not_authorized", "secrets_recorded": False}
    if Path("/proc/meminfo").exists():
        result["resources"]["memory_available_kib"] = next(
            (int(x.split()[1]) for x in Path("/proc/meminfo").read_text().splitlines()
             if x.startswith("MemAvailable:")), None)
    api = GitHub(args.lab_repo)
    for repo in args.lab_repo:
        try:
            result["lab"].append(dict(status="evidence", **api.probe(repo)))
        except Exception as exc:
            result["lab"].append(dict(repo=repo, status="missing", reason=str(exc)))
    result["external_ready"] = bool(args.lab_repo) and all(x["status"] == "evidence" for x in result["lab"])
    result["note"] = "Runner inventory does not prove trusted required checks, real PR flow or Pod lifecycle."
    out = Path(args.out)
    if out.exists():
        raise ValueError("refusing to overwrite readiness")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
