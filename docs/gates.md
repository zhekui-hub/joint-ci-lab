# 门禁说明

## 现状（个人免费号私有仓）

GitHub 对 **免费账号的 private 仓库** 不开放：

- Branch protection rules
- Repository rulesets

因此 `zhekui-hub/joint-ci-*` **无法**在 GitHub 设置里勾选「Required status checks」这类硬门禁。

## 已落地的软门禁（Actions）

主仓 `joint-ci-lab` 工作流：

| Workflow | 作用 |
|---|---|
| `joint-ci-gate` | 每次 push/PR：跑 joint_key + scheduler 单测，再跑 E1–E10；任一失败则 check 红 |
| `joint-ready-soft-check` | 手动/调度写 `joint-ci` commit status，供后续对接 mock 仓 |

合并前请看 Actions 是否绿。这是流程门禁，不是 API 强制拦截。

## 若要硬门禁（真正挡合并）

任选其一：

1. 把实验仓改为 **public**，再开 Branch protection，Required checks 勾选 `unit-tests` / `e1-e10-local` / `joint-ci`
2. 升级 GitHub Pro，私有仓也可开 protection
3. 迁到支持规则的 Org

## 与设计对齐

- 设计里「联合就绪」先 **非 required**：与当前软门禁一致
- 本地断言门禁：`python3 run_local.py` 必须 10/10
- 真连 GitHub App / 跨仓写 Check 仍属后续阶段
