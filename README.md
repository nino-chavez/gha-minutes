# gha-minutes

Diagnose GitHub Actions minute burn and apply the mechanical fix (concurrency
cancellation) to any repo. Portable extract of the 2026-07-04 bc-subscriptions
reduction pass (~2,900 billed min in 4 days → ~50% cut).

## Port to a new repo — the 4-step method

```sh
T=~/Workspace/dev/tools/gha-minutes/gha-minutes.py

# 1. Diagnose — what's burning minutes (needs `gh` auth + repo access).
#    Private repos count against quota; public repos are free (skip them).
python3 $T diagnose --repo OWNER/NAME --since 2026-07-01

# 2. Preview the concurrency edits (DRY RUN, no writes).
python3 $T concurrency --path /path/to/checked-out/repo

# 3. Apply, then validate the YAML.
python3 $T concurrency --path /path/to/checked-out/repo --apply
actionlint /path/to/checked-out/repo/.github/workflows/*.yml   # or PyYAML load

# 4. The two judgment-call interventions (schedule-swap, test-to-local) as a checklist.
python3 $T explain
```

Commit on a branch, open a PR, let CI confirm green (config-only PRs usually
skip path-filtered gates), merge.

## What's mechanical vs. judgment

- **Mechanical (this tool applies):** concurrency cancellation on every workflow
  missing it. ~80% of the savings. Idempotent — re-runnable, skips files that
  already declare `concurrency:`.
- **Judgment (per repo, see `explain`):** per-push→schedule for no-path-filter
  ingest/derive workflows; expensive test/e2e matrix → local pre-push + nightly
  remote backstop; `paths:` filters on deploy/e2e; and the real root cause —
  push *frequency*.

## Two gotchas baked in (the reason not to hand-roll)

1. **Release/publish/migration workflows get `cancel-in-progress: false`** —
   serialize but never kill mid-op (a half-done publish or D1 migration is worse
   than the minutes). Matched by filename hints.
2. **Issue-triggered workflows key the group on the issue number, not
   `github.ref`.** For `on: issues`, github.ref is the constant default branch,
   so a per-ref group makes unrelated issue events cancel each other — silent
   data loss. This is the #1 way a naive port breaks a repo.
