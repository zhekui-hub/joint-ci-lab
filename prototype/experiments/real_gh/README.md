# Real GitHub four-repo live validation (zhekui-hub)

**Audience:** CI专员 on Thinkbook  
**Scope:** `zhekui-hub/joint-ci-*` only — never ChipLTech / company orgs  
**Local fake:** already **42/42 PASS** under `../run_local.py`. This directory is for **可行性 PASS** (real Checks, Actions, runner idle).

## Goals

Prove on live GitHub what fake cannot:

| Goal | Maps to | Why real is required |
|---|---|---|
| Waiting does not occupy runners | E2 / P4 | Need Actions queue / runner metrics |
| Public tests dispatch once; same Check URL | E3 / P5 | Need real `check_run` / `workflow_run` URLs |
| Exclusive failure isolated | E4 | Cross-repo Check conclusions visible on PRs |
| Push invalidates old green checks | E5 / F9 | UI must not keep stale success |
| Cross-repo Check write + Issue on lab | P1 / P2 | App permissions |
| Explicit SHA workflow_dispatch | P3 | Inputs must carry SHAs |
| Whitelist reject | F16 | Must refuse non-`zhekui-hub/joint-ci-*` |

## Repos (four)

| Role | Repo | Notes |
|---|---|---|
| Arsenal / Joint Issue home | `zhekui-hub/joint-ci-lab` | Labels, scheduler workflow, Joint Issues |
| Driver mock | `zhekui-hub/joint-ci-driver` | PR + joint Check target |
| Synapse mock | `zhekui-hub/joint-ci-synapse` | PR + `JOINT_WAIT` / deps body |
| Sim mock | `zhekui-hub/joint-ci-sim` | Optional third participant (E10 / late join) |

All four should be **private** under `zhekui-hub`. Branch protection optional for lab experiments (prefer unprotected `exp/*` branches).

## Prerequisites

1. Thinkbook with `gh` CLI ≥ 2.40, logged into an account that can push to `zhekui-hub/joint-ci-*`.
2. Credential **placeholders** (names only — store values in OS keychain / env, never commit):
   - `JOINT_GH_TOKEN` — fine-grained or classic PAT with `repo` + `workflow` on the four repos  
   - `JOINT_APP_ID` / `JOINT_APP_INSTALLATION_ID` / `JOINT_APP_PRIVATE_KEY_PATH` — if using GitHub App for Check writes  
   - `JOINT_DRY_RUN=0` — only when intentionally hitting real API  
3. Confirm org/user is `zhekui-hub` (F16):
   ```bash
   gh api user --jq .login
   gh repo view zhekui-hub/joint-ci-lab --json nameWithOwner
   ```
4. Lab repo has label `joint-ci` (create once):
   ```bash
   gh label create joint-ci --repo zhekui-hub/joint-ci-lab --color 0E8A16 --description "Joint CI state" || true
   ```
5. Mock repos expose a minimal `workflow_dispatch` workflow accepting `driver_sha` / `synapse_sha` / `sim_sha` (or document equivalent). Stub intended calls: `gh_calls_stub.py`.

## What NOT to do

- Do not target ChipLTech or any non-`zhekui-hub/joint-ci-*` repo.
- Do not paste tokens into YAML, git, or this README.
- Do not treat local 42/42 as 可行性 PASS.

## Quick start (Thinkbook)

```bash
cd /path/to/joint-ci/prototype/experiments/real_gh
export JOINT_GH_TOKEN=...          # from keychain; name only documented here
export JOINT_OWNER=zhekui-hub
# optional dry documentation of intended calls (no network if token unset):
python3 gh_calls_stub.py --help
python3 gh_calls_stub.py plan --scenario e2
# live checklist:
bash checklist_e2_e5.sh --help
# when ready (requires token + repos):
# bash checklist_e2_e5.sh run-e2
```

## Automated probe runner

`run_real.py` is the executable path for P1–P5. It always writes
`RESULTS_REAL.md`; dry-run is the default and performs no network writes:

```bash
python3 run_real.py
```

For a live run, export a token with access limited to the four repositories and
explicitly opt in. The token is passed to `gh` through `GH_TOKEN` and is never
printed:

```bash
export JOINT_GH_TOKEN=      # value from a keychain/secret store
export JOINT_DRY_RUN=0
python3 run_real.py --live
```

The runner resolves `main` SHAs (or uses `JOINT_{LAB,DRIVER,SYNAPSE,SIM}_SHA`),
creates one labelled Joint Issue in the lab, dispatches
`.github/workflows/joint_real_probe.yml` with all three mock SHAs, then writes
`joint-ci` Checks (or commit-status fallbacks) on every participant using the
same workflow-run URL. P4 remains an operator-observed staggered-PR experiment;
record its evidence from `CHECKLIST.md` in the generated report.

Live P1/P5 Check writes may require a GitHub App installation because the
default Actions `GITHUB_TOKEN` is scoped to its own repository. The probe
workflow must be present on the lab default branch before dispatching.

## Files in this directory

| File | Purpose |
|---|---|
| `README.md` | This overview |
| `CHECKLIST.md` | Step-by-step E2/E3/E4/E5 (+ P1–P5 pointers) |
| `gh_calls_stub.py` | Documents intended `gh`/API calls; imports without credentials |
| `checklist_e2_e5.sh` | Shell checklist / optional runner (guards on token + owner) |
| `env.example` | Env var **names** only |

## Mapping: local E* → real validation

| Local (fake, already green) | Real follow-up |
|---|---|
| E2 staggered wait | `CHECKLIST.md` §E2 + P4 |
| E3 public dedupe | §E3 + P5 |
| E4 exclusive isolation | §E4 |
| E5 push invalidation | §E5 |
| E10 / E22 / E38 three-repo | extend §E3 with sim PR |
| E11–E42 races etc. | mostly fake-sufficient; sample R1/F8 on real if time |

## Success for CI专员

- [ ] E2 real: wait phase no heavy runner jobs; one public run after peer Ready  
- [ ] E3 real: both PRs’ joint Checks share one `details_url` / workflow_run  
- [ ] E4 real: driver exclusive fail does not fail synapse public-only path  
- [ ] E5 real: after synapse push, old success gone / pending, new run with new key  
- [ ] P1–P3 at least once on lab App install  
- [ ] Record PR links + run IDs back into `../RESULTS_REAL.md` (create when first live run done)

The repository also contains `workflows/participant_joint_probe.yml`. Copy it
to the default branch of each mock repository when testing P3 directly there;
it accepts and prints the three explicit participant SHAs.
