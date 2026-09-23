"""Author: zhekui. Explicit synthetic protocol fixtures and real subprocess smoke.

plan: build four-member synthetic input; reports: existing-field discovery inputs.
These identities are labelled synthetic and never presented as real repo commits.
"""
import copy
import hashlib
import sys

from .model import member_id


SMOKE = "import json,os;from pathlib import Path;Path(os.environ['JOINT_ARTIFACT']).write_text(json.dumps(dict(snapshot=os.environ['JOINT_SNAPSHOT'],task=os.environ['JOINT_TASK'],completed=True)))"
REPOS = ["lab/arsenal", "lab/driver", "lab/synapse", "lab/sim"]
FIELDS = {"DLC_KERNEL_DRIVER_BRANCH": "lab/driver", "DLC_SYNAPSE_BRANCH": "lab/synapse",
          "DLC_SIM_BRANCH": "lab/sim", "ARSENAL_BRANCH": "lab/arsenal"}


def plan(number=1, delay=0):
    members = [dict(repo=r, pr=number, required=["private"], private_scopes={"private": "head"}, private={"private": "success"},
                    draft=False, open=True, approved=True) for r in REPOS]
    version = hashlib.sha1(b"SYNTHETIC_PROTOCOL_FIXTURE_NOT_REAL_GIT").hexdigest()
    return dict(schema=1, source_kind="synthetic_protocol_fixture", policy_version="fixture-v1",
                versions={r: dict(head=version, base=version, candidate=version) for r in REPOS},
                environment={"image": "sha256:" + "a" * 64, "hardware": "local-process"},
                members=members,
                tasks=[dict(suite=r.split("/")[1] + "-public", params={}, env={},
                            command=[sys.executable, "-c", f"import time;time.sleep({delay});" + SMOKE],
                            consumers=[member_id(m) for m in members]) for r in REPOS])


def reports():
    p = plan()
    refs = {r: "feature-" + r.split("/")[1] for r in REPOS}
    out = []
    for m in p["members"]:
        r = copy.deepcopy(m)
        r.update(branch=refs[m["repo"]], head_repo=m["repo"], versions=p["versions"],
                 environment=p["environment"], policy_version=p["policy_version"],
                 tasks=p["tasks"], body="\n".join(f"{key}={refs[repo]}" for key, repo in FIELDS.items()
                                                 if repo != m["repo"]))
        out.append(r)
    return out
