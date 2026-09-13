# Live validation checklist (E2 / E3 / E4 / E5 + platform)

Operator: CI专员 · Machine: Thinkbook · Orgs: **zhekui-hub only**

Record for each run: date, operator, PR URLs, head SHAs, Joint Issue URL, workflow_run IDs, pass/fail.

---

## Preflight

- [ ] `gh auth status` shows account with push on four repos  
- [ ] `JOINT_GH_TOKEN` present in env (value not logged)  
- [ ] `JOINT_OWNER=zhekui-hub`  
- [ ] Label `joint-ci` exists on `joint-ci-lab`  
- [ ] No open leftover `exp/ci-*` PRs that would cause E6 ambiguity  
- [ ] Confirm whitelist: scripts refuse non-`zhekui-hub/joint-ci-*` (F16)

```bash
export JOINT_OWNER=zhekui-hub
python3 gh_calls_stub.py preflight   # prints intended checks; exits 0 without token
```

---

## E2 — Staggered wait (错峰等待)

**验证点:** Synapse joint waits; no heavy test Pod/runner during wait; Driver Ready → one public run.

1. [ ] Create branch `exp/ci-e2-synapse` on `joint-ci-synapse`; open PR → `main`  
2. [ ] PR body:
   ```
   CI_MODE=joint
   JOINT_WAIT=true
   DLC_KERNEL_DRIVER_BRANCH=exp/ci-e2-driver
   ```
3. [ ] Observe joint Check = pending/waiting; **confirm Actions:** no multirepo/pseudo runner job for this joint yet  
4. [ ] Wait ≥ 5 minutes (P4); re-check still no heavy job  
5. [ ] Create `exp/ci-e2-driver` on `joint-ci-driver`; open Ready PR with `CI_MODE=joint`  
6. [ ] Expect: auto-match / schedule → **one** public workflow_run; both Checks move off waiting  

**Pass:** `pod_wait` ≈ 0 (no runner during wait); `public_runs=1`; synapse Check eventually mirrors driver public result.

**Fail signals:** runner busy during wait; two public runs after Driver appears.

**gh outline:** see `gh_calls_stub.py plan --scenario e2`

---

## E3 — Public dedupe + same Check URL

**验证点:** Driver+Synapse same MultiRepo identity → one run; both Checks share details URL.

1. [ ] Open paired Ready joint PRs (or reuse E2 success pair)  
2. [ ] From lab / scheduler logs note `joint_key`  
3. [ ] Count Actions runs for public test id under that key → expect **1**  
4. [ ] Compare Check `details_url` on driver PR vs synapse PR → **identical**  

**Pass:** `public_runs=1`; same_run_url.

---

## E4 — Exclusive failure isolation

**验证点:** Driver-only exclusive (e.g. 16-card Pseudo) failure does not fail synapse.

1. [ ] Configure driver route / fixture so exclusive test fails; public succeeds  
2. [ ] Run joint  
3. [ ] Driver joint Check = failure; synapse = success (if public ok)  

**Pass:** no broadcast of exclusive failure to peer.

---

## E5 — Push invalidation

**验证点:** After green joint, synapse empty commit → old greens invalidated; new key; re-run.

1. [ ] Start from green E3 pair; note Check conclusions + `joint_key`  
2. [ ] `git commit --allow-empty` + push on synapse PR branch  
3. [ ] Both PRs: old success must not remain mergeable green for joint  
4. [ ] New joint round with new key; driver SHA unchanged but bound to new combo  

**Pass:** invalidate visible; `public_runs` increments by one new public run; final Checks reflect new round.

---

## Optional extensions (same four repos)

| ID | Extra steps |
|---|---|
| E7 Draft | Keep driver Draft until step 5 of E2; then Ready |
| E10 / Sim | Add `joint-ci-sim` PR with `DLC_SIM_BRANCH`; assert union includes arc item |
| P1 Check write | App installation can `checks:write` on three mocks |
| P2 Issue | Joint Issue only on `joint-ci-lab` with label `joint-ci` |
| P3 SHA dispatch | `workflow_dispatch` inputs echo explicit SHAs |

---

## Cleanup

- [ ] Close experiment PRs or mark Draft  
- [ ] Close Joint Issues or label `joint-ci-done`  
- [ ] Delete `exp/ci-*` branches if policy allows  
- [ ] Append outcomes to `../RESULTS_REAL.md` (create on first live run)

## Abort / safety

If any script would touch a repo outside `zhekui-hub/joint-ci-*` → **stop** (F16).  
If token missing → run `plan` / `preflight` only; do not invent credentials.
