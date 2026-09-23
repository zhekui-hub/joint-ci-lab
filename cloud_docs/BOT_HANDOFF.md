<!-- Author: zhekui. Cloud execution instructions; no executable entry points. -->
# Joint CI 云电脑测试交接

## 交付边界

本包验证 `cloud_joint` Python 协调器、独立本机进程 worker、SQLite 状态和 JC-01～JC-44 场景。它不是历史 `coordinate.sh` 的完整续测，也不是生产 CI 替换包。历史 120 用例的 harness、oracle 和最新冻结源码未找到，因此不得报告“120 用例通过”或混用旧结果。

无需修改用户 PR 模板、添加 group ID 或新命令是设计目标；当前合成 fixture 只验证解析与状态规则。真实历史 PR 原样回放、GitHub 原生取消/重跑、Checks 写回、Pod/Runner 与生产接入，必须另有真实证据。

## 环境和权限

- Linux 云电脑，Python >= 3.10，普通用户；本机测试不需要 GitHub token、sudo、Docker 或集群权限。
- 从同一个冻结包解压，每个 Bot 一份独立目录；不得改源码、依赖或测试断言。若包提供外部 SHA256 文件，先校验压缩包；`preflight` 再检查源码身份和存在的 `SOURCE_MANIFEST.json`。
- 不允许多个主机访问同一个 SQLite 文件，不使用 NFS 共享状态。不同机器只能交换完成后的证据文件；同机单次运行内部并发由 CLI 管理。
- 不注册生产 Runner，不写生产仓、不修改 required checks、不 push、不创建 PR、不部署。真实实验只允许已明确授权的隔离 `joint-ci-*` 仓；Runner 标签必须指向云电脑 self-hosted Runner，禁止偷偷改成 `ubuntu-latest`。
- 本包没有在此交接中提供完整 Runner 注册和真实四仓 CI 部署命令。缺少仓库授权、真实入口、凭证、Runner 标签或映像/挂载/资源时，将对应阶段标记 `BLOCKED`；不得猜测配置或临时创造通过结果。

## 冻结运行身份

主 Bot 确认最终包及 source_id，生成一次 `run_id`（只含字母、数字、下划线、点、短横线，最长 100 字符），发给全部 Worker。每个 Worker 执行 `python3 tools/acceptance.py preflight`，把输出中的 source_id 与主 Bot 比对。一个 Worker 用 `1/3`、一个用 `2/3`、一个用 `3/3`；不能各跑 `1/1` 再混合。

运行目录在源码包外，所有 `--out` 指向不存在的新目录。脚本拒绝复用目录；失败目录保留，不删除、不覆盖。改动源码后应由 Build 产生新包、新 source_id、新 run_id，整组重新执行；同一候选版本复测也应使用新 run_id，避免旧证据混入。

## 执行顺序

以下均在解压包根目录执行。`RUN_ID` 是主 Bot 分发的同一值；`RUN_ROOT` 是当前机器的独立路径。先替换占位值，不能逐字运行 `REPLACE_WITH_MASTER_RUN_ID`。

```bash
export PYTHONDONTWRITEBYTECODE=1
RUN_ID='REPLACE_WITH_MASTER_RUN_ID'
RUN_ROOT="$HOME/joint-ci-runs/$RUN_ID-worker1"
mkdir -p "$RUN_ROOT"
python3 tools/acceptance.py preflight > "$RUN_ROOT/preflight.json"
python3 tools/acceptance.py run --run-id "$RUN_ID" --shard 1/3 --jobs 2 --timeout 45 --out "$RUN_ROOT/shard-1"
python3 tools/acceptance.py verify "$RUN_ROOT/shard-1"
```

Worker 2 将 worker1、1/3、shard-1 分别换成 worker2、2/3、shard-2；Worker 3 对应替换为 3。预期分片数量 15、15、14，总计 44。非零退出码表示失败或设置问题；保留 stdout/stderr、checkpoint 和原始 evidence，先定位当前层，不无休止重跑。

`run` 的 rc=0 只表示本地断言通过；带 `external_required` 的条目仍为 `NOT_EVALUATED`。`verify` 复算断言、原始状态合入门禁、审计唯一性、结果身份和 CSV/JSON 一致性，不是另一个独立实现的生产 oracle。

## 主 Bot 汇总

将三个完整 shard 目录搬到主 Bot 的 `incoming`，保持内部相对路径；原目录及日志都要保留。主 Bot 使用同一份冻结源码执行：

