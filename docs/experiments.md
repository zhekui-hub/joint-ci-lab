# 跨仓库联合 CI — 验证实验设计（两层）

目标：用可重复实验证明「公共测试只跑一次、专属分别跑、等待不占 Pod、失效正确」。  
设计正文见 `design.md`。仓坐标：`zhekui-hub/joint-ci-*`（全私有，见 design §0.1）。禁止碰 ChipLTech。禁止自行 `git push`。

两层：

| 层 | 范围 | 能证明什么 | 不能证明什么 |
|---|---|---|---|
| **L1 烟雾** | E1–E10 | 主路径场景按宣称证据层绿 | 不等于生产可行 |
| **L2 大矩阵** | F-* / R-* / P-* | 失败模式、竞态、平台可行性 | 单项绿不能代替整层 PASS |

---

## 0. 证据边界（fake vs 真联调）— 先读

**写死，禁止混用。**

| 证据层 | 环境 | **只能**证明 | **不能**证明 |
|---|---|---|---|
| **fake** | 本地 prototype + fake GhClient / dry-run | 调度逻辑、Issue 状态机、去重键 / `joint_key`、汇总规则（公共 vs 专属、skipped 禁令） | 权限、跨仓 Check 回写、真实 Actions 派发、真实 runner 队列占用、跨仓事件时序 |
| **real** | 真联调 `zhekui-hub/joint-ci-lab` + `joint-ci-driver` / `joint-ci-synapse` / `joint-ci-sim` | 权限、Check 回写、Actions 派发、等待不占真实 runner、跨仓事件时序、同源 Check URL | — |
| **both** | 先 fake 再 real | 逻辑 + 平台各证各的 | 用 fake 结果替代 real |

硬规则：

1. fake 全绿 **禁止**宣称「生产可行」或「可迁公司仓」。
2. E2 waiting 不占 Pod：fake 只证「未 dispatch」；「真实 runner 队列无占用」必须 real。
3. E3 同源 Check URL、E5 旧绿勾失效可见：必须 real。
4. P-* 与标注 `real` 的 F/R：**仅**真联调。
5. 真联调范围仅 `zhekui-hub/joint-ci-*`，凭证/白名单外目标必须拒绝（F16）。

---

## 1. 环境与指标

### 1.1 环境

| 项 | 约定 |
|---|---|
| 主仓 | `zhekui-hub/joint-ci-lab`（Joint Issue，label `joint-ci`） |
| mock | `zhekui-hub/joint-ci-driver` / `joint-ci-synapse` / `joint-ci-sim` |
| 本地 | 优先 fake GhClient；稳定后由**协调者**同步到 `joint-ci-lab` |
| 分支 | 专用测试分支 + 小号 PR，不污染主干保护规则 |
| 禁令 | 不碰 ChipLTech；不自行 `git push`；不改 prototype 代码（本文只定实验） |

### 1.2 每次必记（两层共用）

| 字段 | 说明 |
|---|---|
| 层 / ID | 如 `smoke/E2`、`F8`、`P3` |
| 证据层 | `fake` / `real` / `both` |
| 结论 | PASS / FAIL + 一行原因 |
| 时间 | 开始 / 结束 |

**fake 另记**：输入 fixture、期望状态迁移、`joint_id` / `joint_key`、dry-run 派发列表、`public_runs`（逻辑计数）。

**真联调另记**（缺一项则该 real 项不算绿）：

| 记录项 | 用途 |
|---|---|
| PR 链接 × 各仓 | 关联 |
| head SHA × 各仓 | 与 `versions` / Check 对齐 |
| Joint Issue（lab） | 状态机证据 |
| Check Run id + URL × 各 PR | 同源 / 回写权限 |
| `workflow_run` id / dispatch id | 派发与去重 |
| Actions 队列截图或 API：等待期 runner 占用 | E2 real |
| 跨仓事件时间戳（synchronize / ready / dispatch） | 时序 |
| 权限失败原文（若测 F14/F16） | 可行性负例 |

