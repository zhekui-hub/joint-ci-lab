# Real GitHub Joint CI results

- mode: `live`
- started: `2026-09-13T04:24:00+00:00`
- finished: `2026-09-13T12:27:24.0525878+08:00`
- operator: CI专员 (Thinkbook + grok-box runner)

## Scorecard

| Probe | Result | Evidence |
|---|---|---|
| F16 | **PASS** | whitelist rejects ChipLTech/anything in `run_real.py` |
| P1 | **PASS** | commit status `joint-ci` written on lab/driver/synapse/sim main SHAs (status API fallback; Checks:write not on PAT) |
| P2 | **PASS** | Issues https://github.com/zhekui-hub/joint-ci-lab/issues/4 and https://github.com/zhekui-hub/joint-ci-lab/issues/5 labeled `joint-ci` |
| P3 | **PASS** | `workflow_dispatch` joint_real_probe with explicit SHAs → https://github.com/zhekui-hub/joint-ci-lab/actions/runs/34737873359 |
| P4 | **PENDING** | need staggered synapse+driver PRs (E2 checklist) |
| P5 | **PASS** | all four statuses share target_url=https://github.com/zhekui-hub/joint-ci-lab/actions/runs/34737873359 |

## Notes

- Check Runs API failed on PAT; documented status fallback used (design allows this).
- Probe job may sit queued on `ubuntu-latest`; hardening PR will move it to self-hosted `grok-box`.
- E2/E3/E5 real PR scenarios still open below.
