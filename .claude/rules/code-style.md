---
paths:
  - "harness2/**/*.py"
  - "scripts/**/*.py"
  - "scripts/**/*.sh"
  - "tests/**/*.py"
---

# Code Style

## Non-negotiable per-file requirements

Every new `.py` and `.sh` file starts with the Apache 2.0 header used by all 45 existing
Python files (copy it verbatim from any sibling, e.g. `harness2/config.py`), followed by a
module docstring, followed by `from __future__ import annotations`.

The module docstring is a **design contract**, not a label. Existing ones state the
invariant the module protects (`core/judge.py`: "A judge failure is None, never a zero");
`core/runmark.py`: "presence of output is not enough"). When you change behavior, update the
docstring so it stays true.

## Idioms to match

- Frozen dataclasses (`@dataclass(frozen=True)`) for value objects; validate in `__post_init__`
  and raise `ValueError` with the invalid value repr'd and the legal set named.
- `typing.Protocol` / `runtime_checkable` for the adapter and substrate seams (`core/spec.py`).
- `print()` for operator-facing output. There are zero `logging` imports in the package — do
  not introduce one.
- Named exception subclasses over bare builtins: `ConfigError(RuntimeError)`,
  `BudgetError(AssertionError)`, `ProposerError(RuntimeError)`, `RenderInputError`,
  `RenderBackendError`. New failure classes get their own subclass and a message that carries
  the remediation, in the style of `config.require()`.
- Leading underscore for module-private helpers (`_free_port`, `_endpoint`, `_task_json`).
- Error messages name the fix, not just the problem: `"<VAR> is required for <x>; set it in
  .env or the shell"`.

## Dependencies

`pyproject.toml` declares `dependencies = []` deliberately. Do not add a runtime dependency.
Heavier needs go into an optional extra (`[project.optional-dependencies]`, pinned) or a
pinned upstream checkout under `dependencies/`. Prefer the stdlib — the codebase already
hand-rolls dotenv parsing (`config.load_dotenv`) rather than depend on `python-dotenv`.

## Lint bounds

ruff, `target-version = "py312"`, `line-length = 100`, `select = ["E4","E7","E9","F","I"]`,
with `third_party` and `dependencies` excluded.

ruff is configured but is **not** a declared dependency and is not in `.venv` — run it as
`uvx ruff check .` before every PR, since there is no CI to catch lint. The repo is clean
under this rule set; keep it that way. There is no evidence the project uses `ruff format`,
so do not reformat files wholesale.
