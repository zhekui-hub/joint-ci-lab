# Plan 1–9 Live Results (zhekui-hub joint-ci-*)

**Date:** 2026-09-13 (final executor pass ~07:23Z)  
**Executor:** grok-box subagent via GitHub MCP (`zhekui-hub`). Box `gh` **not** logged in; no `JOINT_GH_TOKEN` / Thinkbook LocalShell.  
**Baseline (#7/#9):** driver#3 / synapse#3 `JOINT_CI_ID=exp-20260913-001b` were CLEAN; probe run `34743813433`; issue https://github.com/zhekui-hub/joint-ci-lab/issues/16  

**Windows path note:** Cannot write `C:\Users\23134\Downloads\...` from this box. Canonical copy: `/workspace/joint-ci/prototype/experiments/real_gh/PLAN_1_9_RESULTS.md` (+ mirrored into lab repo).

## Critical blockers

1. After ~06:54Z, participant `joint-ci-hook` still **succeeds** (writes same-repo `joint-ci=pending`) but **does not create new lab** `joint-ci` / `joint-real-probe` runs.  
   - Latest lab `joint-ci`: run #9 `34743896221` @ 06:54:14Z — https://github.com/zhekui-hub/joint-ci-lab/actions/runs/34743896221  
   - Latest probe: run #13 `34743813433` @ 06:52:13Z — https://github.com/zhekui-hub/joint-ci-lab/actions/runs/34743813433 (`total_count` still 13)  
   - Hook uses `secrets.JOINT_CI_DISPATCH_TOKEN` (fallback `GITHUB_TOKEN`); cross-repo `repository_dispatch` to lab is failing silently.  
2. MCP PAT **lacks `workflow` scope** — cannot create/update `.github/workflows/*` (explicit 403 on plan-5).

## Matrix

| # | Scenario | Verdict | Key evidence |
|---|---|---|---|
| 1 | Staggered wait | **FAIL** (infra) | Driver-only PR https://github.com/zhekui-hub/joint-ci-driver/pull/6 (`exp/plan-1`, SHA `91796b55c78e7709ba86b9424eed794e7b8ee255`, `JOINT_CI_ID=exp-plan-1`). Hook https://github.com/zhekui-hub/joint-ci-driver/actions/runs/34744890580 success; `joint-ci` **pending**; `mergeable_state=blocked`. **No** lab issue for `exp-plan-1`; **no** new `joint-ci` after `34743896221`; **no** new probe after `34743813433`. Synapse second PR https://github.com/zhekui-hub/joint-ci-synapse/pull/6 also pending/blocked — dual success incomplete. |
| 2 | Idempotency | **FAIL** (blocked) | Cannot re-dispatch. Historical only: issue #16 probe×1 id `34743813433` for `joint-49dd1b4acf88c69b` — not re-proven. Probe `total_count` remained 13. |
| 3 | Both-sides update | **FAIL** (blocked) | Driver SHA `c4468ea09b8d955386a7ef90fe4670ada4037bdb` + synapse SHA `c7434aed0fd50ba718a8c21f07710d9d62fd2e55`. Both `joint-ci=pending`, both blocked. Issue #16 still `succeeded` with stale SHAs — no invalidate / new joint_run_key / recover. |
| 4 | Single-side invalidate | **FAIL** (blocked) | Driver-only push https://github.com/zhekui-hub/joint-ci-driver/commit/c4468ea09b8d955386a7ef90fe4670ada4037bdb. Issue #16 never left succeeded → bilateral invalidate not proven. |
| 5 | Independent isolation | **FAIL** (tooling) | MCP 403 without `workflow` scope on `driver-independent-ci.yml`. Closed marker PRs driver#7 / synapse#7. Independent CI still success https://github.com/zhekui-hub/joint-ci-driver/actions/runs/34745070597. |
| 6 | Public failure double-block | **FAIL** (tooling) | Branch `exp/plan-6-probe-fail` created; probe yml `exit 1` edit failed (no workflow scope). Dual failure block not executed. |
| 7 | All-green allow | **PASS** (prior) | `exp-20260913-001b` CLEAN; issue #16 succeeded; probe `34743813433`. |
| 8 | Hard-gate block | **PASS** | SHAs driver=`7a567ad22ee3fa6a93b12669543355462d1487a5` synapse=`c3472a7441a4807c42147c2aca35391841c1c67b`. Both PRs blocked with joint-ci pending. https://github.com/zhekui-hub/joint-ci-driver/pull/3 https://github.com/zhekui-hub/joint-ci-synapse/pull/3 . Restore not completed. |
| 9 | Minimal mock | **PASS** (prior) | Only driver+synapse; no ChipLTech. |

## Overall migration decision

**NO-GO** for company migration.

### Blockers summary
1. `JOINT_CI_DISPATCH_TOKEN` broken/missing on driver/synapse after 06:54Z.
2. No Thinkbook `gh` / box token; MCP cannot write workflows.
3. #8 restore gap — baseline left pending/blocked.
4. #5/#6 need PAT with `workflow` scope.

### Next operator actions (Thinkbook)
1. Repair `JOINT_CI_DISPATCH_TOKEN`; verify new lab `joint-ci` run after hook.
2. `gh auth` with `workflow` scope; re-run #1→#2→#4→#3→#5→#6.
3. Re-dispatch dual success for driver#3/synapse#3.
4. Copy to `C:\Users\23134\Downloads\joint-ci-lab\prototype\experiments\real_gh\PLAN_1_9_RESULTS.md`.
