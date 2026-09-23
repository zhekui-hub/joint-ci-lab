<!-- Author: zhekui. Portable experimental implementation, usage and evidence limits; no executable entry points. -->
# 联合 CI 云端实验交付包

本包包含可执行协调器、44 个规格对应的本地子场景、并行分片执行器、压力测试、源码与证据打包工具、GitHub 只读预检和手动 self-hosted Actions 模板。Python 3.10+，仅标准库；不需要 jq、容器、sudo 或生产 token。

**交付范围是隔离实验实现，不是已上线的生产联合 CI。** 原始 shell Demo 保留在上游仓库，本包不自动启用它。历史 120 用例源码和后续修复包不可获取，因此本包没有假装复制那套用例。

## 已实现

- 原依赖 KEY=value 字段的保守自动关联；默认依赖、不兼容版本、多义匹配退回 legacy 提案，不增加用户填写项。
- 全输入快照、公共测试去重和消费者映射；Draft 与私有 pending 不妨碍公共任务开测。
- SQLite 事务化状态、唯一任务/派发意图、跨进程准入、显式版本条件更新。
- 取消、旧尝试隔离、按消费者保留取消状态的重试、私有检查重新协调；一仓取消不杀其他消费者需要的共享任务。
- 全局/每组并发上限、队列/组容量拒绝、公平队列顺序、无租约自动抢占。
- 真实子进程 smoke、产物身份验证、超时与取消清理；所有合成 Git SHA 明确标注。
- 独立分片与严格汇总；源码漂移、旧轮次、重复 ID、缺证据、伪通过字段均拒绝。

## 尚不能宣称实现或通过

| 项目 | 当前边界 |
|---|---|
| 生产无感 PR 入口 | 已实现解析/发现与内部契约；现有生产模板兼容、原生按钮和事件接线仍需真实 GitHub 验收 |
| 可信生产合入门禁 | 只读 App 来源校验可运行；没有启用跨仓写回与保护规则；实验适配器读取 GitHub reviewDecision；真实门禁尚未验收 |
| Webhook/补偿服务部署 | 已提供隔离 relay、定时补偿、状态写回和部分合入适配器；模板未部署，见 LAB_INTEGRATION.md |
| 分布式高可用 | SQLite 仅用于单机本地磁盘；不能放 NFS 或让不同主机复制 DB 后同时当同一协调器 |
| 真实 Pod/TPU 业务测试 | 本地子进程清理不是 Pod 生命周期；四条 smoke 不代表业务测试矩阵已覆盖 |
| 精细部分重跑 | 第一版安全地全量创建新 attempt；不宣称支持历史 R06/R13 的优化能力 |
| 历史 120 通过率 | 完全不引用；本包使用 JC-01..JC-44 的本地子场景与外部验收清单 |

本地脚本返回 0 表示本地断言通过；报告仍将需要真实 GitHub/Pod 的完整规格标为 NOT_EVALUATED。不得把 44 个本地 PASS 改写成“44 项全层验收通过”。

## 快速运行

执行位置：解压目录；普通用户。先核对包旁路 SHA256。输出目录必须不存在，防止混入旧结果。

```bash
python3 tools/acceptance.py preflight
python3 tools/cloud_preflight.py --out ../readiness-new.json
python3 tools/acceptance.py run --out ../results-new --run-id cloud-001 --jobs 2
python3 tools/acceptance.py verify ../results-new
python3 tools/package.py --evidence ../results-new --out ../evidence-new.tar.gz
```

预期：每个 JC-* 输出本地判定；results-all.csv 是唯一规范结果；independent-recompute.json mismatch=0；打包后重新解包核对。失败时保留原目录和日志，不修改断言或覆盖第一次结果。

多个 Bot 使用 [执行提示词](BOT_PROMPTS.md) 分片；[交接说明](BOT_HANDOFF.md) 定义唯一汇总者。runner 模板是 `templates/cloud-acceptance.yml`，默认未放入 `.github/workflows`，不会自动触发旧 CI。只在明确隔离实验仓库中由 Bot 安装；标签 `self-hosted,Linux,X64,joint-ci-cloud`。

## 高并发和容量

```bash
python3 tools/capacity.py --out ../capacity-10 --groups 10 --jobs 2 --controllers 2 --per-group 1 --max-seconds 120
python3 tools/capacity.py --out ../capacity-100 --groups 100 --jobs 4 --controllers 4 --per-group 2 --max-seconds 300
# 仅在前两档通过且资源允许时：
python3 tools/capacity.py --out ../capacity-1000 --groups 1000 --jobs 6 --controllers 6 --per-group 2 --max-seconds 1800
```

默认存储上限：8 个实际执行、每组 2 个、10000 条 queued、2000 个保留组。配置在同一个 SQLite 事务库内统一生效，不是每进程各自计数。单组热请求不会绕过配额；队列满时整笔事务拒绝，不落半组任务。历史组和 audit 保留，达到保留上限明确阻塞，未实现自动删除策略。

压力测试记录注册延迟 P50/P95/P99、排队 P95/最大值、吞吐、实际并发、重复准入、清理和库大小。max-seconds 是协作式整体预算，清理可能额外耗时；外层强杀需保存并核对本轮 owned 资源。指标只代表该机、该规模、该 smoke，不外推生产容量。

下一阶段的扩容路径：先测 SQLite 锁等待与 API 限流，达到目标容量瓶颈后再替换存储适配器为可条件更新的中心事务服务；按 group 分区单写、同组路由一致。执行者保留准入票据与取消隔离；不能仅把 workers 增多当作高可用。不在未压测前默认引入 Redis/Kafka/K8s。

## 手工调试内部接口

这些命令给实验维护者使用，不增加 PR 用户操作。

```bash
python3 -m cloud_joint.cli --db ../lab.sqlite discover inventory.json
python3 -m cloud_joint.cli --db ../lab.sqlite work --out ../worker-evidence --jobs 4
python3 -m cloud_joint.cli --db ../lab.sqlite summary GROUP_ID
```

inventory 为可信适配器采集的 reports/fields/defaults；绝不能把 PR 正文、webhook 中的 success、命令 argv 或环境变量直接当可信配置。GitHub 只读工具 `tools/github_inventory.py` 只允许显式列出的 `owner/joint-ci-*` 实验仓，拒绝 ChipLTech 生产仓。

生产接入前需同步检查 Arsenal `.github/workflows/ci_router.yml` 与 `arsenal_CI/determine_workflows.sh`；本包未修改这两个文件，模板也不是生产路由的替代品。

## 回滚与权限

本包无需修改系统、注册 runner、创建仓库或变更保护规则即可跑本地子场景。安装 runner、启用实验 workflows 和真实门禁实验由 Bot 按用户授予的实验环境权限执行；无权限就 BLOCKED。禁止触发生产、提升权限或删除无关资源。

本机回滚：停止本轮执行并确认 owned 子进程清理，保留结果目录；旧 shell/生产配置未改，无需生产回滚。未来接线的回滚必须先恢复原测试并通过，再撤销新门禁，不能裸移除必需检查。

真实四仓接线、权限、原生事件限制与回滚见 [LAB_INTEGRATION.md](LAB_INTEGRATION.md)。子进程清理仅覆盖原进程组，不保证主动 setsid 脱离的 daemon 或 Pod。
