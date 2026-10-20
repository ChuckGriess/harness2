---
paths:
  - "tests/**/*.py"
  - "harness2/demo.py"
---

# Testing Standards

Stdlib `unittest` only. No pytest, no plugins, no CI.

```bash
python -m unittest discover -s tests -v
python -m unittest tests.test_workflow.WorkflowTests.<test_name>
```

## What the suite is for

`tests/test_workflow.py` guards the paper's contracts, not line coverage. The five existing
tests pin: recursion budget and resume, evidence completeness plus library prefixing, invalid
proposals retrying with feedback while accumulating cost, no-entry exclusion from the library
on resume, and failed selections never reaching the judge.

Changing budget accounting (`core/ledger.py`), evidence construction (`core/context.py`),
library adoption (`core/library.py`), selection (`core/selection.py`), or judging
(`core/judge.py`) requires a matching regression here in the same change.

## Patterns to follow

- Isolate all filesystem state in `tempfile.TemporaryDirectory()` registered via
  `self.addCleanup(tmp.cleanup)` in `setUp`. Never write under the repo or `runs/`.
- Use `harness2/demo.py` — `DemoAdapter`, `DemoProposer`, `BASE_SCRIPT` — as the integration
  fixture. It runs the real recursion and library workflow with deterministic editors and real
  Python executions, free and credential-free.
- Mock the substrate, proposer, or subprocess boundary (`unittest.mock.Mock`, `patch`,
  `patch.dict` for `config` tables). **Never call a live model from a test.**
- Assert on exact values the contract promises (`(ledger["R"], ledger["P"]) == (4*k, 3*k)`,
  exact deliverable text), not on "something was produced".
- `subTest` for budget sweeps over `k`.

## Pre-PR verification

```bash
uvx ruff check .                    # ruff is configured but not installed in .venv
python -m unittest discover -s tests
python scripts/check_provenance.py
```
