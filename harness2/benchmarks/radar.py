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

"""AI Radar: a watchlist-driven research digest benchmark on the `ca` substrate.

A task is one closed Monday-Sunday window x one watchlist topic, id `<since>/<topic>`; the
deliverable is output/digest.md. Gold lists live outside the repo, under
HARNESS2_RADAR_GOLD_DIR/<task id>/gold.json, and are read in exactly one place, `_gold`, reached
only from `score` (after selection committed) and the offline `graded_tasks` count. The watchlist and source
map are operator-owned inputs: they ride into every rollout's workspace through materialise
ctx, never through the editable tree, so the improver can change how the agent covers them,
never what they say. A judge failure (no gold, API error, malformed verdict) is None, never a
zero; a transport failure during a rollout is raised, never cached as agent behaviour.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

from .. import config
from ..core import runmark as RM
from ..core.spec import BenchPrompts, BenchSpec, resolve_channel_model
from ..substrate.base import Install, RunRequest
from ..substrate.claude_api import OUTPUT, RESULT, TRAJECTORY, WORKSPACE, CaSubstrate
from .tasks import select

DATA = Path(__file__).resolve().parent / "radar_data"
DIGEST = "digest.md"
GOLD = "gold.json"
_URL = re.compile(r"https?://\S+")

RADAR_PROMPTS = BenchPrompts(
    work=(
        "Keeping a technology leader current on AI: finding what the people and organizations "
        "on their watchlist released, published or decided in one week, filtering out hype, and "
        "writing a cited digest of what changed and why it matters to practitioners."
    ),
    domain_word="topic",
    deliverable_word="digest",
    inv_levers=(
        "- collect candidates from every structured source (feeds, releases, discussion, papers) "
        "before searching the open web, and note which sources were unreachable\n"
        "- open the primary source for each kept item and confirm its date inside the window\n"
        "- write the digest file early and revise it, so a budget cut still leaves a usable file"
    ),
    dom_levers=(
        "- prefer releases, API and SDK changes, open models, courses and governance decisions "
        "over commentary about them\n"
        "- merge duplicate coverage of one release into one item with all its links\n"
        "- write why-it-matters lines from the practitioner's side, not the vendor's"
    ),
    yardstick=(
        "Did the digest cover what the watched sources actually shipped in the window, with "
        "every item dated in range, linked to a primary source, free of hype, and useful to a "
        "practitioner deciding what to try or change?"
    ),
    selection_checks=(
        "Prefer the digest whose items are in the window, sourced, deduplicated and practical; "
        "that covers the watched sources most completely; and that does not pad with "
        "announcements, funding news or speculation."
    ),
)

JUDGE_SYSTEM = (
    "You grade an AI-news digest against a curated list of must-know items for one topic and "
    "one date window. You see the digest, the gold list, and the evidence the agent gathered "
    "(its tool calls and results). Judge only from that material; do not use outside knowledge "
    "of what happened."
)

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "gold": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "covered": {"type": "boolean"}},
                "required": ["id", "covered"],
                "additionalProperties": False,
            },
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "valid": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["title", "valid", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["gold", "items"],
    "additionalProperties": False,
}


def _sources() -> dict:
    return json.loads((DATA / "sources.json").read_text(encoding="utf-8"))


def topics() -> list[str]:
    return list(_sources()["topics"])


def task_table() -> dict[str, str]:
    """Task id -> topic, for every configured window x topic."""
    windows = json.loads((DATA / "radar_tasks.json").read_text(encoding="utf-8"))["windows"]
    return {f"{since}/{t}": t for since, _until in windows for t in topics()}


def window_of(task_id: str) -> tuple[date, date]:
    since = task_id.split("/", 1)[0]
    windows = json.loads((DATA / "radar_tasks.json").read_text(encoding="utf-8"))["windows"]
    for s, u in windows:
        if s == since:
            return date.fromisoformat(s), date.fromisoformat(u)
    raise ValueError(f"no window starts on {since!r}; known windows: {windows}")


def render_prompt(topic: str, since: date, until: date, *, label: str = "") -> str:
    """The task as the agent receives it: topic, inclusive window and the watchlist."""
    watchlist = (DATA / "watchlist.md").read_text(encoding="utf-8").strip()
    return (
        f"# TASK {label or f'{since}/{topic}'}\n"
        f"Topic: {topic}\nWindow: {since} to {until} (inclusive)\n\n"
        f"Write output/digest.md covering what the watched sources for this topic shipped, "
        f"published or decided in the window. sources.json in the workspace maps each "
        f"person and organization to their feeds, repos and pages.\n\n"
        f"## Watchlist\n{watchlist}\n"
    )


def radar_spec() -> BenchSpec:
    """The radar BenchSpec. A value, not a code path."""
    channel = resolve_channel_model(
        "claude_api", config.PROPOSER_MODEL_BY_CHANNEL["claude_api"], effort="high"
    )
    return BenchSpec(
        name="radar",
        runs_root=config.RUNS / "radar",
        install_mode="workspace_agents_md",
        prompts=RADAR_PROMPTS,
        channels={"proposer": channel, "selector": channel},
        domain_word="topic",
        leak_globs=(GOLD,),
        proposer_deliverables="contents",
        obs_trunc=50_000,
    )


def _stdlib_problems(rel: str, src: str, siblings: set[str]) -> list[str]:
    try:
        mod = ast.parse(src)
    except SyntaxError as e:
        return [f"{rel}: does not parse ({e.msg}, line {e.lineno})"]
    bad = set()
    for node in ast.walk(mod):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        for n in names:
            top = n.split(".", 1)[0]
            if top not in sys.stdlib_module_names and top not in siblings:
                bad.add(top)
    return [
        f"{rel}: imports {m!r}, which is neither stdlib nor a sibling in scripts/; the sandbox "
        f"has no third-party packages, so rewrite it with the standard library"
        for m in sorted(bad)
    ]


class RadarAdapter:
    """The BenchmarkAdapter surface for AI Radar, on the `ca` substrate only."""

    def __init__(
        self,
        *,
        substrate: str = "ca",
        client=None,
        judge_client=None,
        timeout: int | None = None,
        split_file: Path | None = None,
        gold_dir: Path | str | None = None,
        runs_root: Path | None = None,
    ):
        if substrate != "ca":
            raise ValueError(f"the radar benchmark runs on substrate 'ca' only, got {substrate!r}")
        self.spec = radar_spec()
        if runs_root is not None:
            self.spec = replace(self.spec, runs_root=Path(runs_root))
        self.substrate = CaSubstrate(launcher=self._launch, client=client)
        self._judge_client = judge_client
        self.timeout = int(timeout or 1800)
        self.split_file = split_file
        self.gold_dir = Path(gold_dir or config.RADAR_GOLD_DIR) if (
            gold_dir or config.RADAR_GOLD_DIR
        ) else None

    def tasks(self, split: str = "all") -> list[str]:
        return select(task_table(), split, self.split_file, benchmark="radar")

    def domain_of(self, task_id: str) -> str:
        return task_id.split("/", 1)[1]

    def reference_domain_order(self) -> list[str]:
        return topics()

    def task_prompt(self, task_id: str) -> str:
        since, until = window_of(task_id)
        return render_prompt(self.domain_of(task_id), since, until, label=task_id)

    def task_files(self, task_id: str) -> Path | None:
        return None

    def _run_dir(self, run_id: str) -> Path:
        return self.spec.runs_root / "results" / run_id

    def workspace_files(self) -> dict[str, str]:
        """Operator-owned inputs every rollout's workspace receives, as materialise ctx."""
        return {
            f"file:{name}": (DATA / name).read_text(encoding="utf-8")
            for name in ("sources.json", "watchlist.md")
        }

    def _install(self, harness_dir: Path | None) -> Install:
        return self.substrate.materialise(
            Path(harness_dir) if harness_dir else self.base_harness(), self.spec,
            self.workspace_files(),
        )

    def _launch(self, req: RunRequest, install: Install) -> list[Path]:
        prompt = self.task_prompt(req.task_ref)
        dirs = []
        for run_id in req.run_ids:
            rd = self._run_dir(run_id)
            self.substrate.rollout(prompt, install, rd, timeout=req.timeout)
            dirs.append(rd)
        return dirs

    def execute(
        self, task_id: str, harness_dir: Path | None, run_id: str, *, timeout: int | None = None
    ) -> Path:
        out = self._run_dir(run_id)
        if RM.is_complete(out, harness_dir=harness_dir):
            return out
        if out.is_dir() and any(out.iterdir()):
            print(f"  [radar] re-rolling {run_id}: {RM.why_not(out, harness_dir=harness_dir)}",
                  flush=True)
            shutil.rmtree(out)
        install = self._install(harness_dir)
        try:
            self.substrate.execute(task_id, install, [run_id], int(timeout or self.timeout))
        finally:
            shutil.rmtree(install.staging_dir, ignore_errors=True)
        try:
            result = json.loads((out / RESULT).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise RuntimeError(f"radar rollout wrote no readable {RESULT} at {out}: {e}") from e
        if str(result.get("error", "")).startswith("API call failed"):
            raise RuntimeError(
                f"radar rollout {run_id} hit a transport failure, which is not agent behaviour "
                f"and is not cached: {result['error']}"
            )
        RM.mark_complete(out, run_id=run_id, harness_dir=harness_dir)
        return out

    def _digest(self, run_dir: Path) -> Path:
        return Path(run_dir) / WORKSPACE / OUTPUT / DIGEST

    def delivered(self, run_dir: Path) -> bool:
        p = self._digest(run_dir)
        return p.is_file() and bool(_URL.search(p.read_text(encoding="utf-8", errors="replace")))

    def trajectory(self, run_dir: Path, *, obs_trunc: int) -> str:
        return self.substrate.read_trajectory(
            Path(run_dir) / TRAJECTORY, obs_trunc=obs_trunc, max_chars=200_000
        )

    def deliverable_files(self, run_dir: Path) -> dict[str, str]:
        root = Path(run_dir) / WORKSPACE / OUTPUT
        if not root.is_dir():
            return {}
        return {
            p.relative_to(root).as_posix(): p.read_text(encoding="utf-8", errors="replace")
            for p in sorted(root.rglob("*"))
            if p.is_file()
        }

    def deliverable_text(self, run_dir: Path) -> str:
        p = self._digest(run_dir)
        return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""

    def base_harness(self) -> Path:
        return DATA / "base"

    def validate_tree(self, tree: dict[str, str]) -> None:
        siblings = {Path(r).stem for r in tree if r.startswith("scripts/") and r.endswith(".py")}
        problems = [
            p
            for rel, src in sorted(tree.items())
            if rel.startswith("scripts/") and rel.endswith(".py")
            for p in _stdlib_problems(rel, src, siblings)
        ]
        if problems:
            raise ValueError("radar harness problems:\n- " + "\n- ".join(problems))

    def graded_tasks(self) -> list[str]:
        """Tasks with a usable gold list; for preflight, never for model context."""
        return [t for t in self.tasks() if self._gold(t) is not None]

    def _gold(self, task_id: str) -> list[dict] | None:
        """The ONE read of a gold list; reached only from score() and graded_tasks()."""
        if self.gold_dir is None:
            return None
        p = self.gold_dir / task_id / GOLD
        try:
            items = json.loads(p.read_text(encoding="utf-8")).get("items")
        except (OSError, ValueError, AttributeError):
            return None
        if not isinstance(items, list) or not items:
            return None
        ids = [i.get("id") for i in items if isinstance(i, dict)]
        if len(ids) != len(items) or not all(isinstance(x, str) for x in ids):
            return None
        if len(set(ids)) != len(ids):
            return None
        return items

    def _judge(self, task_id: str, gold: list[dict], digest: str, evidence: str) -> dict:
        since, until = window_of(task_id)
        prompt = (
            f"Topic: {self.domain_of(task_id)}\nWindow: {since} to {until} (inclusive)\n\n"
            f"## Gold list (must-know items)\n{json.dumps(gold, indent=1, ensure_ascii=False)}\n\n"
            f"## Digest\n{digest}\n\n## Evidence the agent gathered\n{evidence}\n\n"
            "For each gold item, covered=true only if some digest item refers to the same "
            "release, publication or decision (any wording).\n"
            "For each digest item, in order, valid=true only if ALL hold: dated inside the "
            "window; carries at least one source URL; its claim is supported by the evidence or "
            "matches a gold item; it is on the topic; it gives practitioners something usable "
            "(not leaderboard claims, funding news or speculation); it does not duplicate an "
            "earlier digest item. Give a short reason either way."
        )
        client = self._judge_client or config.require_anthropic().Anthropic()
        resp = client.messages.create(
            model=config.RADAR_JUDGE_MODEL,
            max_tokens=16_000,
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
        )
        resp = resp if isinstance(resp, dict) else resp.to_dict()
        if resp.get("stop_reason") != "end_turn":
            raise ValueError(f"judge stopped with {resp.get('stop_reason')!r}")
        text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
        return json.loads(text)

    def score(self, run_dir: Path, task_id: str) -> dict | None:
        gold = self._gold(task_id)
        if gold is None:
            print(f"  [radar] no usable gold list for {task_id}; ungraded", flush=True)
            return None
        digest = self.deliverable_text(run_dir)
        if not digest.strip():
            return {"n_passed": 0, "n_criteria": len(gold), "all_pass": False, "recall": 0.0,
                    "precision": None, "items": 0}
        try:
            verdict = self._judge(task_id, gold, digest, self.trajectory(run_dir, obs_trunc=4000))
        except Exception as e:  # any judge failure is ungraded, never a zero
            print(f"  [radar] judge failed for {task_id}: {type(e).__name__}: {e}", flush=True)
            return None
        return _tally(gold, verdict)


def _tally(gold: list[dict], verdict) -> dict | None:
    """Fold a judge verdict into n_passed / n_criteria; None when the verdict is malformed."""
    if not isinstance(verdict, dict):
        return None
    marks = verdict.get("gold")
    items = verdict.get("items")
    if not isinstance(marks, list) or not isinstance(items, list):
        return None
    covered = {m.get("id") for m in marks if isinstance(m, dict) and m.get("covered") is True}
    if {m.get("id") for m in marks if isinstance(m, dict)} != {g["id"] for g in gold}:
        return None
    if not all(isinstance(i, dict) and isinstance(i.get("valid"), bool) for i in items):
        return None
    n_cov = len(covered)
    n_valid = sum(i["valid"] for i in items)
    n_criteria = len(gold) + len(items)
    n_passed = n_cov + n_valid
    return {
        "n_passed": n_passed,
        "n_criteria": n_criteria,
        "all_pass": n_passed == n_criteria,
        "recall": round(n_cov / len(gold), 4),
        "precision": round(n_valid / len(items), 4) if items else None,
        "items": len(items),
    }
