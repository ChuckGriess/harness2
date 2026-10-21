# Harness² — Agentic Development Context

## Rules

@.claude/rules/code-style.md
@.claude/rules/testing-standards.md
@.claude/rules/benchmark-runs.md
@.claude/rules/security-guidelines.md
@.claude/rules/vendored-code.md

## Project Overview

Harness² (arXiv 2610.14224) is a framework for **recursive agent harnessing**: optimizing the
system around a model (prompts, tools, memory, skills, orchestration) while model weights stay
fixed. Pure Python 3.12/3.13, Apache-2.0, originally released by Google LLC. Research code
backing a paper — reproducibility and the integrity of reported numbers matter more than
feature velocity.

Zero runtime dependencies by design (`dependencies = []` in `pyproject.toml`). Everything
heavier lives behind optional extras or under `dependencies/` as a pinned upstream checkout.

Read `README.md`, `docs/SETUP.md`, `docs/EXPERIMENTS.md`, and `docs/BENCHMARKS.md` before
non-trivial work — they are accurate and current. Module docstrings in `harness2/core/` are
written as design contracts; read the docstring before editing a module.

## Fork Layout

This checkout is a **fork**. Two remotes, and they mean different things:

| Remote | Repo | Use |
|---|---|---|
| `origin` | `ChuckGriess/harness2` | Personal fork — all local work pushes here |
| `upstream` | `google-research/harness2` | Original release — read-only; never push |