### 1.3 指标

| 指标 | 期望 | 谁能证 |
|---|---|---|
| `public_runs` | ≈ 1 / 有效代码组合 | fake=逻辑计数；real=Actions 实跑次数 |
| `pod_wait_seconds` | 依赖未齐阶段 ≈ 0 | fake=未 dispatch；real=队列无占用 |
| `check_correctness` | 与归属规则一致 | fake=汇总函数；real=各 PR Check |
| `invalidation_latency` | push 后旧结果失效并 pending | fake=状态机；real=旧绿勾消失 |

---

## 2. 烟雾矩阵 E1–E10

保留既有主路径。每条必须标 `fake可证` / `真联调必证`。未标 real 的通过标准，fake 绿即可算烟雾该项绿；标了 real 的，烟雾可先记「fake 部分绿」，整项烟雾关闭仍等 real。

### 2.1 总表

| ID | 名称 | 验证点 | 期望 | fake可证 | 真联调必证 |
|---|---|---|---|---|---|
| E1 | 单仓基线 | 无联合标记的普通 PR | 行为与改造前一致 | 不进 joint、不建 Issue | 真实工作流集合与改造前抽样一致 |
| E2 | 错峰等待 | 先 Synapse joint，后 Driver | 等待期无测试 Pod；齐备后只跑一轮公共测试 | **未 dispatch**；`waiting_deps`；齐备后逻辑 `public_runs=1` | **真实 runner 队列无占用**；错峰 >N 分钟仍关联 |
| E3 | 公共去重 | Driver+Synapse 同一 MultiRepo | `public_runs=1`，两边 Check 同源 | 派发列表 1 条；同 `joint_key` | **两边 Check URL 同一 `workflow_run`**；Actions `public_runs=1` |
| E4 | 专属保留 | Driver 额外 16 卡 Pseudo | 仅 Driver 受影响 | 汇总不广播专属失败 | 真实 Check：Driver fail / Synapse 仍可 success |
| E5 | Push 失效 | 联合通过后一侧 push | 旧 Check invalidate；两侧 waiting/pending | 新 `joint_key`；旧态 `invalidated` | **两侧旧绿勾不可用**；新 Check pending→重跑 |
| E6 | 冲突/多 PR | 同名分支多个开放 PR | 明确失败原因，不派发 | 不 dispatch；failure 原因=多 PR 歧义 | Check 文案在 PR 上可见 |
| E7 | Draft 门闸 | 关联 PR 为 Draft | 不跑重测试；Ready 后启动 | `all_ready` 门闸；Draft 不派发 | Draft 阶段无真实重测 job |
| E8 | 非联合依赖 | 依赖分支无 PR 且非 `JOINT_WAIT` | 固定 tip SHA 单边（旧行为） | 走 fixed-dep；无长期 waiting Issue | 与改造前「指定依赖分支」文档一致 |
| E9 | 取消/关闭 | 一侧关闭或退出 joint | 关联解除，停止放行 | `cancelled`；对侧不放行 | 已启动 run：能取消则取消；否则结果不用于合入 |
| E10 | 三仓并集 | Driver+Synapse+Sim | 并集正确；Sim 关 abs_unit 不误删 | `merge_remote_tests` 并集正确 | 真实派发列表 = 并集 |

### 2.2 E1 单仓基线

**步骤**

1. 开普通 Driver PR，不写 `CI_MODE=joint`
2. 观察路由与测试集合

**通过标准**

- 工作流集合与改造前抽样 PR 一致（允许文档化的等价重命名）— **real**
- 无 Arsenal Joint Issue 创建 — **fake 可证**

| 层 | 可证 |
|---|---|
| fake | 路由不进联合；不创建 Issue |
| real | 真实 Actions 集合与基线一致 |

### 2.3 E2 错峰等待（核心）

**步骤**

