# E2 auto-match → public_runs=1（lab Actions）

前提：synapse#1 + driver#1 已开；lab 已有 `joint_ci.yml`；secret `JOINT_GH_TOKEN`（跨仓 status/issue/dispatch）。

## 1. 填 SHA
```bash
SYN=$(gh api repos/zhekui-hub/joint-ci-synapse/pulls/1 --jq .head.sha)
DRV=$(gh api repos/zhekui-hub/joint-ci-driver/pulls/1 --jq .head.sha)
jq --arg s "$SYN" --arg d "$DRV" \
  '(.reports[]|select(.repo=="synapse").head_sha)=$s
 | (.reports[]|select(.repo=="driver").head_sha)=$d' \
  e2_pair_dispatch.example.json > /tmp/e2_reports.json
```

## 2. 触发 lab scheduler（不占机等待由 scheduler 状态机保证）
```bash
gh workflow run joint-ci.yml -R zhekui-hub/joint-ci-lab \
  -f report_json="$(jq -c . /tmp/e2_reports.json)" \
  -f joint_id=joint-e2-synapse-1 \
  -f dry_run=0
```

## 3. 断言
- lab Issue `joint-ci`：status `running`→`succeeded`（或 failed）
- public `multirepo_runtest` Actions run **恰好 1**
- driver/synapse 同源 status/check `target_url`
- 等待期无重测试 runner（E2 错峰窗内无对应 job）
