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

        Mapping is persisted at first in_progress observation. A cancelled run
        never observed while current cannot be safely attributed; fail closed.
        """
        if repo not in self.specs:
            raise PermissionError("unexpected participant")
        run = self.api.request(repo, f"actions/runs/{int(run_id)}")
        if run["workflow_id"] not in self.specs[repo].get("native_workflow_ids", []):
            return {"ignored": "workflow not explicitly mapped"}
        attempt = int(run["run_attempt"])
        key = f"{repo}:{run_id}:{attempt}"
        with self.c.store.transaction() as db:
            row = db.execute("SELECT body FROM native_runs WHERE id=?", (key,)).fetchone()
            binding = json.loads(row[0]) if row else None
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
                                                  if binding["consumer"] not in v}
                    self.c._save_group(db, current)
        if run.get("conclusion") == "cancelled":
            self.c.cancel(binding["group"], "native-cancel:" + key, binding["consumer"],
                          (binding["generation"], binding["attempt"]))
        return binding

    def publish(self):
        """Serialize only publishers; never hold SQLite write lock over network."""
        with Path(self.c.store.path + ".publish.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.sync()
            return self._publish_current()

    def _publish_current(self):
        outputs = []
        for group in self.c.store.export()["groups"]:
            summary = self.c.summary(group["id"])
            # Live membership/SHA/base check immediately before each publication.
            valid = group["active"] and not group.get("partial_merge")
            for m in group["plan"]["members"]:
                pr = self.api.request(m["repo"], f'pulls/{m["pr"]}')
                version = group["plan"]["versions"][m["repo"]]
                valid &= (pr["state"] == "open" and pr["head"]["sha"] == version["head"]
                          and pr["base"]["sha"] == version["base"] and not pr["draft"])
            state = "success" if valid and summary["merge_ready"] else "pending"
            if any(t["status"] == "failure" for t in summary["tasks"]) or group["cancelled"]:
                state = "failure"
            for member in group["plan"]["members"]:
                repo = member["repo"]
                sha = group["plan"]["versions"][repo]["head"]
                context = self.specs[repo].get("aggregate_name", "joint-ci-cloud")
                body = dict(state=state, context=context,
                            description=("Joint checks passed" if state == "success" else "Joint checks: " + ",".join(summary["blockers"]))[:140])
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