1. 创建 Synapse PR：`CI_MODE=joint`，`DLC_KERNEL_DRIVER_BRANCH=feature/e2-driver`，`JOINT_WAIT=true`
2. 确认 waiting Check；查 runner/队列，确认无重测试 job
3. 隔 **>5 分钟**（真联调；fake 可缩短时钟）创建对应 Driver PR（同分支）并 Ready
4. 观察自动匹配与一次联合运行

**通过标准**

- `pod_wait_seconds ≈ 0`
- 依赖齐备后恰好 1 次公共测试派发
- Synapse 侧从 waiting → 与 Driver 同源的成功/失败

**失败信号**

- 等待期已有 MultiRepo/Pseudo runner 占用
- Driver 创建后双方又各跑一套公共测试

| 层 | 可证 | 不可证 |
|---|---|---|
| fake | 未写入 dry-run 派发；状态 `waiting_deps`；齐备后只派一次 | 真实队列占用 |
| real | 真实 runner 队列无占用；错峰后仍能关联（交叉 P4） | — |

### 2.4 E3 公共去重

**前置**：E2 成功路径，或同时 Ready 的一对 joint PR

**测量**

- 调度日志：`joint_key`、派发列表
- Actions 中 MultiRepo（或 ARC）run 数量与 Check URL

**通过标准**

- 相同 `(test_id, params, env, versions)` 只出现一次 run
- 两个 PR 的对应 Check 指向同一 `workflow_run` / 日志 URL — **真联调必证**

| 层 | 可证 |
|---|---|
| fake | 合并后公共项只派 1 次；同 `joint_key` 不双派发 |
| real | 两边 Check URL 同源；Actions `public_runs=1` |

### 2.5 E4 专属测试隔离

**步骤**

1. 构造 Driver 路由会选出 16 卡 Pseudo、Synapse 不会的改动组合
2. 人为让 16 卡失败（或注入失败 fixture）

**通过标准**

- Driver 合入检查失败
- Synapse 合入检查仍可通过（若公共与其专属均成功）
- 汇总逻辑未把专属失败广播到另一侧

| 层 | 可证 |
|---|---|
| fake | `per_pr_required` / `exclusive_to` 不广播 |
| real | 真实 Check 结论符合上表 |

### 2.6 E5 失效与重跑

**步骤**

1. 联合成功后，仅向 Synapse push 空 commit
2. 观察两侧联合 Check
3. 等待自动重跑完成

**通过标准**

- 两侧旧成功状态均失效（非残留绿勾）— **真联调必证可见性**
- 新 `joint_key`（Synapse SHA 变）触发新一轮 — fake 可证
- Driver SHA 未变，但联合结果仍绑定新组合

| 层 | 可证 |
|---|---|
| fake | `invalidated` → 新 `joint_key` → 新一轮 |
| real | PR 上旧绿勾不可用于合入；新 Check pending |

### 2.7 E6 歧义依赖

**步骤**

1. 同分支名开两个 Driver PR
2. Synapse joint 指向该分支

**通过标准**

- Check 失败信息指出「多 PR 歧义」
- `public_runs=0`

| 层 | 可证 |
|---|---|
| fake | 不派发；failure 原因正确 |
| real | PR Check 文案可见 |

### 2.8 E7 Draft 门闸

**步骤**

1. Driver PR 保持 Draft，Synapse Ready + joint
2. 再将 Driver 标 Ready

**通过标准**

- Draft 阶段无重测试
- Ready 后自动启动

| 层 | 可证 |
|---|---|
| fake | 保持 `waiting_deps`（reason=draft）；Ready 后进入 ready |
| real | Draft 阶段无真实重测 job |

### 2.9 E8 兼容旧依赖

**步骤**

1. Synapse 指定已存在的 Driver 分支但无 PR，且不设 `JOINT_WAIT`
2. 观察是否按 tip SHA 单边跑（现有语义）

**通过标准**

