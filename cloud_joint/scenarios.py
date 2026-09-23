"""Author: zhekui. Executable local assertions for JC-01..JC-44.

Scenario.run records inputs, state and assertions; external requirements remain
explicitly unverified. These tests execute the real Python coordinator, not the
historical coordinate.sh or production CI. Capacity stress has a separate entry.
"""
from __future__ import annotations

import concurrent.futures
import copy
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from .coordinator import Conflict, Coordinator
from .fixtures import FIELDS, REPOS, SMOKE, plan, reports
from .github import GitHub
from .model import discover, member_id, parse_dependencies, snapshot_id, validate_plan
from .worker import drain, execute


# Higher-layer obligations are never auto-passed by local assertions.
EXTERNAL = {
    1: ["REAL_RUNTIME"], 4: ["REAL_GITHUB"], 7: ["REAL_GITHUB"],
    8: ["REAL_GITHUB"], 9: ["REAL_GITHUB"], 10: ["REAL_GITHUB"],
    13: ["REAL_GITHUB"], 16: ["REAL_RUNTIME"], 17: ["REAL_GITHUB"],
    19: ["REAL_RUNTIME"], 21: ["REAL_GITHUB"], 23: ["REAL_GITHUB"],
    24: ["REAL_GITHUB"], 28: ["REAL_RUNTIME"], 29: ["REAL_GITHUB"],
    30: ["REAL_GITHUB"], 31: ["REAL_GITHUB"], 32: ["SHADOW"],
    33: ["REAL_GITHUB", "REAL_RUNTIME"], 36: ["REAL_GITHUB"],
    37: ["REAL_GITHUB"], 38: ["REAL_GITHUB"], 39: ["REAL_GITHUB"],
    40: ["REAL_GITHUB", "REAL_RUNTIME"], 41: ["REAL_GITHUB"],
    42: ["REAL_GITHUB"], 43: ["REAL_GITHUB"], 44: ["REAL_GITHUB"],
}


