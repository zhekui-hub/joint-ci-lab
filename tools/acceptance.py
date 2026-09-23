#!/usr/bin/env python3
# Author: zhekui. Portable sharded acceptance and strict evidence aggregation.
# main: preflight/run/merge/verify; run_case: isolated timeout and raw evidence;
# summarize: single canonical CSV; merge: reject mixed identities and duplicate IDs.
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cloud_joint.evidence import source_identity, validate_observation, verify_manifest
from cloud_joint.scenarios import EXTERNAL, Scenario


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def preflight():
    if sys.version_info < (3, 10) or os.name != "posix":
        raise RuntimeError("Python >=3.10 and POSIX required")
    identity, _ = source_identity(ROOT)
    if (ROOT / "SOURCE_MANIFEST.json").exists():
        verify_manifest(ROOT)
    return dict(source_id=identity, python=platform.python_version(), platform=platform.platform(),
                cpus=os.cpu_count(), runtime="local POSIX processes; no GitHub/Pod evidence", ready=True)


def run_case(number, output, timeout, identity, run_id):
    evidence_path = output / f"JC-{number:02}" / "evidence.json"
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
    start = time.monotonic()
    try:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "case", str(number),
                                 "--out", str(output / f"JC-{number:02}"),
                                 "--source-id", identity, "--run-id", run_id],
                                cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            case_root = output / f"JC-{number:02}"
            case_root.mkdir(exist_ok=True)
            (case_root / ".abort").touch()
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                stdout, stderr = proc.communicate()
            (output / f"JC-{number:02}.log").write_text(stdout + stderr)
            raise
        (output / f"JC-{number:02}.log").write_text(stdout + stderr)
        evidence = json.loads(evidence_path.read_text()) if evidence_path.is_file() else None
        passed = proc.returncode == 0 and evidence is not None and validate_observation(evidence)
        execution = "COMPLETED" if evidence else "SETUP_FAILED"
        reason = (evidence or {}).get("error") or ("" if passed else "assertion/process/evidence failure")
    except subprocess.TimeoutExpired:
        passed, execution, reason = False, "SETUP_FAILED", "case timeout; inspect owned process cleanup"
    external = EXTERNAL.get(number, [])
    return dict(case_id=f"JC-{number:02}", local_verdict="PASS" if passed else "FAIL",
                verdict=("NOT_EVALUATED" if external else "PASS") if passed else
                        ("FAIL" if execution == "COMPLETED" else "NOT_EVALUATED"),
                execution_status=execution, evidence_class="LOCAL_REAL_SUT",
                external_required=external, reason=reason,
                evidence=f"JC-{number:02}/evidence.json", duration=time.monotonic() - start)