- 不创建长期 waiting Issue（或立即转为 fixed-dep 模式）
- 行为与改造前「指定依赖分支」文档一致

| 层 | 可证 |
|---|---|
| fake | 路径走 fixed-dep，不长期 waiting |
| real | 与现网文档行为一致 |

### 2.10 E9 退出联合

**步骤**

1. 联合 running 或 waiting 中关闭一侧 PR / 去掉 `CI_MODE=joint`
2. 观察调度器

**通过标准**

- Issue 状态 `cancelled`/`invalidated`
- 另一侧不再被该联合结果放行
- 已启动的可取消则取消，不能取消则结果不用于合入 — **取消效果 real 必证**

| 层 | 可证 |
|---|---|
| fake | 状态 `cancelled`；对侧汇总不放行 |
| real | 真实 run 取消或结论不进合入 |

### 2.11 E10 三仓并集

**步骤**

1. 三仓 joint，制造 Sim 关闭 `enable_abs_unit`、Synapse 需要某公共项的组合
2. 核对合并后的派发列表

**通过标准**

- 派发 = 三方需求并集
- 不得因 Sim 配置而丢掉 Synapse 需要的子项
- 仅参数不同的 Pseudo 仍分多条任务

| 层 | 可证 |
|---|---|
| fake | `merge_remote_tests` 并集与 identity 分条 |
| real | 真实派发列表与并集一致 |

---

## 3. 鲁棒性与可行性大矩阵

烟雾只覆盖主路径。本层覆盖失败、竞态、平台可行性。每行：场景 / 期望 / 通过标准 / 证据层（`fake`\|`real`\|`both`）。

### 3.1 必须覆盖的失败模式（F-*）

| ID | 场景 | 期望行为 | 通过标准 | 证据层 |
|---|---|---|---|---|
| F1 | 双方 `deps` 声明冲突（分支名互指不一致） | 不派发；Check failure（声明冲突） | `public_runs=0`；文案含「声明冲突」；Issue 不进 `running` | both |
| F2 | 同分支多个开放 PR（E6 升级：对侧也多 PR / 三仓其一歧义） | 歧义失败，不派发 | 同 E6；任一仓 >1 开放 PR 即整组不派发 | both |
| F3 | 依赖分支不存在或已删 | 失败，不 waiting 死等 | Check failure（分支找不到）；无重测派发 | both |
| F4 | 脏 PR body（缺 `=`、重复键、乱码、只写 joint 无分支） | 解析失败或拒绝进联合 | 不静默当单仓成功；failure/明确忽略规则与文档一致 | both |
| F5 | running 中一侧退出 joint / 关 PR / 转 Draft | 关联解除或回等待；停止放行 | Issue `cancelled` 或 `invalidated`/`waiting_deps`；对侧不放行；进行中 run 按 E9 | both |
| F6 | 同时存在公共失败 + 一侧专属失败 | 公共失败双方 failure；专属只打对应仓 | 汇总矩阵与 design §5.5 一致；无错广播、无错漏打 | fake（逻辑）；real 抽检 Check |
| F7 | 联合未完成 / 等待中 | **禁止** `skipped` 冒充通过 | Check 为 `pending` 或 `failure`；合入检查不得因 skipped 绿 | both |
| F8 | 同 `joint_key` 双边几乎同时上报 | 不双派发 | dry-run/Actions 公共项恰好 1；第二次挂到同一 `workflow_runs` | both |
| F9 | `joint_key` 变（SHA/镜像/配置）后旧绿勾 | 旧结果不可用；回 pending | 新 key 新一轮；旧 success 不参与合入判断 | both（可见性 real） |
| F10 | 仅镜像或测试配置变、SHA 不变 | `joint_key` 变 → 失效重跑 | 新旧 key 不同；旧结果 `invalidated` | both |
| F11 | 一侧 `JOINT_WAIT`，另一侧同组合走 fixed-dep（非 joint） | 不把 fixed-dep 单边结果当联合放行 | 非 joint 侧不纳入联合；joint 侧按声明等待或失败；禁止混用同一 `joint_key` 放行 | both |
| F12 | 三仓：两仓 Ready，一仓 Draft | 保持 `waiting_deps`；不派发重测 | `public_runs=0`；Draft 转 Ready 后才 `ready` | both |
| F13 | 取消后仍跑完的旧 run | 结果不得用于合入 | 汇总忽略该 run；Check 非 success 放行 | both |
| F14 | App/token 对某仓无 `checks:write` | 写回失败可见；不假装成功 | 调度记权限错误；不把「没写成」当 success | **real** |
| F15 | `workflow_dispatch` 失败（5xx/校验错） | 可重试失败项；同 key **不重入**第二套并行跑 | 重试次数有上限；同时 `running` 的同 key 公共 run ≤ 1 | **real** |
| F16 | dispatch / 写 Check 目标不在 `zhekui-hub/joint-ci-*` | **拒绝** | 无对白名单外仓的 API 写/派发；日志明确拒绝 | **real** |

