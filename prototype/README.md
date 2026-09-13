# Joint CI Prototype

可迁入 Arsenal 的第一版骨架：

- `shared/joint_key.py` — joint_key 与测试并集去重（含单测）
- `arsenal/gh_client.py` — `GhClient` + 默认 dry-run `FakeGhClient`
- `arsenal/scheduler.py` — 状态机调度器（默认 `JOINT_DRY_RUN=1`）
- `arsenal/joint_ci.yml` / `repo_hook_example.yml` — workflow 草稿
- `experiments/` — E1–E10 场景 YAML + `run_local.py`（复用 FakeGhClient）

## 本地单测

```bash
cd /workspace/joint-ci/prototype/shared && python3 -m unittest test_joint_key.py -v

cd /workspace/joint-ci/prototype
PYTHONPATH=arsenal:shared python3 -m unittest arsenal.test_scheduler -v
# 或：
cd arsenal && PYTHONPATH=.:../shared python3 -m unittest test_scheduler.py -v
```

## Dry-run 调度示例

```bash
cd /workspace/joint-ci/prototype/arsenal
JOINT_DRY_RUN=1 REPORT_JSON='[{"repo":"synapse","pr_number":456,"head_sha":"aaa","ci_mode":"joint","joint_wait":true,"deps":{"driver_branch":"feature/x"},"remote_tests":[{"id":"multirepo_runtest","params":{"profile":"default"}}]}]' python3 scheduler.py
```

缺依赖时状态为 `waiting_deps`，不派发测试（不占 Pod）。

## 本地实验（E1–E10）

```bash
pip install -r requirements.txt   # PyYAML, pytest
cd experiments
python3 run_local.py              # 全部场景
python3 run_local.py scenario_e2.yaml
python3 run_local.py --json
```

`experiments/fake_gh.py` 从 `arsenal.gh_client` 再导出；实验侧调用：

```python
from scheduler import run, invalidate, workflow_key
from fake_gh import FakeGhClient
```

`PYTHONPATH` 需包含 `prototype/arsenal` 与 `prototype/shared`（`run_local.py` 已自动设置）。

接入真实仓库后：把 `shared/`、`gh_client.py`、`scheduler.py` 放进 Arsenal `joint_ci/`，workflow 放进 `.github/workflows/`，并用 GitHub App 凭证替换 dry-run 客户端。
