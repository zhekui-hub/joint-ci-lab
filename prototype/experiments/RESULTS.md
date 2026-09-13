# Joint CI local experiment results

Date: 2026-09-13  
Last re-verify: 2026-09-13 (post FakeGh → `arsenal/gh_client.py` migration)  
Runner: `prototype/experiments/run_local.py`  
Backend: `FakeGhClient` from `arsenal/gh_client.py` (`experiments/fake_gh.py` re-export; no real GitHub / ChipLTech)

## Inventory

| ID | File | Name |
|---|---|---|
| E1 | `scenario_e1.yaml` | single_repo_baseline |
| E2 | `scenario_e2.yaml` | staggered_wait |
| E3 | `scenario_e3.yaml` | public_dedup |
| E4 | `scenario_e4.yaml` | exclusive_failure_isolation |
| E5 | `scenario_e5.yaml` | push_invalidation |
| E6 | `scenario_e6.yaml` | ambiguous_dependency |
| E7 | `scenario_e7.yaml` | draft_gate |
| E8 | `scenario_e8.yaml` | fixed_dep_compat |
| E9 | `scenario_e9.yaml` | cancel_exit_joint |
| E10 | `scenario_e10.yaml` | three_repo_union |

Supporting code:
- `run_local.py` — scenario runner
- `fake_gh.py` — thin re-export of `FakeGhClient` from `../arsenal/gh_client.py`
- `assert_public_runs.py` — assertions (public_runs, wait/pod, exclusive isolation, invalidation)
- `../arsenal/gh_client.py` — `GhClient` + in-memory `FakeGhClient`
- `../arsenal/scheduler.py` — injectable `gh`, joint_key reuse, invalidate, per-PR aggregation

## Results (all runnable)

| ID | Result | Notes |
|---|---|---|
| E1 | **PASS** | non-joint → `skipped_non_joint`, dispatch=0 |
| E2 | **PASS** | wait phase dispatch=0 / busy=0; then public_runs multirepo=1; same_run_url |
| E3 | **PASS** | public_runs multirepo=1, pseudo=1; same_run_url |
| E4 | **PASS** | exclusive pseudo/16 failure → driver failure, synapse success |
| E5 | **PASS** | push invalidate + new joint_key; public_runs_total multirepo=2 |
| E6 | **PASS** | ambiguous driver PRs → failed/ambiguous_prs, dispatch=0 |
| E7 | **PASS** | draft gate holds dispatch=0; ready → public_runs=1 |
| E8 | **PASS** | JOINT_WAIT=false → fixed-dep single-side run, no long wait |
| E9 | **PASS** | close peer → cancelled |
| E10 | **PASS** | three-repo union; multirepo/arc/pseudo present; public multirepo=1 |

**Score: 10/10 PASS**

## Expanded matrix F1–F10

The expanded fake-backend matrix exercises three participants, public/private
test boundaries, failure propagation, parameter-sensitive dedupe, repeated
events, SHA invalidation, draft gating, and mutual dependency declarations.

| ID | Result | Coverage |
|---|---|---|
| F1 | **PASS** | Driver + Synapse + Sim public union; common test once, distinct tests once |
| F2 | **PASS** | Three-repo staggered arrival; no dispatch while Driver is missing |
| F3 | **PASS** | Public failure broadcast to all three participant checks |
| F4 | **PASS** | Driver private failure is excluded from Arsenal and does not fail peers |
| F5 | **PASS** | Same test ID with different parameters dispatches twice |
| F6 | **PASS** | Mutual dependency declarations are treated as association, not deadlock |
| F7 | **PASS** | Private-only participants produce no Arsenal public run |
| F8 | **PASS** | Repeated scheduler events do not duplicate a public run |
| F9 | **PASS** | Sim SHA change invalidates the three-repo result and reruns public tests |
| F10 | **PASS** | Three-repo Draft gate holds all heavy tests until Ready |

**Expanded score: 10/10 PASS; total scenario score: 21/21 PASS.**

Additional scheduler unit coverage now includes 17 tests for three-repo union,
public failure fan-out, private failure isolation, check-write failure,
running-run recovery, cancellation, SHA reuse protection, and invalidation.

### Post-migration regression (experiment agent re-run)
- `shared` unittest: **6/6 OK**
- `arsenal` unittest: **9/9 OK**
- `experiments/run_local.py`: **10/10 PASS**

## How run_local works

1. Load `scenario_e*.yaml` steps (`create_pr`, `sleep_minutes`, `ready`, `push`, `close_pr`, `inject_failure`, `schedule`, `assert`).
2. Maintain in-memory reports + `FakeGhClient` PR/issue/check/dispatch registry.
3. Call `scheduler.run(reports, gh=fake)` (and `invalidate` on push).
4. Assert on joint status, dispatch counts, `public_runs`, checks, exclusive isolation, joint_key change.

Waiting never starts runners (`runner_busy_seconds` stays 0; `dispatch_count==0` while `waiting_deps`).

## Exact re-run commands

```bash
# deps (once)
pip install --break-system-packages 'PyYAML>=6' 'pytest>=7'

# unit tests (shared)
cd /workspace/joint-ci/prototype/shared && python3 -m unittest test_joint_key.py -v

# unit tests (arsenal scheduler + FakeGh)
cd /workspace/joint-ci/prototype/arsenal && python3 -m unittest discover -v

# all scenarios
cd /workspace/joint-ci/prototype/experiments && python3 run_local.py

# JSON output
cd /workspace/joint-ci/prototype/experiments && python3 run_local.py --json

# single scenario
cd /workspace/joint-ci/prototype/experiments && python3 run_local.py scenario_e2.yaml

# list scenarios
cd /workspace/joint-ci/prototype/experiments && python3 run_local.py --list

# dry-run scheduler smoke (README)
cd /workspace/joint-ci/prototype/arsenal
JOINT_DRY_RUN=1 REPORT_JSON='[{"repo":"synapse","pr_number":456,"head_sha":"aaa","ci_mode":"joint","joint_wait":true,"deps":{"driver_branch":"feature/x"},"remote_tests":[{"id":"multirepo_runtest","params":{"profile":"default"}}]}]' python3 scheduler.py
```

## Blockers

None for local fake-backend runs. Real GitHub / ChipLTech intentionally not used.