def summarize(output, rows, identity, run_id, complete):
    rows = sorted(rows, key=lambda r: r["case_id"])
    local = {s: sum(r["local_verdict"] == s for r in rows) for s in ("PASS", "FAIL")}
    full = {s: sum(r["verdict"] == s for r in rows) for s in ("PASS", "FAIL", "NOT_EVALUATED")}
    summary = dict(run_id=run_id, source_id=identity, total=len(rows), complete_inventory=complete,
                   local=local, acceptance=full, external_tests_executed=False,
                   warning="Local assertions are not full GitHub/Pod/production acceptance.")
    write_json(output / "results.json", dict(summary=summary, cases=rows))
    write_json(output / "summary.json", summary)
    fields = list(rows[0]) if rows else []
    with (output / "results-all.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            writer.writerow(dict(r, external_required=json.dumps(r["external_required"])))
    (output / "TEST_REPORT.md").write_text(
        f"# Joint CI acceptance\n\nRun: `{run_id}`\nSource: `{identity}`\n\n"
        f"Local assertions: {local}\n\nFull specified scope: {full}\n\n"
        "External requirements remain NOT_EVALUATED until real evidence is collected. "
        "Historical 120-case results are not part of this run.\n")
    return summary


def run(args):
    ready = preflight()
    output = Path(args.out).resolve()
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "preflight.json", ready)
    index, count = map(int, args.shard.split("/"))
    if not (1 <= index <= count <= 44) or not (1 <= args.jobs <= 16):
        raise ValueError("shard 1/N..N/N, N<=44; jobs 1..16")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", args.run_id):
        raise ValueError("unsafe run id")
    numbers = [n for n in range(1, 45) if (n - 1) % count == index - 1]
    rows = []
    with concurrent.futures.ThreadPoolExecutor(args.jobs) as pool:
        futures = {pool.submit(run_case, n, output, args.timeout, ready["source_id"], args.run_id): n for n in numbers}
        for future in concurrent.futures.as_completed(futures):
            row = future.result()
            rows.append(row)
            write_json(output / "checkpoint.json", dict(run_id=args.run_id, source_id=ready["source_id"],
                       completed=sorted(r["case_id"] for r in rows), shard=args.shard))
            print(row["case_id"], row["local_verdict"], row["reason"], flush=True)
    if source_identity(ROOT)[0] != ready["source_id"]:
        raise RuntimeError("source changed during acceptance; candidate invalid")
    summary = summarize(output, rows, ready["source_id"], args.run_id, count == 1)
    write_json(output / "shard.json", dict(index=index, count=count, cases=numbers))
    return 0 if summary["local"]["FAIL"] == 0 else 1


def merge(args):
    import shutil
    output = Path(args.out).resolve()
    output.mkdir(parents=True, exist_ok=False)
    identity = source_identity(ROOT)[0]
    rows, seen, runs = [], set(), set()
    for folder in args.inputs:
        if Path(folder).is_symlink():
            raise ValueError("linked shard root")
        root = Path(folder).resolve()
        reject_links(root)
        report = json.loads((root / "results.json").read_text())
        if report["summary"]["source_id"] != identity:
            raise ValueError("mixed source identities")
        verify(root)
        runs.add(report["summary"]["run_id"])
        for row in report["cases"]:
            cid = row["case_id"]
            if not re.fullmatch(r"JC-(0[1-9]|[1-3][0-9]|4[0-4])", cid) or cid in seen:
                raise ValueError("duplicate/unexpected case: " + cid)
            evidence = root / row["evidence"]
            if not evidence.resolve().is_relative_to(root) or evidence.is_symlink():
                raise ValueError("unsafe evidence path")
            if row["local_verdict"] == "PASS":
                if not evidence.is_file() or not validate_observation(json.loads(evidence.read_text())):
                    raise ValueError("PASS without valid observations: " + cid)
            seen.add(cid)
            if (root / cid).exists():
                shutil.copytree(root / cid, output / cid, symlinks=False)
            if (root / (cid + ".log")).exists():
                shutil.copyfile(root / (cid + ".log"), output / (cid + ".log"))
            rows.append(row)
    if len(runs) != 1 or seen != {f"JC-{n:02}" for n in range(1, 45)}:
        raise ValueError("mixed run IDs or incomplete 44-case inventory")
    summary = summarize(output, rows, identity, runs.pop(), True)
    verify(output)
    return 0 if summary["local"]["FAIL"] == 0 else 1


def reject_links(root):
    for path in Path(root).rglob("*"):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("symlink/escape in evidence tree")


