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

"""CaSubstrate: the Claude API tool loop (harness2/claude_loop.py) as a solver substrate.

CA_SURFACE is the c1..c8 shape without subagents or plugins: the loop has no delegation and no
in-loop plugin seam. One install mode, `workspace_agents_md`: always-on files are composed into
the system prompt, skills are advertised by name and description and read through `load_skill`,
and scripts run only through `run_script`. The agent may write only under `output/`. As with
OpenCode, the launcher body is benchmark-supplied; `rollout` is the one way it runs the loop,
and a rollout that stops on a cap or a model failure still returns, with the reason recorded.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Mapping

from .. import claude_loop as CL
from .. import config
from ..core import runmark as RM
from ..core import tree as HT
from ..core.tree import SurfaceSpec
from .base import Install, Launcher, RunRequest

CA_SURFACE = SurfaceSpec(
    name="ca",
    always_on=(
        "systemprompt.md",
        "guardrails.md",
        "LongTermMEMORY.md",
        "tool_descriptions/tool_guidance.md",
    ),
    always_on_max=24_000,
    always_on_cap_reason="the composed system prompt is resent on every turn of the tool loop",
    skill_dir="skills",
    skill_name_rule="must_match",
    trigger_hint=(
        "Fix it by adding a trigger line naming `{name}` to systemprompt.md or "
        'guardrails.md, e.g. "When <the situation this skill handles>, call `load_skill` '
        'with `{name}`."'
    ),
    autotrigger_file="guardrails.md",
    trigger_line="- When {desc}, call `load_skill` with `{name}`.\n",
    subagent_requires_mode=False,
    plugin_dir=None,
    hooks_file=None,
    stubs=(
        (
            "scripts/README.md",
            "<!-- c7: deterministic helpers, run through the `run_script` tool. Add one by "
            "emitting scripts/<name>.py (stdlib only). -->\n",
        ),
        (
            "skills/README.md",
            "<!-- c6: reusable workflows loaded on demand via the `load_skill` tool. Add one as "
            "skills/<name>/SKILL.md with frontmatter `name:` matching the directory and a "
            "`description:`. -->\n",
        ),
    ),
)

TRAJECTORY = "trajectory.jsonl"
RESULT = "result.json"
WORKSPACE = "workspace"
OUTPUT = "output"


def web_tools(max_searches: int = config.CA_MAX_SEARCHES,
              max_fetches: int = config.CA_MAX_FETCHES) -> tuple[dict, ...]:
    """Anthropic-hosted web search and fetch; they run server-side, so need no container egress."""
    return (
        {"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": max_fetches,
         "max_content_tokens": 20_000},
    )


def _frontmatter_desc(body: str) -> str:
    m = re.search(r"^description:\s*(.+)$", body[:600], re.M)
    return m.group(1).strip() if m else "(no description)"


def compose_system(staged: Path) -> str:
    """Always-on text, then the skill index and the script list, as one system prompt."""
    parts = [
        t
        for f in CA_SURFACE.always_on
        if (staged / f).is_file() and (t := (staged / f).read_text(errors="replace").strip())
    ]
    skills = sorted(staged.glob(f"{CA_SURFACE.skill_dir}/*/SKILL.md"), key=lambda p: p.parent.name)
    if skills:
        idx = "\n".join(
            f"- `{p.parent.name}`: {_frontmatter_desc(p.read_text(errors='replace'))}"
            for p in skills
        )
        parts.append(
            f"# AVAILABLE SKILLS\nLoad with the `load_skill` tool when the situation matches:\n{idx}"
        )
    scripts = sorted(p.name for p in staged.glob("scripts/*.py"))
    if scripts:
        parts.append(f"# HELPER SCRIPTS\nRun with the `run_script` tool: {', '.join(scripts)}")
    return "\n\n---\n\n".join(parts)


class CaSubstrate:
    """The Claude API tool loop behind the one Substrate interface."""

    name = "ca"

    def __init__(
        self,
        launcher: Launcher | None = None,
        *,
        client=None,
        model: str = config.CA_MODEL,
        server_tools: tuple[dict, ...] | None = None,
    ):
        self._launcher = launcher
        self._client = client
        self.model = model
        self.server_tools = web_tools() if server_tools is None else server_tools

    def surface(self) -> SurfaceSpec:
        return CA_SURFACE

    def materialise(self, tree_dir: Path, spec, ctx: Mapping[str, str]) -> Install:
        mode = spec.install_mode
        if mode != "workspace_agents_md":
            raise ValueError(
                f"install_mode {mode!r} is not a ca mode; this substrate supports only "
                f"'workspace_agents_md'"
            )
        tree_dir = Path(tree_dir)
        if not HT.is_tree(CA_SURFACE, tree_dir):
            raise ValueError(f"{tree_dir} is not a ca harness tree (no always-on file present)")
        staged = Path(tempfile.mkdtemp(prefix="adapt-ca-"))
        try:
            shutil.copytree(tree_dir, staged, dirs_exist_ok=True)
            for key, body in ctx.items():
                if key.startswith("file:"):
                    p = staged / HT.normalize_rel(key[len("file:") :])
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(body, encoding="utf-8")
            out = staged / "_composed" / "system.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(compose_system(staged), encoding="utf-8")
        except BaseException:
            shutil.rmtree(staged, ignore_errors=True)
            raise
        return Install(
            staging_dir=staged, fingerprint=RM.fingerprint(tree_dir), extras={"install_mode": mode}
        )

    def rollout(self, prompt: str, install: Install, run_dir: Path, *, timeout: int):
        """Run one conversation in a fresh `run_dir/workspace`; returns the LoopResult.

        Writes run_dir/trajectory.jsonl and run_dir/result.json. Idempotent by run_dir: an
        existing workspace is replaced, never merged.
        """
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        ws = run_dir / WORKSPACE
        if ws.exists():
            shutil.rmtree(ws)
        shutil.copytree(
            install.staging_dir, ws, ignore=shutil.ignore_patterns("_composed", "README.md")
        )
        (ws / OUTPUT).mkdir(exist_ok=True)
        box = CL.ToolBox(
            ws, writable=OUTPUT, scripts=frozenset(p.name for p in ws.glob("scripts/*.py"))
        )
        system = (install.staging_dir / "_composed" / "system.md").read_text(encoding="utf-8")
        client = self._client or config.require_anthropic().Anthropic()
        res = CL.run(
            client,
            model=self.model,
            system=system,
            prompt=prompt,
            box=box,
            caps=CL.Caps(config.CA_MAX_TURNS, config.CA_MAX_USD_CENTS / 100, float(timeout)),
            capture=run_dir / TRAJECTORY,
            server_tools=self.server_tools,
        )
        (run_dir / RESULT).write_text(
            json.dumps(
                {"ok": res.ok, "error": res.error, "stop": res.stop, "turns": res.turns,
                 "cost_usd": round(res.cost_usd, 6), "model": self.model},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return res

    def execute(
        self, task_ref: str, install: Install, run_ids: list[str], timeout: int
    ) -> list[Path]:
        if self._launcher is None:
            raise RuntimeError(
                "CaSubstrate was constructed without a launcher body; the benchmark adapter "
                "must supply one (it owns the task prompt and the run_dir location)"
            )
        return self._launcher(
            RunRequest(task_ref=task_ref, run_ids=tuple(run_ids), timeout=int(timeout)), install
        )

    def read_trajectory(
        self, stream_path: Path, *, obs_trunc: int, max_chars: int, args_trunc: int = 400
    ) -> str:
        return render_capture(
            Path(stream_path), obs_trunc=obs_trunc, max_chars=max_chars, args_trunc=args_trunc
        )

    def preflight(self) -> None:
        """Checks the SDK extra and that a credential NAME is set; calls no model."""
        config.require_anthropic()
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is required for the ca substrate; set it in .env or the shell"
            )


_TRUNC_FMT = "…(+{n} chars — render truncation)"


def _clip(s, n: int) -> str:
    s = "" if s is None else str(s)
    s = s.replace("\x00", "")
    if n <= 0 or len(s) <= n:
        return s
    return s[:n] + _TRUNC_FMT.format(n=len(s) - n)


def _server_result(block: dict, obs_trunc: int) -> str:
    """One server-tool result block as a grammar line; an error object is an [ERROR]."""
    name = block.get("type", "").removesuffix("_tool_result")
    content = block.get("content")
    if isinstance(content, dict) and (
        "error_code" in content or str(content.get("type", "")).endswith("error")
    ):
        return f"[ERROR] {name}: {content.get('error_code', content.get('type'))}"
    if isinstance(content, list):
        hits = [f"{c.get('title', '')} — {c.get('url', '')}" for c in content if isinstance(c, dict)]
        return f"[OBSERVATION] {_clip(chr(10).join(hits), obs_trunc)}"
    if isinstance(content, dict):
        doc = content.get("content") if isinstance(content.get("content"), dict) else {}
        src = doc.get("source") if isinstance(doc.get("source"), dict) else {}
        body = src.get("data") or doc.get("text") or ""
        return f"[OBSERVATION] {content.get('url', '')}\n{_clip(body, obs_trunc)}"
    return f"[OBSERVATION] {_clip(content, obs_trunc)}"


def render_capture(path: Path, *, obs_trunc: int, max_chars: int, args_trunc: int = 400) -> str:
    """Render claude_loop's JSONL capture into the shared [ACTION]/[OBSERVATION]/[ERROR] grammar."""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"(failed to read trajectory: {e})"
    lines: list[str] = []
    for ln in raw.splitlines():
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("event") == "end":
            if rec.get("error"):
                lines.append(f"[ERROR] {_clip(rec['error'], 300)}")
            continue
        content = rec.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type", "")
            if t == "text" and str(b.get("text", "")).strip():
                lines.append(f"[ASSISTANT] {_clip(b['text'], obs_trunc)}")
            elif t == "thinking" and str(b.get("thinking", "")).strip():
                lines.append(f"[THINKING] {_clip(b['thinking'], obs_trunc)}")
            elif t in ("tool_use", "server_tool_use"):
                args = json.dumps(b.get("input"), default=str, ensure_ascii=False)
                lines.append(f"[ACTION] {b.get('name', 'tool')}({_clip(args, args_trunc)})")
            elif t.endswith("_tool_result") and t != "tool_result":
                lines.append(_server_result(b, obs_trunc))
            elif t == "tool_result":
                tag = "[OBSERVATION ERROR]" if b.get("is_error") else "[OBSERVATION]"
                lines.append(f"{tag} {_clip(b.get('content'), obs_trunc)}")
    rendered = "\n".join(lines).strip()
    if not rendered:
        return "(no parseable events in trajectory)"
    if len(rendered) > max_chars:
        dropped = len(rendered) - max_chars
        rendered = rendered[:max_chars] + "\n" + _TRUNC_FMT.format(n=dropped)
    return rendered
