# Public-only joint-ci Plan alignment (2026-09-13)

This note supersedes legacy experiment assertions that modeled Arsenal dispatching
`exclusive_to` tests and folding them into per-PR `joint-ci` conclusions.

Current contract:

- Arsenal dispatches only `public_tests_requested` (or legacy `remote_tests` items
  without `exclusive_to`).
- A public test is dispatched once per `joint_run_key`.
- Public failure writes the check/status name exactly `joint-ci` as failure to all
  participant head SHAs; public success writes success to all.
- Waiting writes `joint-ci=pending` and exits without occupying a test runner.
- `exclusive_to` is compatibility input only and is never dispatched by Arsenal.
  Exclusive tests run in each business repo's `*-independent-ci` workflow.
- Any participant SHA change invalidates the old public result and creates a new
  `joint_run_key`.

Scenario E4 and E26 now verify that injected exclusive failures remain undispatched
and do not change `joint-ci`. Real business-repo independent-ci isolation and branch
protection remain end-to-end gaps; fake/prototype PASS does not close those gaps.