class Scenario:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=False)
        self.db = self.root / "state.sqlite"
        self.c = Coordinator(self.db)
        self.p = plan()
        self.assertions = []
        self.inputs = []

    def check(self, name, actual, expected):
        self.assertions.append(dict(name=name, actual=actual, expected=expected,
                                    passed=actual == expected))
        if actual != expected:
            raise AssertionError(f"{name}: {actual!r} != {expected!r}")

    def rejected(self, name, action, exception=(ValueError, Conflict, PermissionError)):
        try:
            action()
        except exception as exc:
            self.check(name, True, True)
            self.assertions[-1]["observed_exception"] = type(exc).__name__
        else:
            self.check(name, False, True)

    def sync(self, p=None):
        p = p or self.p
        self.inputs.append(copy.deepcopy(p))
        return self.c.reconcile(p)

    def run_workers(self):
        return drain(self.db, self.root / "workers", jobs=4, timeout=5)

    def run(self, number):
        error = None
        try:
            getattr(self, f"case_{number:02}")()
            if not self.assertions:
                raise AssertionError("scenario has no assertions")
        except Exception as exc:
            error = type(exc).__name__ + ": " + str(exc)
        state = self.c.store.export()
        evidence = dict(case_id=f"JC-{number:02}", inputs=self.inputs,
                        assertions=self.assertions, error=error, state=state,
                        summaries=[self.c.summary(g["id"]) for g in state["groups"]],
                        evidence_class="LOCAL_REAL_SUT", external_required=EXTERNAL.get(number, []))
        (self.root / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
        return evidence

    def case_01(self):
        broken = plan()
        del broken["versions"][REPOS[-1]]
        self.rejected("missing declared source rejected", lambda: self.sync(broken))
        self.check("no dispatch before inputs", len(self.c.pending()), 0)
        self.sync()
        self.check("arrival enqueues four lanes", len(self.c.pending()), 4)

    def case_02(self):
        for m in self.p["members"]:
            m.update(draft=True, private={})
        gid = self.sync()
        self.run_workers()
        self.check("Draft allows public", self.c.summary(gid)["public_success"], True)
        self.check("Draft blocks merge", self.c.summary(gid)["merge_ready"], False)

    def case_03(self):
        self.p["members"][0]["private"] = {}
        gid = self.sync()
        self.run_workers()
        self.check("private missing blocks", self.c.summary(gid)["merge_ready"], False)
        self.p["members"][0]["private"] = {"private": "success"}
        self.sync()
        self.check("private event reconciles", self.c.summary(gid)["merge_ready"], True)
        self.check("no public rerun", len(self.c.pending()), 0)

    def case_04(self):
        gid = self.sync()
        self.run_workers()
        for value in ["pending", "failure", "skipped", "neutral", None, "success"]:
            self.p["members"][0]["private"] = {"private": value}
            self.sync()
            self.check(f"required {value}", self.c.summary(gid)["merge_ready"], value == "success")

    def case_05(self):
        t = copy.deepcopy(self.p["tasks"][0])
        self.p["tasks"].append(t)
        self.check("identical tasks merge", len(validate_plan(self.p)["tasks"]), 4)
        t["params"] = {"cards": 16}
        self.check("different params preserved", len(validate_plan(self.p)["tasks"]), 5)
        t["params"] = {}
        t["env"] = {"MODE": "other"}
        self.check("different env preserved", len(validate_plan(self.p)["tasks"]), 5)

    def case_06(self):
        original = snapshot_id(validate_plan(self.p))
        self.p["tasks"].reverse()
        self.p["members"].reverse()
        self.check("canonical order", snapshot_id(validate_plan(self.p)), original)
        self.p["environment"]["hardware"] = "changed"
        self.check("semantic change invalidates", snapshot_id(validate_plan(self.p)) != original, True)

    def case_07(self):
        gid = self.sync()
        self.run_workers()
        s = self.c.summary(gid)
        self.check("four observed lanes", len(s["tasks"]), 4)
        self.check("four-lane success", s["merge_ready"], True)
        broken = plan()
        del broken["versions"][REPOS[-1]]
        self.rejected("missing Sim source", lambda: self.sync(broken))
        two = plan(2)
        two["members"] = two["members"][:2]
        for t in two["tasks"]:
            t["consumers"] = [member_id(m) for m in two["members"]]
        self.check("two PR with four dependencies", len(validate_plan(two)["members"]), 2)

    def case_08(self):
        barrier = threading.Barrier(8)
        def race(_):
            barrier.wait(timeout=5)
            return Coordinator(self.db).reconcile(self.p)
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            ids = list(pool.map(race, range(8)))
        self.check("one group", len(set(ids)), 1)
        self.check("unique tasks", len(self.c.pending()), 4)
        self.run_workers()
        claims = [r for r in self.c.store.export()["audit"] if r["kind"] == "claim"]
        self.check("four heavy admissions", len(claims), 4)

    def case_09(self):
        self.sync()
        tid = self.c.pending()[0]["id"]
        barrier = threading.Barrier(8)
        def claim(i):
            barrier.wait(timeout=5)
            return Coordinator(self.db).claim(tid, str(i))
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            tickets = list(pool.map(claim, range(8)))
        self.check("one admitted from eight entries", sum(t is not None for t in tickets), 1)

    def case_10(self):
        self.sync()
        tid = self.c.pending()[0]["id"]
        first = self.c.claim(tid, "response-lost-owner")
        second = self.c.claim(tid, "dispatch-retry")
        self.check("server accepted first", first is not None, True)
        self.check("lost-response retry not admitted", second, None)
        self.check("durable intent", tid in [i["id"] for i in self.c.store.export()["intents"]], True)

    def case_11(self):
        # Real child termination inside a transaction must roll back all rows.
        script = "from cloud_joint.store import Store;import os; s=Store(__import__('sys').argv[1]);\nwith s.transaction() as d:\n s.put(d,'groups','crash',{'bad':True});os._exit(17)"
        r = subprocess.run([sys.executable, "-c", script, str(self.db)], capture_output=True)
        self.check("injected crash", r.returncode, 17)
        self.check("no partial transaction", self.c.store.export()["groups"], [])
        gid = self.sync()
        self.check("restart recovers intent", len(Coordinator(self.db).pending()), 4)
        self.run_workers()
        self.check("restart converges", Coordinator(self.db).summary(gid)["merge_ready"], True)

    def case_12(self):
        gid = self.sync()
        rev = self.c.summary(gid)["revision"]
        self.c.reconcile(self.p, rev)
        self.rejected("stale writer conflict", lambda: self.c.reconcile(self.p, rev))
        new = self.c.summary(gid)["revision"]
        self.c.reconcile(self.p, new)
        self.check("failed writer reread retry", self.c.summary(gid)["revision"], new + 1)

    def case_13(self):
        gid = self.sync()
        self.run_workers()
        old = self.c.summary(gid)
        self.p["versions"][REPOS[1]]["head"] = "b" * 40
        self.sync()
        self.check("unchanged A invalidated", self.c.summary(gid)["merge_ready"], False)
        self.check("new generation", self.c.summary(gid)["generation"], old["generation"] + 1)

    def case_14(self):
        gid = self.sync()
        for field in ("base", "candidate"):
            old = self.c.summary(gid)["snapshot"]
            self.p["versions"][REPOS[0]][field] = "c" * 40
            self.sync()
            self.check(field + " invalidates", self.c.summary(gid)["snapshot"] != old, True)
        old = self.c.summary(gid)["snapshot"]
        self.p["environment"]["image"] = "sha256:" + "b" * 64
        self.sync()
        self.check("image invalidates", self.c.summary(gid)["snapshot"] != old, True)

    def case_15(self):
        gid = self.sync()
        t = self.c.claim(self.c.pending()[0]["id"], "old")
        self.p["versions"][REPOS[0]]["head"] = "d" * 40
        self.sync()
        result = dict(snapshot=t["snapshot"], task=t["id"], owner="old", exit_code=0,
                      artifact_sha256="a" * 64, execution_observed=True)
        self.check("old success rejected", self.c.finish(t, result, clean=True), False)
        self.check("current not green", self.c.summary(gid)["merge_ready"], False)

    def case_16(self):
        self.p = plan(delay=1)
        gid = self.sync()
        tid = self.c.pending()[0]["id"]
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            f = pool.submit(execute, self.db, tid, self.root / "workers", 4)
            deadline = time.monotonic() + 3
            while not any(t["status"] == "running" for t in self.c.store.export()["tasks"]):
                if time.monotonic() > deadline:
                    raise AssertionError("worker did not enter running")
                time.sleep(.01)
            self.c.cancel(gid, "cancel-1")
            observed = f.result(timeout=6)
        self.check("worker cleanup", observed["clean"], True)
        self.check("cancel not success", self.c.summary(gid)["merge_ready"], False)

    def case_17(self):
        gid = self.sync()
        ticket = self.c.claim(self.c.pending()[0]["id"], "writer")
        self.c.cancel(gid, "barrier-cancel")
        result = dict(task=ticket["id"], snapshot=ticket["snapshot"], owner="writer",
                      exit_code=0, artifact_sha256="b" * 64, execution_observed=True)
        self.check("late writer rejected", self.c.finish(ticket, result, clean=True), False)
        self.check("no revive", self.c.summary(gid)["public_success"], False)

    def case_18(self):
        gid = self.sync()
        self.c.cancel(gid, "cancel")
        for _ in range(20):
            self.sync()
        self.check("20 consumed reports no revival", len(self.c.pending()), 0)
        self.c.retry(gid, "native-run-123-attempt-2")
        self.c.retry(gid, "native-run-123-attempt-2")
        self.check("one retry attempt", self.c.summary(gid)["attempt"], 2)
        self.check("four retry tasks", len(self.c.pending()), 4)
        self.c.cancel(gid, "late-native-cancel", expected=(1, 1))
        self.check("old cancellation cannot cancel retry", len(self.c.pending()), 4)

    def case_19(self):
        gid = self.sync()
        self.c.claim(self.c.pending()[0]["id"], "offline-live-worker")
        self.c.cancel(gid, "cancel")
        self.rejected("no lease takeover", lambda: self.c.retry(gid, "retry"))
        self.check("old owner remains unclean", any(not t["clean"] for t in self.c.store.export()["tasks"]), True)

    def case_20(self):
        gid = self.sync()
        self.p["members"][0]["private"] = {}
        self.sync()
        self.run_workers()
        self.p["members"][0]["private"] = {"private": "success"}
        # Explicitly local compensation function, not evidence of a deployed webhook.
        Coordinator(self.db).reconcile(self.p)
        self.check("fresh reread converges", self.c.summary(gid)["merge_ready"], True)

    def case_21(self):
        calls = []
        def transport(args, body):
            calls.append(args)
            if len(calls) < 3:
                return SimpleNamespace(returncode=1, stderr="HTTP 429", stdout="")
            return SimpleNamespace(returncode=0, stderr="", stdout='{"ok":true}')
        g = GitHub(["lab/joint-ci-test"], command=transport, sleep=lambda _: None)
        self.check("bounded transient recovery", g.request("lab/joint-ci-test", ""), {"ok": True})
        self.check("observed retry count", len(calls), 3)
        self.rejected("default read only", lambda: g.request("lab/joint-ci-test", "", "POST", {}))

    def case_22(self):
        gid = self.sync()
        new = plan()
        new["members"][3]["pr"] = 2
        for t in new["tasks"]:
            t["consumers"] = [member_id(m) for m in new["members"]]
        new_id = self.sync(new)
        self.check("new group", new_id != gid, True)
        self.check("old group fenced", "superseded_group" in self.c.summary(gid)["blockers"], True)

    def case_23(self):
        gid = self.sync()
        ticket = self.c.claim(self.c.pending()[0]["id"], "worker")
        forged = dict(ticket, fence="incorrect")
        self.check("wrong owner fence rejected", self.c.finish(forged, {"exit_code": 0}, clean=True), False)
        self.check("self-reported green insufficient", self.c.finish(ticket, {"exit_code": 0}, clean=True), False)
        self.check("forged result cannot merge", self.c.summary(gid)["merge_ready"], False)

    def case_24(self):
        gid = self.sync()
        t = self.c.pending()[0]
        execute(self.db, t["id"], self.root / "workers")
        self.check("one lane not whole suite", self.c.summary(gid)["public_success"], False)
        self.p["tasks"] = []
        self.rejected("empty success rejected", lambda: self.sync())

    def case_25(self):
        self.p["tasks"][0]["command"] = [sys.executable, "-c", "pass"]
        gid = self.sync()
        self.run_workers()
        self.check("zero exit missing artifact fails", self.c.summary(gid)["public_success"], False)

    def case_26(self):
        gid = self.sync()
        self.run_workers()
        self.c.retry(gid, "retry-all")
        self.check("retry retains full set", len(self.c.pending()), 4)
        self.check("history preserved", len(self.c.store.export()["tasks"]), 8)

    def case_27(self):
        import random
        gid = self.sync()
        attempt, cancelled = 1, False
        trace = []
        for i in range(80):
            op = random.Random(i + 173).choice(["report", "cancel", "retry"])
            if op == "report":
                self.sync()
            elif op == "cancel":
                self.c.cancel(gid, f"c{i}")
                cancelled = True
            else:
                self.c.retry(gid, f"r{i}")
                attempt += 1
                cancelled = False
            s = self.c.summary(gid)
            expected = dict(attempt=attempt, cancelled=cancelled, merge_ready=False,
                            queued=0 if cancelled else 4)
            actual = dict(attempt=s["attempt"], cancelled="cancelled_consumers" in s["blockers"],
                          merge_ready=s["merge_ready"], queued=len(self.c.pending()))
            trace.append(dict(op=op, expected=expected, actual=actual))
            self.check(f"model step {i}", actual, expected)
        (self.root / "model-trace.json").write_text(json.dumps(trace, indent=2))

    def case_28(self):
        self.c.store.configure(max_running=3, per_group=1)
        ids = [self.sync(plan(i + 1, delay=.03)) for i in range(6)]
        self.run_workers()
        self.check("all groups drain", all(self.c.summary(g)["merge_ready"] for g in ids), True)
        events = []
        for t in self.c.store.export()["tasks"]:
            events += [(t["started_at"], 1), (t["finished_at"], -1)]
        running = peak = 0
        for _, delta in sorted(events):
            running += delta
            peak = max(peak, running)
        self.check("global bound enforced", peak <= 3, True)
        self.check("actual overlap", peak > 1, True)
        self.check("zero leftover", running, 0)

    def case_29(self):
        sha = "a" * 40
        check = dict(name="private", head_sha=sha, app={"id": 99}, status="completed", conclusion="success")
        g = GitHub(["lab/joint-ci-test"], command=lambda a, b: SimpleNamespace(
            returncode=0, stderr="", stdout=json.dumps({"check_runs": [check]})))
        self.check("wrong app not trusted", g.private_checks("lab/joint-ci-test", sha, {"private": 42}), {"private": "pending"})
        check["app"]["id"] = 42
        self.check("correct source", g.private_checks("lab/joint-ci-test", sha, {"private": 42}), {"private": "success"})

    def case_30(self):
        gid = self.sync()
        self.run_workers()
        self.p["members"][0].update(open=False, merged=True)
        self.sync()
        self.check("partial merge not green", self.c.summary(gid)["merge_ready"], False)

    def case_31(self):
        self.p["members"].append(dict(self.p["members"][0], pr=2))
        self.rejected("same repository multiple PR rejected", lambda: self.sync())

    def case_32(self):
        rs = reports()
        result = discover(rs, FIELDS, {r: "main" for r in REPOS})
        self.check("existing dependency fields associate", result["mode"], "joint")
        self.check("public union preserved", len(result["plan"]["tasks"]), 4)
        self.check("all private gates preserved", len(result["plan"]["members"]), 4)

    def case_33(self):
        gid = self.sync()
        self.c.cancel(gid, "disable-joint")
        self.check("rollback revokes old gate", self.c.summary(gid)["merge_ready"], False)
        self.check("no shared queued work", self.c.pending(), [])

    def case_34(self):
        from .evidence import validate_observation
        bad = dict(assertions=[dict(name="fake", expected=1, actual=2, passed=True)], error=None)
        self.check("recompute rejects forged passed bit", validate_observation(bad), False)

    def case_35(self):
        import os
        import shutil
        self.case_17()
        # Real SUT mutation, only in this case's disposable evidence directory.
        mutant = self.root / "mutant"
        package = Path(__file__).resolve().parent
        shutil.copytree(package, mutant / "cloud_joint", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        path = mutant / "cloud_joint" / "coordinator.py"
        source = path.read_text()
        guard = 'if task["status"] == "cancel_requested" or not current:'
        self.check("mutation target unique", source.count(guard), 1)
        path.write_text(source.replace(guard, "if False:  # injected missing cancellation fence"))
        code = "from cloud_joint.scenarios import Scenario;import sys; e=Scenario(sys.argv[1]).run(17);sys.exit(1 if e['error'] else 0)"
        mutated = subprocess.run([sys.executable, "-c", code, str(self.root / "mutant-observed")],
                                 cwd=mutant, env=dict(os.environ, PYTHONPATH=str(mutant)),
                                 capture_output=True, text=True, timeout=15)
        self.check("removed cancellation fence detected", mutated.returncode, 1)
        from .evidence import validate_observation
        altered = copy.deepcopy(self.assertions)
        altered[0]["actual"] = not altered[0]["actual"]
        self.check("mutated observation detected", validate_observation(dict(assertions=altered, error=None)), False)

    def case_36(self):
        from .adapter import associate
        rs = reports()
        self.check("no new markers", any("JOINT" in r["body"] for r in rs), False)
        self.check("unmodified existing fields accepted", discover(rs, FIELDS, {r: "main" for r in REPOS})["mode"], "joint")
        self.check("automatic component association", associate(dict(reports=rs, fields=FIELDS,
                   defaults={r: "main" for r in REPOS}))[0]["mode"], "joint")

    def case_37(self):
        rs = reports()
        for r in rs:
            r["body"] = "\n".join(f"{k}=main" for k in FIELDS)
        self.check("default refs not grouped", discover(rs, FIELDS, {r: "main" for r in REPOS})["mode"], "legacy")
        self.rejected("conflicting values not guessed", lambda: parse_dependencies("DLC_SIM_BRANCH=a\nDLC_SIM_BRANCH=b", FIELDS))

    def case_38(self):
        rs = reports()
        self.check("single PR need not await future peers", discover(rs[:1], FIELDS, {})["mode"], "legacy")
        self.check("later peers discovered", discover(rs, FIELDS, {})["mode"], "joint")

    def case_39(self):
        gid = self.sync()
        self.run_workers()
        for _ in range(2):
            self.c.retry(gid, "native:workflow-1:run-100:attempt-2")
        self.check("native identity dedup contract", self.c.summary(gid)["attempt"], 2)

    def case_40(self):
        gid = self.sync()
        self.c.cancel(gid, "cancel-A", member_id(self.p["members"][0]))
        self.check("B retains shared jobs", len(self.c.pending()), 4)
        self.run_workers()
        self.check("B public completes", self.c.summary(gid)["public_success"], True)
        self.check("A cancellation blocks group", self.c.summary(gid)["merge_ready"], False)
        a, b = [member_id(m) for m in self.p["members"][:2]]
        self.c.cancel(gid, "cancel-B", b)
        self.c.retry(gid, "A-native-rerun", consumer=a)
        self.run_workers()
        group = self.c.store.export()["groups"][0]
        self.check("A retry preserves B cancellation", group["cancelled"], [b])
        self.check("B cannot be revived by A", self.c.summary(gid)["merge_ready"], False)

    def case_41(self):
        self.c.store.configure(max_queued=3)
        self.rejected("overload rejects transaction", lambda: self.sync())
        self.check("no partial queue on overload", self.c.pending(), [])
        self.check("no partial group on overload", self.c.store.export()["groups"], [])

    def case_42(self):
        gid = self.sync()
        self.p["members"][1]["private"] = {}
        self.sync()
        self.check("actionable private blocker", "lab/driver#1:private" in self.c.summary(gid)["blockers"], True)

    def case_43(self):
        rs = reports()
        rs[1]["versions"] = copy.deepcopy(rs[1]["versions"])
        rs[1]["versions"][REPOS[0]]["head"] = "f" * 40
        self.check("do not change peer inputs", discover(rs, FIELDS, {})["mode"], "legacy")

    def case_44(self):
        gid = self.sync()
        self.c.cancel(gid, "cancel")
        self.sync()
        self.check("same generation stays cancelled", self.c.pending(), [])
        self.p["versions"][REPOS[0]]["head"] = "e" * 40
        self.sync()
        self.check("new commit new generation", self.c.summary(gid)["generation"], 2)
        self.check("new commit auto queues", len(self.c.pending()), 4)
