# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Shared plumbing for the CLIs: adapter construction and the on-disk conventions.

The conventions are defined here once, because run_sweep writes them and
judge/report read them back.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import config
from ..core import library as LIB
from ..core import schedule as SCHED
from ..core.spec import BenchmarkAdapter

BENCHES = ("jb", "wb", "lab", "radar")
WB_SUBSETS = ("office", "web", "code")
SUBSTRATES = ("oc", "cx", "ca", "cma")
RADAR_ONLY = ("ca", "cma")

MODES_DEFAULT = "parallel"
MODES_HELP = (
    "parallel | sequential (agg / seq accepted: the on-disk tokens); "
    "comma list, e.g. 'parallel' or 'parallel,sequential'"
)


class HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    """Defaults in the help text, module docstring kept verbatim as the description."""


def make_adapter(
    bench: str, subset: str | None, substrate: str, split_file: Path | None = None
) -> BenchmarkAdapter:
    """The one place a CLI turns (--bench, --subset, --substrate) into an adapter."""
    if bench not in BENCHES:
        raise ValueError(f"unknown bench {bench!r}; expected one of {BENCHES}")
    if substrate not in SUBSTRATES:
        raise ValueError(f"unknown substrate {substrate!r}; expected one of {SUBSTRATES}")
    if bench == "wb":
        if subset not in WB_SUBSETS:
            raise ValueError(f"--bench wb needs --subset from {WB_SUBSETS}, got {subset!r} ")
        from ..benchmarks.workbuddy import WbAdapter

        return WbAdapter(subset, substrate=substrate, split_file=split_file)
    if subset:
        raise ValueError(f"--subset applies to --bench wb only, got {subset!r} for {bench}")
    if bench == "radar":
        from ..benchmarks.radar import RadarAdapter

        return RadarAdapter(substrate=substrate, split_file=split_file)
    if substrate in RADAR_ONLY:
        raise ValueError(f"substrate {substrate!r} runs the radar benchmark only, not {bench!r}")
    if bench == "jb":
        from ..benchmarks.jobbench import JobBenchAdapter

        return JobBenchAdapter(substrate=substrate, split_file=split_file)
    from ..benchmarks.lab import LabAdapter

    return LabAdapter(substrate=substrate, split_file=split_file)


def judge_model_for(adapter: BenchmarkAdapter) -> str:
    """The judge model a cell grades with and records in its run identity."""
    return config.RADAR_JUDGE_MODEL if adapter.spec.name == "radar" else config.JUDGE_MODEL


def validate_models_for(adapter: BenchmarkAdapter, *, judge: bool = False) -> None:
    """Model settings check for this cell; ca/cma need the SDK and a credential."""
    if adapter.substrate.name in RADAR_ONLY:
        adapter.substrate.preflight()
        return
    config.validate_models(judge=judge)


def runs_root_for(adapter: BenchmarkAdapter) -> Path:
    """Scheduler state: summaries, harnesses and selections, not the rollout store."""
    return adapter.spec.runs_root / "adapt"


def library_root_for(adapter: BenchmarkAdapter, k: int, *, seed: int | None, tag: str = "") -> Path:
    """Per-(benchmark, substrate, k, seed, tag) experience-library root (the improver's R).

    Separate seeds and tags keep independent task streams from sharing entries.
    """
    SCHED.validate_tag(tag)
    variant = adapter.substrate.name
    if seed is not None:
        variant += f"-s{int(seed)}"
    if tag:
        variant += f"-{tag}"
    return LIB.root_for(
        adapter.spec.runs_root / "library", benchmark=adapter.spec.name, k=k, variant=variant
    )


def summary_namespace(adapter: BenchmarkAdapter, k: int, *, seed: int | None, tag: str = "") -> str:
    """The namespace whose task dirs hold summary.json for BOTH modes: run_task keeps the
    summary in the parallel mode's namespace (on-disk token "agg") whichever modes ran."""
    return SCHED.ns(adapter.spec.name, adapter.substrate.name, "agg", k, seed=seed, tag=tag)


def add_cell_args(p: argparse._ActionsContainer) -> None:
    """The (benchmark, substrate) cell selectors; `p` may be a parser or an argument group."""
    p.add_argument("--bench", required=True, choices=BENCHES, help="jb | wb | lab | radar")
    p.add_argument(
        "--subset", default=None, choices=WB_SUBSETS, help="WB only: office | web | code"
    )
    p.add_argument(
        "--substrate",
        default="oc",
        choices=SUBSTRATES,
        help="task-executor substrate: oc (opencode) | cx (codex) | ca (Claude API, radar) | "
        "cma (Claude Managed Agents, radar)",
    )


def add_modes_arg(p: argparse._ActionsContainer) -> None:
    p.add_argument("--mode", "--modes", dest="modes", default=MODES_DEFAULT, help=MODES_HELP)


def add_run_identity_args(p: argparse._ActionsContainer) -> None:
    """The run-identity knobs that enter the namespace."""
    p.add_argument(
        "--k", type=int, default=1, help="scale: R=4k rollouts, P=3k improver sessions per task"
    )
    p.add_argument(
        "--seed", type=int, default=None, help="stream-order seed; omit for sorted task order"
    )
    add_modes_arg(p)


def parse_modes(raw: str) -> tuple[str, ...]:
    """'parallel,sequential' (or the on-disk tokens 'agg,seq') -> tokens in MODES order."""
    names = [s.strip() for s in raw.split(",") if s.strip()]
    if not names:
        raise ValueError(
            f"--modes takes a comma list of parallel | sequential (agg | seq), got {raw!r}"
        )
    tokens = set()
    for name in names:
        try:
            tokens.add(SCHED.mode_token(name))
        except ValueError as exc:
            raise ValueError(f"--modes: {exc} (got {raw!r})") from None
    return tuple(t for t in SCHED.MODES if t in tokens)


def mode_label(token: str) -> str:
    """Human name for an on-disk token ('agg' -> 'parallel'); unknown tokens pass through."""
    return SCHED.MODE_NAMES.get(token, token)
