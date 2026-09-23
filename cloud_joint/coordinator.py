"""Author: zhekui. Transactional snapshots, outbox, execution admission and fencing.

Coordinator.reconcile: refresh trusted live inputs; claim/finish: fence workers;
cancel/retry: preserve attempt history; summary: independently derive merge gate.
This module never grants production permissions or treats a GitHub payload as proof.
"""
from __future__ import annotations

import copy
import json
import re
import secrets
import time
from collections import defaultdict, deque

from .model import digest, group_id, member_id, snapshot_id, validate_plan
from .store import Store


class Conflict(ValueError):
    pass


class Coordinator:
    def __init__(self, path):
        self.store = Store(path)

    def _save_group(self, db, group):
        group["revision"] += 1
        Store.put(db, "groups", group["id"], group)

    def _new_tasks(self, db, group):
        limits = Store.limits(db)
        queued = db.execute("SELECT count(*) FROM tasks WHERE json_extract(body,'$.status')='queued'").fetchone()[0]
        if queued + len(group["plan"]["tasks"]) > limits["max_queued"]:
            raise Conflict("queue capacity exceeded; no partial dispatch committed")
        for spec in group["plan"]["tasks"]:
            key = digest(spec)
            tid = digest([group["id"], group["generation"], group["attempt"], key])
            task = dict(id=tid, group=group["id"], snapshot=group["snapshot"],
                        generation=group["generation"], attempt=group["attempt"],
                        task_key=key, spec=spec, status="queued", owner=None,
                        fence=None, result=None, clean=True, queued_at=time.time())
            if set(spec["consumers"]) <= set(group["cancelled"]):
                task["status"] = "cancelled"
            Store.put(db, "tasks", tid, task)
            if task["status"] == "queued":
                Store.put(db, "intents", tid, dict(id=tid, task=tid, kind="dispatch", acknowledged=False))

    def _retire(self, db, group):
        for task in Store.tasks(db, group["id"]):
            if task["status"] == "running":
                task["status"] = "cancel_requested"
            elif task["status"] == "queued":
                task["status"] = "cancelled"
            else:
                continue
            Store.put(db, "tasks", task["id"], task)

    def reconcile(self, plan, expected_revision=None):
        plan = validate_plan(plan)
        gid, snapshot = group_id(plan), snapshot_id(plan)
        with self.store.transaction() as db:
            group = Store.get(db, "groups", gid)
            if expected_revision is not None and (group or {}).get("revision", 0) != expected_revision:
                raise Conflict("revision changed; reread before retry")
            if group is None and db.execute("SELECT count(*) FROM groups").fetchone()[0] >= Store.limits(db)["max_groups"]:
                raise Conflict("group capacity exceeded; operator retention required")
            identities = {member_id(m) for m in plan["members"]}
            # Moving a member retires its old group instead of allowing split ownership.
            previous = {row[0] for identity in identities for row in
                        db.execute("SELECT gid FROM members WHERE id=? AND gid<>?", (identity, gid))}
            for old_id in previous:
                old = Store.get(db, "groups", old_id)
                if old["active"]:
                    old["active"] = False
                    self._retire(db, old)
                    self._save_group(db, old)
                    db.execute("DELETE FROM members WHERE gid=?", (old_id,))
            if group is None:
                group = dict(id=gid, generation=1, attempt=1, revision=0,
                             snapshot=snapshot, plan=plan, cancelled=[], active=True)
                self._new_tasks(db, group)
            elif group["snapshot"] != snapshot or not group["active"]:
                self._retire(db, group)
                group.update(generation=group["generation"] + 1, attempt=1,
                             snapshot=snapshot, plan=plan, cancelled=[], active=True)
                self._new_tasks(db, group)
            else:
                group["plan"] = plan  # Private/Draft changes do not rerun public tests.
            self._save_group(db, group)
            for identity in identities:
                db.execute("INSERT INTO members VALUES(?,?) ON CONFLICT(id) DO UPDATE SET gid=excluded.gid",
                           (identity, gid))
            Store.audit(db, "reconcile", dict(group=gid, generation=group["generation"], revision=group["revision"]))
        return gid

    def _event(self, db, event_id, payload):
        fingerprint = digest(payload)
        old = db.execute("SELECT digest FROM events WHERE id=?", (event_id,)).fetchone()
        if old:
            if old[0] != fingerprint:
                raise Conflict("event identity reused for different operation")
            return False
        db.execute("INSERT INTO events VALUES(?,?)", (event_id, fingerprint))
        return True

    def cancel(self, gid, event_id, consumer=None, expected=None):
        with self.store.transaction() as db:
            group = Store.get(db, "groups", gid)
            if not group:
                raise ValueError("unknown group")
            if expected is not None and tuple(expected) != (group["generation"], group["attempt"]):
                Store.audit(db, "stale_cancel", dict(group=gid, expected=expected))
                return
            members = {member_id(m) for m in group["plan"]["members"]}
            if consumer is not None and consumer not in members:
                raise ValueError("unknown consumer")
            if not self._event(db, event_id, ["cancel", gid, consumer]):
                return
            group["cancelled"] = sorted(set(group["cancelled"]) | ({consumer} if consumer else members))
            for task in Store.tasks(db, gid):
                if set(task["spec"]["consumers"]) <= set(group["cancelled"]):
                    if task["status"] in ("queued", "running"):
                        task["status"] = "cancel_requested" if task["owner"] else "cancelled"
                        Store.put(db, "tasks", task["id"], task)
            self._save_group(db, group)
            Store.audit(db, "cancel", dict(group=gid, consumer=consumer))

    def retry(self, gid, event_id, expected=None, consumer=None):
        with self.store.transaction() as db:
            group = Store.get(db, "groups", gid)
            if not group or not group["active"]:
                raise ValueError("unknown/inactive group")
            if expected is not None and tuple(expected) != (group["generation"], group["attempt"]):
                raise Conflict("retry targets stale generation/attempt")
            if consumer is not None and consumer not in {member_id(m) for m in group["plan"]["members"]}:
                raise ValueError("unknown retry consumer")
            if any(not t["clean"] for t in Store.tasks(db, gid)):
                raise Conflict("old execution cleanup unconfirmed")
            if not self._event(db, event_id, ["retry", gid, consumer]):
                return
            self._retire(db, group)
            cancelled = [] if consumer is None else [m for m in group["cancelled"] if m != consumer]
            group.update(attempt=group["attempt"] + 1, cancelled=cancelled)
            self._new_tasks(db, group)
            self._save_group(db, group)
            Store.audit(db, "retry", dict(group=gid, attempt=group["attempt"]))

    def pending(self):
        with self.store.transaction() as db:
            groups = defaultdict(deque)
            for row in db.execute("SELECT body FROM tasks WHERE json_extract(body,'$.status')='queued' ORDER BY rowid"):
                task = json.loads(row[0])
                if task["status"] == "queued":
                    groups[task["group"]].append(task)
            result = []
            while groups:
                for gid in list(groups):
                    result.append(groups[gid].popleft())
                    if not groups[gid]:
                        del groups[gid]
            return result

    def claim(self, tid, owner):
        if not owner:
            raise ValueError("execution owner required")
        with self.store.transaction() as db:
            task = Store.get(db, "tasks", tid)
            if not task or task["status"] != "queued":
                return None
            group = Store.get(db, "groups", task["group"])
            if not group["active"] or (task["generation"], task["attempt"]) != (group["generation"], group["attempt"]):
                return None
            if any(not m.get("open", True) for m in group["plan"]["members"]):
                return None
            limits = Store.limits(db)
            active = [json.loads(r[0]) for r in db.execute("SELECT body FROM tasks WHERE json_extract(body,'$.clean')=0")]
            if len(active) >= limits["max_running"] or sum(t["group"] == group["id"] for t in active) >= limits["per_group"]:
                return None
            # No lease-based takeover: old work must have verifiable cleanup first.
            if any(not t["clean"] and (t["generation"], t["attempt"]) !=
                   (group["generation"], group["attempt"]) for t in Store.tasks(db, group["id"])):
                return None
            own_members = {member_id(m) for m in group["plan"]["members"]}
            for old_task in active:
                if old_task["group"] != group["id"]:
                    old_group = Store.get(db, "groups", old_task["group"])
                    if own_members & {member_id(m) for m in old_group["plan"]["members"]}:
                        return None
            task.update(status="running", owner=owner, fence=secrets.token_hex(24),
                        clean=False, started_at=time.time())
            Store.put(db, "tasks", tid, task)
            Store.audit(db, "claim", dict(task=tid, owner=owner))
            return copy.deepcopy(task)

    def is_current(self, ticket):
        with self.store.transaction() as db:
            task = Store.get(db, "tasks", ticket["id"])
            group = Store.get(db, "groups", ticket["group"])
            return bool(task and group and group["active"] and task["status"] == "running"
                        and task["owner"] == ticket["owner"] and task["fence"] == ticket["fence"]
                        and task["snapshot"] == group["snapshot"]
                        and task["attempt"] == group["attempt"])

    def finish(self, ticket, result, *, clean):
        """Only a trusted worker/adapter calls this after independent observation.

        No public HTTP result endpoint exists. Artifact and process observations
        are produced by worker.py; never pass untrusted webhook claims here.
        """
        with self.store.transaction() as db:
            task = Store.get(db, "tasks", ticket["id"])
            if not task or task["owner"] != ticket.get("owner") or task["fence"] != ticket.get("fence"):
                return False
            group = Store.get(db, "groups", task["group"])
            current = (group["active"] and task["generation"] == group["generation"]
                       and task["attempt"] == group["attempt"] and task["snapshot"] == group["snapshot"])
            if task["status"] not in ("running", "cancel_requested"):
                return False
            task["clean"] = bool(clean)
            if task["status"] == "cancel_requested" or not current:
                task["status"] = "cancelled" if clean else "cancel_requested"
            else:
                valid = (result.get("snapshot") == task["snapshot"]
                         and result.get("task") == task["id"]
                         and result.get("owner") == task["owner"]
                         and result.get("exit_code") == 0
                         and bool(re.fullmatch(r"[0-9a-f]{64}", result.get("artifact_sha256") or ""))
                         and result.get("execution_observed") is True)
                task["status"] = "success" if valid and clean else "failure"
            task["result"] = result
            task["finished_at"] = time.time()
            Store.put(db, "tasks", task["id"], task)
            Store.audit(db, "finish", dict(task=task["id"], status=task["status"], clean=clean))
            return current and task["status"] == "success"

    def summary(self, gid):
        with self.store.transaction() as db:
            group = Store.get(db, "groups", gid)
            if group is None:
                raise ValueError("unknown group")
            tasks = [t for t in Store.tasks(db, gid) if t["generation"] == group["generation"]
                     and t["attempt"] == group["attempt"]]
            blockers = []
            if group.get("native_blockers"):
                blockers.append("native_event_unresolved")
            if not group["active"]:
                blockers.append("superseded_group")
            if group["cancelled"]:
                blockers.append("cancelled_consumers")
            public = bool(tasks) and len(tasks) == len(group["plan"]["tasks"]) and all(
                t["status"] == "success" and t["clean"] for t in tasks)
            if not public:
                blockers.append("public_incomplete")
            for m in group["plan"]["members"]:
                prefix = member_id(m)
                for condition in ("draft", "merged"):
                    if m.get(condition):
                        blockers.append(prefix + ":" + condition)
                if not m.get("open", True):
                    blockers.append(prefix + ":closed")
                if not m.get("approved", False):
                    blockers.append(prefix + ":approval")
                for check in m["required"]:
                    receipt = m.get("private", {}).get(check)
                    scope = m["private_scopes"][check]
                    passed = (receipt == "success") if scope == "head" else (
                        isinstance(receipt, dict) and receipt.get("conclusion") == "success"
                        and receipt.get("snapshot") == group["snapshot"])
                    if not passed:
                        blockers.append(prefix + ":" + check)
            return dict(group=gid, revision=group["revision"], generation=group["generation"],
                        attempt=group["attempt"], snapshot=group["snapshot"],
                        public_success=public, merge_ready=not blockers,
                        blockers=sorted(blockers), tasks=tasks)