Sync with upstream via `git fetch upstream && git merge upstream/main`. Never push to
`upstream`. An upstream contribution requires a signed
[Google CLA](https://cla.developers.google.com/) and a PR against `google-research/harness2`;
this `.claude/` directory is fork-local tooling and should be excluded from any such PR.

## Setup Commands

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e '.[render]'            # add ,bridge for the Codex LiteLLM bridge
harness2-demo --output runs/demo --k 2   # free, deterministic, no credentials
python -m unittest discover -s tests -v  # test suite
uvx ruff check .                         # lint; ruff is configured but NOT installed
```

Linux + Python 3.12 is the supported platform for benchmark runs. Model-backed benchmarks
additionally need Node.js 20+, gcloud CLI, Pandoc/Poppler (LAB), and Docker (WorkBuddy).

## Architecture

Three axes stay orthogonal; the seam is `harness2/core/spec.py`.

| Axis | Location | Owns |
|---|---|---|
| Method | `harness2/core/` | recursion, prompts, selection, library, ledger — substrate- and benchmark-free |
| Substrate | `harness2/substrate/` | the agent framework a rollout runs under (OpenCode default, Codex) |
| Benchmark | `harness2/benchmarks/` | tasks, splits, launchers, deliverables, grading (JobBench, LAB, WorkBuddy) |

Adding a benchmark or substrate means supplying a spec + adapter, not branching inside
`core/`. Benchmark text reaches prompts as **values**, never as branches in `prompts.py`.

Entry points (`pyproject.toml [project.scripts]`): `harness2` (adapt), `harness2-judge`,
`harness2-report`, `harness2-setup`, `harness2-check`, `harness2-demo`, `harness2-bridge`.

## Code Conventions

- **Apache 2.0 header on every new `.py` and `.sh` file.** 45/45 Python files carry it; a file
  without one will not pass review.
- `from __future__ import annotations` at the top of every module.
- Frozen dataclasses for value objects; `typing.Protocol` for the adapter/substrate seams.
- Module docstring states the contract and the invariant it protects. Keep it true when you
  change behavior.
- `print()` for operator-facing output — there are zero `logging` imports; do not introduce one.
- Domain-specific exception subclasses (`ConfigError`, `BudgetError`, `ProposerError`,
  `RenderInputError`) over bare `RuntimeError`/`ValueError`.
- ruff: line-length 100, `select = ["E4","E7","E9","F","I"]`, `third_party`/`dependencies` excluded.
- **Do not add runtime dependencies.** New third-party code goes in an optional extra or a
  pinned checkout under `dependencies/`.

Full detail: `.claude/rules/code-style.md`.

## Benchmark Runs — Cost and Integrity

Model-backed runs **cost real money**: one task at budget `k` is R=4k executor rollouts plus
P=3k improvement stages (`harness2/core/ledger.py`). Before any real run:

1. `harness2-demo` — free, deterministic, exercises the real recursion and library workflow.
2. `harness2 ... --dry-run` — previews task selection with no model calls.
3. `--max-tasks 1 --only-domains <one>` — smallest real slice.

`harness2`, `harness2-judge`, and `harness2-report` must be invoked with the **same**
benchmark, executor, subset, `--k`, `--mode`, and seed, or judging reads the wrong namespace.
Repeating a command resumes it. Use a fresh `HARNESS2_RUNS_DIR` when models or task selection
change — the saved configuration blocks incompatible resumes.

A judge failure is recorded as `score=None` (ungraded) and excluded from the denominator —
**never coerce it to `0`**. Same for tasks that never completed.

Full detail: `.claude/rules/benchmark-runs.md`.

## Security

- Secrets are env vars, loaded from `.env` by `config.load_dotenv()` with the shell winning.
  Names only in any document: `HARNESS2_CODEX_API_KEY`, `HARNESS2_JUDGE_API_KEY`,
  `HARNESS2_BRIDGE_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`. Never read or echo values.
- `harness2-bridge` mints a LiteLLM master key into `dependencies/codex/bridge.json`
  (chmod 0600). Treat that file as a credential — never read, print, or commit it.
- **Benchmark-answer leakage is an active threat.** All three benchmarks are public and
  JobBench publishes `RUBRICS.json`. Executor network access is unrestricted and Codex runs
  with its sandbox disabled. Adapters strip grading criteria before exposing task data to the
  improver (`benchmarks/lab.py:_task_json`) — preserve that. See
  `docs/EXPERIMENTS.md#web-access` for the host blocklist to apply before evaluation.
- Every adapter shells out via `subprocess` (10 modules). Pass argument lists, never
  `shell=True` with interpolated task data.

Full detail: `.claude/rules/security-guidelines.md`.

## Testing

`tests/test_workflow.py` — stdlib `unittest`, five regressions over the paper's execution,
evidence, and adoption contracts. No pytest, no CI workflow.

```bash
python -m unittest discover -s tests -v
python -m unittest tests.test_workflow.WorkflowTests.test_recursion_budget_and_resume
```

Tests isolate state in `tempfile.TemporaryDirectory()` registered with `addCleanup`, and use
`harness2/demo.py` (`DemoAdapter`, `DemoProposer`) as the free deterministic fixture. Mock the
substrate and proposer, never a live model. A behavior change to budget accounting, evidence
completeness, library adoption, or selection needs a matching regression here.

Full detail: `.claude/rules/testing-standards.md`.

## Vendored Code

`third_party/` and `dependencies/` are upstream-owned. Never edit them. Upstream commits are
pinned in `config.DEPENDENCY_PINS`, patches documented in `dependencies/PINNED.md`, and
release-asset checksums verified by `python scripts/check_provenance.py` against
`dependencies/ASSETS.json`. Changing a release-owned runner overlay means updating its
checksum in `ASSETS.json` in the same change.

Full detail: `.claude/rules/vendored-code.md`.

## Ports

No fixed service ports. Only `harness2-bridge` listens.

| Service | Default | Env Var | Offset |
|---|---|---|---|
| LiteLLM bridge (Vertex Responses API for Codex) | ephemeral free port, persisted in `dependencies/codex/bridge.json` | `--port` flag; URL consumed via `HARNESS2_CODEX_BASE_URL` | n/a — already per-run unique |

Bridge binds `127.0.0.1` by default; `--host 0.0.0.0` is required only so WorkBuddy containers
can reach it via `host.docker.internal`. Parallel sessions need no offsetting — each bridge
picks its own free port.

## Known Gaps

- No `.gitignore`. `runs/`, `__pycache__/`, and `harness2.egg-info/` show as untracked in
  `git status` — never stage them.
- No CI. Run `uvx ruff check .`, `python -m unittest discover -s tests`, and
  `python scripts/check_provenance.py` locally before opening a PR. All three pass as of
  2026-10-20 (5 tests, 47 asset checksums, 0 lint findings).
- ruff is configured in `pyproject.toml` but declared in no extra and absent from `.venv` —
  invoke it as `uvx ruff` rather than assuming it is on `PATH`.

## PR Conventions

| Setting | Value |
|---|---|
| Platform | GitHub |
| Default remote | `origin` = `ChuckGriess/harness2` (fork) |
| Target branch | `main` on the fork |
| Branch naming | `<type>/<description>` |
| Title format | Conventional commits: `<type>(scope): description` |
| Draft by default | No |

Upstream contributions target `google-research/harness2` `main`, require a signed Google CLA,
and go through PR review per `CONTRIBUTING.md` — including for project members.

### PR Body Template

```
## Summary
<1-3 bullet points>

## Test Plan
- [ ] `uvx ruff check .`
- [ ] `python -m unittest discover -s tests`
- [ ] <behavior-specific verification>
```

## Ticket Conventions

| Setting | Value |
|---|---|
| Issue tracker | Linear (MCP configured) |
| Technical description | Yes |
| Acceptance criteria | Yes |
| DoR / DoD | Yes |
| AI estimation | Yes |
| Title format | `[Module] Brief description` |
| Duplicate check | Search issue tracker |

## Protected Branches

NEVER commit directly to: main, master, develop, qa, staging, dev.

## Anti-Loop Protocol

- Max 3 retries for any failing operation
- Same error twice triggers root cause analysis
- After 3 failures, ask the user or document the blocker and move on
