"""Author: zhekui. Independent evidence checks and source identity.

validate_observation: recompute assertions plus audit/state invariants;
source_identity: hash deliverable sources, excluding execution outputs and caches.
"""
import hashlib
import json
from pathlib import Path


def source_identity(root):
    root = Path(root)
    files = []
    for directory in ("cloud_joint", "tools", "templates", "cloud_docs"):
        for path in sorted((root / directory).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                files.append((str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest()))
    if not files:
        raise ValueError("no source files")
    return hashlib.sha256(json.dumps(files, separators=(",", ":")).encode()).hexdigest(), dict(files)


def validate_observation(evidence):
    assertions = evidence.get("assertions", [])
    if evidence.get("error") or not assertions:
        return False
    if not all(a.get("actual") == a.get("expected") and a.get("passed") is True for a in assertions):
        return False
    state = evidence.get("state", {})
    claims = {}
    for event in state.get("audit", []):
        if event["kind"] == "claim":
            body = json.loads(event["body"])
            claims[body["task"]] = claims.get(body["task"], 0) + 1
    if any(n != 1 for n in claims.values()):
        return False
    for task in state.get("tasks", []):
        if task["status"] == "success":
            r = task.get("result") or {}
            if not (task["clean"] and r.get("exit_code") == 0 and r.get("execution_observed") is True
                    and r.get("snapshot") == task["snapshot"] and r.get("task") == task["id"]
                    and r.get("owner") == task["owner"] and r.get("artifact_sha256")):
                return False
    summaries = {s["group"]: s for s in evidence.get("summaries", [])}
    for group in state.get("groups", []):
        current = [t for t in state["tasks"] if t["group"] == group["id"] and
                   t["generation"] == group["generation"] and t["attempt"] == group["attempt"]]
        public = bool(current) and len(current) == len(group["plan"]["tasks"]) and all(
            t["status"] == "success" and t["clean"] for t in current)
        blockers = []
        if group.get("native_blockers"):
            blockers.append("native_event_unresolved")
        if not group["active"]:
            blockers.append("superseded_group")
        if group["cancelled"]:
            blockers.append("cancelled_consumers")
        if not public:
            blockers.append("public_incomplete")
        for member in group["plan"]["members"]:
            prefix = f'{member["repo"]}#{member["pr"]}'
            for name in ("draft", "merged"):
                if member.get(name):
                    blockers.append(prefix + ":" + name)
            if not member.get("open", True):
                blockers.append(prefix + ":closed")
            if not member.get("approved", False):
                blockers.append(prefix + ":approval")
            for name in member["required"]:
                receipt = member.get("private", {}).get(name)
                if member["private_scopes"][name] == "snapshot":
                    ok = isinstance(receipt, dict) and receipt.get("conclusion") == "success" and receipt.get("snapshot") == group["snapshot"]
                else:
                    ok = receipt == "success"
                if not ok:
                    blockers.append(prefix + ":" + name)
        observed = summaries.get(group["id"], {})
        if (observed.get("public_success") != public or observed.get("merge_ready") != (not blockers)
                or observed.get("blockers") != sorted(blockers)):
            return False
    return True


def verify_manifest(root):
    root = Path(root).resolve()
    manifest = json.loads((root / "SOURCE_MANIFEST.json").read_text())
    for name, expected in manifest["files"].items():
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError("unsafe/missing source: " + name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("source hash mismatch: " + name)
    identity, files = source_identity(root)
    if identity != manifest["source_id"] or files != manifest["files"]:
        raise ValueError("source inventory mismatch")
    return manifest