def verify(root):
    if Path(root).is_symlink():
        raise ValueError("linked evidence root")
    root = Path(root).resolve()
    reject_links(root)
    report = json.loads((root / "results.json").read_text())
    rows = report["cases"]
    if len({r["case_id"] for r in rows}) != len(rows):
        raise ValueError("duplicate cases")
    if report["summary"]["source_id"] != source_identity(ROOT)[0]:
        raise ValueError("source identity differs from verifier")
    if report["summary"]["complete_inventory"] and {r["case_id"] for r in rows} != {f"JC-{n:02}" for n in range(1, 45)}:
        raise ValueError("incomplete full inventory")
    for r in rows:
        if not re.fullmatch(r"JC-(0[1-9]|[1-3][0-9]|4[0-4])", r["case_id"]):
            raise ValueError("unknown case")
        if r["external_required"] != EXTERNAL.get(int(r["case_id"][3:]), []):
            raise ValueError("canonical external requirements changed")
        if r["evidence"] != r["case_id"] + "/evidence.json":
            raise ValueError("evidence path does not match case")
        if r["local_verdict"] == "PASS":
            path = root / r["evidence"]
            observed = json.loads(path.read_text())
            if (observed.get("case_id") != r["case_id"] or observed.get("source_id") != report["summary"]["source_id"]
                    or observed.get("run_id") != report["summary"]["run_id"]):
                raise ValueError("case/source/run evidence identity mismatch")
            if not path.resolve().is_relative_to(root) or not validate_observation(observed):
                raise ValueError("independent recompute mismatch")
            for task in observed["state"]["tasks"]:
                if task["status"] == "success":
                    artifact = path.parent / "workers" / task["id"] / "artifact.json"
                    if not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest() != task["result"]["artifact_sha256"]:
                        raise ValueError("raw artifact missing/hash mismatch")
        expected = ("NOT_EVALUATED" if r["external_required"] else "PASS") if r["local_verdict"] == "PASS" else (
            "FAIL" if r["execution_status"] == "COMPLETED" else "NOT_EVALUATED")
        if r["verdict"] != expected:
            raise ValueError("scope promoted without external evidence")
    with (root / "results-all.csv").open(newline="") as f:
        csv_rows = list(csv.DictReader(f))
    if len(csv_rows) != len(rows) or any(c["case_id"] != r["case_id"] or c["verdict"] != r["verdict"] for c, r in zip(csv_rows, rows)):
        raise ValueError("CSV/JSON mismatch")
    summary = report["summary"]
    expected_local = {v: sum(r["local_verdict"] == v for r in rows) for v in ("PASS", "FAIL")}
    expected_full = {v: sum(r["verdict"] == v for r in rows) for v in ("PASS", "FAIL", "NOT_EVALUATED")}
    if summary["local"] != expected_local or summary["acceptance"] != expected_full or summary["total"] != len(rows):
        raise ValueError("summary mismatch")
    if json.loads((root / "summary.json").read_text()) != summary:
        raise ValueError("duplicate summary mismatch")
    write_json(root / "independent-recompute.json", dict(mismatch=0, cases=len(rows),
               scope="assertion equality, audit admission uniqueness, result identity, CSV/JSON counts"))
    return 0


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    r = sub.add_parser("run")
    r.add_argument("--out", required=True)
    r.add_argument("--run-id", required=True)
    r.add_argument("--shard", default="1/1")
    r.add_argument("--jobs", type=int, default=2)
    r.add_argument("--timeout", type=float, default=45)
    c = sub.add_parser("case")
    c.add_argument("number", type=int, choices=range(1, 45))
    c.add_argument("--out", required=True)
    c.add_argument("--source-id")
    c.add_argument("--run-id", default="standalone")
    m = sub.add_parser("merge")
    m.add_argument("inputs", nargs="+")
    m.add_argument("--out", required=True)
    v = sub.add_parser("verify")
    v.add_argument("root")
    args = parser.parse_args()
    if args.command == "preflight":
        print(json.dumps(preflight(), indent=2))
        return 0
    if args.command == "case":
        identity = source_identity(ROOT)[0]
        if args.source_id and args.source_id != identity:
            raise ValueError("source changed before case execution")
        evidence = Scenario(args.out).run(args.number)
        evidence.update(source_id=identity, run_id=args.run_id)
        write_json(Path(args.out) / "evidence.json", evidence)
        return 0 if validate_observation(evidence) else 1
    if args.command == "run":
        return run(args)
    if args.command == "merge":
        return merge(args)
    return verify(args.root)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        sys.exit(2)
