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

"""Render the score report for one cell from cached judged.json rows — read-only.

The figure each benchmark reports per (mode, k) — partial credit and all-criteria pass on LAB,
one score elsewhere — with pick provenance, the k=1 noise-floor note and the sanity warnings;
fell-back counts and improver spend come from the task summaries.

    python3 -m harness2.cli.report --bench jb --substrate oc --k 1,2

With --trajectory OUT.html it instead renders one sweep round by round from
rollouts_judged.json (cli.judge --rollouts): the score of every rollout, the improver's
notes, each composed harness's diff from the one before it, and each deliverable.
"""

from __future__ import annotations

import argparse
import difflib
import html as H
import json
import sys
from pathlib import Path

from ..core import report as R
from ..core import schedule as SCHED
from ..core import tree as TREE
from . import _common as C
from .judge import _executions_root, _load_rows


def _spend_and_fallbacks(ns_dir: Path, modes: tuple[str, ...], k: int) -> tuple[dict, dict]:
    """(fell_back, usd) keyed (mode token, k), from the summaries the sweep checkpointed."""
    fell_back: dict[tuple[str, int], int] = {}
    usd: dict[tuple[str, int], float] = {}
    for summary_path in sorted(ns_dir.glob("*/summary.json")):
        try:
            summary = json.loads(summary_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  ! skipping unreadable {summary_path}: {exc}", flush=True)
            continue
        records = SCHED._modes_of(summary)
        for mode in modes:
            key = (mode, k)
            rec = (records or {}).get(mode) or {}
            fell_back[key] = fell_back.get(key, 0) + len(rec.get("fell_back") or [])
            spend = sum(float(s.get("usd") or 0.0) for s in rec.get("stages") or [])
            sel = (summary.get("selections") or {}).get(mode) or {}
            usd[key] = usd.get(key, 0.0) + spend + float(sel.get("usd") or 0.0)
    return fell_back, usd


_CSS = """
:root{--bg:#fff;--fg:#1d1d1f;--mute:#6e6e73;--line:#d2d2d7;--accent:#2f6fde;--add:#e6f4ea;
--del:#fdecea}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--fg:#f2f2f2;--mute:#a1a1a6;
--line:#3a3a3c;--accent:#6b9cff;--add:#13361f;--del:#3d1717}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0 auto;
max-width:960px;padding:24px 16px}
h1{font-size:22px}h2{font-size:18px;margin-top:32px}.mute{color:var(--mute)}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid var(--line);
padding:6px 8px;text-align:left;vertical-align:top}.sel{font-weight:600;color:var(--accent)}
details{margin:4px 0}pre{overflow-x:auto;font-size:12px;white-space:pre-wrap}
.a{background:var(--add)}.d{background:var(--del)}svg text{fill:var(--mute);font-size:11px}
"""


def _pct(f: float | None) -> str:
    return "ungraded" if f is None else f"{100 * f:.1f}%"


def _chart(axis: list[tuple[str, float | None]]) -> str:
    """Inline SVG line of the improvement axis; ungraded points are gaps, not zeros."""
    w, h, pad = 520, 180, 28
    step = (w - 2 * pad) / max(1, len(axis) - 1)
    parts, path = [], []
    for i, (name, f) in enumerate(axis):
        x = pad + i * step
        parts.append(f'<text x="{x:.0f}" y="{h - 6}" text-anchor="middle">{H.escape(name)}</text>')
        if f is None:
            path.append(None)
            continue
        y = h - pad - f * (h - 2 * pad)
        path.append((x, y))
        parts.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="4" fill="var(--accent)"/>')
        parts.append(f'<text x="{x:.0f}" y="{y - 8:.0f}" text-anchor="middle">{_pct(f)}</text>')
    segs = [
        f'<line x1="{a[0]:.0f}" y1="{a[1]:.0f}" x2="{b[0]:.0f}" y2="{b[1]:.0f}" '
        f'stroke="var(--accent)" stroke-width="2"/>'
        for a, b in zip(path, path[1:])
        if a and b
    ]
    return (
        f'<svg viewBox="0 0 {w} {h}" width="100%" style="max-width:{w}px" role="img" '
        f'aria-label="score per round">{"".join(segs + parts)}</svg>'
    )


def _tree_diff(surface, before: Path, after: Path) -> str:
    """Unified diff of two harness snapshots, as escaped HTML lines."""
    a, b = TREE.read_tree(surface, before), TREE.read_tree(surface, after)
    out = []
    for rel in sorted(set(a) | set(b)):
        for ln in difflib.unified_diff(
            a.get(rel, "").splitlines(), b.get(rel, "").splitlines(),
            f"{before.name}/{rel}", f"{after.name}/{rel}", lineterm="",
        ):
            cls = "a" if ln.startswith("+") else "d" if ln.startswith("-") else ""
            out.append(f'<span class="{cls}">{H.escape(ln)}</span>' if cls else H.escape(ln))
    return "\n".join(out)


def trajectory_html(adapter, ns_dir: Path, mode_dirs: dict[str, Path], title: str) -> str:
    """Self-contained page: per task and mode, the score axis, every rollout with its notes,
    the composed-harness diff of each round, and each rollout's deliverable."""
    rows_path = ns_dir / "rollouts_judged.json"
    if not rows_path.is_file():
        raise SystemExit(f"no {rows_path} — run cli.judge --rollouts first")
    rows = json.loads(rows_path.read_text())
    by_run = {(r["task"], r["mode"], r["label"]): r.get("run_id", "") for r in rows}
    surface = adapter.substrate.surface()
    body = [f"<h1>{H.escape(title)}</h1>",
            '<p class="mute">Scores are post-hoc (cli.judge --rollouts); the selector and the '
            "improver never saw them. Ungraded means the judge failed — not a zero.</p>"]
    for summary_path in sorted(ns_dir.glob("*/summary.json")):
        summary = json.loads(summary_path.read_text())
        task = summary.get("task_id", "")
        base = [e for e in summary.get("base") or [] if isinstance(e, dict) and e.get("run_dir")]
        root = _executions_root(base[0]) if base else None
        for mode, mdir in mode_dirs.items():
            steps = R.trajectory(summary, rows, mode)
            if not steps:
                continue
            print(R.render_trajectory(steps, title=f"{task} [{C.mode_label(mode)}]"), "\n")
            hroot = mdir / task.replace("/", "__") / "harnesses"
            body.append(f"<h2>{H.escape(task)} — {H.escape(C.mode_label(mode))}</h2>")
            body.append(_chart(R.spine(steps)))
            body.append("<table><tr><th>round</th><th>rollout</th><th>score</th>"
                        "<th>improver notes</th></tr>")
            prev = "base"
            for s in steps:
                cls = ' class="sel"' if s.selected else ""
                score = _pct(s.frac) + (
                    f" ({s.n_passed}/{s.n_criteria})" if s.frac is not None else "")
                cell = [H.escape(s.notes)]
                if s.kind == "final":
                    diff = _tree_diff(surface, hroot / prev, hroot / s.label)
                    cell.append(f"<details><summary>harness diff {H.escape(prev)} → "
                                f"{H.escape(s.label)}</summary><pre>{diff or '(no change)'}"
                                "</pre></details>")
                    if mode == "seq":
                        prev = s.label
                run_id = by_run.get((task, "base" if s.kind == "base" else mode, s.label), "")
                rd = root / run_id if root and run_id else None
                if rd and rd.is_dir() and adapter.delivered(rd):
                    cell.append("<details><summary>deliverable</summary><pre>"
                                f"{H.escape(adapter.deliverable_text(rd))}</pre></details>")
                body.append(
                    f"<tr{cls}><td>{s.round}</td><td>{H.escape(s.label)} {H.escape(s.dim)}"
                    f"{' ✓ selected' if s.selected else ''}</td><td>{score}</td>"
                    f"<td>{''.join(cell)}</td></tr>"
                )
            body.append("</table>")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Harness² trajectory</title><style>{_CSS}</style></head>"
        f"<body>{''.join(body)}</body></html>"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="harness2.cli.report", description=__doc__, formatter_class=C.HelpFormatter
    )
    C.add_cell_args(p.add_argument_group("cell"))
    g = p.add_argument_group("run identity (names the sweeps)")
    g.add_argument("--k", default="1", help="comma list of scales to report, e.g. 1,2,3")
    g.add_argument(
        "--seed",
        type=int,
        default=None,
        help="stream-order seed of the sweep; omit for the seedless namespace",
    )
    C.add_modes_arg(g)
    p.add_argument(
        "--trajectory",
        type=Path,
        metavar="OUT.html",
        help="instead of the summary, write a per-round view of one k (score of every rollout "
        "from cli.judge --rollouts, improver notes, harness diffs, deliverables) to OUT.html",
    )
    args = p.parse_args(argv)
    modes = C.parse_modes(args.modes)
    try:
        ks = sorted({int(s) for s in args.k.split(",") if s.strip()})
    except ValueError:
        raise SystemExit(f"--k takes a comma list of ints, got {args.k!r}")
    if not ks:
        raise SystemExit("--k named no scale")

    adapter = C.make_adapter(args.bench, args.subset, args.substrate)
    if args.trajectory:
        if len(ks) != 1:
            raise SystemExit("--trajectory reports one sweep; pass a single --k")
        k, root = ks[0], C.runs_root_for(adapter)
        ns_dir = root / C.summary_namespace(adapter, k, seed=args.seed)
        mode_dirs = {
            m: root / SCHED.ns(adapter.spec.name, adapter.substrate.name, m, k, seed=args.seed)
            for m in modes
        }
        title = f"{adapter.spec.name}-{adapter.substrate.name} k={k} trajectory"
        page = trajectory_html(adapter, ns_dir, mode_dirs, title)
        args.trajectory.parent.mkdir(parents=True, exist_ok=True)
        args.trajectory.write_text(page, encoding="utf-8")
        print(f"wrote {args.trajectory}")
        return 0
    rows, fell_back, usd = [], {}, {}
    for k in ks:
        ns_dir = C.runs_root_for(adapter) / C.summary_namespace(adapter, k, seed=args.seed)
        judged = ns_dir / "judged.json"
        got = _load_rows(judged)
        if not got:
            print(f"  ! no judged rows at {judged} — run cli.judge for k={k} first", flush=True)
            continue
        rows.extend(r for r in got if r.mode in modes)
        fb, sp = _spend_and_fallbacks(ns_dir, modes, k)
        fell_back.update(fb)
        usd.update(sp)
    if not rows:
        raise SystemExit("nothing to report")

    title = (
        f"{adapter.spec.name}-{adapter.substrate.name}"
        + (f" seed={args.seed}" if args.seed is not None else "")
        + f" modes={','.join(C.mode_label(m) for m in modes)}"
    )
    summaries = R.summarise(rows, fell_back=fell_back, usd=usd)
    print(R.render(summaries, title=title, bench=adapter.spec.name))
    spend = sum(s.usd for s in summaries)
    if spend:
        print(
            f"\n  improver (editors, composer, selection, reflection) spend across reported modes: "
            f"${spend:.2f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
