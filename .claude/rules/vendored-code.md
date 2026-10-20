---
paths:
  - "third_party/**"
  - "dependencies/**"
  - "harness2/install.py"
  - "scripts/check_provenance.py"
---

# Vendored and Pinned Code

`third_party/` and `dependencies/` are upstream-owned. **Never edit files inside them** and
never reformat or lint them — ruff excludes both trees via `extend-exclude`.

## Pins live in one place

Upstream commits are pinned in `config.DEPENDENCY_PINS` with their origins in
`config.DEPENDENCY_ORIGINS` (job-bench-eval, workbuddy-bench, harvey-labs, opencode). Tool
versions are constants in `harness2/config.py` (`CODEX_VERSION`, `WB_OC_HARNESS_VER`,
`WB_CX_HARNESS_VER`). Bumping an upstream means editing the pin there and recording the change
in `dependencies/PINNED.md` — not pointing at a branch.

## Provenance is checksum-enforced

`scripts/check_provenance.py` verifies every release-owned runner overlay and patch file
against the SHA-256 digests in `dependencies/ASSETS.json`, and fails on any changed or missing
asset. If you change a file under `third_party/` that the release owns (a runner overlay, a
patch), you must update its digest in `ASSETS.json` in the same change, and `PROVENANCE.md`
alongside it. Run the check before every PR:

```bash
python scripts/check_provenance.py
```

Installation is the only thing that writes into these trees — `harness2/install.py` performs
repeatable installation of pinned tools and release-owned overlays, driven by
`harness2-setup <bench>`. Changes to overlay content belong there, not in hand edits.

## Packaging

`MANIFEST.in` controls the sdist contents (docs, scripts, assets, `dependencies/README.md`,
`dependencies/PINNED.md`, `harness2/third_party/*`), with `__pycache__`, `*.py[cod]`, and
`*.log` globally excluded. New non-Python files that must ship need an explicit entry.
Licensing: Apache-2.0 for the project; vendored trees retain their upstream licenses, noted in
`dependencies/README.md` and the per-tree `LICENSE` files. Do not relicense or strip those.