### 3.2 并发与竞态（R-*）

| ID | 场景 | 期望行为 | 通过标准 | 证据层 |
|---|---|---|---|---|
| R1 | 两边几乎同时 `synchronize` | 串行处理；最多一轮有效派发对应最终 SHA 组合 | 最终 `versions` = 两侧最新 SHA；无「各派一套旧 SHA」 | both |
| R2 | `waiting`→`ready` 与一侧 push 交错 | 不得用过期 SHA 派发 | 派发前重查 head；不一致则 `invalidated` 再固定 | both |
| R3 | 调度器两次唤醒重叠（无锁或锁失效） | 同 `joint_id` 不双派发 | 有锁：第二唤醒短退出；无锁再现时仍靠 `joint_key` 禁第二套公共 run | both |

### 3.3 权限与平台可行性（P-*，仅真联调）

全部 **real**。仓：`zhekui-hub/joint-ci-*`。fake 绿 **不算**本表。

| ID | 场景 | 期望行为 | 通过标准 | 证据层 |
|---|---|---|---|---|
| P1 | 跨仓写 Check 到 driver / synapse / sim | App 向三仓指定 `head_sha` 写 Check | 三仓 PR 上均可见 joint Check；URL 可点 | **real** |
| P2 | Joint Issue 落在 lab + label | 只在 `joint-ci-lab` 建 Issue | Issue 存在；label=`joint-ci`；标题含 `joint_id`；mock 仓无多余联合 Issue | **real** |
| P3 | 显式 SHA 触发 mock workflow | dispatch 带固定 SHA，不靠 `github.head_ref` | 被调 workflow inputs 含三仓 SHA；run 检出处与 inputs 一致 | **real** |
| P4 | 错峰 >N 分钟（默认 N=5）后对侧才开 PR | 仍匹配同一 `joint_id`，只跑一轮 | 等待期无真实 runner；齐备后 `public_runs=1` | **real** |
| P5 | 真实 Actions 公共测试 | 相同组合只跑一份 | 该 `joint_key` 下 `public_runs=1`（API/UI 计数） | **real** |

### 3.4 通过标准总表（什么叫 PASS）

| PASS 名 | 必须绿的项 | 明确 **不算** |
|---|---|---|
| **烟雾 PASS** | E1–E10 在**宣称的证据层**全部绿（只标 fake 的项 fake 即可；标了 real/both 的 real 部分也要绿才关该项） | 大矩阵未跑；fake 替代 real |
| **逻辑 PASS** | fake 下：状态机 + 去重 + 汇总 + F/R 中证据层为 `fake` 或 `both` 的项全绿 | P-*；F14/F15/F16；「生产可行」 |
| **可行性 PASS** | 全部 P-* + F/R 中标 `real` 的项 + **E2 / E3 / E5 真联调**绿 | 仅 fake 全绿 |
| **禁止** | — | **用 fake 全绿宣称「生产可行」** |

