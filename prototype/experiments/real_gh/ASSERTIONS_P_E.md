# 真联调可执行步骤与断言（P1–P5 · E2/E3/E5）

坐标：仅 `zhekui-hub/joint-ci-{lab,driver,synapse,sim}`。本机 Codex / Thinkbook 写 live；本目录不与 Codex harden 抢改 runner。

证据规则：P-* 与标 real 的项必须以真仓证据为绿；本地 fake E1–E42 只算可行性旁证。

---

## P1 — Check / status 回写
**步骤**
1. 任一对 joint PR Ready 后触发调度。
2. 在 driver + synapse 上读 Check 或 Commit Status（PAT 无 `checks:write` 时允许 status fallback）。

**断言**
- 两侧均出现 `joint-ci`（或约定名）结论。
- 公共成功时两侧 `details_url` / target_url **同源**（同一 workflow_run）。
- 失败时结论与归属规则一致（见 E4）。

**现状（协调者同步）:** 已有 live；P1 用 status fallback。

---

## P2 — Joint Issue 落 lab
**步骤:** 齐备后查 `joint-ci-lab` Issues，label=`joint-ci`。

**断言**
- Issue 仅在 lab，不在三业务仓。
- body 含 joint 状态块（status / joint_key / versions）。
- waiting→running→succeeded|failed 可追踪。

**现状:** Issue #4/#5 已有证据。

---

## P3 — 显式 SHA dispatch
**步骤:** 打开 lab Actions 对应 `workflow_dispatch` / run。

**断言**
- inputs / 日志含 driver+synapse（+sim）**明确 head SHA**，非隐式 tip 漂移。
- 同一 `joint_key` 下公共测试只派发一次。

**现状:** dispatch run `34737873359` 已引用。

---

## P4 — 等待不占机（错峰）
**步骤:** 见 E2；先开 synapse `JOINT_WAIT`，≥5min 再开 driver。

**断言**
- waiting 阶段：**无** multirepo/pseudo 重测试 runner job。
- `pod_wait_seconds ≈ 0`（或 Actions 无对应 run）。

**现状:** synapse PR#1 错峰中（协调者）。

---

## P5 — 白名单 / 安全边界（F16）
**步骤:** 对非 `zhekui-hub/joint-ci-*` 目标跑拒绝探测。

**断言:** 拒绝且不派发。已有 live PASS（协调者）。

---

## E2 — 错峰等待（real）
1. synapse：`exp/ci-e2-synapse`，body `CI_MODE=joint` + `JOINT_WAIT=true` + `DLC_KERNEL_DRIVER_BRANCH=exp/ci-e2-driver`
2. 确认 waiting + **无重测试 job**；等 ≥5min 再确认一次
3. driver：同分支 Ready + `CI_MODE=joint`
4. **断言:** 公共 `public_runs=1`；两侧离 waiting；无「等待期占机」；禁止双跑公共测试

**进行中:** https://github.com/zhekui-hub/joint-ci-synapse/pull/1

---

## E3 — 公共去重 + 同源 URL（准备断言）
**前置:** E2 成功对，或同时 Ready 的一对 joint PR。

| # | 采集 | 断言 |
|---|---|---|
| 1 | lab Issue / 日志 `joint_key` | 两侧 versions 写入同一 key |
| 2 | Actions：该 key 下 public test runs | `count == 1` |
| 3 | driver vs synapse Check/status URL | **完全相同** |
| 4 | 可选：再触发一次同 SHA 调度 | 不新增 public run（或显式 no-op） |

**Fail:** 两侧各一条 public run；URL 不一致。

**采集命令提纲**
```bash
# 替换 OWNER/PR/SHA；仅 zhekui-hub
gh api repos/zhekui-hub/joint-ci-driver/commits/$DRIVER_SHA/status
gh api repos/zhekui-hub/joint-ci-synapse/commits/$SYNAPSE_SHA/status
gh run list -R zhekui-hub/joint-ci-lab --limit 20
```

---

## E5 — Push 失效（准备断言）
**前置:** E3 绿对；记下 `joint_key0`、两侧 conclusion、public run id。

| # | 步骤 | 断言 |
|---|---|---|
| 1 | synapse 空 commit push | 旧 success **不得**残留为可合入绿 |
| 2 | 两侧 joint 进入 pending/invalidated | 无陈旧绿勾 |
| 3 | 新 round | `joint_key1 != joint_key0`（synapse SHA 变） |
| 4 | driver SHA 未变 | 仍绑定新组合 |
| 5 | Actions | 新增恰好 **1** 次 public run（相对失效前） |

**Fail:** 旧绿仍 mergeable；key 不变却重跑混乱；双侧各跑一套。

```bash
# synapse 空提交后
git -C synapse commit --allow-empty -m "e5-invalidate" && git push
# 再采 status / Issue / runs，对比失效前后
```

---

## 本环境限制
- 共享机 `gh` **未登录**、无 `JOINT_GH_TOKEN` → 不能代跑真联调。
- 不改 Thinkbook/`feat/real-gh-feasibility` 上 Codex 正在 harden 的 runner。
- 本地旁证：`cd prototype/experiments && python3 run_local.py` → E1–E42 应 42/42。

## 下一步
1. E2 满 5min 后开 driver → 记 P4/E2 证据进 Thinkbook `RESULTS_REAL.md`
2. 用上表收 E3（同源 URL + public_runs=1）
3. 空推 synapse 收 E5
4. 有 token 时把本机 `RESULTS_REAL.md` 与 Thinkbook 证据合并回 lab

---

## 自动化收证脚本（共享盘）

`assert_e3_e5.py` — 只读采集 / E3 断言（需 Thinkbook `gh` 登录）：

```bash
export JOINT_OWNER=zhekui-hub
export JOINT_DRIVER_PR=1 JOINT_SYNAPSE_PR=1
python3 assert_e3_e5.py collect
python3 assert_e3_e5.py assert-e3   # same target_url && public_runs_estimate==1
python3 assert_e3_e5.py plan-e5
# E4（注入专属失败后）:
# python3 assert_e3_e5.py assert-e4 --driver-sha SHA --synapse-sha SHA
```

前置：lab Actions 已挂 scheduler，synapse#1/driver#1 auto-match 完成后再跑 assert-e3。
