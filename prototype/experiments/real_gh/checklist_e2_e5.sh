#!/usr/bin/env bash
# Interactive / documented live checklist runner for E2–E5.
# Does nothing destructive without JOINT_GH_TOKEN and JOINT_DRY_RUN=0.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
OWNER="${JOINT_OWNER:-zhekui-hub}"
LAB="${JOINT_LAB_REPO:-joint-ci-lab}"
DRIVER="${JOINT_DRIVER_REPO:-joint-ci-driver}"
SYNAPSE="${JOINT_SYNAPSE_REPO:-joint-ci-synapse}"
SIM="${JOINT_SIM_REPO:-joint-ci-sim}"

die() { echo "ERROR: $*" >&2; exit 1; }

guard_owner() {
  [[ "$OWNER" == "zhekui-hub" ]] || die "F16: JOINT_OWNER must be zhekui-hub (got $OWNER)"
}

usage() {
  cat <<USAGE
Usage: $0 <command>

Commands:
  help           Show this help
  preflight      Run Python stub preflight + basic gh auth check (read-only)
  show-e2        Print E2 steps (no network writes)
  show-e3        Print E3 steps
  show-e4        Print E4 steps
  show-e5        Print E5 steps
  plan-all       Dump JSON plans for e2/e3/e4/e5 via gh_calls_stub.py
  run-e2         LIVE: only if JOINT_GH_TOKEN set and JOINT_DRY_RUN=0
                 (currently prints exact commands for operator confirmation)

Env names: JOINT_OWNER JOINT_GH_TOKEN JOINT_DRY_RUN JOINT_*_REPO
Repos: $OWNER / {$LAB,$DRIVER,$SYNAPSE,$SIM}
USAGE
}

cmd_preflight() {
  guard_owner
  python3 "$ROOT/gh_calls_stub.py" preflight
  if command -v gh >/dev/null 2>&1; then
    gh auth status || true
    gh repo view "$OWNER/$LAB" --json nameWithOwner --jq .nameWithOwner || true
  else
    echo "gh not installed — install before live run"
  fi
}

cmd_plan_all() {
  guard_owner
  for s in e2 e3 e4 e5; do
    echo "##### plan $s #####"
    python3 "$ROOT/gh_calls_stub.py" plan --scenario "$s"
  done
}

require_live() {
  guard_owner
  [[ "${JOINT_DRY_RUN:-1}" == "0" ]] || die "Set JOINT_DRY_RUN=0 for live (currently ${JOINT_DRY_RUN:-1})"
  [[ -n "${JOINT_GH_TOKEN:-}" ]] || die "JOINT_GH_TOKEN not set"
  command -v gh >/dev/null 2>&1 || die "gh CLI required"
}

cmd_run_e2() {
  require_live
  cat <<'LIVE'
LIVE E2 — operator confirmation required.
Intended sequence (edit SHAs/branches as needed):

  # 1) tip of main on synapse
  MAIN=$(gh api repos/zhekui-hub/joint-ci-synapse/git/ref/heads/main --jq .object.sha)
  # 2) create branch exp/ci-e2-synapse from MAIN; open PR with joint body
  # 3) wait >=5m; confirm no heavy Actions job
  # 4) create driver branch exp/ci-e2-driver + PR CI_MODE=joint
  # 5) poll checks + lab issues with label joint-ci

See CHECKLIST.md §E2 and: python3 gh_calls_stub.py plan --scenario e2
This script does not auto-create PRs (avoids accidental writes). Run gh commands manually.
LIVE
}

main() {
  local cmd="${1:-help}"
  case "$cmd" in
    help|-h|--help) usage ;;
    preflight) cmd_preflight ;;
    show-e2) sed -n '/## E2 /,/## E3 /p' "$ROOT/CHECKLIST.md" ;;
    show-e3) sed -n '/## E3 /,/## E4 /p' "$ROOT/CHECKLIST.md" ;;
    show-e4) sed -n '/## E4 /,/## E5 /p' "$ROOT/CHECKLIST.md" ;;
    show-e5) sed -n '/## E5 /,/## Optional /p' "$ROOT/CHECKLIST.md" ;;
    plan-all) cmd_plan_all ;;
    run-e2) cmd_run_e2 ;;
    *) usage; die "unknown command: $cmd" ;;
  esac
}

main "$@"