执行约定：

- 先 L1 烟雾（fake），再 L2 中 `fake|both`，最后真联调（P-* + real 项 + E2/E3/E5）。
- both 项：fake 失败则不必上 real；fake 绿仍必须补 real 才关该项。
- 任一层红：只宣称该层未过，不得用其他层绿抵消。

---

## 4. 门禁：哪一层绿才能进 P1 / P2 / …

与 `design.md` §9 对齐。两列门禁：**烟雾/逻辑（fake）** vs **可行性（真联调）**。

| 要进入 | 烟雾/逻辑（fake）必须 | 可行性（真联调）必须 | 未满足时 |
|---|---|---|---|
| **P0 关闭** | 本文 + design 评审通过（含 §0 证据边界） | 无 | 不开始实现 |
| **P1 骨架** | E2 fake：未 dispatch + `waiting_deps→ready`；单测状态机 | 不要求 | 可写 scheduler dry-run；**不可**对真网宣称不占机 |
| **P2 真派发开发** | 逻辑 PASS 中与去重/汇总相关：E3/E4 + F6/F7/F8（fake） | 建议开 P1/P3 真联调，但**不**用 fake 代替同源 URL | 禁止宣布「公共只跑一次（生产）」 |
| **P3 自动关联** | E2+E5+E7 的 fake 部分绿 | **E2/E3/E5 real** 至少各过一次；P3 显式 SHA 绿 | 禁止关「错峰不占真实 runner / 旧绿勾失效」 |
| **P4 harden** | E6+E9+E10 + E1/E8；F/R 中 `fake\|both` 全绿（逻辑 PASS） | P1–P5 + F14/F15/F16 + E2/E3/E5 real → **可行性 PASS** | 无可行性 PASS **禁止**迁公司仓 / 宣称生产可行 |
| **迁公司仓** | 逻辑 PASS | 可行性 PASS | 一律拒绝 |

阶段成功标准（可执行）：

| 阶段 | 烟雾/逻辑（fake） | 可行性（真联调） |
|---|---|---|
| P1 | E2 人工/自动：waiting 未 dispatch | 可选预跑 P4 错峰；不挡 P1 代码 |
| P2 | E3+E4 自动化绿（逻辑计数 / 汇总） | 同源 Check 与 `public_runs=1` 记在可行性，不记在 fake |
| P3 | E2+E5+E7 fake 绿 | E2 真实队列空闲 + E5 旧绿勾失效 + E7 无真实重测 job |
| P4 | E6+E9+E10、E1/E8、大矩阵 fake\|both | 可行性 PASS |

---

## 5. 脚手架

实现时在 `joint-ci-lab` 增加 `joint_ci/experiments/`（本地先写 `/workspace/joint-ci`，由协调者推送）：

| 文件 | 作用 |
|---|---|
| `scenario.yaml` | 参与仓、分支、标记、期望断言、`evidence: fake\|real\|both` |
| `run_experiment.sh` | 默认 fake backend；真连用 `gh`（**仅** `zhekui-hub/joint-ci-*`）建分支/PR、写 body、轮询 Check |
| `assert.py` | 断言 `public_runs`、Check 结论、Issue 状态机、去重键、F/R 期望 |
| `workflow_dispatch` 入口 | 仅维护者；真联调手工入口 |

最小断言（fake 逻辑；real 另加 URL/队列）：

```python
assert waiting_phase_runner_seconds(joint_id) < 30  # real：查真实队列；fake：无 dispatch
assert count_runs(joint_id, test_id="multirepo_runtest") == 1
assert same_run_url(driver_check, synapse_check)  # 仅 real
assert check_state(driver, "joint") == "failure"  # E4
assert check_state(synapse, "joint") == "success"
assert check_conclusion_not_skipped_as_pass(pr)
```

脚手架不得对白名单外仓库发 API（F16）。
