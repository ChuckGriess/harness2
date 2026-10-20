---
paths:
  - "harness2/config.py"
  - "harness2/cli/bridge.py"
  - "harness2/benchmarks/**/*.py"
  - "harness2/substrate/**/*.py"
---

# Security Guidelines

## Secrets

Configuration loads from `.env` via `config.load_dotenv()` using `setdefault`, so the shell
always wins. Document **names only**, never values:
`HARNESS2_CODEX_API_KEY`, `HARNESS2_JUDGE_API_KEY`, `HARNESS2_BRIDGE_KEY`,
`HARNESS2_JUDGE_BASE_URL`, `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_CLOUD_PROJECT`.

`harness2-bridge` generates a LiteLLM master key with `secrets.token_urlsafe(24)` and persists
it to `dependencies/codex/bridge.json` at mode `0600`. Treat that file as a credential: never
read it, print it, include it in a diff, or commit it. Vertex auth is Application Default
Credentials (`gcloud auth application-default login`) — no key material belongs in the repo.

The repo has no `.gitignore`. Verify `git status` before staging so no `runs/` artifact,
`bridge.json`, or `.env` is swept in.

## Benchmark-answer leakage

This is the project's primary integrity threat, and it is documented as a real occurrence: in
a validation run, a Codex rollout on JobBench downloaded its task's `RUBRICS.json` unprompted.

- Adapters must strip grading criteria before task data reaches the improver — see
  `benchmarks/lab.py:_task_json`. Preserve this when touching any adapter's task exposure.
- Executor network access is unrestricted and Codex runs with its sandbox **disabled**, so
  restriction belongs at the network layer (egress firewall / DNS policy), not a tool setting.
  Block `huggingface.co`, `hf.co`, `*.hf.co`, `github.com`, `raw.githubusercontent.com` for
  the executor after setup. See `docs/EXPERIMENTS.md#web-access`.
- `benchmarks/wb_render.py:_admit()` is a positive allowlist of four output locations and
  raises on anything else. Keep it an allowlist — never widen it to a denylist.

## Subprocess boundaries

Ten modules shell out (`install.py`, `preflight.py`, `core/proposer.py`, `cli/setup.py`, and
every benchmark adapter). Always pass an argument list; never `shell=True` with interpolated
task, model, or path data. Set an explicit `timeout` as `preflight.probe()` does, and handle
`OSError` / `subprocess.TimeoutExpired` rather than letting them escape as crashes.

Proposer prompts are delivered over **stdin**, not argv (`core/proposer.py`) — keep untrusted
or large text off the command line.

## Model-facing text

Prompts live entirely in `core/prompts.py`. Benchmark text enters as values through
`BenchPrompts` slots, never as branches. Task content and rollout transcripts are untrusted
input from a model's perspective — do not add code paths that let them select prompts,
reach the judge before selection, or widen a filesystem allowlist.
