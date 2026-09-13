# Joint CI 上报 / 状态 schema

## report payload (各仓 → Arsenal)

```json
{
  "schema_version": 1,
  "repo": "synapse",
  "pr_number": 456,
  "head_sha": "abc",
  "base_ref": "main",
  "ci_mode": "joint",
  "joint_wait": true,
  "deps": {
    "driver_branch": "feature/xxx",
    "sim_branch": null
  },
  "local_tests": ["format", "commitlint"],
  "remote_tests": [
    {"id": "multirepo_runtest", "params": {"profile": "default"}, "exclusive_to": null},
    {"id": "pseudo", "params": {"cards": 4}, "exclusive_to": null}
  ]
}
```

## joint issue body (Arsenal)

机器可解析的 frontmatter + 人类可读表格。状态机：
`waiting_deps → ready → running → succeeded|failed → invalidated|cancelled`
