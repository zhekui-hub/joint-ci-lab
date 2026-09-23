"""Author: zhekui. Execute admitted argv, record real artifacts and reap own group.

execute: claim before spawning, monitor cancellation, kill only owned processes;
drain: bounded parallel workers. Supported runtime: POSIX, local SQLite disk.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import uuid

from .coordinator import Coordinator


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def execute(db_path, task_id, root, timeout=30):
    if os.name != "posix":
        raise RuntimeError("worker requires POSIX process groups")
    c = Coordinator(db_path)
    ticket = c.claim(task_id, str(uuid.uuid4()))
    if ticket is None:
        return {"task": task_id, "admitted": False}
    directory = Path(root).resolve() / task_id
    log = directory / "stdout.log"
    artifact = directory / "artifact.json"
    proc = None
    timed_out = cancelled = False
    started = time.monotonic()
    result = dict(task=task_id, owner=ticket["owner"], snapshot=ticket["snapshot"],
                  exit_code=None, execution_observed=False, artifact_sha256=None)
    try:
        directory.mkdir(parents=True, exist_ok=False)
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")}
        # Commands and this environment are operator-owned suite config, never PR text.
        env.update(ticket["spec"].get("env", {}))
        env.update(JOINT_ARTIFACT=str(artifact), JOINT_SNAPSHOT=ticket["snapshot"],
                   JOINT_TASK=task_id)
        with log.open("wb") as stream:
            proc = subprocess.Popen(ticket["spec"]["command"], cwd=directory,
                                    env=env, stdout=stream, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            result["execution_observed"] = True
            result["pid"] = proc.pid
            while proc.poll() is None:
                cancelled = not c.is_current(ticket) or (Path(root).resolve().parent / ".abort").exists()
                timed_out = time.monotonic() - started > timeout
                if cancelled or timed_out:
                    break
                time.sleep(0.025)
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
            # Also terminate children left behind after the direct process exits.
            if _group_alive(proc.pid):
                os.killpg(proc.pid, signal.SIGKILL)
            result["exit_code"] = proc.returncode
        if artifact.is_file() and not artifact.is_symlink():
            payload = json.loads(artifact.read_text())
            if payload == {"snapshot": ticket["snapshot"], "task": task_id, "completed": True}:
                result["artifact_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        clean = proc is None or not _group_alive(proc.pid)
        result.update(timed_out=timed_out, cancelled=cancelled,
                      duration=time.monotonic() - started, clean=clean)
        if timed_out or cancelled:
            result["exit_code"] = -1
        result["accepted"] = c.finish(ticket, result, clean=clean)
        if directory.is_dir():
            (directory / "observed.json").write_text(json.dumps(result, indent=2) + "\n")
    return dict(result, admitted=True)


def drain(db_path, root, jobs=2, timeout=30, deadline=None):
    if not 1 <= jobs <= 32 or timeout <= 0:
        raise ValueError("jobs must be 1..32 and timeout positive")
    c = Coordinator(db_path)
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        while pending := c.pending():
            # Submit only bounded batches; do not materialize one Future per backlog item.
            progressed = False
            for offset in range(0, len(pending), jobs):
                if deadline is not None and time.monotonic() >= deadline:
                    return results
                budget = timeout if deadline is None else min(timeout, max(.01, deadline - time.monotonic()))
                futures = [pool.submit(execute, db_path, t["id"], root, budget)
                           for t in pending[offset:offset + jobs]]
                batch = [f.result() for f in futures]
                accepted = [r for r in batch if r["admitted"]]
                results.extend(accepted)
                progressed |= bool(accepted)
            if not progressed:
                break  # Other controller/worker owns capacity; no long-lived busy wait.
    return results
