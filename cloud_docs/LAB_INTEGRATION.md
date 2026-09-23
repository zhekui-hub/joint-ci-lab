# 隔离四仓 GitHub 联调

此入口已实现，尚未在真实 GitHub Runner 上验收。代码中的 smoke 只验证调度协议，不 checkout 或测试四仓业务代码。业务测试、Pod 清理及生产兼容仍须独立接入验证，不能由 smoke PASS 推导。

## 执行条件

执行上下文：隔离 Linux 云电脑，Python 3.10+、gh；普通用户。维护者需要四个 `owner/joint-ci-*` 仓库的 workflow 配置权限，以及实验 GitHub App 的 PR/Checks/Actions/Contents 读取、commit statuses 写入和事件派发权限。权限只授予实验仓；不记录 token 值，不使用生产凭证。

1. 在四仓安装 `templates/lab-private.yml` 和 `templates/participant-event-relay.yml`，在控制仓安装 `templates/lab-controller.yml`。这是实际远端变更，Bot 只能在用户已授权的隔离仓执行。先核对仓名、默认分支、现有保护规则，保存原文件版本。生产 Arsenal 路由未修改；将来接入生产必须检查 `arsenal/.github/workflows/ci_router.yml` 和 `arsenal/arsenal_CI/determine_workflows.sh`。
2. 配置 relay 使用的 `JOINT_CI_CONTROL_REPO` 变量和 `JOINT_CI_LAB_APP_TOKEN` secret；以模板内名字为准。不要将凭证传给公共 worker。App token 应由实验环境安全提供并及时刷新，本包不提供凭证生命周期服务。
3. 在同一台持久状态机配置两个独立 runner 槽位：`joint-ci-controller` 和 `joint-ci-worker`，以及通用 `self-hosted,Linux,X64`。两个槽位必须读写同一个本地磁盘 SQLite 路径，不能 NFS、复制数据库、多台各自认为是主节点。单个 runner 不能同时处理执行和取消事件。分片验收的 `joint-ci-cloud` runner 可在其他主机。
4. 执行一次私有实验 workflow，核对实际 Check 名 `lab-private` 及其 App ID，然后生成配置。App ID 不是 installation ID。配置是维护者可信输入，PR 不得修改 argv、环境或配置。

```bash
# 包根目录，本机普通用户；GH_TOKEN 由秘密管理提供，不在命令中写明
python3 tools/make_lab_config.py --arsenal OWNER/joint-ci-arsenal --driver OWNER/joint-ci-driver --synapse OWNER/joint-ci-synapse --sim OWNER/joint-ci-sim --check-app-id VERIFIED_ID --out /absolute/new-config.json
python3 tools/controller.py --config /absolute/new-config.json --db /absolute/new-state.sqlite
```

成功应得到只读 shadow 提案；配置输出路径必须不存在。404/403 查仓库白名单和 App 范围；没有唯一 lab-private workflow 或 Check 则先修实验入口。配置默认需要 GitHub APPROVED；仅在明确不要求 review 的实验仓可设 `approval_policy=not_required`。设置控制仓变量 `JOINT_CI_CONFIG_PATH`、`JOINT_CI_STATE_DB`、`JOINT_CI_WORKER_EVIDENCE` 为状态主机绝对路径。只在核对提案后启用模板中 `--apply-lab-status`，该选项会写实验 commit status，但不修改保护规则或合并 PR。

## 必须采集的真实验收

四仓使用原有依赖字段互指分支，无需 group ID 或标签。记录 PR/head/base/candidate SHA、App/check 来源、原生 run ID/attempt、实际执行次数、状态写回和清理证据。逐项执行 push、Draft 切换、私有成功/失败、原生 cancel/re-run、旧事件重放、部分人工合入、并发关联更新。依赖固定 SHA/default 分支不能因恰有同 SHA 的 PR 而替换为 merge candidate。真实 required-check 来源保护需由实验仓管理员配置并验证，本包 commit status 写回不等于已启用可信保护。

原生事件必须先观察到对应 attempt 的 `in_progress` 才能绑定；漏掉该事件的 cancel/re-run 会持久化 native_event_unresolved 并阻止旧绿灯，不能猜测归属；普通 sync 不清除阻塞，后续被正确绑定的新原生重跑才恢复对应消费者。5 分钟补偿负责重读 PR/check 状态，但不能恢复已经丢失的历史取消归属。必须将漏事件及重新运行晚到列为外部必测项，尚不能宣称生产无感兼容已通过。

两仓人工合入之间仍有竞态。首次合入被观察到后旧组失效，记录 GitHub 返回的实际 merge commit SHA 和时间，再刷新剩余提案；GitHub API 未给出合入方式时记录 unknown，不猜 squash/rebase。跨 GitHub/本地事务不存在原子提交；发布后版本变化会补写 pending，但补偿窗口仍存在。

## 清理与回滚

只支持可信 smoke 子进程及其原进程组的清理；主动脱离进程组的 daemon 和 Pod 不在本地清理保证内。worker 被外层强杀后保留未清理任务并阻止重新准入，需核实 owned 资源后恢复，不能只改数据库强行通过。

停止实验调度、等待并确认本轮 owned 任务退出、保存状态库和证据；恢复实验 workflow 的原版本。若试验过 required checks，先恢复并验证原 CI，再撤下新检查。不要删除他人 runner、仓库、文件或取消无关运行。缺权限/镜像/挂载/真实业务入口时记录 BLOCKED，保留外部 NOT_EVALUATED。
