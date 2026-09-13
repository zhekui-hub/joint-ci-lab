# 跨仓库联合 CI — 验证实验设计

目标：用可重复实验证明「公共测试只跑一次、专属分别跑、等待不占 Pod、失效正确」。

> 设计正文见 `design.md`。实验仓坐标见 design §0.1（`zhekui-hub/joint-ci-*`，全私有）；阶段门禁与 P0–P4 对齐 design §9。禁止碰 ChipLTech。

## 0. 实验环境约定

- 主仓 `zhekui-hub/joint-ci-lab`；mock：`joint-ci-driver` / `joint-ci-synapse` / `joint-ci-sim`
- 本地优先对接 prototype fake backend；稳定后由协调者同步推到 `joint-ci-lab`
- 使用专用测试分支与小号 PR，避免污染主干保护规则
- 记录每次实验的：PR 链接、head SHA、Joint Issue（lab）、workflow_run id、开始/结束时间、runner 占用时长
- 指标：
  - `public_runs`：公共测试实际启动次数（期望 ≈ 1 / 有效代码组合）
  - `pod_wait_seconds`：依赖未齐备阶段 runner 占用秒数（期望 ≈ 0）
  - `check_correctness`：各 PR Check 是否与归属规则一致
  - `invalidation_latency`：push 后旧结果失效并进入 pending 的时间

## 实验矩阵

| ID | 名称 | 验证点 | 期望 |
|---|---|---|---|
| E1 | 单仓基线 | 无联合标记的普通 PR | 行为与改造前一致 |
| E2 | 错峰等待 | 先 Synapse joint，后 Driver | 等待期无测试 Pod；齐备后只跑一轮公共测试 |
| E3 | 公共去重 | Driver+Synapse 需要同一 MultiRepo | `public_runs=1`，两边 Check 同源 |
| E4 | 专属保留 | Driver 额外 16 卡 Pseudo | 仅 Driver 失败/成功受影响 |
| E5 | Push 失效 | 联合通过后一侧 push | 旧 Check invalidate；两侧回到 waiting/pending |
| E6 | 冲突/多 PR | 同名分支多个开放 PR | 明确失败原因，不派发 |
| E7 | Draft 门闸 | 关联 PR 为 Draft | 不跑重测试；Ready 后启动 |
| E8 | 非联合依赖 | 依赖分支无 PR 且非 JOINT_WAIT | 固定 tip SHA 单边验证（兼容旧行为） |
| E9 | 取消/关闭 | 一侧关闭或退出 joint | 关联解除，停止放行 |
| E10 | 三仓并集 | Driver+Synapse+Sim | 并集正确；Sim 关闭 abs_unit 不被错误省略 |

---

## E1 单仓基线

**步骤**

1. 开普通 Driver PR，不写 `CI_MODE=joint`
2. 观察路由与测试集合

**通过标准**

- 工作流集合与改造前抽样 PR 一致（允许文档化的等价重命名）
- 无 Arsenal Joint Issue 创建

## E2 错峰等待（核心）

**步骤**

1. 创建 Synapse PR：`CI_MODE=joint`，`DLC_KERNEL_DRIVER_BRANCH=feature/e2-driver`，`JOINT_WAIT=true`
2. 确认出现 waiting Check；查询 runner/队列，确认无重测试 job
3. 隔 >5 分钟创建对应 Driver PR（同分支）并 Ready
4. 观察自动匹配与一次联合运行

**通过标准**

- `pod_wait_seconds ≈ 0`
- 依赖齐备后恰好 1 次公共测试派发
- Synapse 侧从 waiting → 与 Driver 同源的成功/失败

**失败信号**

- 等待期已有 MultiRepo/Pseudo runner 占用
- Driver 创建后双方又各跑一套公共测试

## E3 公共去重

**前置**：E2 成功路径，或同时 Ready 的一对 joint PR

**测量**

- Arsenal 调度日志中的 `joint_key`、派发列表
- Actions 中 MultiRepo（或 ARC）run 数量

**通过标准**

- 相同 `(test_id, params, env, versions)` 只出现一次 run
- 两个 PR 的对应 Check 指向同一 `workflow_run` / 日志 URL

## E4 专属测试隔离

**步骤**

1. 构造 Driver 路由会选出 16 卡 Pseudo、Synapse 不会的改动组合
2. 人为让 16 卡失败（或注入失败 fixture）

**通过标准**

- Driver 合入检查失败
- Synapse 合入检查仍可通过（若公共与其专属均成功）
- 汇总逻辑未把专属失败广播到另一侧

## E5 失效与重跑

**步骤**

1. 联合成功后，仅向 Synapse push 空 commit
2. 观察两侧联合 Check
3. 等待自动重跑完成

**通过标准**

- 两侧旧成功状态均失效（非残留绿勾）
- 新 `joint_key`（因 Synapse SHA 变）触发新一轮
- Driver SHA 未变，但联合结果仍绑定新组合

## E6 歧义依赖

**步骤**

1. 同分支名开两个 Driver PR
2. Synapse joint 指向该分支

**通过标准**

- Check 失败信息指出「多 PR 歧义」
- `public_runs=0`

## E7 Draft 门闸

**步骤**

1. Driver PR 保持 Draft，Synapse Ready + joint
2. 再将 Driver 标 Ready

**通过标准**

- Draft 阶段无重测试
- Ready 后自动启动

## E8 兼容旧依赖

**步骤**

1. Synapse 指定已存在的 Driver 分支但无 PR，且不设 `JOINT_WAIT`
2. 观察是否按 tip SHA 单边跑（现有语义）

**通过标准**

- 不创建长期 waiting Issue（或立即转为 fixed-dep 模式）
- 行为与改造前「指定依赖分支」文档一致

## E9 退出联合

**步骤**

1. 联合 running 或 waiting 中关闭一侧 PR / 去掉 `CI_MODE=joint`
2. 观察调度器

**通过标准**

- Issue 状态 `cancelled`/`invalidated`
- 另一侧不再被该联合结果放行
- 已启动的可取消则取消，不能取消则结果不用于合入

## E10 三仓并集

**步骤**

1. 三仓 joint，制造 Sim 关闭 `enable_abs_unit`、Synapse 需要某公共项的组合
2. 核对合并后的派发列表

**通过标准**

- 派发 = 三方需求并集
- 不得因 Sim 配置而丢掉 Synapse 需要的子项
- 仅参数不同的 Pseudo 仍分多条任务

---

## 自动化实验脚手架（实现时一并交付）

建议在 Arsenal 增加 `joint_ci/experiments/`：

1. `scenario.yaml`：描述参与仓、分支名、标记、期望断言
2. `run_experiment.sh`：用 GitHub App / `gh` 建分支与 PR、写 body、轮询 Check
3. `assert.py`：断言 `public_runs`、Check 结论、Issue 状态机迁移
4. CI 中的 `workflow_dispatch` 手工实验入口（仅维护者可跑）

最小断言伪代码：

```python
assert waiting_phase_runner_seconds(joint_id) < 30
assert count_runs(joint_id, test_id="multirepo_runtest") == 1
assert same_run_url(driver_check, synapse_check)
assert check_state(driver, "joint") == "failure"  # E4
assert check_state(synapse, "joint") == "success"
```

## 发布门禁建议

与 `design.md` §9 一致：

- P0：设计评审通过（含 Origin 边界与开放问题建议默认）

- P1：E2 人工过一遍（等待不占机）
- P2：E3 + E4 必须自动化绿
- P3：E2 + E5 + E7 绿
- P4：E6 + E9 + E10 绿；E1/E8 回归绿
