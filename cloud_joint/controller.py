"""Author: zhekui. Isolated GitHub event/compensation/status-publication bridge.

Controller.sync: authoritative refresh plus current status projection;
native_event: bind original workflow run attempts to one consumer;
publish: App-authenticated lab status write, never merges PRs or changes rules.
All controller invocations share local durable DB; no PR code in this process.
"""
from __future__ import annotations

import copy
import fcntl
import json
import time
from pathlib import Path

from .adapter import associate, inventory
from .coordinator import Coordinator, Conflict
from .model import canonical, digest, member_id, parse_dependencies
from .store import Store


class Controller:
    def __init__(self, db, api, config):
        self.c = Coordinator(db)
        self.api = api
        self.config = config
        self.specs = {r["repo"]: r for r in config["repositories"]}

    def sync(self):
        for group in self.c.store.export()["groups"]:
            merges = []
            for member in group["plan"]["members"]:
                live = self.api.request(member["repo"], f'pulls/{member["pr"]}')
                if live.get("merged"):
                    merges.append(dict(member=member_id(member), commit=live["merge_commit_sha"],
                                       at=live.get("merged_at"), method="not_exposed_by_pull_endpoint"))
            if merges:
                with self.c.store.transaction() as db:
                    current = Store.get(db, "groups", group["id"])
                    current["partial_merge"] = merges
                    current["active"] = False
                    self.c._retire(db, current)
                    self.c._save_group(db, current)
        data = inventory(self.api, self.config)
        proposals = associate(data)
        active_ids = set()
        for proposal in proposals:
            if proposal["mode"] == "joint":
                active_ids.add(self.c.reconcile(proposal["plan"]))
                self._inherit_pending_events()
        # Missing open participants must invalidate existing checks, including
        # manual partial merges. Preserve their actual merge identity in state.
        for group in self.c.store.export()["groups"]:
            if not group["active"] or group["id"] in active_ids:
                continue
            p = copy.deepcopy(group["plan"])
            merged = []
            for m in p["members"]:
                live = self.api.request(m["repo"], f'pulls/{m["pr"]}')
                if live.get("merged"):
                    m.update(open=False, merged=True, merge_commit_sha=live["merge_commit_sha"],
                             merged_at=live.get("merged_at"))
                    merged.append(dict(member=member_id(m), commit=live["merge_commit_sha"],
                                       at=live.get("merged_at")))
                elif live["state"] != "open":
                    m.update(open=False)
            if merged or any(not m["open"] for m in p["members"]):
                self.c.reconcile(p)
                with self.c.store.transaction() as db:
                    current = Store.get(db, "groups", group["id"])
                    current["partial_merge"] = merged
                    self.c._retire(db, current)
                    self.c._save_group(db, current)
            else:
                # Relation changed or cannot be proven. Revoke instead of silently
                # leaving the prior group green; legacy dispatch is not automatic.
                self.c.cancel(group["id"], "relation-invalid:" + digest(data))
        return proposals

    def native_event(self, repo, run_id):
        """Payload is only a hint: fetch actual GitHub run before acting.

        Control mapping requires in_progress. Late first-attempt success can
        acknowledge only its own receipt, without retry/cancellation authority.
        """
        if repo not in self.specs:
            raise PermissionError("unexpected participant")
        run = self.api.request(repo, f"actions/runs/{int(run_id)}")
        return self._handle_native_run(repo, run_id, run)

    def _handle_native_run(self, repo, run_id, run):
        if run["workflow_id"] not in self.specs[repo].get("native_workflow_ids", []):
            return {"ignored": "workflow not explicitly mapped"}
        attempt = int(run["run_attempt"])
        key = f"{repo}:{run_id}:{attempt}"
        with self.c.store.transaction() as db:
            row = db.execute("SELECT body FROM native_runs WHERE id=?", (key,)).fetchone()
            binding = json.loads(row[0]) if row else None
        if binding and binding.get("terminal_only"):
            if run["status"] != "completed" or run.get("conclusion") != "success" or run["head_sha"] != binding["head"]:
                raise Conflict("terminal receipt cannot control native execution")
            with self.c.store.transaction() as db:
                self._reject_newer_binding(db, repo, run_id)
            return binding
        if binding is None and self._late_success(run):
            return self._acknowledge_late_success(repo, run_id, run)
        if binding is None:
            candidates = []
            for g in self.c.store.export()["groups"]:
                for m in g["plan"]["members"]:
                    if (g["active"] and m["repo"] == repo and g["plan"]["versions"][repo]["head"] == run["head_sha"]
                            and any(p["number"] == m["pr"] for p in run.get("pull_requests", []))):
                        candidates.append((g, m))
            if len(candidates) != 1 or run["status"] != "in_progress":
                # Persist uncertainty: finalize/scheduled refresh must not reuse
                # old green public results for an unobserved native attempt.
                with self.c.store.transaction() as db:
                    for row in db.execute("SELECT id FROM groups").fetchall():
                        current = Store.get(db, "groups", row[0])
                        affected = [member_id(m) for m in current["plan"]["members"]
                                    if m["repo"] == repo and current["plan"]["versions"][repo]["head"] == run["head_sha"]]
                        if current["active"] and affected:
                            current.setdefault("native_blockers", {})[key] = affected
                            self.c._save_group(db, current)
                raise Conflict("native run lacks unambiguous active binding; retain pending")
            g, m = candidates[0]
            binding = dict(group=g["id"], consumer=member_id(m), generation=g["generation"],
                           attempt=g["attempt"], head=run["head_sha"])
            if attempt > 1:
                self.c.retry(g["id"], "native-retry:" + key,
                             expected=(g["generation"], g["attempt"]), consumer=member_id(m))
                binding["attempt"] = self.c.summary(g["id"])["attempt"]
            with self.c.store.transaction() as db:
                db.execute("INSERT OR IGNORE INTO native_runs VALUES(?,?)", (key, canonical(binding)))
                if attempt > 1:
                    current = Store.get(db, "groups", g["id"])
                    current["native_blockers"] = {k: v for k, v in current.get("native_blockers", {}).items()
                                                  if k.startswith("unverified:") or binding["consumer"] not in v}
                    self.c._save_group(db, current)
        if run.get("conclusion") == "cancelled":
            self.c.cancel(binding["group"], "native-cancel:" + key, binding["consumer"],
                          (binding["generation"], binding["attempt"]))
        return binding

    @staticmethod
    def _late_success(run):
        # A missed rerun must not silently skip the public retry semantics.
        return (run.get("status") == "completed" and run.get("conclusion") == "success"
                and run.get("run_attempt") == 1)

    @staticmethod
    def _reject_newer_binding(db, repo, run_id):
        prefix = f"{repo}:{run_id}:"
        if any(r[0].startswith(prefix) and int(r[0][len(prefix):]) > 1
               for r in db.execute("SELECT id FROM native_runs") if not r[0].startswith("pending:")):
            raise Conflict("late success belongs to an older native attempt")

    def _acknowledge_late_success(self, repo, run_id, run):
        """A terminal receipt is not an execution binding or reusable CI result.

        Revalidate live PR identity, then atomically recheck local membership.
        Only the exact blocker is removed. Private/public gates remain independent.
        """
        key = f"{repo}:{run_id}:1"
        prs = {p["number"]: self.api.request(repo, f'pulls/{p["number"]}')
               for p in run.get("pull_requests", [])}
        with self.c.store.transaction() as db:
            candidates = []
            for row in db.execute("SELECT body FROM groups"):
                g = json.loads(row[0])
                for m in g["plan"]["members"]:
                    pr = prs.get(m["pr"]) if m["repo"] == repo else None
                    version = g["plan"]["versions"].get(repo, {})
                    if (g["active"] and pr and pr["state"] == "open" and not pr.get("merged")
                            and pr["head"]["sha"] == version.get("head") == run["head_sha"]
                            and pr["base"]["sha"] == version.get("base")
                            and pr["head"]["repo"]["full_name"] == repo):
                        candidates.append((g, m))
            if len(candidates) != 1:
                raise Conflict("late success lacks unambiguous current PR identity; retain pending")
            g, m = candidates[0]
            # A previously observed newer attempt must never be superseded.
            self._reject_newer_binding(db, repo, run_id)
            binding = dict(group=g["id"], consumer=member_id(m), generation=g["generation"],
                           attempt=g["attempt"], head=run["head_sha"], terminal_only=True)
            db.execute("INSERT OR IGNORE INTO native_runs VALUES(?,?)", (key, canonical(binding)))
            if key in g.get("native_blockers", {}):
                del g["native_blockers"][key]
                self.c._save_group(db, g)
            return binding

    def _consume_native_event(self, repo, run_id, recovery=False):
        if repo not in self.specs or run_id < 1:
            raise ValueError("invalid native event hint")
        key = f"unverified:{repo}:{run_id}"
        receipt_id = f"pending:{repo}:{run_id}"
        # Journal before network I/O. Recovery preserves the known head scope.
        with self.c.store.transaction() as db:
            old = db.execute("SELECT body FROM native_runs WHERE id=?", (receipt_id,)).fetchone()
            receipt = json.loads(old[0]) if recovery and old else dict(repo=repo, blocker=key, head=None)
            receipt["recovery_checked_at"] = time.time()
            db.execute("INSERT INTO native_runs VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                       (receipt_id, canonical(receipt)))
        self._inherit_pending_events()
        run = self.api.request(repo, f"actions/runs/{run_id}")
        receipt["head"] = run["head_sha"]
        with self.c.store.transaction() as db:
            db.execute("UPDATE native_runs SET body=? WHERE id=?", (canonical(receipt), receipt_id))
        self._inherit_pending_events()
        if recovery and not self._late_success(run):
            return None  # Background recovery must not invent retry/cancel intent.
        binding = self._handle_native_run(repo, run_id, run)
        with self.c.store.transaction() as db:
            db.execute("DELETE FROM native_runs WHERE id=?", (receipt_id,))
        self._inherit_pending_events()
        return binding

    def _recover_late_successes(self):
        # Bounded round-robin probes: unresolved receipts cannot starve later ones.
        with Path(self.c.store.path + ".event.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.c.store.transaction() as db:
                receipts = [(r[0], json.loads(r[1])) for r in db.execute(
                    "SELECT id,body FROM native_runs WHERE id LIKE 'pending:%'")]
            errors = []
            started = time.monotonic()
            for receipt_id, receipt in sorted(receipts, key=lambda r: (r[1].get("recovery_checked_at", 0), r[0]))[:16]:
                if time.monotonic() - started >= 5:
                    break  # One transport request can still take its own timeout.
                try:
                    self._consume_native_event(receipt["repo"], int(receipt_id.rsplit(":", 1)[1]), recovery=True)
                except (Conflict, RuntimeError) as exc:
                    errors.append(str(exc))
            if errors:
                raise Conflict("late-event recovery incomplete: " + "; ".join(errors))

    def _inherit_pending_events(self):
        """Use the existing native_runs journal for unresolved event receipts.

        Receipt IDs are distinct from attempt binding IDs. This survives empty
        databases and new group creation without requiring a schema migration.
        """
        with self.c.store.transaction() as db:
            receipts = [json.loads(r[0]) for r in db.execute(
                "SELECT body FROM native_runs WHERE id LIKE 'pending:%'")]
            for row in db.execute("SELECT id FROM groups").fetchall():
                g = Store.get(db, "groups", row[0])
                blockers = {k: v for k, v in g.get("native_blockers", {}).items()
                            if not k.startswith("unverified:")}
                for receipt in receipts:
                    repo = receipt["repo"]
                    affected = [member_id(m) for m in g["plan"]["members"] if m["repo"] == repo
                                and (receipt.get("head") is None or
                                     g["plan"]["versions"][repo]["head"] == receipt["head"])]
                    if g["active"] and affected:
                        blockers[receipt["blocker"]] = affected
                if blockers != g.get("native_blockers", {}):
                    g["native_blockers"] = blockers
                    self.c._save_group(db, g)

    def publish(self):
        """Serialize only publishers; never hold SQLite write lock over network."""
        with Path(self.c.store.path + ".publish.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.sync()
            return self._publish_current()

    def cycle(self, hint=None, publish=False):
        """Handle time-sensitive events before inventory, refreshing only once.

        Errors remain visible to the CLI. Failed refresh never projects success;
        an explicit pending projection revokes old statuses when transport works.
        A missing initial group stays pending; later cycles can acknowledge a
        uniquely attributed first-attempt success without user intervention.
        """
        result = dict(proposals=[], native_event=None, statuses=[], errors=[], timings={},
                      mode="LAB_STATUS_WRITE" if publish else "READ_ONLY_SHADOW")

        def stage(name, action):
            start = time.monotonic()
            try:
                return action()
            except Exception as exc:
                # Only transport/coordination errors are intended for disclosure.
                message = str(exc) if isinstance(exc, (RuntimeError, Conflict)) else type(exc).__name__
                result["errors"].append(dict(stage=name, error=message))
            finally:
                result["timings"][name] = round(time.monotonic() - start, 6)

        if hint and hint.get("run_id"):
            with Path(self.c.store.path + ".event.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                result["native_event"] = stage("native_event", lambda: self._consume_native_event(
                    hint["repo"], int(hint["run_id"])))

        def refresh_and_project():
            result["proposals"] = stage("sync", self.sync) or []
            if not result["errors"]:
                stage("late_success_recovery", self._recover_late_successes)
            if publish:
                result["statuses"] = stage("publish", lambda: self._publish_current(
                    force_pending=any(e["stage"] in ("native_event", "sync")
                                      for e in result["errors"]))) or []

        if publish:
            with Path(self.c.store.path + ".publish.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                refresh_and_project()
        else:
            refresh_and_project()
        return result

    def _publish_current(self, force_pending=False):
        outputs = []
        for group in self.c.store.export()["groups"]:
            summary = self.c.summary(group["id"])
            # Live membership/SHA/base check immediately before each publication.
            valid = group["active"] and not group.get("partial_merge")
            for m in ([] if force_pending else group["plan"]["members"]):
                pr = self.api.request(m["repo"], f'pulls/{m["pr"]}')
                version = group["plan"]["versions"][m["repo"]]
                valid &= (pr["state"] == "open" and pr["head"]["sha"] == version["head"]
                          and pr["base"]["sha"] == version["base"] and not pr["draft"])
            state = "success" if valid and summary["merge_ready"] else "pending"
            if any(t["status"] == "failure" for t in summary["tasks"]) or group["cancelled"]:
                state = "failure"
            if force_pending:
                state = "pending"
            for member in group["plan"]["members"]:
                repo = member["repo"]
                sha = group["plan"]["versions"][repo]["head"]
                context = self.specs[repo].get("aggregate_name", "joint-ci-cloud")
                body = dict(state=state, context=context,
                            description=("Joint checks passed" if state == "success" else "Joint checks: " + ",".join(summary["blockers"]))[:140])
                if force_pending:
                    body["description"] = "Joint refresh/event failed; revalidation required"
                url = self.config.get("details_url")
                if url:
                    body["target_url"] = url
                key = repo + ":" + sha + ":" + context
                with self.c.store.transaction() as db:
                    owner = db.execute("SELECT gid FROM members WHERE id=?", (member_id(member),)).fetchone()
                    if owner and owner[0] != group["id"]:
                        continue  # Superseded group cannot overwrite new group's check.
                # Stale publishers fail before writing. Scheduled sync can repair
                # the remaining external API window; no zero-race claim is made.
                with self.c.store.transaction() as db:
                    current = Store.get(db, "groups", group["id"])
                    if current["revision"] != summary["revision"]:
                        raise Conflict("state changed before status projection")
                    old = db.execute("SELECT body FROM publications WHERE id=?", (key,)).fetchone()
                    publication = dict(group=group["id"], generation=group["generation"], body=body)
                    if old and json.loads(old[0]) == publication:
                        continue
                self.api.request(repo, f"statuses/{sha}", "POST", body)
                with self.c.store.transaction() as db:
                    changed = Store.get(db, "groups", group["id"])["revision"] != summary["revision"]
                    if changed:
                        Store.audit(db, "publication_invalidated", dict(group=group["id"], repo=repo, sha=sha))
                if changed:
                    self.api.request(repo, f"statuses/{sha}", "POST", dict(body, state="pending", description="Joint inputs changed; revalidating"))
                    raise Conflict("state changed during publication; pending repaired")
                with self.c.store.transaction() as db:
                    db.execute("INSERT INTO publications VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                               (key, canonical(publication)))
                outputs.append(dict(repo=repo, sha=sha, state=state))
        return outputs
