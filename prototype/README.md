# Joint CI Prototype

可迁入 Arsenal 的第一版骨架：

- `shared/joint_key.py` — joint_key 与测试并集去重（含单测）
- `arsenal/scheduler.py` — 状态机调度器（默认 dry-run）
- `arsenal/joint_ci.yml` — Arsenal workflow 入口
- `arsenal/repo_hook_example.yml` — 业务仓上报钩子示例
- `experiments/` — E2 等场景 YAML + 断言

## 本地试跑

```bash
cd prototype/shared && python -m unittest test_joint_key.py
cd ../arsenal
JOINT_DRY_RUN=1 REPORT_JSON='[{"repo":"synapse","pr_number":456,"head_sha":"aaa","ci_mode":"joint","joint_wait":true,"deps":{"driver_branch":"feature/x"},"remote_tests":[{"id":"multirepo_runtest","params":{"profile":"default"}}]}]' python scheduler.py
```

接入真实仓库后：把 `shared/` 与 `scheduler.py` 放进 Arsenal `joint_ci/`，workflow 放进 `.github/workflows/`，并用 GitHub App 凭证替换 dry-run 客户端。
