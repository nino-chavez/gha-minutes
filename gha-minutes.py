#!/usr/bin/env python3
"""
gha-minutes — diagnose GitHub Actions minute burn and apply the mechanical fix
(concurrency cancellation) to any repo.

Portable extract of the 2026-07-04 bc-subscriptions reduction pass. Two of three
interventions are mechanical and live here; the other two need per-repo judgment
(see the checklist printed by `explain`).

Usage:
  # 1. Diagnose: what ran, how often, since when (needs `gh` auth + repo access)
  gha-minutes.py diagnose --repo OWNER/NAME [--since 2026-07-01]

  # 2. Preview the concurrency edits for a checked-out repo (DRY RUN, no writes)
  gha-minutes.py concurrency --path /path/to/repo

  # 3. Apply them
  gha-minutes.py concurrency --path /path/to/repo --apply

  # The two judgment-call interventions, as a checklist
  gha-minutes.py explain

Design notes (the non-obvious lessons this bakes in):
  - Release / data-mutation / publish workflows get `cancel-in-progress: false`
    (serialize, but never kill a run mid-op). Killing a half-done publish or a
    D1 migration is worse than paying the minutes.
  - Issue-triggered workflows (`on: issues`/`issue_comment`) key the concurrency
    group on the issue number, NOT github.ref. github.ref is the constant default
    branch for issue events, so a per-ref group makes UNRELATED issue events cancel
    each other — silent data loss. This is the #1 way a naive port breaks a repo.
  - Insertion anchor is the top-level `jobs:` line, which every valid workflow has
    exactly once. Idempotent: files that already declare `concurrency:` are skipped.
"""
import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

# Workflows where a mid-run kill is worse than the minutes: serialize, don't cancel.
# Matched as substrings against the filename stem (lowercased).
NO_CANCEL_HINTS = (
    "publish", "release", "deploy-prod", "sdk-", "terraform",
    "changelog", "migration", "lockfile", "backfill", "seed",
)


def _stem_is_no_cancel(stem: str) -> bool:
    s = stem.lower()
    return any(h in s for h in NO_CANCEL_HINTS)


def _on_block(text: str) -> str:
    """Return the raw text of the top-level `on:` block (best-effort, indentation-based)."""
    lines = text.splitlines()
    out, capturing = [], False
    for ln in lines:
        if re.match(r"^on:\s*(#.*)?$", ln) or re.match(r"^on:\s*\[", ln):
            capturing = True
            out.append(ln)
            continue
        if capturing:
            # a new top-level key (column 0, non-comment) ends the block
            if re.match(r"^[A-Za-z_]", ln):
                break
            out.append(ln)
    return "\n".join(out)


def _is_issue_triggered(text: str) -> bool:
    on = _on_block(text)
    return bool(re.search(r"^\s*(issues|issue_comment):", on, re.MULTILINE))


