---
paths:
  - "harness2/cli/**/*.py"
  - "harness2/benchmarks/**/*.py"
  - "harness2/core/schedule.py"
  - "harness2/core/ledger.py"
  - "scripts/run_*.sh"
---

# Benchmark Runs

## Cost guardrails

One task at recursion budget `k` is **R = 4k executor rollouts and P = 3k improvement
stages** (`core/ledger.py`). Every one is a real paid model call. Escalate deliberately:

1. `harness2-demo --output runs/demo --k 2` — free, deterministic, no credentials, exercises
   the real recursion and library workflow.
2. `harness2 --bench <b> ... --dry-run` — previews task selection with zero model calls.
3. `harness2-check --bench <b>` — validates installation and credentials before spending.
4. `--max-tasks 1 --only-domains <one-domain>` — smallest real slice.

Never launch a full sweep on the user's behalf without explicit confirmation of the spend.

## The namespace invariant

`harness2`, `harness2-judge`, and `harness2-report` address runs through a namespace built by
`core/schedule.ns()` — the only function that builds one. All three must be invoked with the
same benchmark, executor (`--substrate`), subset, `--k`, `--mode`, and seed, or judging and
reporting read a different namespace than the refinement run wrote. `cli/_common.py` defines
these on-disk conventions once; `run_sweep` writes them and `judge`/`report` read them back —
change them in one place only.

## Resume and output

Repeating a command resumes it. Output roots: `runs/lab/`, `runs/jb/`, `runs/wb/<subset>/`,
overridable with `HARNESS2_RUNS_DIR`. Use a **fresh** `HARNESS2_RUNS_DIR` when models or task
selection change — the saved configuration deliberately blocks incompatible resumes.

Completion is recorded explicitly by the code that observed the run return
(`core/runmark.py`); the presence of output files is not evidence of completion. Preserve that.

## Scoring integrity

- A judge failure is `score=None` (ungraded) and is **excluded from the denominator**. Never
  coerce it to `0`, and never count a never-completed task as a failure.
- Only the run the blind terminal selector chose is scored (`cli/judge.py`). Selection happens
  before and without the judge; nothing in the method imports `core/judge.py`.
- `harness2-judge --bases` writes `base_judged.json` beside `judged.json` for base-harness
  comparison; `harness2-report` covers the refined harness only.
- `harness2-report` is read-only over cached `judged.json` rows.

## Reproducibility

When comparing budgets or modes, hold task selection, executor, improver, judge, and web
access fixed. JobBench's dataset resolves at download time, not by commit — report the
revision recorded in
`dependencies/job-bench-eval/dataset/main/.harness2-download.json` alongside results.
Task partitions come from `scripts/split_tasks.py` and are local files, not release assets.
