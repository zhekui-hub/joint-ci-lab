# Joint CI Lab

Personal private sandbox for cross-repo Joint CI experiments (`zhekui-hub`).

Not company code. Do not push here from ChipLTech repos.

## Layout

- `docs/` — design + experiment matrix
- `prototype/` — scheduler, joint_key, workflow drafts, local experiments

## Sibling mock repos

- https://github.com/zhekui-hub/joint-ci-driver
- https://github.com/zhekui-hub/joint-ci-synapse
- https://github.com/zhekui-hub/joint-ci-sim

## Quick test

```bash
cd prototype/shared && python -m unittest test_joint_key.py -v
```