def cmd_concurrency(args: argparse.Namespace) -> int:
    repo = Path(args.path).expanduser().resolve()
    wf_dir = repo / ".github" / "workflows"
    if not wf_dir.is_dir():
        print(f"error: {wf_dir} does not exist", file=sys.stderr)
        return 2

    files = sorted(p for p in wf_dir.iterdir() if p.suffix in (".yml", ".yaml"))
    planned, skipped, no_anchor = [], [], []

    for f in files:
        text = f.read_text()
        if re.search(r"(?m)^concurrency:", text):
            skipped.append(f.name)
            continue
        m = re.search(r"(?m)^jobs:\s*$", text)
        if not m:
            no_anchor.append(f.name)
            continue

        issue = _is_issue_triggered(text)
        cancel = "false" if (_stem_is_no_cancel(f.stem) or issue) else "true"
        if issue:
            group_key = "${{ github.event.issue.number || github.ref }}"
            comment = (
                "  # issue-triggered: key on issue number so unrelated issue events\n"
                "  # don't cancel each other (github.ref is the constant default branch).\n"
            )
        else:
            group_key = "${{ github.ref }}"
            comment = ""
        block = (
            "concurrency:\n"
            f"{comment}"
            f"  group: ${{{{ github.workflow }}}}-{group_key}\n"
            f"  cancel-in-progress: {cancel}\n\n"
        )
        planned.append((f, m.start(), block, cancel, issue))

    mode = "APPLYING" if args.apply else "DRY RUN (pass --apply to write)"
    print(f"== gha-minutes concurrency :: {repo.name} :: {mode} ==")
    print(f"   {len(planned)} to add | {len(skipped)} already have it | {len(files)} total\n")
    for f, _, _, cancel, issue in planned:
        tag = "cancel" if cancel == "true" else "serial"
        tag += ",per-issue" if issue else ""
        print(f"   + {f.name:<44} [{tag}]")
    if no_anchor:
        print("\n   ! no top-level `jobs:` anchor (skipped, inspect manually):")
        for n in no_anchor:
            print(f"     - {n}")

    if args.apply:
        for f, pos, block, _, _ in planned:
            text = f.read_text()
            m = re.search(r"(?m)^jobs:\s*$", text)  # re-find on fresh read
            f.write_text(text[: m.start()] + block + text[m.start():])
        print(f"\n   wrote {len(planned)} files. Validate: "
              f"`actionlint {wf_dir}/*.yml` or `python -c 'import yaml,glob; "
              f"[yaml.safe_load(open(p)) for p in glob.glob(\"{wf_dir}/*.yml\")]'`")
    return 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    repo = args.repo
    since = args.since
    base = f"/repos/{repo}/actions/runs?created=>={since}&per_page=100"

    total = subprocess.run(
        ["gh", "api", f"/repos/{repo}/actions/runs?created=>={since}&per_page=1",
         "--jq", ".total_count"],
        capture_output=True, text=True)
    if total.returncode != 0:
        print(f"error: gh api failed — {total.stderr.strip()}", file=sys.stderr)
        return 2
    print(f"== gha-minutes diagnose :: {repo} :: since {since} ==")
    print(f"   total runs: {total.stdout.strip()}\n")

    rows = subprocess.run(
        ["gh", "api", "--paginate", base,
         "--jq", ".workflow_runs[] | [.name, .event] | @tsv"],
        capture_output=True, text=True)
    lines = [l for l in rows.stdout.splitlines() if l.strip()]
    by_wf = Counter(l.split("\t")[0] for l in lines)
    by_event = Counter(l.split("\t")[1] for l in lines if "\t" in l)

    print("   by trigger event:")
    for ev, n in by_event.most_common():
        print(f"     {n:>5}  {ev}")
    print("\n   top 20 workflows by run count (frequency = your lever):")
    for name, n in by_wf.most_common(20):
        print(f"     {n:>5}  {name}")
    print("\n   note: run count x avg billed-min/run = minutes. GitHub rounds each")
    print("   JOB up to the next minute, so many short jobs still cost 1 min each.")
    print("   Get per-run billed min from the jobs API (the /timing `billable`")
    print("   field is unreliable for private repos — compute ceil-per-job instead).")
    return 0


def cmd_explain(_args: argparse.Namespace) -> int:
    print(__doc__)
    print("""
Per-repo JUDGMENT interventions (not automated — decide each):

  A. Per-push -> schedule, for high-frequency low-freshness workflows.
     Candidates: anything that INGESTS / INDEXES / DERIVES and has no `paths:`
     filter (so it fires on every push). Safe iff a staleness SLA of N hours is
     acceptable AND the job is idempotent (re-ingesting a window is a no-op).
     Change `on: push` -> `on: schedule: - cron: '5 */2 * * *'` and switch any
     `before..after` range logic to a `--since="Nh ago"` window.

  B. Expensive test/e2e matrix -> local pre-push, remote nightly backstop.
     Only if the repo has a pre-push hook mechanism (husky/.githooks) and a fast
     local subset exists. Remove the `push:` trigger, keep `schedule` (nightly) +
     `workflow_dispatch`, and run the fast subset in the hook gated on the target
     branch (read refs from stdin; only gate pushes to your integration branches).
     Trade-off: local hooks are bypassable (--no-verify) and single-environment;
     the nightly remote run is the cross-env safety net. State this explicitly.

  C. Also worth checking per repo:
     - `paths:` filters on every deploy / e2e / matrix workflow (don't run the
       28-min browser matrix on a docs-only push).
     - duplicate deploy variants (e.g. -gcp + cloudflare) both firing.
     - push frequency itself: N pushes/day x M push-triggered workflows is the
       real multiplier. Batching commits into one push beats any config change.
""")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="gha-minutes", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("diagnose", help="count runs by workflow + event via gh api")
    d.add_argument("--repo", required=True, help="OWNER/NAME")
    d.add_argument("--since", default="", help="ISO date, e.g. 2026-07-01 (default: all)")
    d.set_defaults(func=cmd_diagnose)

    c = sub.add_parser("concurrency", help="add concurrency blocks (dry-run unless --apply)")
    c.add_argument("--path", required=True, help="path to a checked-out repo")
    c.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    c.set_defaults(func=cmd_concurrency)

    e = sub.add_parser("explain", help="print the per-repo judgment-call checklist")
    e.set_defaults(func=cmd_explain)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
