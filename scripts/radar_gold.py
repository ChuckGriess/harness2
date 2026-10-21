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

"""Draft AI Radar gold lists from the watchlist's own sources, for a person to prune.

Gold lists decide what the radar judge counts as must-know, so they are never written into this
repository and never promoted without review. Drafting runs the base harness's feed and GitHub
fetchers for one task's topic and window and writes <gold-dir>/<task>/gold.draft.json; edit it
(delete noise, add what the fetchers cannot see, such as page-only and search-only sources),
then --promote validates it into gold.json.

    python scripts/radar_gold.py --task 2026-10-12/frontier-applications
    python scripts/radar_gold.py --all
    python scripts/radar_gold.py --task 2026-10-12/frontier-applications --promote
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from harness2 import config
from harness2.benchmarks import radar

FETCHERS = ("feeds.py", "github.py")


def gold_root(raw: str) -> Path:
    """The gold directory, refusing any location inside the repository."""
    if not raw:
        raise SystemExit("set HARNESS2_RADAR_GOLD_DIR or pass --gold-dir (outside this repo)")
    root = Path(raw).expanduser().resolve()
    repo = config.ROOT.resolve()
    if root == repo or repo in root.parents:
        raise SystemExit(f"refusing gold dir {root}: it is inside the repository {repo}")
    return root


def parse_lines(text: str) -> list[dict]:
    """Fetcher stdout -> candidate items, one per distinct URL, in output order."""
    items, seen = [], set()
    for ln in text.splitlines():
        parts = [p.strip() for p in ln.split(" | ")]
        if len(parts) != 4 or not parts[3].startswith("http") or parts[3] in seen:
            continue
        seen.add(parts[3])
        items.append({"id": f"g{len(items) + 1}", "date": parts[0], "source": parts[1],
                      "title": parts[2], "url": parts[3]})
    return items


def draft(task: str, root: Path) -> Path:
    since, until = radar.window_of(task)
    topic = task.split("/", 1)[1]
    ws = Path(tempfile.mkdtemp(prefix="radar-gold-"))
    try:
        shutil.copytree(radar.DATA / "base" / "scripts", ws / "scripts")
        shutil.copy(radar.DATA / "sources.json", ws / "sources.json")
        out = []
        for name in FETCHERS:
            p = subprocess.run(
                [sys.executable, f"scripts/{name}", "--topic", topic, "--since", str(since),
                 "--until", str(until)],
                cwd=ws, capture_output=True, text=True, timeout=600,
            )
            out.append(p.stdout)
            trailer = False
            for ln in p.stdout.splitlines():
                trailer = trailer or ln.startswith("# ")
                if trailer or ln.startswith("!"):
                    print(f"  {name}: {ln}")
    finally:
        shutil.rmtree(ws, ignore_errors=True)
    dest = root / task / "gold.draft.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    items = parse_lines("\n".join(out))
    dest.write_text(json.dumps({"task": task, "items": items}, indent=1) + "\n", encoding="utf-8")
    print(f"{task}: {len(items)} candidates -> {dest}")
    return dest


def promote(task: str, root: Path) -> Path:
    src = root / task / "gold.draft.json"
    items = json.loads(src.read_text(encoding="utf-8")).get("items")
    if not isinstance(items, list) or not items:
        raise SystemExit(f"{src}: no items to promote")
    ids = [i.get("id") for i in items if isinstance(i, dict)]
    if len(ids) != len(items) or len(set(ids)) != len(ids) or not all(ids):
        raise SystemExit(f"{src}: every item needs a unique non-empty id")
    dest = root / task / "gold.json"
    dest.write_text(json.dumps({"task": task, "items": items}, indent=1) + "\n", encoding="utf-8")
    print(f"{task}: promoted {len(items)} items -> {dest}")
    return dest


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--task")
    g.add_argument("--all", action="store_true")
    p.add_argument("--gold-dir", default=config.RADAR_GOLD_DIR)
    p.add_argument("--promote", action="store_true", help="validate gold.draft.json into gold.json")
    args = p.parse_args(argv)
    root = gold_root(args.gold_dir)
    tasks = radar.RadarAdapter().tasks() if args.all else [args.task]
    for task in tasks:
        radar.window_of(task)
        (promote if args.promote else draft)(task, root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