```bash
python3 tools/acceptance.py merge "$RUN_ROOT/incoming/shard-1" "$RUN_ROOT/incoming/shard-2" "$RUN_ROOT/incoming/shard-3" --out "$RUN_ROOT/canonical"
python3 tools/acceptance.py verify "$RUN_ROOT/canonical"
```

此时主 Bot 的 RUN_ROOT 应为其自身目录（例如 `$HOME/joint-ci-runs/$RUN_ID-master`）。成功条件是同 source_id、同 run_id、JC-01～JC-44 恰好一次、`complete_inventory=true`、独立复算 `mismatch=0`，以及本地失败数 0。混合身份、重复 ID、缺失 ID、无证据的 PASS 必须失败关闭。

`canonical/results-all.csv` 是唯一权威总表；各 shard 的 CSV 属于输入证据，不能改名成新的总表。`merge` 同时复制逐用例日志；仍需保留原 incoming shard，以便追溯汇总输入。

## 容量测试：10 → 100 → 1000

容量测试独占一台空闲云电脑，由一个 Bot 顺序执行。先记录 CPU/内存/磁盘可用量，避免与三个分片并跑造成不可解释的资源争抢。默认 4 个 worker、2 个 controller、每组最多 2 个 worker；每一档通过并核实清理后才进入下一档。记录合成输入、机器规格、参数和 source_id；不能把本地吞吐量换算成生产 Pod 容量。

```bash
timeout --signal=TERM --kill-after=10s 90s python3 tools/capacity.py --groups 10 --jobs 4 --controllers 2 --per-group 2 --delay 0.03 --max-seconds 60 --out "$RUN_ROOT/capacity-10"
timeout --signal=TERM --kill-after=10s 180s python3 tools/capacity.py --groups 100 --jobs 4 --controllers 2 --per-group 2 --delay 0.03 --max-seconds 120 --out "$RUN_ROOT/capacity-100"
timeout --signal=TERM --kill-after=10s 360s python3 tools/capacity.py --groups 1000 --jobs 4 --controllers 2 --per-group 2 --delay 0.03 --max-seconds 300 --out "$RUN_ROOT/capacity-1000"
```

每行是单独门禁，不得一次粘贴自动越过上一档失败。GNU `timeout` 必须先确认存在；缺失时容量阶段 `BLOCKED`，不自动安装系统包。`--max-seconds` 是协作式调度预算及验收阈值，会停止新批次并缩短在途任务预算；资源清理仍可能额外耗时，外层 timeout 提供最终上界。超时需检查并终止本轮拥有的残留进程，保留目录和日志，不能用泛化 pkill 影响其他 Bot。

验收查看 `metrics.json`：`passed=true`、completed_groups 等于请求值、duplicates=0、unclean=0、pending=0、actual_peak<=4、per_group_peak<=2。记录注册 p50/p95/p99、queue_p95/queue_max、tasks_per_second、耗时和数据库大小。资源不足或前档失败时高档 `BLOCKED`，不得降低规模后仍标原档通过。

## 分层结论与交付

| 层级 | 允许结论 | 未具备依赖时 |
|---|---|---|
| LOCAL_REAL_SUT | Python 协调器 + 本机真实子进程的指定断言通过 | SETUP_FAILED / NOT_EVALUATED |
| REAL_GITHUB | 需真实事件、固定 SHA、run/check ID、身份及产物关联 | BLOCKED，条目保留 NOT_EVALUATED |
| REAL_RUNTIME | 需实际 Runner/容器/Pod、入口/参数/挂载、资源清理证据 | BLOCKED，不能使用本机进程冒充 |
| SHADOW | 需真实旧流程与新调度结果对比，不写合入门禁 | BLOCKED |
| 生产可用 | 需额外集成、授权和全部规定证据 | 本包不得宣称 |

输出一个新结果压缩包和 SHA256，至少包含：原包身份/manifest、主机和预算说明、三个完整 shard、canonical、容量目录、失败日志、BLOCKED.md。报告分别列本地 PASS/FAIL 和完整验收 PASS/FAIL/NOT_EVALUATED，列出未执行外部层和原因。禁止改 assertion 或手动把状态调绿。

回滚是停止本次拥有的测试进程并保留证据；本地实验没有生产变更。清理目录需要另行明确指令，不自行删除原包、失败目录或旧结果。
